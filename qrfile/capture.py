"""屏幕采集：截图（实时解码）与录屏（无损帧序列）。

为什么"录屏"落的是 PNG 帧序列而不是 MP4：

* 本机没有 ffmpeg / OpenCV 时也能用，不引入额外依赖；
* PNG 是无损的，二维码模块一个像素都不会被视频编码器抹掉；
* 解码端可以直接复用现成的 ``qrfile decode <目录>``。

如果用户已经有视频文件（手机录屏、OBS 等），:func:`iter_video_frames` 会在
装了 OpenCV 时解码它，否则给出明确的安装提示。
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from PIL import Image, ImageGrab, ImageSequence

from .errors import InputError

#: ``(left, top, right, bottom)``，与 PIL.ImageGrab 的 bbox 一致
Region = tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class CaptureOptions:
    region: Region | None = None
    fps: float = 8.0
    change_only: bool = True
    #: 两帧 32×32 灰度签名的平均差超过这个值才算"画面变了"。
    #: 太低会被鼠标闪烁触发，太高会漏掉真实换帧。
    diff_threshold: float = 0.8
    #: 采集后端：auto / dxcam / mss / imagegrab。诊断接口问题用。
    backend: str = "auto"


# --------------------------------------------------------------------------
# 区域解析
# --------------------------------------------------------------------------


def parse_region(text: str) -> Region:
    """``x,y,w,h`` → ``(x, y, x+w, y+h)``。"""
    parts = [p.strip() for p in text.replace("，", ",").split(",")]
    if len(parts) != 4:
        raise InputError(f"区域写法看不懂：{text!r}（应该像 100,200,800,600 表示 x,y,w,h）")
    try:
        x, y, width, height = (int(p) for p in parts)
    except ValueError:
        raise InputError(f"区域写法看不懂：{text!r}（四个数都得是整数）") from None
    if width <= 0 or height <= 0:
        raise InputError(f"区域宽高必须为正：{width}×{height}")
    return (x, y, x + width, y + height)


@dataclass(frozen=True, slots=True)
class WindowInfo:
    """跨平台的窗口描述。Windows 来自 pygetwindow，Linux 来自 wmctrl/xdotool。"""

    title: str
    left: int
    top: int
    width: int
    height: int
    minimized: bool = False

    @property
    def region(self) -> Region:
        return (self.left, self.top, self.left + self.width, self.top + self.height)


def enable_dpi_awareness() -> None:
    """让进程按物理像素报告窗口坐标。

    否则 pygetwindow 拿到的是 DPI 缩放后的逻辑坐标，而 mss/dxcam 按物理像素
    抓屏 —— 两者错位，截出来的就是偏移/半张码。
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        try:
            # 2 = per-monitor DPI aware
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def activate_window(title: str) -> bool:
    """尽量把目标窗口调到前台，避免被终端窗口遮住。"""
    if sys.platform == "win32":
        try:
            import pygetwindow as gw

            needle = title.lower()
            matches = [
                window
                for window in gw.getWindowsWithTitle(title)
                if needle in (window.title or "").lower()
            ]
            if not matches:
                return False
            window = matches[0]
            if getattr(window, "isMinimized", False):
                window.restore()
            window.activate()
            return True
        except Exception:
            return False

    if sys.platform.startswith("linux"):
        wmctrl = shutil.which("wmctrl")
        if wmctrl:
            try:
                proc = subprocess.run(
                    [wmctrl, "-a", title],
                    timeout=5,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if proc.returncode == 0:
                    return True
            except (OSError, subprocess.TimeoutExpired):
                pass

        xdotool = shutil.which("xdotool")
        if xdotool:
            try:
                found = subprocess.run(
                    [xdotool, "search", "--name", title],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                window_ids = [line.strip() for line in found.stdout.splitlines() if line.strip()]
                if window_ids:
                    subprocess.run(
                        [xdotool, "windowactivate", window_ids[0]],
                        timeout=5,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    return True
            except (OSError, subprocess.TimeoutExpired):
                pass

    return False


def _parse_wmctrl(text: str) -> list[WindowInfo]:
    """解析 ``wmctrl -lG`` 的输出。抽成纯函数，方便在非 Linux 上测试。"""
    windows: list[WindowInfo] = []
    for line in text.splitlines():
        # 0x03a00007  0  1920  0  1920  1080  hostname  窗口标题
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        try:
            left, top, width, height = (int(parts[index]) for index in range(2, 6))
        except ValueError:
            continue
        windows.append(
            WindowInfo(
                title=parts[7].strip(),
                left=left,
                top=top,
                width=width,
                height=height,
            )
        )
    return windows


def _linux_windows() -> list[WindowInfo]:
    """用 ``wmctrl`` 列 X11 窗口。Wayland 下通常拿不到全局列表，返回空。"""
    executable = shutil.which("wmctrl")
    if not executable:
        return []

    try:
        proc = subprocess.run(
            [executable, "-lG"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []

    return _parse_wmctrl(proc.stdout)


def _xdotool_windows(title: str) -> list[WindowInfo]:
    """Linux 下按标题精确找窗口（wmctrl 不可用时）。"""
    executable = shutil.which("xdotool")
    if not executable:
        return []

    try:
        found = subprocess.run(
            [executable, "search", "--name", title],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []

    windows: list[WindowInfo] = []
    for window_id in [line.strip() for line in found.stdout.splitlines() if line.strip()]:
        try:
            geometry = subprocess.run(
                [executable, "getwindowgeometry", "--shell", window_id],
                capture_output=True, text=True, timeout=5,
            )
            name = subprocess.run(
                [executable, "getwindowname", window_id],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            continue

        values: dict[str, str] = {}
        for line in geometry.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key.strip()] = value.strip()
        try:
            windows.append(
                WindowInfo(
                    title=name or title,
                    left=int(values["X"]),
                    top=int(values["Y"]),
                    width=int(values["WIDTH"]),
                    height=int(values["HEIGHT"]),
                )
            )
        except (KeyError, ValueError):
            continue
    return windows


def _list_windows() -> list[WindowInfo]:
    """列出可见窗口。Windows/macOS 走 pygetwindow，Linux 走 wmctrl。"""
    if sys.platform.startswith("linux"):
        return _linux_windows()

    try:
        import pygetwindow as gw
    except ImportError:
        return []

    try:
        raw = gw.getAllWindows()
    except Exception:
        return []

    windows: list[WindowInfo] = []
    for window in raw:
        title = (getattr(window, "title", "") or "").strip()
        if not title or not getattr(window, "visible", False):
            continue
        windows.append(
            WindowInfo(
                title=title,
                left=int(getattr(window, "left", 0) or 0),
                top=int(getattr(window, "top", 0) or 0),
                width=int(getattr(window, "width", 0) or 0),
                height=int(getattr(window, "height", 0) or 0),
                minimized=bool(getattr(window, "isMinimized", False)),
            )
        )
    return windows


def resolve_window(title: str) -> tuple[Region, str]:
    """按标题找到窗口的屏幕矩形。返回 ``(region, 实际标题)``。"""
    needle = title.lower()
    matches = [
        window
        for window in _list_windows()
        if needle in window.title.lower() and not window.minimized
    ]

    if not matches and sys.platform.startswith("linux"):
        matches = _xdotool_windows(title)

    if not matches:
        hint = "--region x,y,w,h"
        if sys.platform.startswith("linux"):
            hint += "（Linux 的 --window 需要 wmctrl 或 xdotool；没有就用整屏或 --region）"
        raise InputError(
            f"没有找到标题包含 {title!r} 的可见窗口。可以用 {hint} 手动指定。"
        )

    window = max(matches, key=lambda item: item.width * item.height)
    region = window.region
    if region[2] - region[0] < 50 or region[3] - region[1] < 50:
        raise InputError(f"窗口 {window.title!r} 的尺寸异常：{region}")
    return region, window.title


#: 常见的"远程桌面 / 虚拟机控制台"窗口标题关键词。
#: 用小写比较；中文关键词不受 lower() 影响。
REMOTE_WINDOW_HINTS = (
    "远程桌面",
    "remote desktop",
    "mstsc",
    "rdp",
    "vnc",
    "console",
    "vsphere",
    "vmware",
    "virtual machine",
    "虚拟机",
    "云桌面",
    "teamviewer",
    "anydesk",
    "todesk",
    "向日葵",
)


def guess_remote_window() -> tuple[Region, str] | None:
    """自动找一个最像"远程桌面 / 虚拟机控制台"的窗口。

    找不到就返回 ``None`` —— 调用方应退回整屏采集（通常也能用）。
    这只是省掉 ``--window`` 的便利功能，不是必须的。
    """
    scored: list[tuple[int, int, WindowInfo]] = []

    for window in _list_windows():
        if not window.title or window.minimized:
            continue
        if window.width < 400 or window.height < 300:
            continue

        lowered = window.title.lower()
        score = sum(1 for hint in REMOTE_WINDOW_HINTS if hint in lowered)
        if score:
            scored.append((score, window.width * window.height, window))

    if not scored:
        return None

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    best = scored[0][2]
    return best.region, best.title


def resolve_region(
    *, region: str | None = None, window: str | None = None
) -> tuple[Region | None, str]:
    """把命令行参数变成实际的采集区域。

    返回 ``(region, 说明文字)``。``region=None`` 表示整个虚拟屏。
    """
    if region and window:
        raise InputError("--region 和 --window 只能二选一")
    if region:
        return parse_region(region), f"区域 {region}"
    if window:
        rect, actual = resolve_window(window)
        left, top, right, bottom = rect
        return rect, f"窗口 {actual!r}（{right - left}×{bottom - top} @ {left},{top}）"
    return None, "整个屏幕"


# --------------------------------------------------------------------------
# 变化检测
# --------------------------------------------------------------------------


def _signature(image: Image.Image) -> bytes:
    """快速指纹：32×32 灰度。用来判断这一帧和上一帧是否不同。"""
    return image.convert("L").resize((32, 32), Image.BILINEAR).tobytes()


def _changed(previous: bytes | None, current: bytes, threshold: float) -> bool:
    if previous is None:
        return True
    try:
        import numpy as np

        a = np.frombuffer(previous, dtype=np.uint8).astype(np.int16)
        b = np.frombuffer(current, dtype=np.uint8).astype(np.int16)
        return float(np.abs(a - b).mean()) > threshold
    except ImportError:  # pragma: no cover
        return previous != current


#: 缓存 mss 会话。它内部持有 X11/GDI 句柄，每帧重建很浪费。
_MSS_SESSION = None

#: dxcam（DXGI Desktop Duplication）会话。Windows 上优先用它 ——
#: VNC / RDP 客户端常用 DirectX 渲染，GDI 的 BitBlt 会拿到旧帧或黑屏。
_DXCAM_CAMERA = None
_DXCAM_DISABLED = False
_DXCAM_SIZE: tuple[int, int] | None = None

#: 最近一次成功抓屏用的后端，给 `qrfile doctor` 显示用。
_LAST_BACKEND = "未知"


def grab_backend() -> str:
    """返回最近一次成功抓屏用的后端名称。"""
    return _LAST_BACKEND


def _dxcam_primary_size() -> tuple[int, int]:
    global _DXCAM_SIZE
    if _DXCAM_SIZE is None:
        try:
            import re

            import dxcam

            match = re.search(r"Res:\((\d+),\s*(\d+)\)", dxcam.output_info())
            _DXCAM_SIZE = (int(match.group(1)), int(match.group(2))) if match else (0, 0)
        except Exception:
            _DXCAM_SIZE = (0, 0)
    return _DXCAM_SIZE


def _grab_dxcam(region: Region | None) -> Image.Image:
    """Windows：用 DXGI Desktop Duplication 抓屏。

    这是解决「VNC/RDP 窗口用 DirectX 渲染，BitBlt 只拿到旧帧」的关键后端。
    """
    global _DXCAM_CAMERA, _DXCAM_DISABLED

    if _DXCAM_DISABLED:
        raise InputError("dxcam 不可用")

    try:
        import dxcam
    except ImportError as exc:
        _DXCAM_DISABLED = True
        raise InputError(f"dxcam 未安装：{exc}") from exc

    if region is not None:
        width, height = _dxcam_primary_size()
        if width and height:
            left, top, right, bottom = region
            if left < 0 or top < 0 or right > width or bottom > height:
                raise InputError("采集区域超出主显示器，改用 mss")

    if _DXCAM_CAMERA is None:
        _DXCAM_CAMERA = dxcam.create(output_idx=0, output_color="BGRA")
        if _DXCAM_CAMERA is None:
            _DXCAM_DISABLED = True
            raise InputError("dxcam 初始化失败")

    frame = _DXCAM_CAMERA.grab(region=region, new_frame_only=False)
    if frame is None:
        raise InputError("dxcam 没有返回画面")

    # frame 是 BGRA；取 RGB 三通道交给 PIL
    rgb = frame[:, :, [2, 1, 0]]
    return Image.fromarray(rgb, mode="RGB")


def _grab_mss(region: Region | None) -> Image.Image:
    global _MSS_SESSION

    import mss

    if _MSS_SESSION is None:
        _MSS_SESSION = mss.mss()

    if region is None:
        monitor = _MSS_SESSION.monitors[0]  # 所有显示器的并集
    else:
        left, top, right, bottom = region
        monitor = {
            "left": left,
            "top": top,
            "width": right - left,
            "height": bottom - top,
        }

    shot = _MSS_SESSION.grab(monitor)
    return Image.frombytes("RGB", shot.size, shot.rgb)


def _grab_external(region: Region | None) -> Image.Image:
    """Linux 兜底：Wayland 用 grim，X11 用 scrot / ImageMagick import。"""
    global _LAST_BACKEND

    if region is None:
        geometry = None
    else:
        left, top, right, bottom = region
        geometry = (left, top, right - left, bottom - top)

    with tempfile.TemporaryDirectory(prefix="qrfile-shot-") as tmp:
        target = os.path.join(tmp, "shot.png")
        commands: list[list[str]] = []

        grim = shutil.which("grim")
        if grim:
            commands.append(
                [grim, target]
                if geometry is None
                else [grim, "-g", f"{geometry[0]},{geometry[1]} {geometry[2]}x{geometry[3]}", target]
            )

        scrot = shutil.which("scrot")
        if scrot:
            commands.append(
                [scrot, "-o", target]
                if geometry is None
                else [scrot, "-a", f"{geometry[0]},{geometry[1]},{geometry[2]},{geometry[3]}", "-o", target]
            )

        magick = shutil.which("import")
        if magick:
            command = [magick, "-window", "root"]
            if geometry is not None:
                command += ["-crop", f"{geometry[2]}x{geometry[3]}+{geometry[0]}+{geometry[1]}"]
            command.append(target)
            commands.append(command)

        if not commands:
            raise InputError(
                "没有可用的抓屏后端。Linux 请安装 mss，或 grim（Wayland）/ scrot（X11）。"
            )

        errors: list[str] = []
        for command in commands:
            try:
                proc = subprocess.run(command, capture_output=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired) as exc:
                errors.append(f"{os.path.basename(command[0])}: {exc}")
                continue
            if proc.returncode != 0 or not os.path.isfile(target):
                errors.append(f"{os.path.basename(command[0])}: exit {proc.returncode}")
                continue
            with Image.open(target) as image:
                _LAST_BACKEND = os.path.basename(command[0])
                return image.convert("RGB")

        raise InputError("抓屏失败：" + "；".join(errors))


CAPTURE_BACKENDS = ("auto", "dxcam", "mss", "imagegrab")


def _attempt_backend(name: str, region: Region | None) -> Image.Image:
    """按名字跑一个后端。失败抛异常，由 grab 决定是否继续。"""
    global _MSS_SESSION, _LAST_BACKEND

    if name == "dxcam":
        image = _grab_dxcam(region)
        _LAST_BACKEND = "dxcam"
        return image
    if name == "mss":
        image = _grab_mss(region)
        _LAST_BACKEND = "mss"
        return image
    if name == "imagegrab":
        image = ImageGrab.grab(bbox=region, all_screens=region is None)
        _LAST_BACKEND = "ImageGrab"
        return image
    if name == "external":
        return _grab_external(region)
    raise InputError(f"未知的采集后端：{name!r}")


def is_wslg() -> bool:
    """是否运行在 WSLg 下。

    WSLg 的 Weston 既不支持 X11 的 XGetImage，也不支持 wlr-screencopy，
    因此 Linux 进程无法录制 WSLg 桌面；需要改用 Windows 版接收端。
    """
    return bool(os.environ.get("WSL_DISTRO_NAME")) or os.path.isdir("/mnt/wslg")


def grab(region: Region | None, *, backend: str = "auto") -> Image.Image:
    """抓一帧。``region=None`` 时抓整个虚拟屏（多显示器也算）。

    ``backend`` 可以强制指定：``dxcam`` / ``mss`` / ``imagegrab``。
    默认 ``auto`` 的优先级：

    * Windows：dxcam（DXGI）→ mss → ImageGrab
    * Wayland：grim → mss → ImageGrab
    * 其它：mss → ImageGrab → 外部工具
    """
    backend = backend.lower()
    if backend not in CAPTURE_BACKENDS:
        raise InputError(
            f"未知的采集后端 {backend!r}，可选：{'、'.join(CAPTURE_BACKENDS)}"
        )

    if backend == "auto":
        order: list[str] = []
        if sys.platform == "win32":
            order.append("dxcam")
        if (
            sys.platform.startswith("linux")
            and os.environ.get("WAYLAND_DISPLAY")
            and shutil.which("grim")
        ):
            order.append("external")
        order += ["mss", "imagegrab"]
        if sys.platform.startswith("linux"):
            order.append("external")
    else:
        order = [backend]

    errors: list[str] = []
    for name in order:
        try:
            return _attempt_backend(name, region)
        except Exception as exc:
            if name == "mss":
                _MSS_SESSION = None  # 句柄可能失效，下次重建
            errors.append(f"{name}: {exc}")

    message = (
        f"抓屏失败（{'；'.join(errors)}）。"
        "Windows 请确认不在锁屏 / UAC 安全桌面；"
        "Linux 请确认 DISPLAY/WAYLAND_DISPLAY 正常，或安装 mss / grim / scrot。"
    )
    if is_wslg():
        message += (
            "\n检测到 WSLg：WSLg 的 Weston 不支持 X11 抓屏或 wlr-screencopy，"
            "Linux 侧无法录制屏幕。请改用 Windows 版 qrfile 作为接收端"
            "（它能抓 Windows 桌面），或在真实 Linux 桌面会话中运行。"
        )
    raise InputError(message)


def iter_frames(
    options: CaptureOptions,
    *,
    stop=None,
    max_seconds: float = 0.0,
    on_tick: Callable[[float], None] | None = None,
) -> Iterator[Image.Image]:
    """按 fps 持续抓屏，只产出"画面确实变了"的帧。

    ``stop`` 是一个 ``threading.Event``（可选）。Ctrl+C 也能中断。
    """
    interval = 1.0 / max(options.fps, 0.2)
    started = time.perf_counter()
    previous: bytes | None = None

    while True:
        if stop is not None and stop.is_set():
            return
        if max_seconds and time.perf_counter() - started >= max_seconds:
            return

        tick_start = time.perf_counter()
        image = grab(options.region, backend=options.backend)
        signature = _signature(image)

        if not options.change_only or _changed(previous, signature, options.diff_threshold):
            previous = signature
            yield image
            elapsed = time.perf_counter() - tick_start
            if on_tick is not None:
                on_tick(elapsed)
            # 解码耗时不计入 fps —— 否则解码慢的时候采集会越来越稀
            sleep_for = interval - elapsed
        else:
            sleep_for = interval - (time.perf_counter() - tick_start)

        if sleep_for > 0:
            time.sleep(sleep_for)


# --------------------------------------------------------------------------
# 录屏：无损帧序列
# --------------------------------------------------------------------------


def record(
    options: CaptureOptions,
    output_dir: str | os.PathLike[str],
    *,
    stop=None,
    max_seconds: float = 0.0,
    progress: Callable[[int, float], None] | None = None,
) -> int:
    """把变化的帧写成 ``frame_000001.png`` …… 返回保存的帧数。"""
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    saved = 0

    for image in iter_frames(options, stop=stop, max_seconds=max_seconds):
        saved += 1
        path = target / f"frame_{saved:06d}.png"
        # compress_level=1：录屏是临时数据，速度比体积重要
        image.save(path, format="PNG", compress_level=1)
        if progress is not None:
            progress(saved, time.perf_counter() - started)

    return saved


# --------------------------------------------------------------------------
# 视频文件解码（可选依赖）
# --------------------------------------------------------------------------


def iter_video_frames(path: str | os.PathLike[str]) -> Iterator[Image.Image]:
    """从视频 / 动图里逐帧取图。

    GIF / WebP 走 Pillow；MP4 / MKV / AVI 需要 OpenCV。
    """
    source = Path(path)
    if not source.exists():
        raise InputError(f"文件不存在：{source}")

    suffix = source.suffix.lower()
    if suffix in {".gif", ".webp", ".apng", ".png"}:
        try:
            image = Image.open(source)
        except OSError as exc:
            raise InputError(f"打不开 {source}：{exc}") from exc
        for frame in ImageSequence.Iterator(image):
            yield frame.convert("RGB")
        return

    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise InputError(
            f"解码 {source.suffix} 需要 OpenCV：pip install opencv-python-headless。"
            f"或者先用录屏工具导出 PNG 帧序列再喂给 decode。"
        ) from exc

    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise InputError(f"OpenCV 打不开视频：{source}")
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                return
            # OpenCV 是 BGR，PIL 要 RGB
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            yield Image.fromarray(rgb)
    finally:
        capture.release()
