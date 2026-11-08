"""界面主题：颜色、字体与 QSS。

只用系统字体和内置资源，保证离线环境下的显示一致。
"""

from __future__ import annotations

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

#: 设计令牌（与 docs/UI-PLAN.html 保持一致）
COLORS = {
    "bg": "#f3f6fb",
    "surface": "#ffffff",
    "line": "#e3e9f1",
    "text": "#0f172a",
    "muted": "#5f6b7c",
    "faint": "#94a3b8",
    "accent": "#0ea5e9",
    "accent_dark": "#075985",
    "ok": "#0f9f6e",
    "warn": "#b45309",
    "danger": "#b42318",
    "sidebar": "#0b1b2c",
    "sidebar_active": "#172e47",
}

QSS = """
QWidget {
    color: #0f172a;
    font-size: 10pt;
}
QMainWindow, QWidget#root {
    background: #f3f6fb;
}
QWidget#sidebar {
    background: #0b1b2c;
}
QLabel#brand {
    color: #ffffff;
    font-size: 15pt;
    font-weight: 800;
}
QLabel#brandMark {
    background: #2dd4bf;
    color: #062033;
    border-radius: 8px;
    font-weight: 800;
    font-size: 12pt;
}
QPushButton#nav {
    color: #9fb3c8;
    text-align: left;
    padding: 9px 12px;
    border: none;
    border-radius: 8px;
    background: transparent;
}
QPushButton#nav:hover {
    background: #13263c;
    color: #ffffff;
}
QPushButton#nav:checked {
    background: #172e47;
    color: #ffffff;
    border-left: 3px solid #2dd4bf;
    padding-left: 9px;
}
QLabel#railFoot {
    color: #6d8399;
    font-size: 9pt;
}
QFrame#card {
    background: #ffffff;
    border: 1px solid #e3e9f1;
    border-radius: 12px;
}
QLabel#pageTitle {
    font-size: 20pt;
    font-weight: 800;
}
QLabel#pageSub {
    color: #6b7788;
}
QLabel#sectionTitle {
    font-size: 11pt;
    font-weight: 700;
}
QLabel#mutedLabel {
    color: #6b7788;
}
QLabel#faintLabel {
    color: #94a3b8;
    font-size: 9pt;
}
QLabel#statValue {
    font-weight: 700;
}
QPushButton#primary {
    background: #0ea5e9;
    color: #04121f;
    border: none;
    border-radius: 8px;
    padding: 9px 18px;
    font-weight: 700;
}
QPushButton#primary:hover {
    background: #38bdf8;
}
QPushButton#primary:disabled {
    background: #cbd5e1;
    color: #64748b;
}
QPushButton#ghost {
    background: #ffffff;
    border: 1px solid #dbe3ec;
    border-radius: 8px;
    padding: 8px 14px;
    color: #334155;
}
QPushButton#ghost:hover {
    border-color: #94a3b8;
}
QPushButton#ghost:disabled {
    color: #94a3b8;
    background: #f8fafc;
}
QPushButton#danger {
    background: #fee2e2;
    color: #991b1b;
    border: none;
    border-radius: 8px;
    padding: 9px 16px;
    font-weight: 700;
}
QPushButton#danger:hover {
    background: #fecaca;
}
QPushButton#danger:disabled {
    background: #f1f5f9;
    color: #94a3b8;
}
QLineEdit, QSpinBox, QComboBox {
    background: #ffffff;
    border: 1px solid #dbe3ec;
    border-radius: 8px;
    padding: 7px 10px;
    selection-background-color: #bae6fd;
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {
    border: 1px solid #38bdf8;
}
QLineEdit:disabled {
    background: #f1f5f9;
    color: #64748b;
}
QCheckBox, QRadioButton {
    spacing: 7px;
}
QLabel#chip {
    border-radius: 9px;
    padding: 4px 10px;
    font-size: 9pt;
    font-weight: 700;
}
QLabel#chip[kind="ok"] {
    background: #dcfce7;
    color: #166534;
}
QLabel#chip[kind="warn"] {
    background: #fef3c7;
    color: #92400e;
}
QLabel#chip[kind="err"] {
    background: #fee2e2;
    color: #991b1b;
}
QLabel#chip[kind="info"] {
    background: #e0f2fe;
    color: #075985;
}
QLabel#chip[kind="muted"] {
    background: #e2e8f0;
    color: #475569;
}
QPlainTextEdit#log {
    background: #0b1220;
    color: #c7d7e6;
    border: none;
    border-radius: 8px;
    font-family: "Consolas", "DejaVu Sans Mono", monospace;
    font-size: 9pt;
    padding: 8px;
}
QLabel#banner {
    background: #fffbeb;
    border: 1px solid #fde68a;
    border-radius: 9px;
    padding: 8px 10px;
    color: #92400e;
}
QLabel#errorBanner {
    background: #fef2f2;
    border: 1px solid #fecaca;
    border-radius: 9px;
    padding: 8px 10px;
    color: #991b1b;
}
QListWidget {
    background: #ffffff;
    border: 1px solid #e3e9f1;
    border-radius: 10px;
    padding: 4px;
}
QListWidget::item {
    padding: 7px 8px;
    border-radius: 6px;
}
QListWidget::item:selected {
    background: #e0f2fe;
    color: #075985;
}
QScrollArea {
    border: none;
    background: transparent;
}
QScrollArea > QWidget > QWidget {
    background: transparent;
}
QToolTip {
    background: #0f172a;
    color: #ffffff;
    border: none;
    padding: 5px 8px;
}
QDialog {
    background: #f3f6fb;
}
"""


def apply_theme(app: QApplication) -> None:
    """统一使用 Fusion 风格，避免 Windows / Linux 默认控件差异过大。"""
    app.setStyle("Fusion")
    font = QFont()
    font.setPointSize(10)
    app.setFont(font)
    app.setStyleSheet(QSS)
