"""子进程桥接：启动 ``qrfile`` CLI，逐行解析 NDJSON 事件。

界面不直接跑传输循环，原因见方案：全屏播放器需要独占 Tk 事件循环，
抓屏/解码也不应该和 Qt 主线程抢时间。子进程同时提供了天然的“停止”边界。

这里用 ``subprocess.Popen`` 而不是 ``QProcess``：Windows 下需要
``CREATE_NO_WINDOW`` 避免 GUI 启动子进程时弹出黑色控制台；PySide6 没有
暴露 QProcess 的 CreateProcess 参数修改接口，Popen 反而更直接。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal


def command_prefix() -> list[str]:
    """返回启动 qrfile CLI 的命令前缀。

    打包后 GUI 叫 ``qrfile-gui``，CLI 是同一目录下的 ``qrfile`` /
    ``qrfile.exe``；源码运行时用当前解释器加 ``-m qrfile``。
    """
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        cli_name = "qrfile.exe" if sys.platform == "win32" else "qrfile"
        sibling = exe.with_name(cli_name)
        if sibling.exists() and sibling != exe:
            return [str(sibling)]
        return [str(exe)]
    return [sys.executable, "-m", "qrfile"]


class ChildJob(QObject):
    """一个 CLI 子任务。"""

    event = Signal(dict)
    log = Signal(str)
    finished = Signal(int)
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._proc: subprocess.Popen[str] | None = None
        self._events_mode = True
        self._stop_requested = False
        self._finished_emitted = False
        self._out_thread: threading.Thread | None = None
        self._err_thread: threading.Thread | None = None
        self._wait_thread: threading.Thread | None = None

    # ---------------------------------------------------------------- 状态

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ---------------------------------------------------------------- 启动

    def start(
        self,
        args: list[str],
        *,
        cwd: str,
        events_mode: bool = True,
        prefix: list[str] | None = None,
    ) -> bool:
        if self.running:
            self.failed.emit("已有任务在运行")
            return False

        command = list(prefix) if prefix else command_prefix()
        command += list(args)
        self._events_mode = events_mode
        self._stop_requested = False
        self._finished_emitted = False

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        # 让 CLI 知道 stdout/stderr 是 GUI 的管道，必须输出 UTF-8
        env["QRFILE_UTF8_PIPE"] = "1"

        kwargs: dict = {}
        if sys.platform == "win32":
            # GUI 是 windowed 子系统；不加这个标志，子进程会弹一个黑色控制台。
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        try:
            self._proc = subprocess.Popen(
                command,
                cwd=str(cwd),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                **kwargs,
            )
        except OSError as exc:
            self._proc = None
            self.failed.emit(str(exc))
            return False

        self._out_thread = threading.Thread(
            target=self._pump_stdout, name="qrfile-gui-stdout", daemon=True
        )
        self._err_thread = threading.Thread(
            target=self._pump_stderr, name="qrfile-gui-stderr", daemon=True
        )
        self._wait_thread = threading.Thread(
            target=self._wait, name="qrfile-gui-wait", daemon=True
        )
        self._out_thread.start()
        self._err_thread.start()
        self._wait_thread.start()
        return True

    # ---------------------------------------------------------------- 读取

    def _pump_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                self._handle_stdout_line(line.rstrip("\r\n"))
        except Exception as exc:  # pragma: no cover - 管道异常
            self.log.emit(f"[读取输出失败] {exc}")

    def _handle_stdout_line(self, line: str) -> None:
        if not line:
            return
        if self._events_mode and line.lstrip().startswith("{"):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                self.log.emit(line)
                return
            if isinstance(payload, dict):
                self.event.emit(payload)
                return
        self.log.emit(line)

    def _pump_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                text = line.rstrip("\r\n")
                if text.strip():
                    self.log.emit(text)
        except Exception as exc:  # pragma: no cover - 管道异常
            self.log.emit(f"[读取错误输出失败] {exc}")

    def _wait(self) -> None:
        proc = self._proc
        if proc is None:
            return
        code = proc.wait()
        if self._out_thread is not None:
            self._out_thread.join(timeout=1.0)
        if self._err_thread is not None:
            self._err_thread.join(timeout=1.0)
        if not self._finished_emitted:
            self._finished_emitted = True
            self.finished.emit(int(code))

    # ---------------------------------------------------------------- 控制

    def send_command(self, command: str) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or not self.running:
            return
        try:
            proc.stdin.write(command.rstrip("\n") + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def stop(self, *, timeout_ms: int = 2500) -> None:
        proc = self._proc
        if proc is None or not self.running:
            return
        self._stop_requested = True
        try:
            proc.terminate()
        except OSError:
            return
        threading.Timer(timeout_ms / 1000, self._kill_if_running).start()

    def _kill_if_running(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def wait_for_finished(self, timeout_ms: int = 3000) -> bool:
        proc = self._proc
        if proc is None:
            return True
        try:
            proc.wait(timeout=timeout_ms / 1000)
            return True
        except subprocess.TimeoutExpired:
            return False
