"""打包成 qrfile-gui 时的入口。

GUI 不负责传输本身：它启动同目录下的 ``qrfile`` / ``qrfile.exe`` 子进程，
读取 NDJSON 事件并更新界面。这样全屏播放器、抓屏和解码都在独立进程里。
"""

from qrfile.ui.app import main

if __name__ == "__main__":
    raise SystemExit(main())
