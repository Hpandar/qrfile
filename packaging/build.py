"""把 qrfile 打成单文件可执行程序（PyInstaller）。

用法::

    python packaging/build.py              # 单文件 exe（默认）
    python packaging/build.py --onedir     # 目录版：启动快得多，但要带一整个文件夹
    python packaging/build.py --no-verify  # 跳过构建后的自检（不推荐）

打完**会自动跑一遍产物自己的 `selftest`** —— PyInstaller 最常见的坑就是
"打包成功但运行时缺模块"，不实测等于没打。

为什么要显式收集那两个包：

* ``charset_normalizer`` —— 有 mypyc 编译出来的子模块和一张数据表，
  纯靠静态分析抓不全，运行到文本判定那一步才会炸。
* ``reportlab`` —— 生成 PDF 时要读自带的字体数据文件，不收进去会
  ``FileNotFoundError``。

排除项都是**用不到的大块头**。注意：

* ``numpy`` 被排除，但实时链路有纯 PIL 兜底，只是渲染慢一点；
* **``tkinter`` 不能排除** —— ``qrfile send`` 的全屏播放器要用它，
  早期版本把它排掉了，结果是"打包成功，但 send 一跑就报没有图形界面"。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "qrfile"
LAUNCHER = ROOT / "packaging" / "launcher.py"


def _tcl_tk_binaries() -> list[tuple[Path, str]]:
    """收集 Python 发行版自带的 Tcl/Tk 共享库。

    uv / python-build-standalone 把 ``libtcl9.0.so``、``libtcl9tk9.0.so``
    放在 ``<base_prefix>/lib``，而 ``_tkinter`` 没有指向那里的 RPATH，
    PyInstaller 的静态分析找不到它们。产物运行起来就变成"tkinter 不可用"，
    ``push`` 打不开全屏窗口。这里显式补进产物根目录。
    """
    if not sys.platform.startswith("linux"):
        return []
    libdir = Path(sys.base_prefix) / "lib"
    if not libdir.is_dir():
        return []
    candidates = sorted(libdir.glob("libtcl*.so*")) + sorted(libdir.glob("libtk*.so*"))
    return [(path, ".") for path in candidates]

#: 确认过用不到的大块头。加了能显著减小体积；加错会在构建后的自检里暴露。
EXCLUDES = [
    "matplotlib",
    "scipy",
    "pandas",
    "pytest",
    "hypothesis",
    "IPython",
    "PIL.ImageQt",   # 用到 Qt 才需要
    "PyQt5",
    "PySide2",
    "PySide6",
]


def build(*, onedir: bool, clean: bool, verify: bool) -> int:
    if clean:
        for path in (ROOT / "build", ROOT / "dist"):
            shutil.rmtree(path, ignore_errors=True)
            print(f"已清理 {path}")

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onedir" if onedir else "--onefile",
        "--console",
        "--name",
        NAME,
        "--noconfirm",
        "--collect-all",
        "charset_normalizer",
        "--collect-data",
        "reportlab",
        # tkinter 的 PIL 桥接不会被静态分析自动抓到。
        # _tkinter_finder 是运行期动态 import 的，漏了会在 PhotoImage 时才炸：
        #   ModuleNotFoundError: No module named 'PIL._tkinter_finder'
        "--hidden-import",
        "PIL.ImageTk",
        "--hidden-import",
        "PIL._tkinter_finder",
        "--hidden-import",
        "PIL._imagingtk",
        # 跨平台抓屏后端（在函数内部 import，显式声明更稳）
        "--hidden-import",
        "mss",
        "--hidden-import",
        "dxcam",
        "--collect-all",
        "dxcam",
        "--collect-all",
        "comtypes",
        "--distpath",
        str(ROOT / "dist"),
        "--workpath",
        str(ROOT / "build"),
        "--specpath",
        str(ROOT / "packaging"),
    ]
    for module in EXCLUDES:
        command += ["--exclude-module", module]
    for path, destination in _tcl_tk_binaries():
        command += ["--add-binary", f"{path}{os.pathsep}{destination}"]
    command.append(str(LAUNCHER))

    print("构建中：", " ".join(command[:6]), "…")
    result = subprocess.run(command, cwd=str(ROOT), check=False)
    if result.returncode != 0:
        print("构建失败。")
        return result.returncode

    artifact = ROOT / "dist" / (f"{NAME}.exe" if sys.platform == "win32" else NAME)
    if onedir:
        artifact = ROOT / "dist" / NAME / artifact.name
    if not artifact.exists():
        print(f"构建结束了但找不到产物：{artifact}")
        return 1

    size_mb = artifact.stat().st_size / (1024 * 1024)
    print(f"\n产物：{artifact}（{size_mb:.1f} MiB）")

    if verify:
        print("\n用产物自己跑一遍自检（确认不是「打包成功但跑不起来」）…")
        check = subprocess.run(
            [str(artifact), "selftest", "--full"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        tail = "\n".join(check.stdout.splitlines()[-3:])
        print(tail)
        if check.returncode != 0:
            print(f"自检没过（退出码 {check.returncode}）—— 这个产物不能用。")
            return 1
        print("自检通过。")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="把 qrfile 打成可执行程序")
    parser.add_argument("--onedir", action="store_true",
                        help="打成目录版（启动更快，代价是要带一整个文件夹）")
    parser.add_argument("--clean", action="store_true", help="先清掉 build/ 和 dist/")
    parser.add_argument("--no-verify", action="store_true", help="跳过构建后的自检")
    args = parser.parse_args()
    return build(onedir=args.onedir, clean=args.clean, verify=not args.no_verify)


if __name__ == "__main__":
    raise SystemExit(main())
