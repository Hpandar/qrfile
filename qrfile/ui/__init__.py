"""qrfile 图形界面（PySide6 / Qt 6）。

``qrfile gui`` 启动这个包；CLI 与 GUI 共用 ``qrfile.events`` 的结构化事件。
"""

from __future__ import annotations


def main() -> int:
    from .app import main as _main

    return _main()


__all__ = ["main"]
