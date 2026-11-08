"""主窗口与程序入口。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, QSettings, QTimer, Qt
from PySide6.QtGui import QGuiApplication, QKeySequence, QScreen, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..capture import guess_remote_window, list_monitors
from ..errors import InputError
from .overlay import CaptureOverlay
from .pages import (
    DiagnosticsPage,
    HomePage,
    ReceiveMonitorPage,
    ReceivePage,
    SendMonitorPage,
    SendPage,
    SettingsPage,
)
from .theme import apply_theme

NAV_ITEMS = [
    ("home", "首页"),
    ("send", "发送"),
    ("recv", "接收"),
    ("diag", "诊断"),
    ("settings", "设置"),
]


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.settings = QSettings("qrfile", "qrfile")
        self.job = None
        self.active_kind: str | None = None
        self.overlay = CaptureOverlay()
        self._receive_timer: QTimer | None = None
        self._pending_receive: dict | None = None
        self._receive_screen_index: int | None = None
        self._receive_countdown = 0
        self._last_send_params: dict | None = None

        self.setWindowTitle("qrfile · 二维码文件传输")
        self.resize(1120, 780)
        self.setMinimumSize(980, 700)

        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            self.move(
                geo.x() + max(0, (geo.width() - self.width()) // 2),
                geo.y() + max(0, (geo.height() - self.height()) // 3),
            )

        self._build_ui()
        self._connect()
        self._navigate("home")
        self._maximize_shortcut = QShortcut(QKeySequence("F11"), self)
        self._maximize_shortcut.activated.connect(self._toggle_maximize)

    def _toggle_maximize(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    # ---------------------------------------------------------------- 布局

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(218)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(16, 18, 16, 16)
        side.setSpacing(4)

        brand_row = QHBoxLayout()
        brand_row.setSpacing(9)
        mark = QLabel("q")
        mark.setObjectName("brandMark")
        mark.setFixedSize(30, 30)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand = QLabel("qrfile")
        brand.setObjectName("brand")
        brand_row.addWidget(mark)
        brand_row.addWidget(brand)
        brand_row.addStretch(1)
        maximize = QPushButton("□")
        maximize.setFixedSize(26, 26)
        maximize.setCursor(Qt.CursorShape.PointingHandCursor)
        maximize.setToolTip("最大化 / 还原（F11）")
        maximize.setStyleSheet(
            "QPushButton{background:transparent;color:#9fb3c8;"
            "border:1px solid #31465e;border-radius:6px;padding:0;}"
            "QPushButton:hover{color:#ffffff;border-color:#64748b;}"
        )
        maximize.clicked.connect(self._toggle_maximize)
        brand_row.addWidget(maximize)
        side.addLayout(brand_row)
        side.addSpacing(18)

        self.nav_buttons: dict[str, QPushButton] = {}
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        for key, text in NAV_ITEMS:
            nav = QPushButton(text)
            nav.setObjectName("nav")
            nav.setCheckable(True)
            nav.setCursor(Qt.CursorShape.PointingHandCursor)
            if key in ("send", "recv"):
                nav.setProperty("role", key)
            nav.clicked.connect(lambda _checked=False, k=key: self._navigate(k))
            self.nav_group.addButton(nav)
            self.nav_buttons[key] = nav
            side.addWidget(nav)

        side.addStretch(1)
        foot = QLabel(f"v{__version__} · CLI 核心\n离线运行 · 无外部依赖")
        foot.setObjectName("railFoot")
        side.addWidget(foot)
        layout.addWidget(sidebar)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(26, 22, 26, 20)
        self.stack = QStackedWidget()
        content_layout.addWidget(self.stack)
        layout.addWidget(content, 1)

        self.home = HomePage()
        self.send_page = SendPage(self.settings)
        self.receive_page = ReceivePage(self.settings)
        self.send_monitor = SendMonitorPage()
        self.receive_monitor = ReceiveMonitorPage()
        self.diagnostics = DiagnosticsPage()
        self.settings_page = SettingsPage(self.settings)

        self.pages = {
            "home": self.home,
            "send": self.send_page,
            "recv": self.receive_page,
            "send_monitor": self.send_monitor,
            "recv_monitor": self.receive_monitor,
            "diag": self.diagnostics,
            "settings": self.settings_page,
        }
        for page in self.pages.values():
            self.stack.addWidget(page)

        mode = str(self.settings.value("defaults/mode", "fast"))
        index = self.send_page.mode.findData(mode)
        self.send_page.mode.setCurrentIndex(max(0, index))

    def _connect(self) -> None:
        self.home.send_requested.connect(lambda: self._navigate("send"))
        self.home.receive_requested.connect(lambda: self._navigate("recv"))
        self.home.diagnostics_requested.connect(lambda: self._navigate("diag"))
        self.home.recent_activated.connect(self._open_recent)

        self.send_page.start_requested.connect(self._start_send)
        self.receive_page.start_requested.connect(self._start_receive)

        self.send_monitor.command.connect(self._command)
        self.send_monitor.stop_requested.connect(self._stop)
        self.send_monitor.resume_requested.connect(self._resume_send)
        self.send_monitor.interval_requested.connect(self._apply_interval_command)
        self.receive_monitor.stop_requested.connect(self._stop)

        self.diagnostics.run_requested.connect(self._run_diagnostics)
        self.settings_page.saved.connect(self._apply_settings)

    # ---------------------------------------------------------------- 导航

    def _navigate(self, key: str) -> None:
        if key == "send" and self._running_kind("send"):
            page_key = "send_monitor"
        elif key == "recv" and self._running_kind("recv"):
            page_key = "recv_monitor"
        else:
            page_key = key

        page = self.pages.get(page_key)
        if page is None:
            return
        self.stack.setCurrentWidget(page)
        nav_key = "send" if page_key == "send_monitor" else (
            "recv" if page_key == "recv_monitor" else page_key
        )
        button = self.nav_buttons.get(nav_key)
        if button is not None:
            button.setChecked(True)
        if key == "home":
            self.home.refresh(self.settings)

    def _running_kind(self, kind: str) -> bool:
        return (
            self.job is not None
            and self.job.running
            and self.active_kind == kind
        )

    def _open_recent(self, kind: str, path: str) -> None:
        if kind == "发送":
            self.send_page.set_source(path)
            self._navigate("send")
        else:
            self.receive_page.set_output(path)
            self._navigate("recv")

    # ---------------------------------------------------------------- 启动

    def _start_send(self, params: dict) -> None:
        if self.job is not None and self.job.running:
            QMessageBox.information(self, "请稍等", "已有任务在运行，先停止当前任务。")
            return

        args = [
            "push",
            str(params["path"]),
            "--duration",
            str(params["duration"]),
            "--events-json",
        ]
        mode = params.get("mode", "fast")
        if mode == "safe":
            args.append("--safe")
        elif mode == "camera":
            args.append("--camera")

        self.send_monitor.reset()
        self._navigate("send_monitor")
        self._last_send_params = dict(params)
        self._start_job(args, cwd=Path.home(), kind="send", events_mode=True)

        path = str(params["path"])
        self._remember("recent/send", path)
        self.settings.setValue("defaults/mode", mode)
        self.settings.setValue("defaults/duration", params["duration"])
        self.settings_page.load()
        self.send_monitor.events.append_line(f"[界面] 发送：{path}")

    def _apply_interval_command(self, text: str) -> None:
        """应用播放区间：正在播放时直接切页并恢复播放；已退出则续播同一会话。"""
        text = text.strip()
        if (
            self.job is not None
            and self.job.running
            and self.active_kind == "send"
        ):
            if text:
                self.job.send_command(f"interval {text}")
            self.job.send_command("resume")
            self.send_monitor.events.append_line(
                "[界面] 已应用播放区间并恢复播放"
            )
            return
        self._resume_send(interval_text=text)

    def _resume_send(self, interval_text: str = "") -> None:
        """按同一会话继续播放（Esc / 停止之后仍然可用）。"""
        if self.job is not None and self.job.running:
            if self.active_kind == "send":
                if interval_text:
                    self.job.send_command(f"interval {interval_text}")
                self.job.send_command("resume")
            return

        params = self._last_send_params
        if not params:
            QMessageBox.information(
                self, "没有可继续的播放", "请先到发送页开始一次播放。"
            )
            return

        total = self.send_monitor.total_frames
        if not interval_text:
            if total <= 0:
                QMessageBox.information(
                    self,
                    "没有会话信息",
                    "无法确定会话分片数，请回到发送页重新开始。",
                )
                return
            interval_text = f"0-{total - 1}"

        args = [
            "push",
            "--repair",
            interval_text,
            "--events-json",
            "--duration",
            str(params.get("duration", 1800)),
        ]
        self.send_monitor.reset()
        self._navigate("send_monitor")
        self._start_job(args, cwd=Path.home(), kind="send", events_mode=True)
        self.send_monitor.events.append_line(
            f"[界面] 继续播放（同一会话）：{interval_text}"
        )

    def _start_receive(self, params: dict) -> None:
        if self.job is not None and self.job.running:
            QMessageBox.information(self, "请稍等", "已有任务在运行，先停止当前任务。")
            return
        if self._receive_timer is not None and self._receive_timer.isActive():
            QMessageBox.information(self, "请稍等", "接收倒计时进行中。")
            return

        target, label, screen_index = self._receive_overlay_target(params)
        self._receive_screen_index = screen_index
        self._pending_receive = dict(params)
        self._receive_countdown = 3

        self.receive_monitor.reset()
        self._navigate("recv_monitor")
        self.receive_monitor.set_countdown(3)
        self.receive_monitor.events.append_line(f"[界面] 接收区域：{label}")
        self.receive_monitor.events.append_line("[界面] 3 秒后开始接收…")

        self.overlay.show_target(target, label, countdown=3)
        self._receive_timer = QTimer(self)
        self._receive_timer.setInterval(1000)
        self._receive_timer.timeout.connect(self._tick_receive_countdown)
        self._receive_timer.start()

    def _tick_receive_countdown(self) -> None:
        self._receive_countdown -= 1
        if self._receive_countdown > 0:
            self.overlay.set_countdown(self._receive_countdown)
            self.receive_monitor.set_countdown(self._receive_countdown)
            self.receive_monitor.events.append_line(
                f"[界面] {self._receive_countdown}…"
            )
            return

        if self._receive_timer is not None:
            self._receive_timer.stop()
            self._receive_timer = None
        self.overlay.set_border_only()
        self.receive_monitor.set_countdown(0)

        params = self._pending_receive
        self._pending_receive = None
        if params is not None:
            self._launch_receive(params)

    def _launch_receive(self, params: dict) -> None:
        args = ["pull", str(params["output"]), "--events-json"]
        if params.get("safe"):
            args.append("--safe")

        screen = params.get("screen")
        if screen is not None:
            args += ["--screen", str(screen)]
        elif params.get("window"):
            args += ["--window", str(params["window"])]
        elif params.get("region"):
            args += ["--region", str(params["region"])]

        self._start_job(
            args,
            cwd=Path(params["output"]),
            kind="recv",
            events_mode=True,
        )
        self._remember("recent/receive", str(params["output"]))
        self.receive_monitor.events.append_line("[界面] 倒计时结束，开始接收")

    def _receive_overlay_target(
        self, params: dict
    ) -> tuple[QRect, str, int | None]:
        """确定倒计时/边框显示在哪个区域。"""
        screen_index = params.get("screen")
        if screen_index is not None:
            try:
                monitor = next(
                    item
                    for item in list_monitors()
                    if item.index == int(screen_index)
                )
                screen = self._qt_screen_for_monitor(monitor)
                if screen is not None:
                    return (
                        QRect(screen.geometry()),
                        f"接收区域 · {monitor.describe()}",
                        int(screen_index),
                    )
            except (InputError, StopIteration):
                pass

        try:
            guessed = guess_remote_window()
        except Exception:
            guessed = None
        if guessed is not None:
            rect, title = guessed
            return (
                self._physical_to_logical(rect),
                f"自动识别 · {title}",
                None,
            )

        primary = QGuiApplication.primaryScreen()
        if primary is not None:
            return QRect(primary.geometry()), "接收区域 · 自动（整屏）", None
        return QRect(0, 0, 1, 1), "接收区域 · 自动", None

    def _qt_screen_for_monitor(self, monitor) -> QScreen | None:
        screens = QGuiApplication.screens()
        for screen in screens:
            dpr = screen.devicePixelRatio() or 1.0
            if (
                abs(round(screen.geometry().width() * dpr) - monitor.width) <= 2
                and abs(
                    round(screen.geometry().height() * dpr) - monitor.height
                )
                <= 2
            ):
                return screen
        if 0 <= monitor.index - 1 < len(screens):
            return screens[monitor.index - 1]
        return QGuiApplication.primaryScreen()

    def _physical_to_logical(self, rect: tuple[int, int, int, int]) -> QRect:
        """把 mss 的物理像素区域换算成 Qt 的逻辑坐标。"""
        x1, y1, x2, y2 = rect
        center = QPoint((x1 + x2) // 2, (y1 + y2) // 2)
        for screen in QGuiApplication.screens():
            dpr = screen.devicePixelRatio() or 1.0
            geo = screen.geometry()
            physical = QRect(
                round(geo.x() * dpr),
                round(geo.y() * dpr),
                round(geo.width() * dpr),
                round(geo.height() * dpr),
            )
            if physical.contains(center):
                return QRect(
                    round((x1 - physical.x()) / dpr + geo.x()),
                    round((y1 - physical.y()) / dpr + geo.y()),
                    max(1, round((x2 - x1) / dpr)),
                    max(1, round((y2 - y1) / dpr)),
                )
        return QRect(x1, y1, max(1, x2 - x1), max(1, y2 - y1))

    def _run_diagnostics(self, which: str) -> None:
        if self.job is not None and self.job.running:
            QMessageBox.information(self, "请稍等", "已有任务在运行，先停止当前任务。")
            return
        self.diagnostics.clear()
        self.diagnostics.append(f"$ qrfile {which}")
        self._navigate("diag")
        self._start_job([which], cwd=Path.home(), kind="diag", events_mode=False)

    def _start_job(
        self, args: list[str], *, cwd: Path, kind: str, events_mode: bool
    ) -> None:
        from .runner import ChildJob

        job = ChildJob(self)
        job.event.connect(self._on_event)
        job.log.connect(self._on_log)
        job.finished.connect(self._on_finished)
        job.failed.connect(self._on_failed)
        self.job = job
        self.active_kind = kind
        if not job.start(args, cwd=str(cwd), events_mode=events_mode):
            self.job = None
            self.active_kind = None

    # ---------------------------------------------------------------- 事件

    def _on_event(self, event: dict) -> None:
        kind = event.get("type", "")
        if self.active_kind == "send":
            target = self.send_monitor
            if kind == "plan":
                target.set_plan(event)
            elif kind == "page":
                target.set_page(event)
            elif kind == "interval":
                target.set_interval(event)
            elif kind == "interval_error":
                target.events.append_line(
                    f"[错误] 播放区间无效：{event.get('message', '')}"
                )
            elif kind == "paused":
                target.set_paused(bool(event.get("value")))
            elif kind == "state":
                target.set_state(event)
            elif kind == "error":
                target.events.append_line(
                    f"[错误] {event.get('message', '')}"
                )
            elif kind == "warning":
                target.events.append_line(
                    f"[提示] {event.get('message', '')}"
                )
            elif kind == "player_started":
                target.events.append_line(
                    f"[播放器] 已启动 · {event.get('pages', '?')} 屏"
                )
        elif self.active_kind == "recv":
            target = self.receive_monitor
            if kind == "window":
                target.set_window(event)
                if self._receive_screen_index is None and event.get("rect"):
                    self.overlay.update_target(
                        self._physical_to_logical(tuple(event["rect"])),
                        f"接收区域 · {event.get('description', '自动')}",
                    )
            elif kind == "progress":
                target.set_progress(event)
            elif kind == "missing":
                target.set_missing(event)
            elif kind == "restore_progress":
                target.set_restore_progress(event)
            elif kind == "state":
                target.set_state(event)
            elif kind == "error":
                target.set_error(event)
            elif kind == "warning":
                target.events.append_line(
                    f"[提示] {event.get('message', '')}"
                )
        elif self.active_kind == "diag":
            if kind == "error":
                self.diagnostics.append(
                    f"错误：{event.get('message', '')}"
                )

    def _on_log(self, line: str) -> None:
        if self.active_kind == "send":
            self.send_monitor.events.append_text(line)
        elif self.active_kind == "recv":
            self.receive_monitor.events.append_text(line)
        elif self.active_kind == "diag":
            self.diagnostics.append(line)

    def _on_finished(self, code: int) -> None:
        self._cancel_receive_countdown()
        self.overlay.clear()
        kind = self.active_kind
        if kind == "send":
            self.send_monitor.finish(code)
        elif kind == "recv":
            self.receive_monitor.finish(code)
        elif kind == "diag":
            self.diagnostics.append(f"\n[进程结束，退出码 {code}]")
        self.job = None
        self.active_kind = None
        self.home.refresh(self.settings)

    def _on_failed(self, message: str) -> None:
        QMessageBox.critical(self, "启动失败", message)
        self._cancel_receive_countdown()
        self.overlay.clear()
        self.job = None
        self.active_kind = None

    # ---------------------------------------------------------------- 控制

    def _command(self, command: str) -> None:
        if self.active_kind == "send" and self.job is not None:
            self.job.send_command(command)

    def _stop(self) -> None:
        if self._receive_timer is not None and self._receive_timer.isActive():
            self._cancel_receive_countdown()
            self.overlay.clear()
            self.receive_monitor.set_countdown(0)
            self.receive_monitor.events.append_line("[界面] 已取消接收")
            return
        if self.job is None:
            return
        self.job.stop()
        self.overlay.clear()
        if self.active_kind == "send":
            self.send_monitor.events.append_line("[界面] 正在停止播放…")
        elif self.active_kind == "recv":
            self.receive_monitor.events.append_line("[界面] 正在停止接收…")

    def _cancel_receive_countdown(self) -> None:
        if self._receive_timer is not None:
            self._receive_timer.stop()
            self._receive_timer = None
        self._pending_receive = None

    def _remember(self, key: str, path: str) -> None:
        raw = self.settings.value(key, [])
        if isinstance(raw, str):
            items = [raw] if raw else []
        elif isinstance(raw, (list, tuple)):
            items = [str(item) for item in raw]
        else:
            items = []
        items = [item for item in items if item != path]
        items.insert(0, path)
        self.settings.setValue(key, items[:5])
        self.settings.sync()

    def _apply_settings(self, values: dict) -> None:
        index = self.send_page.mode.findData(values.get("mode", "fast"))
        self.send_page.mode.setCurrentIndex(max(0, index))
        self.send_page.duration.setValue(int(values.get("duration", 1800)))

    # ---------------------------------------------------------------- 关闭

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if self.job is not None and self.job.running:
            answer = QMessageBox.question(
                self,
                "退出",
                "任务仍在运行，停止并退出？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.job.stop()
        self._cancel_receive_countdown()
        self.overlay.clear()
        event.accept()


def main() -> int:
    smoke = os.environ.get("QRFILE_GUI_SMOKE", "")
    smoke_job = smoke == "job"
    smoke_window = smoke in ("1", "job")
    offscreen = os.environ.get("QT_QPA_PLATFORM") == "offscreen"
    if (
        sys.platform.startswith("linux")
        and not offscreen
        and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    ):
        raise InputError(
            "没有图形显示（DISPLAY / WAYLAND_DISPLAY 未设置）。"
            "请在有桌面的环境运行，或继续使用命令行。"
        )

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("qrfile")
    app.setOrganizationName("qrfile")
    app.setApplicationDisplayName("qrfile")
    apply_theme(app)

    window = MainWindow()
    window.show()
    if smoke_window:
        from PySide6.QtCore import QTimer

        if smoke_job:
            # 端到端自检：让 GUI 启动同目录的 CLI 子进程（doctor），
            # 验证 frozen 环境下的 command_prefix 和事件管道。
            QTimer.singleShot(300, lambda: window._run_diagnostics("doctor"))

            def check_smoke_job() -> None:
                if window.job is None:
                    text = window.diagnostics.output.toPlainText()
                    app.exit(0 if "qrfile" in text else 2)
                else:
                    QTimer.singleShot(250, check_smoke_job)

            QTimer.singleShot(900, check_smoke_job)
            QTimer.singleShot(30000, lambda: app.exit(3))
        else:
            QTimer.singleShot(900, app.quit)
    return int(app.exec())
