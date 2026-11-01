"""全屏播放器：把会话分片按自适应布局循环播放。

两个设计点：

1. **渲染和显示解耦。** 后台线程负责把"一屏"渲染成 PIL 图像，主线程只管按
   fps 取图显示。v40 的 segno 编码并不便宜，如果和 tkinter 的事件循环挤在
   一起，高 fps 下会明显顿。
2. **有界队列。** 队列长度只有 2 —— 渲染追不上时生产者自动阻塞，内存里永远
   最多三张整屏图（不到 10 MB），10 MB 的文件也不会把内存吃爆。
"""

from __future__ import annotations

import io
import os
import queue
import random
import threading
from typing import Sequence

import segno
from PIL import Image, ImageDraw, ImageFont

from .chunk import HEADER_SIZE
from .errors import ChunkError
from .livelayout import Layout

_STATUS_HEIGHT_MIN = 28


# --------------------------------------------------------------------------
# 单张码 → 图像
# --------------------------------------------------------------------------


def _matrix_to_image(qr, layout: Layout) -> Image.Image:
    """把 segno 的二维码放大成 PIL 图。优先走 numpy，没有就退回 PNG 渲染。"""
    px = layout.module_px
    margin = layout.margin

    try:
        import numpy as np

        arr = np.array(qr.matrix, dtype=np.uint8)
        arr = np.pad(arr, margin, mode="constant", constant_values=0)
        if px > 1:
            arr = np.kron(arr, np.ones((px, px), dtype=np.uint8))
        # segno 的矩阵里 1 = 深色模块
        return Image.fromarray(((1 - arr) * 255).astype(np.uint8), mode="L")
    except ImportError:  # pragma: no cover - numpy 是可选依赖
        pass

    buffer = io.BytesIO()
    qr.save(buffer, kind="png", scale=px, border=margin)
    buffer.seek(0)
    return Image.open(buffer).convert("L")


def qr_image(payload: bytes, layout: Layout, *, mask: int | None = 3) -> Image.Image:
    """把一个分片渲染成一张二维码。

    ``mask`` 固定时跳过 segno 的"试遍 8 种掩码取最优"步骤 —— 实测从 45 ms
    降到 9.5 ms（v23），整整 4.8 倍。代价是二维码的图案不是规范意义上的最优，
    但 8 种掩码都是合法 QR，干净渲染下实测 40/40 可解。手机拍屏（``--camera``）
    会传 ``mask=None`` 换回最优掩码，稳妥优先。
    """
    # 用 0 填充到本布局的最大载荷，让**每一帧都用同一个 QR 版本、同样大小**。
    # 否则短分片会被 segno 自动降到更小的版本，播放时看起来忽大忽小。
    # 解码端只按分片头里的 body_len 取载荷，尾部填充会被 unpack_frame 忽略
    # （它默认容忍尾部多余字节），所以不影响兼容性和校验。
    target = layout.body_capacity + HEADER_SIZE
    if len(payload) < target:
        payload = payload + b"\x00" * (target - len(payload))

    try:
        qr = segno.make(
            payload,
            error=layout.ecc.lower(),
            mode="byte",
            micro=False,
            boost_error=True,
            mask=mask,
        )
    except segno.DataOverflowError as exc:
        raise ChunkError(
            f"分片 {len(payload)} 字节超出 v{layout.version}-{layout.ecc} 的容量"
            f"（{layout.body_capacity + 32} 字节）：{exc}"
        ) from exc

    if qr.version is None or qr.version > layout.version:
        raise ChunkError(
            f"segno 选了 v{qr.version}，超过布局上限 v{layout.version}"
            f"（载荷 {len(payload)} 字节）"
        )

    return _matrix_to_image(qr, layout)


# --------------------------------------------------------------------------
# 一屏
# --------------------------------------------------------------------------


_font_cache: dict[int, ImageFont.ImageFont] = {}


def _status_font(size: int = 18) -> ImageFont.ImageFont:
    if size not in _font_cache:
        font: ImageFont.ImageFont | None = None
        for name in ("consola.ttf", "DejaVuSansMono.ttf", "arial.ttf"):
            try:
                font = ImageFont.truetype(name, size)
                break
            except OSError:
                continue
        _font_cache[size] = font or ImageFont.load_default()
    return _font_cache[size]


