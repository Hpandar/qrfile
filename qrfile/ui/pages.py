"""界面页面：首页、发送、接收、监控、诊断、设置与播放区间弹窗。"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..capture import list_monitors
from ..errors import InputError
from ..session import interval_pages
from .theme import RECV_COLOR, SEND_COLOR
from .widgets import Card, Chip, ChunkMap, EventLog, PageHeader, ProgressRing, StatRow


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def button(text: str, kind: str = "ghost") -> QPushButton:
    widget = QPushButton(text)
    widget.setObjectName(kind)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    return widget


def muted(text: str, *, faint: bool = False) -> QLabel:
    label = QLabel(text)
    label.setObjectName("faintLabel" if faint else "mutedLabel")
    label.setWordWrap(True)
    return label


def section_title(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("sectionTitle")
    return label


def card_with_title(title: str) -> tuple[Card, QVBoxLayout]:
    card = Card()
    layout = QVBoxLayout(card)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(9)
    if title:
        layout.addWidget(section_title(title))
    return card, layout


def _paths_from_settings(settings: QSettings, key: str) -> list[str]:
    raw = settings.value(key, [])
    if isinstance(raw, str):
        return [raw] if raw else []
    if isinstance(raw, (list, tuple)):
        return [str(item) for item in raw if str(item).strip()]
    return []


def _remember(settings: QSettings, key: str, path: str) -> None:
    items = [item for item in _paths_from_settings(settings, key) if item != path]
    items.insert(0, path)
    settings.setValue(key, items[:5])


class PathDropEdit(QLineEdit):
    """支持把文件/目录直接拖进来的路径输入框。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        urls = event.mimeData().urls()
        if not urls:
            return
        path = urls[0].toLocalFile()
        if path:
            self.setText(path)
            event.acceptProposedAction()


# --------------------------------------------------------------------------
# 首页
# --------------------------------------------------------------------------


