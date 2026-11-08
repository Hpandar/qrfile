"""接收区域覆盖层：倒计时 + 彩色边框。

窗口本身透明、置顶、鼠标穿透，不抢焦点。倒计时阶段显示大号数字和区域标签；
正式开始采集后只保留细边框，避免标签挡住二维码内容。
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QWidget

BORDER_COLOR = QColor("#22c55e")
COUNTDOWN_BG = QColor(8, 18, 32, 220)
TEXT_COLOR = QColor("#ffffff")


class CaptureOverlay(QWidget):
    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self._virtual = QRect()
        self._target = QRect()
        self._label = ""
        self._countdown: int | None = None
        self._border_only = True

    # ---------------------------------------------------------------- 控制

    def show_target(
        self,
        target: QRect,
        label: str,
        *,
        countdown: int | None = None,
    ) -> None:
        screen = QGuiApplication.primaryScreen()
        self._virtual = screen.virtualGeometry() if screen is not None else QRect()
        self.setGeometry(self._virtual)
        self._target = QRect(target)
        self._label = label
        self._countdown = countdown
        self._border_only = countdown is None
        self.show()
        self.raise_()
        self.update()

    def update_target(self, target: QRect, label: str | None = None) -> None:
        self._target = QRect(target)
        if label is not None:
            self._label = label
        self.update()

    def set_countdown(self, value: int | None) -> None:
        self._countdown = value
        self._border_only = value is None
        self.update()

    def set_border_only(self) -> None:
        self._countdown = None
        self._label = ""
        self._border_only = True
        self.update()

    def clear(self) -> None:
        self._countdown = None
        self._label = ""
        self._target = QRect()
        self.hide()

    # ---------------------------------------------------------------- 绘制

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if self._target.isNull():
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        local = self._target.translated(-self._virtual.topLeft())
        # 边框画在区域内侧，避免全屏时被窗口边缘裁掉。
        border = local.adjusted(2, 2, -2, -2)
        pen = QPen(BORDER_COLOR, 4)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(border)

        # 四个角再加粗一点，远看也清楚
        corner = max(18, min(border.width(), border.height()) // 10)
        thick = QPen(BORDER_COLOR, 7)
        painter.setPen(thick)
        left, top, right, bottom = (
            border.left(), border.top(), border.right(), border.bottom()
        )
        painter.drawLine(left, top, left + corner, top)
        painter.drawLine(left, top, left, top + corner)
        painter.drawLine(right - corner, top, right, top)
        painter.drawLine(right, top, right, top + corner)
        painter.drawLine(left, bottom, left + corner, bottom)
        painter.drawLine(left, bottom - corner, left, bottom)
        painter.drawLine(right - corner, bottom, right, bottom)
        painter.drawLine(right, bottom - corner, right, bottom)

        if self._border_only:
            return

        if self._label:
            self._draw_label(painter, border)
        if self._countdown is not None:
            self._draw_countdown(painter, border)

    def _draw_label(self, painter: QPainter, border: QRect) -> None:
        font = QFont(self.font())
        font.setPointSize(10)
        font.setBold(True)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(self._label) + 24
        height = metrics.height() + 12
        box = QRectF(border.left() + 12, border.top() + 12, width, height)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(COUNTDOWN_BG)
        painter.drawRoundedRect(box, 7, 7)
        painter.setPen(TEXT_COLOR)
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, self._label)

    def _draw_countdown(self, painter: QPainter, border: QRect) -> None:
        size = max(92, min(180, min(border.width(), border.height()) // 4))
        box = QRectF(
            border.center().x() - size / 2,
            border.center().y() - size / 2,
            size,
            size,
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(COUNTDOWN_BG)
        painter.drawRoundedRect(box, 18, 18)

        font = QFont(self.font())
        font.setPointSize(max(32, int(size * 0.42)))
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(TEXT_COLOR)
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, str(self._countdown))
