"""打包成单文件可执行程序时的入口。

为什么需要这个文件：``qrfile/__main__.py`` 里用的是相对导入（``from .cli import main``），
那是给 ``python -m qrfile`` 用的；PyInstaller 是把脚本当**顶层模块**跑的，
相对导入会直接失败。所以这里放一个只有绝对导入的薄启动器。

构建命令见 packaging/build.py。
"""

from qrfile.cli import main

if __name__ == "__main__":
    main()
