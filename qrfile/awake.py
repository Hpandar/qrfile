"""运行期间阻止系统息屏 / 休眠。

实时链路是"屏幕一直在播、采集端一直在看"，任何一端息屏都会让整条链路失效：

* A 机器息屏 → 二维码被黑屏/锁屏挡住；
* B 机器息屏 → 本地截图拿到的是黑屏。

所以 ``push`` / ``pull`` / ``record`` 运行期间自动进入"保持唤醒"状态，退出时
恢复。三个平台各用各的原生机制：

* Windows：``SetThreadExecutionState(ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED)``
* Linux：``systemd-inhibit``（阻止 idle/sleep）+ ``xdg-screensaver reset`` 兜底
* macOS：``caffeinate``

注意：它阻止的是**系统电源管理**导致的息屏。手动锁屏（Win+L / Super+L）、
关闭 RDP 窗口、切换用户，是另一回事，工具管不了。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import threading
import time


class KeepAwake:
    """上下文管理器：进入时阻止息屏，退出时恢复。"""

    def __init__(self) -> None:
        self._child: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._backend = ""

    def __enter__(self) -> "KeepAwake":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()

    @property
    def backend(self) -> str:
        """实际生效的后端名称；空字符串表示没找到可用机制。"""
        return self._backend

    # ------------------------------------------------------------------ 启动

    def start(self) -> None:
        if os.name == "nt":
            if self._start_windows():
                return
        if sys.platform == "darwin" and shutil.which("caffeinate"):
            try:
                self._child = subprocess.Popen(
                    ["caffeinate", "-dimsu"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._backend = "caffeinate"
                return
            except OSError:
                pass
        if sys.platform.startswith("linux"):
            self._start_linux()

    def _start_windows(self) -> bool:
        try:
            kernel32 = ctypes.windll.kernel32
            ES_CONTINUOUS = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            ES_DISPLAY_REQUIRED = 0x00000002
            kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
            )
            self._backend = "SetThreadExecutionState"
            return True
        except Exception:
            return False

    def _start_linux(self) -> None:
        # systemd-inhibit 负责阻止 idle/sleep；孩子进程活着就生效，杀掉即释放。
        if shutil.which("systemd-inhibit"):
            try:
                self._child = subprocess.Popen(
                    [
                        "systemd-inhibit",
                        "--what=idle:sleep",
                        "--who=qrfile",
                        "--why=qrfile live transfer",
                        "--mode=block",
                        "sleep",
                        "infinity",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                # 没有 systemd 的环境（比如部分 WSL）会立刻退出 —— 别谎报后端
                time.sleep(0.1)
                if self._child.poll() is not None:
                    self._child = None
                else:
                    self._backend = "systemd-inhibit"
            except OSError:
                self._child = None

        # GNOME 的屏保是独立机制；有 xdg-screensaver 就每 30 秒重置一次。
        if shutil.which("xdg-screensaver"):
            self._thread = threading.Thread(
                target=self._reset_loop, name="qrfile-awake", daemon=True
            )
            self._thread.start()
            self._backend = (
                f"{self._backend}+xdg-screensaver" if self._backend else "xdg-screensaver"
            )

    def _reset_loop(self) -> None:
        while not self._stop.wait(30):
            try:
                subprocess.run(
                    ["xdg-screensaver", "reset"],
                    timeout=5,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass

    # ------------------------------------------------------------------ 停止

    def stop(self) -> None:
        self._stop.set()

        if self._child is not None:
            self._child.terminate()
            try:
                self._child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._child.kill()
            self._child = None

        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

        if os.name == "nt" and self._backend == "SetThreadExecutionState":
            try:
                # ES_CONTINUOUS：清掉本线程的显示/系统唤醒请求
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            except Exception:
                pass
