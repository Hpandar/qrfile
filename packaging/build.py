"""把 qrfile 打成独立可执行程序（PyInstaller）。

用法::

    python packaging/build.py              # CLI：dist/qrfile
    python packaging/build.py --gui        # 界面：dist/qrfile-gui
    python packaging/build.py --all        # 两个都打
    python packaging/build.py --onedir     # 目录版（启动快，需带整个文件夹）
    python packaging/build.py --no-verify  # 跳过构建后的自检

两个入口：

* ``qrfile``     —— console，保留全部 CLI 命令；
* ``qrfile-gui`` —— windowed，PySide6 控制台界面，通过同目录下的
  ``qrfile`` / ``qrfile.exe`` 子进程执行传输。

GUI 产物必须和 CLI 产物放在同一个目录，否则它会找不到子进程。
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
GUI_NAME = "qrfile-gui"
CLI_LAUNCHER = ROOT / "packaging" / "launcher.py"
GUI_LAUNCHER = ROOT / "packaging" / "gui_launcher.py"


def _setup_console() -> None:
    """中文 Windows 控制台可能是 GBK；打印子进程输出时不要因为字符编码崩掉。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


_setup_console()

#: 确认过用不到的大块头。加错会在构建后的自检里暴露。
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
]

COMMON_HIDDEN_IMPORTS: list[str] = ["mss"]


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


def _artifact_path(name: str, onedir: bool) -> Path:
    filename = f"{name}.exe" if sys.platform == "win32" else name
    if onedir:
        return ROOT / "dist" / name / filename
    return ROOT / "dist" / filename


def _verify(artifact: Path, *, gui: bool) -> bool:
    print("\n用产物自己跑一遍自检（确认不是「打包成功但跑不起来」）…")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"

    if gui:
        # job 模式会进一步启动同目录的 CLI（doctor），验证 frozen 子进程通道。
        env["QRFILE_GUI_SMOKE"] = "job"
        env["QT_QPA_PLATFORM"] = "offscreen"
        check = subprocess.run(
            [str(artifact)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=90,
        )
        if check.returncode != 0:
            print(check.stdout[-1200:])
            print(check.stderr[-1200:])
            print(f"界面自检没过（退出码 {check.returncode}）—— 这个产物不能用。")
            return False
        print("界面自检通过（Qt 窗口创建 + CLI 子进程事件通道）。")
        return True

    check = subprocess.run(
        [str(artifact), "selftest", "--full"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    tail = "\n".join(check.stdout.splitlines()[-3:])
    print(tail)
    if check.returncode != 0:
        print(f"自检没过（退出码 {check.returncode}）—— 这个产物不能用。")
        return False
    print("自检通过。")
    return True


def build(
    *,
    target: str,
    onedir: bool,
    clean: bool,
    verify: bool,
) -> int:
    gui = target == "gui"
    name = GUI_NAME if gui else NAME
    launcher = GUI_LAUNCHER if gui else CLI_LAUNCHER

    if clean:
        for path in (ROOT / "build", ROOT / "dist"):
            shutil.rmtree(path, ignore_errors=True)
            print(f"已清理 {path}")

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onedir" if onedir else "--onefile",
        "--windowed" if (gui and sys.platform != "linux") else "--console",
        "--name",
        name,
        "--noconfirm",
        "--collect-all",
        "charset_normalizer",
        "--collect-data",
        "reportlab",
        "--hidden-import",
        "qrfile.ui.app" if gui else "qrfile.cli",
        "--distpath",
        str(ROOT / "dist"),
        "--workpath",
        str(ROOT / "build"),
        "--specpath",
        str(ROOT / "packaging"),
    ]

    if sys.platform == "win32":
        # 自动定位 VNC / 远程桌面窗口
        command += ["--hidden-import", "pygetwindow"]

    if not gui:
        command += [
            # tkinter 的 PIL 桥接不会被静态分析自动抓到。
            "--hidden-import",
            "PIL.ImageTk",
            "--hidden-import",
            "PIL._tkinter_finder",
            "--hidden-import",
            "PIL._imagingtk",
            # 抓屏后端在函数内部 import，显式声明更稳。
            "--hidden-import",
            "mss",
            "--hidden-import",
            "dxcam",
            "--collect-all",
            "dxcam",
            "--collect-all",
            "comtypes",
        ]
    else:
        # GUI 只负责控制台，捕获/播放交给同目录的 CLI，不需要 dxcam / Tk。
        command += [
            "--hidden-import",
            "PySide6.QtCore",
            "--hidden-import",
            "PySide6.QtGui",
            "--hidden-import",
            "PySide6.QtWidgets",
        ]

    for module in COMMON_HIDDEN_IMPORTS:
        command += ["--hidden-import", module]

    excludes = list(EXCLUDES)
    if not gui:
        # CLI 不需要 Qt；排除后体积更小。
        excludes += ["PySide6", "shiboken6"]
    for module in excludes:
        command += ["--exclude-module", module]

    # Tcl/Tk 只给 CLI（全屏播放器）用
    if not gui:
        for path, destination in _tcl_tk_binaries():
            command += ["--add-binary", f"{path}{os.pathsep}{destination}"]
    command.append(str(launcher))

    print(f"构建 {'GUI' if gui else 'CLI'}：", " ".join(command[:6]), "…")
    result = subprocess.run(command, cwd=str(ROOT), check=False)
    if result.returncode != 0:
        print(f"{name} 构建失败。")
        return result.returncode

    artifact = _artifact_path(name, onedir)
    if not artifact.exists():
        print(f"构建结束了但找不到产物：{artifact}")
        return 1

    size_mb = artifact.stat().st_size / (1024 * 1024)
    print(f"\n产物：{artifact}（{size_mb:.1f} MiB）")

    if verify and not _verify(artifact, gui=gui):
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="把 qrfile 打成可执行程序")
    parser.add_argument("--gui", action="store_true",
                        help="构建图形界面 qrfile-gui")
    parser.add_argument("--all", action="store_true",
                        help="同时构建 CLI 和 GUI")
    parser.add_argument("--onedir", action="store_true",
                        help="打成目录版（启动更快，代价是要带一整个文件夹）")
    parser.add_argument("--clean", action="store_true", help="先清掉 build/ 和 dist/")
    parser.add_argument("--no-verify", action="store_true", help="跳过构建后的自检")
    args = parser.parse_args()

    if args.all:
        targets = ["cli", "gui"]
    elif args.gui:
        targets = ["gui"]
    else:
        targets = ["cli"]

    for index, target in enumerate(targets):
        code = build(
            target=target,
            onedir=args.onedir,
            clean=args.clean and index == 0,
            verify=not args.no_verify,
        )
        if code:
            return code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