def compose_page(
    frames: Sequence[bytes],
    page_index: int,
    layout: Layout,
    *,
    fps: float,
    paused: bool = False,
    mask: int | None = 3,
) -> Image.Image:
    """渲染第 ``page_index`` 屏 —— 一张和屏幕等大的灰度图。"""
    width, height = layout.screen_width, layout.screen_height
    page = Image.new("L", (width, height), 255)

    start, end = layout.chunk_range(page_index, len(frames))
    batch = frames[start:end]

    for offset, payload in enumerate(batch):
        image = qr_image(payload, layout, mask=mask)
        row, column = divmod(offset, layout.columns)
        x = layout.gap + column * (layout.tile_px + layout.gap)
        y = layout.gap + row * (layout.tile_px + layout.gap)
        # 最后一个分片可能版本更小 —— 在格子里居中，画面才整齐
        x += max(0, (layout.tile_px - image.width) // 2)
        y += max(0, (layout.tile_px - image.height) // 2)
        page.paste(image, (x, y))

    if layout.status_height >= _STATUS_HEIGHT_MIN:
        bar_top = height - layout.status_height
        draw = ImageDraw.Draw(page)
        draw.rectangle([0, bar_top, width, height], fill=0)
        total_pages = layout.page_count(len(frames))
        state = "已暂停" if paused else "播放中"
        text = (
            f" qrfile 第 {page_index + 1}/{total_pages} 屏  ·  "
            f"分片 {start + 1}-{end}/{len(frames)}  ·  "
            f"{fps:.1f} fps  ·  {state}  ·  空格 暂停  ←/→ 手动  ↑/↓ 调速  Esc 退出"
        )
        font = _status_font(max(12, layout.status_height // 3))
        draw.text((12, bar_top + max(4, (layout.status_height - 18) // 2)), text, fill=255, font=font)

    return page


# --------------------------------------------------------------------------
# 生产者线程
# --------------------------------------------------------------------------


class _PageProducer(threading.Thread):
    def __init__(
        self,
        frames: Sequence[bytes],
        layout: Layout,
        queue_size: int = 2,
        mask: int | None = 3,
        pages: Sequence[int] | None = None,
        shuffle: bool = False,
    ):
        super().__init__(name="qrfile-page-producer", daemon=True)
        self.frames = frames
        self.layout = layout
        self.mask = mask
        self.page_count = layout.page_count(len(frames))
        #: 指定时只循环这些页（补播模式）
        self.pages = list(pages) if pages is not None else None
        #: 每轮打乱页序 —— 周期性卡顿就不会每次都错过同一批码
        self.shuffle = shuffle
        self.out: queue.Queue[tuple[int, Image.Image]] = queue.Queue(maxsize=queue_size)
        self.stop_event = threading.Event()
        self.fps = 5.0
        self.paused = False
        self.error: BaseException | None = None

    def _order(self) -> list[int]:
        if self.pages is not None:
            return list(self.pages)
        order = list(range(self.page_count))
        if self.shuffle and len(order) > 1:
            random.shuffle(order)
        return order

    def run(self) -> None:  # pragma: no cover - 线程体，逻辑由测试单独覆盖
        while not self.stop_event.is_set():
            for index in self._order():
                if self.stop_event.is_set():
                    return
                try:
                    image = compose_page(
                        self.frames,
                        index,
                        self.layout,
                        fps=self.fps,
                        paused=self.paused,
                        mask=self.mask,
                    )
                except BaseException as exc:  # 渲染失败要让主线程知道
                    self.error = exc
                    return

                while not self.stop_event.is_set():
                    try:
                        self.out.put((index, image), timeout=0.2)
                        break
                    except queue.Full:
                        continue


# --------------------------------------------------------------------------
# 播放器
# --------------------------------------------------------------------------


class Player:
    """全屏循环播放。``run()`` 会阻塞到用户按 Esc / 关闭窗口。"""

    def __init__(
        self,
        frames: Sequence[bytes],
        layout: Layout,
        *,
        fps: float = 5.0,
        windowed: bool = False,
        title: str = "qrfile",
        mask: int | None = 3,
        pages: Sequence[int] | None = None,
        shuffle: bool = False,
    ):
        self.frames = frames
        self.layout = layout
        self.fps = max(0.2, min(60.0, fps))
        self.windowed = windowed
        self.title = title
        self.mask = mask
        self.pages = pages
        self.shuffle = shuffle
        self.producer = _PageProducer(
            frames, layout, mask=mask, pages=pages, shuffle=shuffle
        )
        self.root = None

    def _interval_ms(self) -> int:
        return max(15, int(1000 / self.fps))

    def run(self, *, duration: float = 0.0) -> None:  # pragma: no cover - 需要真实显示器
        import tkinter as tk
        from PIL import ImageTk

        if not self.frames:
            raise ChunkError("没有可播放的分片")

        self.producer.fps = self.fps
        self.producer.start()

        root = tk.Tk()
        self.root = root
        root.title(self.title)
        root.configure(bg="black")
        root.config(cursor="none")

        width = self.layout.screen_width
        height = self.layout.screen_height
        state = {
            "paused": False,
            "fullscreen": not self.windowed,
            "forced": False,
        }

        def apply_window_mode() -> None:
            if state["fullscreen"]:
                # 保留窗口管理器管理（overrideredirect=False），这样键盘焦点和
                # Alt+Tab 才正常；只额外给一个明确的 0,0 几何兜底。
                root.overrideredirect(False)
                root.geometry(f"{width}x{height}+0+0")
                root.attributes("-topmost", True)
                try:
                    root.attributes("-fullscreen", True)
                except tk.TclError:
                    pass
            else:
                try:
                    root.attributes("-fullscreen", False)
                except tk.TclError:
                    pass
                root.overrideredirect(False)
                root.geometry(f"{min(width, 1000)}x{min(height, 700)}+0+0")
                root.attributes("-topmost", False)
            try:
                root.focus_force()
            except tk.TclError:
                pass

        apply_window_mode()

        label = tk.Label(root, bd=0, highlightthickness=0, bg="black")
        label.pack(expand=True, fill="both")
        label.image = None

        def report_geometry() -> None:
            # 窗口刚创建时可能还没被 WM 映射（winfo_x 会返回 -32768），延后打印
            try:
                print(
                    f"窗口：{root.winfo_x()},{root.winfo_y()} "
                    f"{root.winfo_width()}x{root.winfo_height()} · "
                    f"屏幕 {root.winfo_screenwidth()}x{root.winfo_screenheight()} · "
                    f"DISPLAY={os.environ.get('DISPLAY', '')}",
                    flush=True,
                )
            except tk.TclError:
                pass

        def tick() -> None:
            if self.producer.error is not None:
                print(f"渲染失败：{self.producer.error}")
                stop()
                return
            if not state["paused"]:
                try:
                    _, image = self.producer.out.get_nowait()
                except queue.Empty:
                    root.after(15, tick)
                    return
                photo = ImageTk.PhotoImage(image)
                label.configure(image=photo)
                label.image = photo
            root.after(self._interval_ms(), tick)

        def toggle_pause(event=None) -> None:
            state["paused"] = not state["paused"]
            self.producer.paused = state["paused"]

        def faster(event=None) -> None:
            self.fps = min(60.0, self.fps * 1.25)
            self.producer.fps = self.fps

        def slower(event=None) -> None:
            self.fps = max(0.2, self.fps / 1.25)
            self.producer.fps = self.fps

        def stop(event=None) -> None:
            self.producer.stop_event.set()
            try:
                root.grab_release()
            except tk.TclError:
                pass
            try:
                root.destroy()
            except tk.TclError:
                pass

        def toggle_fullscreen(event=None) -> None:
            state["fullscreen"] = not state["fullscreen"]
            apply_window_mode()

        root.bind("<space>", toggle_pause)
        root.bind("<Up>", faster)
        root.bind("<Down>", slower)
        root.bind("<Escape>", stop)
        root.bind("q", stop)
        root.bind("Q", stop)
        root.bind("<Control-q>", stop)
        root.bind("<Control-Q>", stop)
        root.bind("<Control-c>", stop)
        root.bind("<Control-C>", stop)
        root.bind("f", toggle_fullscreen)
        root.protocol("WM_DELETE_WINDOW", stop)

        def ensure_visible() -> None:
            """正常 WM 下什么都不抢；只有窗口没被映射出来时才强制 0,0 无边框。

            - 普通 WM：`-fullscreen` 生效，焦点由 WM 管，Esc/q 正常；
            - 精简 WM / 某些 VNC：窗口会停在 -32768（未映射），这时退回无边框
              显式几何，并且每秒抢一次焦点，保证退出键可用。
            """
            try:
                broken = (
                    not root.winfo_viewable()
                    or root.winfo_x() <= -30000
                    or root.winfo_y() <= -30000
                )
                if broken:
                    state["forced"] = True
                    root.overrideredirect(True)
                    root.geometry(f"{width}x{height}+0+0")
                    root.attributes("-topmost", True)
                    root.deiconify()
                    root.lift()
                if state.get("forced"):
                    root.focus_force()
            except tk.TclError:
                pass
            root.after(1000, ensure_visible)

        def engage_keyboard_grab() -> None:
            """把键盘事件抓到本窗口 —— 不依赖 WM 的焦点策略。

            VNC / 精简 WM 下窗口可能拿不到焦点，Esc / q 就完全没反应。
            X11 上用全局抓取（grab_set_global）可以保证按键一定送达；
            Windows 等平台不支持时退回普通 grab_set。
            """
            try:
                root.grab_set_global()
            except tk.TclError:
                try:
                    root.grab_set()
                except tk.TclError:
                    pass

        root.after(800, ensure_visible)
        root.after(1000, engage_keyboard_grab)
        root.after(1600, report_geometry)

        if duration > 0:
            root.after(int(duration * 1000), stop)

        root.after(0, tick)
        try:
            root.mainloop()
        finally:
            self.producer.stop_event.set()
            self.producer.join(timeout=2.0)