class HomePage(QWidget):
    send_requested = Signal()
    receive_requested = Signal()
    diagnostics_requested = Signal()
    recent_activated = Signal(str, str)  # kind, path

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(16)

        root.addWidget(
            PageHeader(
                "选择这台机器的角色",
                "先确定发送 / 接收，再进入对应的传输流程。",
            )
        )

        roles = QHBoxLayout()
        roles.setSpacing(14)
        roles.addWidget(
            self._role_card(
                "发", "发送机器", "把文件播出去",
                "选择文件或目录，全屏循环播放二维码。适合被隔离的那台机器。",
                "开始发送  →", "primary", "send", self.send_requested.emit,
            ),
            1,
        )
        roles.addWidget(
            self._role_card(
                "收", "接收机器", "把文件收回来",
                "自动定位远程桌面窗口，截屏、解码、校验并还原文件树。",
                "开始接收  →", "recv", "recv", self.receive_requested.emit,
            ),
            1,
        )
        root.addLayout(roles)

        bottom = QHBoxLayout()
        bottom.setSpacing(14)

        recent_card, recent_layout = card_with_title("最近任务")
        self.recent = QListWidget()
        self.recent.setMinimumHeight(120)
        self.recent.itemActivated.connect(self._activate_recent)
        recent_layout.addWidget(self.recent)
        bottom.addWidget(recent_card, 3)

        env_card, env_layout = card_with_title("环境检查")
        env_layout.addWidget(muted("图形界面 · Qt 6 / 高 DPI"))
        env_layout.addWidget(muted("传输核心 · CLI 已就绪"))
        env_layout.addWidget(muted("压缩器 · zlib / lzma"))
        env_layout.addStretch(1)
        check = button("运行环境检查")
        check.clicked.connect(self.diagnostics_requested.emit)
        env_layout.addWidget(check)
        bottom.addWidget(env_card, 2)

        root.addLayout(bottom, 1)

    def _role_card(
        self,
        mark_text: str,
        chip_text: str,
        title: str,
        body: str,
        button_text: str,
        button_kind: str,
        chip_kind: str,
        callback,
    ) -> Card:
        card = Card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(7)

        top = QHBoxLayout()
        mark = QLabel(mark_text)
        mark.setFixedSize(40, 36)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if chip_kind == "recv":
            mark.setStyleSheet(
                "background:#dcfce7;color:#166534;border-radius:10px;"
                "font-weight:800;font-size:14pt;"
            )
        else:
            mark.setStyleSheet(
                "background:#e0f2fe;color:#075985;border-radius:10px;"
                "font-weight:800;font-size:14pt;"
            )
        top.addWidget(mark)
        top.addStretch(1)
        top.addWidget(Chip(chip_text, chip_kind))
        layout.addLayout(top)

        name = QLabel(title)
        name.setStyleSheet("font-size:15pt;font-weight:800;")
        layout.addWidget(name)
        layout.addWidget(muted(body))
        layout.addStretch(1)

        action = button(button_text, button_kind)
        action.clicked.connect(callback)
        layout.addWidget(action, 0, Qt.AlignmentFlag.AlignLeft)
        return card

    def refresh(self, settings: QSettings) -> None:
        self.recent.clear()
        entries: list[tuple[str, str]] = []
        for kind, key in (("发送", "recent/send"), ("接收", "recent/receive")):
            for path in _paths_from_settings(settings, key):
                entries.append((kind, path))
        if not entries:
            item = QListWidgetItem("还没有任务记录")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.recent.addItem(item)
            return
        for kind, path in entries[:10]:
            item = QListWidgetItem(f"{kind} · {path}")
            item.setData(Qt.ItemDataRole.UserRole, (kind, path))
            self.recent.addItem(item)

    def _activate_recent(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(data, tuple) and len(data) == 2:
            self.recent_activated.emit(str(data[0]), str(data[1]))


# --------------------------------------------------------------------------
# 发送设置
# --------------------------------------------------------------------------


class SendPage(QWidget):
    start_requested = Signal(dict)

    def __init__(self, settings: QSettings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(16)

        root.addWidget(
            PageHeader(
                "发送",
                "在发送机器（被隔离的那台）选择要传出的文件或目录，点击“开始播放”。",
            )
        )

        source_card, source_layout = card_with_title("要发送什么？")
        row = QHBoxLayout()
        self.path = PathDropEdit()
        self.path.setPlaceholderText("文件或目录的完整路径，也可以直接拖入")
        self.path.setClearButtonEnabled(True)
        pick_file = button("选择文件…")
        pick_file.clicked.connect(self._pick_file)
        pick_dir = button("选择目录…")
        pick_dir.clicked.connect(self._pick_dir)
        row.addWidget(self.path, 1)
        row.addWidget(pick_file)
        row.addWidget(pick_dir)
        source_layout.addLayout(row)
        source_layout.addWidget(
            muted("目录会保留根目录、嵌套目录、空目录、符号链接与中文/emoji 文件名。")
        )
        root.addWidget(source_card)

        options = QHBoxLayout()
        options.setSpacing(14)

        mode_card, mode_layout = card_with_title("播放模式")
        self.mode = QComboBox()
        self.mode.addItem("快速 · 一屏多码（推荐）", "fast")
        self.mode.addItem("稳健 · 一屏一码", "safe")
        self.mode.addItem("手机拍屏 · ECC H", "camera")
        mode_layout.addWidget(self.mode)
        mode_layout.addWidget(
            muted("默认快速；远端画面被缩放时选稳健，手机拍屏选手机模式。")
        )
        options.addWidget(mode_card, 2)

        duration_card, duration_layout = card_with_title("自动退出")
        self.duration = QSpinBox()
        self.duration.setRange(0, 86400)
        self.duration.setSingleStep(60)
        self.duration.setSuffix(" 秒")
        self.duration.setSpecialValueText("一直播放")
        self.duration.setValue(int(settings.value("defaults/duration", 1800)))
        duration_layout.addWidget(self.duration)
        duration_layout.addWidget(muted("防止播放器一直占用屏幕；Esc 可随时退出。"))
        options.addWidget(duration_card, 1)
        root.addLayout(options)

        root.addStretch(1)
        actions = QHBoxLayout()
        actions.addStretch(1)
        start = button("开始播放  →", "primary")
        start.clicked.connect(self._start)
        actions.addWidget(start)
        root.addLayout(actions)

    def _pick_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择要发送的文件")
        if path:
            self.path.setText(path)

    def _pick_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择要发送的目录")
        if path:
            self.path.setText(path)

    def set_source(self, path: str) -> None:
        self.path.setText(path)

    def _start(self) -> None:
        text = self.path.text().strip()
        if not text:
            QMessageBox.warning(self, "参数有误", "请选择要发送的文件或目录。")
            return
        source = Path(text).expanduser()
        if not source.exists():
            QMessageBox.warning(self, "参数有误", f"路径不存在：\n{source}")
            return
        self.start_requested.emit(
            {
                "path": str(source),
                "mode": self.mode.currentData(),
                "duration": self.duration.value(),
            }
        )


# --------------------------------------------------------------------------
# 接收设置
# --------------------------------------------------------------------------


class ReceivePage(QWidget):
    start_requested = Signal(dict)

    def __init__(self, settings: QSettings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(16)

        root.addWidget(
            PageHeader(
                "接收",
                "在接收机器（你手边这台）选择还原目录，点击“开始接收”。",
            )
        )

        if os.environ.get("WSL_DISTRO_NAME") or Path("/mnt/wslg").exists():
            warning = QLabel(
                "检测到 WSLg：Linux 侧无法抓取 WSLg 桌面。请改用 Windows 版"
                "接收端（它能抓 Windows 桌面），或在真实 Linux 桌面会话中运行。"
            )
            warning.setObjectName("banner")
            warning.setWordWrap(True)
            root.addWidget(warning)

        output_card, output_layout = card_with_title("保存到哪里？")
        row = QHBoxLayout()
        self.output = QLineEdit()
        self.output.setPlaceholderText("输出目录的完整路径")
        self.output.setClearButtonEnabled(True)
        pick = button("选择目录…")
        pick.clicked.connect(self._pick_dir)
        row.addWidget(self.output, 1)
        row.addWidget(pick)
        output_layout.addLayout(row)
        output_layout.addWidget(
            muted("收齐后自动校验 SHA-256；任何一项不通过都不会写盘。")
        )
        root.addWidget(output_card)

        options_card, options_layout = card_with_title("采集设置")
        self.safe = QCheckBox("稳健解码（远端画面被缩放、识别率低时勾选）")
        options_layout.addWidget(self.safe)

        screen_row = QHBoxLayout()
        screen_row.addWidget(muted("接收屏幕"))
        self.screen = QComboBox()
        self.screen.currentIndexChanged.connect(self._sync_screen_fields)
        refresh = button("刷新")
        refresh.clicked.connect(self._load_monitors)
        screen_row.addWidget(self.screen, 1)
        screen_row.addWidget(refresh)
        options_layout.addLayout(screen_row)
        options_layout.addWidget(
            muted("多屏时选择接收哪一块；自动会优先寻找远程桌面窗口。", faint=True)
        )

        window_row = QHBoxLayout()
        window_row.addWidget(muted("目标窗口标题"))
        self.window = QLineEdit()
        self.window.setPlaceholderText("可选；自动找错窗口时填")
        window_row.addWidget(self.window, 1)
        options_layout.addLayout(window_row)

        region_row = QHBoxLayout()
        region_row.addWidget(muted("采集区域"))
        self.region = QLineEdit()
        self.region.setPlaceholderText("可选；格式 x,y,w,h")
        region_row.addWidget(self.region, 1)
        options_layout.addLayout(region_row)
        root.addWidget(options_card)

        root.addStretch(1)
        actions = QHBoxLayout()
        actions.addStretch(1)
        start = button("开始接收  →", "recv")
        start.clicked.connect(self._start)
        actions.addWidget(start)
        root.addLayout(actions)

        self._load_monitors()

    def _load_monitors(self) -> None:
        current = self.screen.currentData() if self.screen.count() else None
        self.screen.blockSignals(True)
        self.screen.clear()
        self.screen.addItem("自动（窗口 / 整屏）", None)
        try:
            for monitor in list_monitors():
                self.screen.addItem(monitor.describe(), monitor.index)
        except InputError as exc:
            self.screen.setToolTip(f"无法枚举显示器：{exc}")
        self.screen.blockSignals(False)

        if current is not None:
            index = self.screen.findData(current)
            if index >= 0:
                self.screen.setCurrentIndex(index)
        self._sync_screen_fields()

    def _sync_screen_fields(self) -> None:
        selected = self.screen.currentData() is not None
        self.window.setEnabled(not selected)
        self.region.setEnabled(not selected)
        if selected:
            self.window.clear()
            self.region.clear()

    def _pick_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择接收目录")
        if path:
            self.output.setText(path)

    def set_output(self, path: str) -> None:
        self.output.setText(path)

    def _start(self) -> None:
        text = self.output.text().strip()
        if not text:
            QMessageBox.warning(self, "参数有误", "请选择接收目录。")
            return
        output = Path(text).expanduser()
        try:
            output.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "参数有误", f"无法创建目录：\n{exc}")
            return
        self.start_requested.emit(
            {
                "output": str(output),
                "safe": self.safe.isChecked(),
                "window": self.window.text().strip(),
                "region": self.region.text().strip(),
                "screen": self.screen.currentData(),
            }
        )


# --------------------------------------------------------------------------
# 发送监控
# --------------------------------------------------------------------------


class SendMonitorPage(QWidget):
    command = Signal(str)
    stop_requested = Signal()
    resume_requested = Signal()
    interval_requested = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.total_frames = 0
        self.total_pages = 0
        self.per_screen = 1
        self.last_page = -1
        self._paused = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(14)

        self.header = PageHeader("发送监控", "播放器已在独立窗口全屏运行。")
        root.addWidget(self.header)

        info = QHBoxLayout()
        self.state_label = muted("等待开始…")
        self.page_label = QLabel("—")
        self.page_label.setStyleSheet("font-weight:700;")
        info.addWidget(self.state_label, 1)
        info.addWidget(self.page_label)
        root.addLayout(info)

        cards = QHBoxLayout()
        cards.setSpacing(14)

        progress_card, progress_layout = card_with_title("本轮进度")
        ring_row = QHBoxLayout()
        self.ring = ProgressRing(color=SEND_COLOR)
        ring_row.addWidget(self.ring, 0, Qt.AlignmentFlag.AlignCenter)
        progress_layout.addLayout(ring_row)
        self.frames_stat = StatRow("已播放分片", "—")
        self.pages_stat = StatRow("总屏数", "—")
        self.fps_stat = StatRow("帧率", "—")
        progress_layout.addWidget(self.frames_stat)
        progress_layout.addWidget(self.pages_stat)
        progress_layout.addWidget(self.fps_stat)
        cards.addWidget(progress_card, 2)

        map_card, map_layout = card_with_title("页面覆盖")
        self.chunk_map = ChunkMap(received_color=SEND_COLOR)
        map_layout.addWidget(self.chunk_map, 1)
        legend = QHBoxLayout()
        legend.addWidget(muted("蓝色 = 已播放", faint=True))
        legend.addWidget(muted("灰色 = 未播放", faint=True))
        legend.addStretch(1)
        map_layout.addLayout(legend)
        cards.addWidget(map_card, 3)

        interval_card, interval_layout = card_with_title("播放区间（可选）")
        self.interval_input = QLineEdit()
        self.interval_input.setPlaceholderText("例如 12-14,204；留空 = 全部")
        interval_layout.addWidget(self.interval_input)
        interval_row = QHBoxLayout()
        apply_button = button("应用并播放", "primary")
        apply_button.clicked.connect(self._apply_interval)
        edit_button = button("高级…")
        edit_button.clicked.connect(self._open_interval_dialog)
        interval_row.addWidget(apply_button)
        interval_row.addWidget(edit_button)
        interval_layout.addLayout(interval_row)
        self.interval_info = muted("一屏多码时会自动映射到整页。", faint=True)
        interval_layout.addWidget(self.interval_info)
        interval_layout.addStretch(1)
        cards.addWidget(interval_card, 2)
        root.addLayout(cards, 3)

        controls = QHBoxLayout()
        controls.addStretch(1)
        self.pause_button = button("暂停", "ghost")
        self.pause_button.clicked.connect(self._toggle_pause)
        self.resume_button = button("继续播放", "primary")
        self.resume_button.setVisible(False)
        self.resume_button.clicked.connect(lambda: self.resume_requested.emit())
        self.stop_button = button("停止播放", "danger")
        self.stop_button.clicked.connect(self.stop_requested.emit)
        controls.addWidget(self.resume_button)
        controls.addWidget(self.pause_button)
        controls.addWidget(self.stop_button)
        root.addLayout(controls)

        self.events = EventLog()
        root.addWidget(self.events, 0)

    def reset(self) -> None:
        self.total_frames = 0
        self.total_pages = 0
        self.per_screen = 1
        self.last_page = -1
        self._paused = False
        self.header.chip.set_status("启动中", "send")
        self.state_label.setText("正在准备分片…")
        self.page_label.setText("—")
        self.ring.set_value(0)
        self.frames_stat.set_value("—")
        self.pages_stat.set_value("—")
        self.fps_stat.set_value("—")
        self.chunk_map.set_data(0)
        self.interval_input.clear()
        self.interval_info.setText("一屏多码时会自动映射到整页。")
        self.pause_button.setText("暂停")
        self.pause_button.setEnabled(False)
        self.resume_button.setVisible(False)
        self.stop_button.setEnabled(True)
        self.events.clear()

    def set_plan(self, data: dict) -> None:
        self.total_frames = int(data.get("total", 0))
        self.total_pages = int(data.get("pages", 0))
        layout = data.get("layout") or {}
        self.per_screen = max(1, int(layout.get("per_screen", 1)))
        self.frames_stat.set_value(f"0 / {self.total_frames:,}")
        self.pages_stat.set_value(f"{self.total_pages:,} 屏")
        self.fps_stat.set_value(f"{data.get('fps', 0):.1f} fps")
        self.state_label.setText(
            f"会话 {data.get('session', '')} · {layout.get('description', '')}"
        )
        self.chunk_map.set_data(self.total_pages)
        self.pause_button.setEnabled(True)

    def set_page(self, data: dict) -> None:
        page = int(data.get("page", 0))
        total_pages = int(data.get("total_pages", self.total_pages or 1))
        self.last_page = page
        self.ring.set_value((page + 1) / max(1, total_pages) * 100)
        self.page_label.setText(f"第 {page + 1} / {total_pages} 屏")
        if self.total_frames:
            self.frames_stat.set_value(
                f"约 {min(self.total_frames, (page + 1) * self.per_screen):,}"
                f" / {self.total_frames:,}"
            )
        self.fps_stat.set_value(f"{data.get('fps', 0):.1f} fps")
        self.chunk_map.set_data(total_pages, received=set(range(page + 1)))

    def set_state(self, data: dict) -> None:
        state = data.get("state", "")
        mapping = {
            "preparing": ("准备中", "send"),
            "playing": ("播放中", "send"),
            "completed": ("已完成", "send"),
            "cancelled": ("已停止", "warn"),
        }
        text, kind = mapping.get(state, (state or "运行中", "info"))
        self.header.chip.set_status(text, kind)
        if state == "completed":
            self.stop_button.setEnabled(False)
            self.pause_button.setEnabled(False)

    def set_paused(self, value: bool) -> None:
        self._paused = bool(value)
        self.pause_button.setText("继续" if self._paused else "暂停")
        self.state_label.setText("播放已暂停" if self._paused else "播放中")

    def set_interval(self, data: dict) -> None:
        text = str(data.get("text", ""))
        if text:
            self.interval_input.setText(text)
            self.interval_info.setText(
                f"已应用：{data.get('ranges', text)} · "
                f"{len(data.get('pages', []))} 页"
            )
        else:
            self.interval_input.clear()
            self.interval_info.setText("已恢复完整播放。")

    def _apply_interval(self) -> None:
        text = self.interval_input.text().strip()
        self.interval_requested.emit(text)
        self.events.append_line(f"[界面] 应用播放区间：{text or '全部'}")

    def _open_interval_dialog(self) -> None:
        dialog = IntervalDialog(
            total=self.total_frames or 1,
            per_screen=self.per_screen,
            text=self.interval_input.text().strip(),
            parent=self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.interval_input.setText(dialog.value())
            self._apply_interval()

    def _toggle_pause(self) -> None:
        self._paused = not self._paused
        self.command.emit("pause" if self._paused else "resume")
        self.pause_button.setText("继续" if self._paused else "暂停")

    def finish(self, code: int) -> None:
        self.stop_button.setEnabled(False)
        self.pause_button.setEnabled(False)
        if code == 0:
            self.header.chip.set_status("播放结束", "send")
        else:
            self.header.chip.set_status("已结束", "warn")
        self.resume_button.setVisible(True)
        self.resume_button.setEnabled(True)
        self.events.append_line(f"[进程] 播放器退出，代码 {code}")


# --------------------------------------------------------------------------
# 接收监控
# --------------------------------------------------------------------------


class ReceiveMonitorPage(QWidget):
    stop_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.total = 0
        self.missing: set[int] = set()
        self.ranges = ""
        self._output_path = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(14)

        self.header = PageHeader("接收监控", "自动定位远程桌面窗口并持续解码。")
        root.addWidget(self.header)

        top = QHBoxLayout()
        self.window_label = muted("窗口：等待定位…")
        self.progress_label = QLabel("—")
        self.progress_label.setStyleSheet("font-weight:700;")
        top.addWidget(self.window_label, 1)
        top.addWidget(self.progress_label)
        root.addLayout(top)

        cards = QHBoxLayout()
        cards.setSpacing(14)

        progress_card, progress_layout = card_with_title("已接收分片")
        self.ring = ProgressRing(color=RECV_COLOR)
        progress_layout.addWidget(self.ring, 0, Qt.AlignmentFlag.AlignCenter)
        self.received_stat = StatRow("已接收", "—")
        self.missing_stat = StatRow("缺失", "—")
        self.duplicates_stat = StatRow("重复 / 坏帧", "—")
        progress_layout.addWidget(self.received_stat)
        progress_layout.addWidget(self.missing_stat)
        progress_layout.addWidget(self.duplicates_stat)
        cards.addWidget(progress_card, 2)

        map_card, map_layout = card_with_title("分片覆盖")
        self.chunk_map = ChunkMap(received_color=RECV_COLOR)
        map_layout.addWidget(self.chunk_map, 1)
        legend = QHBoxLayout()
        legend.addWidget(muted("绿色 = 已接收", faint=True))
        legend.addWidget(muted("橙色 = 缺失", faint=True))
        legend.addStretch(1)
        map_layout.addLayout(legend)
        cards.addWidget(map_card, 3)

        missing_card, missing_layout = card_with_title("缺失分片")
        self.missing_label = QLabel("等待数据…")
        self.missing_label.setObjectName("banner")
        self.missing_label.setWordWrap(True)
        missing_layout.addWidget(self.missing_label)
        self.missing_input = QLineEdit()
        self.missing_input.setReadOnly(True)
        self.missing_input.setPlaceholderText("缺片范围会显示在这里")
        missing_layout.addWidget(self.missing_input)
        copy_row = QHBoxLayout()
        copy_button = button("复制区间", "recv")
        copy_button.clicked.connect(self._copy_missing)
        help_button = button("怎么用？")
        help_button.clicked.connect(self._show_help)
        copy_row.addWidget(copy_button)
        copy_row.addWidget(help_button)
        missing_layout.addLayout(copy_row)
        missing_layout.addWidget(
            muted("复制后到发送机器播放器底部的「播放区间」粘贴，回车即可。", faint=True)
        )
        missing_layout.addStretch(1)
        cards.addWidget(missing_card, 2)
        root.addLayout(cards, 3)

        self.restore_bar = QProgressBar()
        self.restore_bar.setObjectName("recvProgress")
        self.restore_bar.setTextVisible(True)
        self.restore_bar.setVisible(False)
        root.addWidget(self.restore_bar)

        self.done_label = QLabel("")
        self.done_label.setObjectName("successBanner")
        self.done_label.setWordWrap(True)
        self.done_label.setVisible(False)
        self.open_button = button("打开目录", "recv")
        self.open_button.setVisible(False)
        self.open_button.clicked.connect(self._open_output)
        done_row = QHBoxLayout()
        done_row.addWidget(self.done_label, 1)
        done_row.addWidget(self.open_button)
        root.addLayout(done_row)

        controls = QHBoxLayout()
        controls.addStretch(1)
        self.stop_button = button("停止接收", "danger")
        self.stop_button.clicked.connect(self.stop_requested.emit)
        controls.addWidget(self.stop_button)
        root.addLayout(controls)

        self.events = EventLog()
        root.addWidget(self.events, 0)

    def reset(self) -> None:
        self.total = 0
        self.missing = set()
        self.ranges = ""
        self._output_path = ""
        self.header.chip.set_status("启动中", "recv")
        self.window_label.setText("窗口：正在定位…")
        self.progress_label.setText("—")
        self.ring.set_value(0)
        self.received_stat.set_value("—")
        self.missing_stat.set_value("—")
        self.duplicates_stat.set_value("—")
        self.chunk_map.set_data(0)
        self.missing_label.setObjectName("banner")
        self.missing_label.style().unpolish(self.missing_label)
        self.missing_label.style().polish(self.missing_label)
        self.missing_label.setText("等待数据…")
        self.missing_input.clear()
        self.restore_bar.setVisible(False)
        self.restore_bar.setRange(0, 1)
        self.restore_bar.setValue(0)
        self.done_label.setVisible(False)
        self.done_label.clear()
        self.open_button.setVisible(False)
        self.stop_button.setEnabled(True)
        self.events.clear()

    def set_restore_progress(self, data: dict) -> None:
        done = int(data.get("written", 0))
        total = int(data.get("total", 0))
        path = str(data.get("path", ""))
        self.header.chip.set_status("还原中", "recv")
        self.restore_bar.setVisible(True)
        self.restore_bar.setRange(0, max(1, total))
        self.restore_bar.setValue(done)
        self.restore_bar.setFormat(f"还原 {done}/{total}")
        if path:
            self.progress_label.setText(f"正在还原：{path}")

    def _open_output(self) -> None:
        if not self._output_path:
            return
        target = Path(self._output_path)
        if target.is_file():
            target = target.parent
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def set_countdown(self, seconds: int) -> None:
        if seconds > 0:
            self.header.chip.set_status(f"{seconds} 秒后开始", "recv")
            self.progress_label.setText("准备接收…")
        else:
            self.header.chip.set_status("接收中", "recv")

    def set_window(self, data: dict) -> None:
        description = data.get("description") or "整个屏幕"
        rect = data.get("rect")
        if rect:
            left, top, right, bottom = rect
            self.window_label.setText(
                f"窗口：{description} · {right - left}×{bottom - top}"
            )
        else:
            self.window_label.setText(f"窗口：{description}")

    def set_progress(self, data: dict) -> None:
        total = int(data.get("total", 0))
        received = int(data.get("received", 0))
        percent = float(data.get("percent", 0))
        if total:
            self.total = total
        self.ring.set_value(percent)
        self.received_stat.set_value(f"{received:,} / {total:,}")
        self.missing_stat.set_value(f"{max(0, total - received):,}")
        self.duplicates_stat.set_value(
            f"{int(data.get('duplicates', 0))} / {int(data.get('damaged', 0))}"
        )
        self.progress_label.setText(f"{received:,} / {total:,} 片")

    def set_missing(self, data: dict) -> None:
        indices = data.get("indices") or []
        self.missing = set(int(index) for index in indices)
        self.ranges = str(data.get("ranges", ""))
        self.total = int(data.get("total", self.total))
        self.missing_label.setText(f"缺 {len(self.missing)} 片：{self.ranges}")
        self.missing_input.setText(self.ranges)
        if self.total:
            received = set(range(self.total)) - self.missing
            self.chunk_map.set_data(self.total, received=received, missing=self.missing)

    def set_state(self, data: dict) -> None:
        state = data.get("state", "")
        mapping = {
            "receiving": ("接收中", "recv"),
            "verifying": ("校验中", "recv"),
            "restoring": ("还原中", "recv"),
            "completed": ("完成", "recv"),
            "cancelled": ("已停止", "warn"),
        }
        text, kind = mapping.get(state, (state or "运行中", "info"))
        self.header.chip.set_status(text, kind)
        if state in ("completed", "cancelled"):
            self.stop_button.setEnabled(False)
        if state == "completed":
            self.ring.set_value(100)
            self.missing_label.setObjectName("banner")
            self.missing_label.style().unpolish(self.missing_label)
            self.missing_label.style().polish(self.missing_label)
            self.missing_label.setText("已收齐全部片")
            if self.total:
                self.chunk_map.set_data(
                    self.total, received=set(range(self.total))
                )
            output = str(data.get("output", ""))
            content = str(data.get("content_path") or output)
            tops = list(data.get("top_level") or [])
            written = int(data.get("written", 0))
            self.restore_bar.setVisible(True)
            self.restore_bar.setRange(0, 1)
            self.restore_bar.setValue(1)
            self.restore_bar.setFormat("还原完成")
            self.progress_label.setText("还原完成")
            self._output_path = content
            if len(tops) == 1 or not tops:
                text = (
                    f"还原完成：{content}（{written} 个文件）"
                    if content
                    else f"还原完成（{written} 个文件）"
                )
            else:
                preview = "、".join(tops[:6])
                if len(tops) > 6:
                    preview += "…"
                text = (
                    f"还原完成：{output}\n顶层条目：{preview}"
                    f"（共 {written} 个文件）"
                )
            self.done_label.setText(text)
            self.done_label.setVisible(True)
            self.open_button.setVisible(bool(self._output_path))
        if state == "verifying":
            self.progress_label.setText("收齐，正在校验并还原…")
        if state == "restoring":
            self.restore_bar.setVisible(True)
            self.progress_label.setText("正在还原文件…")

    def set_error(self, data: dict) -> None:
        message = str(data.get("message", "接收失败"))
        hint = str(data.get("hint", ""))
        self.header.chip.set_status("失败", "err")
        self.missing_label.setText(message)
        self.missing_label.setObjectName("errorBanner")
        self.missing_label.style().unpolish(self.missing_label)
        self.missing_label.style().polish(self.missing_label)
        if hint:
            self.events.append_line(f"[错误] {hint}")
        self.stop_button.setEnabled(False)

    def _copy_missing(self) -> None:
        if not self.ranges:
            return
        QGuiApplication.clipboard().setText(self.ranges)
        self.events.append_line(f"[界面] 已复制缺片范围：{self.ranges}")

    def _show_help(self) -> None:
        QMessageBox.information(
            self,
            "怎么使用缺片范围",
            "1. 点击“复制区间”。\n"
            "2. 切到发送机器（或被隔离机器的播放器窗口）。\n"
            "3. 在播放器底部「播放区间」输入框粘贴并回车。\n\n"
            "一屏多码时，播放器会自动映射到缺失分片所在的整页。",
        )

    def finish(self, code: int) -> None:
        self.stop_button.setEnabled(False)
        if code != 0 and self.header.chip.property("kind") != "err":
            self.header.chip.set_status(f"已结束（{code}）", "warn")
        self.events.append_line(f"[进程] 接收进程退出，代码 {code}")


# --------------------------------------------------------------------------
# 诊断
# --------------------------------------------------------------------------


class DiagnosticsPage(QWidget):
    run_requested = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(14)

        self.header = PageHeader(
            "诊断",
            "检查播放、抓屏、解码、压缩能力；输出会显示在下方。",
        )
        root.addWidget(self.header)

        buttons = QHBoxLayout()
        doctor = button("检查本机环境（doctor）", "primary")
        doctor.clicked.connect(lambda: self.run_requested.emit("doctor"))
        compressors = button("列出压缩器")
        compressors.clicked.connect(lambda: self.run_requested.emit("compressors"))
        clear = button("清空")
        clear.clicked.connect(self.clear)
        copy = button("复制")
        copy.clicked.connect(self._copy)
        export = button("导出…")
        export.clicked.connect(self._export)
        buttons.addWidget(doctor)
        buttons.addWidget(compressors)
        buttons.addStretch(1)
        buttons.addWidget(copy)
        buttons.addWidget(export)
        buttons.addWidget(clear)
        root.addLayout(buttons)

        self.output = QPlainTextEdit()
        self.output.setObjectName("log")
        self.output.setReadOnly(True)
        self.output.setPlaceholderText("点击“检查本机环境”开始…")
        root.addWidget(self.output, 1)

    def clear(self) -> None:
        self.output.clear()

    def append(self, text: str) -> None:
        self.output.appendPlainText(text.rstrip())

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.output.toPlainText())

    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "导出诊断日志", "qrfile-diagnostics.txt", "文本文件 (*.txt)"
        )
        if path:
            Path(path).write_text(self.output.toPlainText(), encoding="utf-8")


# --------------------------------------------------------------------------
# 设置
# --------------------------------------------------------------------------


class SettingsPage(QWidget):
    saved = Signal(dict)

    def __init__(self, settings: QSettings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(16)

        root.addWidget(PageHeader("设置", "默认值会应用到新的发送/接收任务。"))

        card, layout = card_with_title("传输默认值")
        mode_row = QHBoxLayout()
        mode_row.addWidget(muted("默认发送模式"))
        self.mode = QComboBox()
        self.mode.addItem("快速 · 一屏多码", "fast")
        self.mode.addItem("稳健 · 一屏一码", "safe")
        self.mode.addItem("手机拍屏", "camera")
        mode_row.addWidget(self.mode, 1)
        layout.addLayout(mode_row)

        duration_row = QHBoxLayout()
        duration_row.addWidget(muted("默认自动退出"))
        self.duration = QSpinBox()
        self.duration.setRange(0, 86400)
        self.duration.setSingleStep(60)
        self.duration.setSuffix(" 秒")
        self.duration.setSpecialValueText("一直播放")
        duration_row.addWidget(self.duration, 1)
        layout.addLayout(duration_row)

        layout.addWidget(
            muted("主题、语言和高级采集后端将在后续里程碑提供。", faint=True)
        )
        root.addWidget(card)

        actions = QHBoxLayout()
        actions.addStretch(1)
        save = button("保存设置", "primary")
        save.clicked.connect(self._save)
        actions.addWidget(save)
        root.addLayout(actions)
        root.addStretch(1)

        self.load()

    def load(self) -> None:
        mode = str(self.settings.value("defaults/mode", "fast"))
        index = self.mode.findData(mode)
        self.mode.setCurrentIndex(max(0, index))
        self.duration.setValue(int(self.settings.value("defaults/duration", 1800)))

    def _save(self) -> None:
        values = {
            "mode": self.mode.currentData(),
            "duration": self.duration.value(),
        }
        self.settings.setValue("defaults/mode", values["mode"])
        self.settings.setValue("defaults/duration", values["duration"])
        self.settings.sync()
        self.saved.emit(values)


# --------------------------------------------------------------------------
# 播放区间弹窗
# --------------------------------------------------------------------------


class IntervalDialog(QDialog):
    def __init__(
        self,
        *,
        total: int,
        per_screen: int | None,
        text: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.total = max(1, int(total))
        self.per_screen = per_screen
        self.setWindowTitle("播放区间")
        self.setMinimumWidth(540)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(10)

        title = QLabel("播放区间")
        title.setObjectName("pageTitle")
        root.addWidget(title)
        root.addWidget(
            muted("只循环缺失分片所在的页，不会重播整轮；留空表示恢复完整播放。")
        )

        self.input = QLineEdit(text)
        self.input.setPlaceholderText("例如 12-14,204,900-905")
        self.input.textChanged.connect(self._refresh_preview)
        root.addWidget(self.input)

        self.preview = QLabel("")
        self.preview.setObjectName("banner")
        self.preview.setWordWrap(True)
        root.addWidget(self.preview)

        buttons = QDialogButtonBox()
        reset = buttons.addButton("恢复完整播放", QDialogButtonBox.ButtonRole.ResetRole)
        cancel = buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        apply_button = buttons.addButton("应用并播放", QDialogButtonBox.ButtonRole.AcceptRole)
        reset.clicked.connect(self._reset)
        cancel.clicked.connect(self.reject)
        apply_button.clicked.connect(self.accept)
        root.addWidget(buttons)

        self._refresh_preview(text)

    def _reset(self) -> None:
        self.input.clear()
        self.accept()

    def _refresh_preview(self, text: str | None = None) -> None:
        text = self.input.text().strip() if text is None else text.strip()
        if not text:
            self.preview.setText("将恢复完整播放。")
            self.preview.setStyleSheet("")
            return
        try:
            indices, pages = interval_pages(
                text, self.total, self.per_screen or 1
            )
        except InputError as exc:
            self.preview.setText(f"错误：{exc}")
            self.preview.setStyleSheet("color:#991b1b;")
            return
        self.preview.setStyleSheet("")
        if self.per_screen is None:
            self.preview.setText(
                f"{len(indices)} 个分片；发送机器会按当前布局自动映射到整页。"
            )
        else:
            self.preview.setText(
                f"{len(indices)} 个分片 · 覆盖 {len(pages)} 页（第 "
                + "、".join(str(page + 1) for page in pages[:8])
                + ("…" if len(pages) > 8 else "")
                + " 页）"
            )

    def value(self) -> str:
        return self.input.text().strip()
