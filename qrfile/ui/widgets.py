"""可复用的界面组件：状态标签、卡片、进度环、分片热力图、事件日志。"""

from __future__ import annotations

import math
from datetime import datetime

from PySide6.QtCore import QPointF, QRectF, Qt, QSize
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .theme import COLORS


class Card(QFrame):
    """白底圆角卡片。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")


class Chip(QLabel):
    """状态胶囊：ok / warn / err / info / muted。"""

    def __init__(self, text: str = "", kind: str = "muted", parent=None) -> None:
        super().__init__(text, parent)
        self.setObjectName("chip")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_kind(kind)

    def set_kind(self, kind: str) -> None:
        self.setProperty("kind", kind)
        style = self.style()
        style.unpolish(self)
        style.polish(self)

    def set_status(self, text: str, kind: str = "muted") -> None:
        self.setText(text)
        self.set_kind(kind)


class StatRow(QWidget):
    """一行「标签 —— 数值」。"""

    def __init__(self, label: str, value: str = "—", parent=None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 2, 0, 2)
        row.setSpacing(8)
        self.label = QLabel(label)
        self.label.setObjectName("mutedLabel")
        self.value = QLabel(value)
        self.value.setObjectName("statValue")
        self.value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.label)
        row.addStretch(1)
        row.addWidget(self.value)

    def set_value(self, value: str) -> None:
        self.value.setText(value)


class PageHeader(QWidget):
    """页面标题 + 说明 + 右侧状态胶囊。"""

    def __init__(self, title: str, subtitle: str = "", parent=None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        self.title = QLabel(title)
        self.title.setObjectName("pageTitle")
        self.subtitle = QLabel(subtitle)
        self.subtitle.setObjectName("pageSub")
        self.subtitle.setWordWrap(True)
        text_col.addWidget(self.title)
        text_col.addWidget(self.subtitle)

        self.chip = Chip("就绪", "muted")
        row.addLayout(text_col, 1)
        row.addWidget(self.chip, 0, Qt.AlignmentFlag.AlignTop)


class ProgressRing(QWidget):
    """圆环进度，中间显示百分比。"""

    def __init__(self, parent=None, *, color: str = COLORS["accent"]) -> None:
        super().__init__(parent)
        self._value = 0
        self._color = QColor(color)
        self.setMinimumSize(132, 132)
        self.setMaximumSize(170, 170)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def set_color(self, color: str) -> None:
        self._color = QColor(color)
        self.update()

    def set_value(self, value: float) -> None:
        value = max(0, min(100, int(round(value))))
        if value != self._value:
            self._value = value
            self.update()

    def value(self) -> int:
        return self._value

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt 命名
        return QSize(140, 140)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        side = min(self.width(), self.height()) - 18
        rect = QRectF(
            (self.width() - side) / 2,
            (self.height() - side) / 2,
            side,
            side,
        )

        pen = QPen(QColor("#e6edf5"), 10)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(rect, 0, 360 * 16)

        if self._value > 0:
            pen = QPen(self._color, 10)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawArc(rect, 90 * 16, -int(360 * 16 * self._value / 100))

        painter.setPen(QColor(COLORS["text"]))
        font = QFont(self.font())
        font.setPointSize(19)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, f"{self._value}%")


class ChunkMap(QWidget):
    """分片覆盖热力图。

    ``total`` 是被划分的总分片数；``received`` / ``missing`` 是分片号集合。
    单元格数会按总数降采样，避免 6 万个分片时把界面拖垮。
    """

    MAX_CELLS = 240
    GAP = 3

    def __init__(self, parent=None, *, received_color: str = "#34d399") -> None:
        super().__init__(parent)
        self._total = 0
        self._received: set[int] = set()
        self._missing: set[int] = set()
        self._received_color = QColor(received_color)
        self._columns = 24
        self._cell_rects: list[tuple[QRectF, int, int, bool]] = []
        self.setMouseTracking(True)
        self.setMinimumHeight(120)
        self.setMaximumHeight(260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_received_color(self, color: str) -> None:
        self._received_color = QColor(color)
        self.update()

    def set_data(
        self,
        total: int,
        *,
        received: set[int] | None = None,
        missing: set[int] | None = None,
    ) -> None:
        self._total = max(0, int(total))
        self._received = set(received or ())
        self._missing = set(missing or ())
        self.update()

    def _layout(self) -> tuple[int, float, list[tuple[int, int, bool, bool]]]:
        total = self._total
        if total <= 0:
            return 0, 0.0, []

        cells = min(total, self.MAX_CELLS)
        group = max(1, math.ceil(total / cells))
        cells = math.ceil(total / group)
        # 小数量时分少一点列，单元格更大更好看；大数量时最多 24 列。
        preferred = int(math.ceil(math.sqrt(cells * 1.8)))
        columns = max(6, min(self._columns, preferred, cells))
        rows = math.ceil(cells / columns)

        available_w = max(1, self.width() - 2)
        available_h = max(1, self.height() - 2)
        cell_w = (available_w - (columns - 1) * self.GAP) / columns
        cell_h = (available_h - (rows - 1) * self.GAP) / rows
        size = max(2.0, min(cell_w, cell_h))

        missing_buckets = [False] * cells
        for index in self._missing:
            if 0 <= index < total:
                missing_buckets[min(cells - 1, index // group)] = True

        received_buckets = [False] * cells
        for index in self._received:
            if 0 <= index < total:
                received_buckets[min(cells - 1, index // group)] = True

        states: list[tuple[int, int, bool, bool]] = []
        for cell in range(cells):
            start = cell * group
            end = min(total, start + group)
            states.append(
                (start, end, missing_buckets[cell], received_buckets[cell])
            )
        return columns, size, states

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._cell_rects.clear()

        columns, size, states = self._layout()
        if not states:
            painter.setPen(QColor(COLORS["faint"]))
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter, "等待数据…"
            )
            return

        rows = math.ceil(len(states) / columns)
        total_w = columns * size + (columns - 1) * self.GAP
        total_h = rows * size + (rows - 1) * self.GAP
        x0 = (self.width() - total_w) / 2
        y0 = (self.height() - total_h) / 2

        green = self._received_color
        amber = QColor("#fbbf24")
        idle = QColor("#e2e8f0")

        for cell, (start, end, is_missing, is_received) in enumerate(states):
            row, column = divmod(cell, columns)
            x = x0 + column * (size + self.GAP)
            y = y0 + row * (size + self.GAP)
            rect = QRectF(x, y, size, size)

            if is_missing:
                color = amber
            elif is_received:
                color = green
            else:
                color = idle
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            radius = max(1.5, min(4.0, size * 0.22))
            painter.drawRoundedRect(rect, radius, radius)
            self._cell_rects.append((rect, start, end, is_missing))

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        pos = event.position()
        for rect, start, end, is_missing in self._cell_rects:
            if rect.contains(pos):
                span = str(start) if start == end - 1 else f"{start}-{end - 1}"
                state = "缺失" if is_missing else "已覆盖"
                self.setToolTip(f"分片 {span} · {state}")
                return
        self.setToolTip("")

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self.setToolTip("")


class EventLog(QPlainTextEdit):
    """只读事件日志，自动限制长度。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("log")
        self.setReadOnly(True)
        self.setMaximumBlockCount(500)
        self.setMinimumHeight(90)
        self.setMaximumHeight(150)
        self.setPlaceholderText("等待事件…")

    def append_line(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.appendPlainText(f"{stamp}  {text}")

    def append_text(self, text: str) -> None:
        for line in text.rstrip().splitlines():
            if line.strip():
                self.appendPlainText(line)


class HLine(QFrame):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.HLine)
        self.setStyleSheet("color: #e3e9f1;")
        self.setFixedHeight(1)
