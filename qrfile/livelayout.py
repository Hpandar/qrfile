"""整屏自适应布局 —— 决定"一屏放几个码、每个码多大"。

这是实时链路里最影响效率的一个参数，而且**不能拍脑袋**：

* 屏幕分辨率、每模块像素数、纠错等级三者互相制约；
* 想放更多码就得让码更小，但每模块像素低于阈值就解不出来；
* 想每码装更多数据就得用更高版本，但高版本本身占地方。

所以这里直接枚举 ``(version, module_px)`` 的所有可行组合，按
**单屏净载荷 = 格子数 × 每码载荷** 打分取最大。这样在 1080p 上它会自己得出
"v40 × 3×2" 或 "v30 × 4×3" 之类的最优解，而不是固定写死一套。
"""

from __future__ import annotations

from dataclasses import dataclass

from .capacity import ECC_LEVELS, Profile, byte_capacity, modules_per_side
from .chunk import HEADER_SIZE
from .errors import InputError

MAX_VERSION = 40

#: 允许的每模块像素范围（自动模式）。4 是截图链路的稳妥下限，
#: 8 是"屏很大、只放一两个码"时的上限。
MIN_MODULE_PX = 4
MAX_MODULE_PX = 8


@dataclass(frozen=True, slots=True)
class Layout:
    """一屏的排布方案。"""

    version: int
    ecc: str
    module_px: int
    margin: int
    gap: int
    columns: int
    rows: int
    screen_width: int
    screen_height: int
    status_height: int
    #: 每个物理 tile 承载几个独立二维码。1 = 普通黑白 QR；
    #: 3 = RGB 三通道（R/G/B 各一张 QR）。
    channels: int = 1

    # ---------------------------------------------------------------- 几何

    @property
    def modules(self) -> int:
        return modules_per_side(self.version)

    @property
    def tile_px(self) -> int:
        return (self.modules + 2 * self.margin) * self.module_px

    @property
    def tiles_per_screen(self) -> int:
        """一屏有多少个物理 tile。"""
        return self.columns * self.rows

    @property
    def per_screen(self) -> int:
        """一屏承载多少个分片（码）。

        RGB 三通道时一个 tile 承载 3 个分片，所以这里要乘 ``channels``；
        页面映射、区间播放和 ``bytes_per_screen`` 都依赖这个值。
        """
        return self.columns * self.rows * self.channels

    @property
    def usable_height(self) -> int:
        return self.screen_height - self.status_height

    # ---------------------------------------------------------------- 容量

    @property
    def body_capacity(self) -> int:
        """每张码的净载荷字节数（已扣掉 32 字节分片头）。"""
        return byte_capacity(self.version, self.ecc) - HEADER_SIZE

    @property
    def bytes_per_screen(self) -> int:
        return self.per_screen * self.body_capacity

    # ---------------------------------------------------------------- 描述

    @property
    def profile(self) -> Profile:
        """给 :class:`qrfile.render.Renderer` 用的 profile。"""
        return Profile(
            name=f"live-v{self.version}",
            version_max=self.version,
            ecc=self.ecc,
            description="实时链路自适应布局",
        )

    def page_count(self, total_chunks: int) -> int:
        return max(1, -(-total_chunks // self.per_screen))

    def chunk_range(self, page_index: int, total_chunks: int) -> tuple[int, int]:
        start = page_index * self.per_screen
        end = min(start + self.per_screen, total_chunks)
        return start, end

    def describe(self) -> str:
        grid = f"{self.columns}×{self.rows}"
        if self.channels > 1:
            grid += f"×{self.channels}通道"
        return (
            f"v{self.version}-{self.ecc} · {self.module_px}px/模块 · "
            f"{grid}={self.per_screen} 码/屏 · "
            f"单码 {self.body_capacity} B · 单屏 {self.bytes_per_screen} B"
        )


def _layouts_for(
    screen_width: int,
    screen_height: int,
    *,
    ecc: str,
    mode: str,
    module_px: int | None,
    min_module_px: int,
    margin: int,
    gap: int,
    status_height: int,
    max_codes: int,
    channels: int,
) -> list[Layout]:
    usable_w = screen_width
    usable_h = screen_height - status_height
    if usable_h < 100:  # 屏幕太矮，状态栏就不要了
        usable_h = screen_height
        status_height = 0

    if module_px is not None:
        px_candidates = [max(1, module_px)]
    else:
        px_candidates = list(range(max(1, min_module_px), MAX_MODULE_PX + 1))

    results: list[Layout] = []

    for px in px_candidates:
        for version in range(MAX_VERSION, 0, -1):
            modules = modules_per_side(version)
            tile = (modules + 2 * margin) * px
            if tile > usable_w or tile > usable_h:
                continue

            if mode == "single":
                columns = rows = 1
            else:
                columns = max(1, (usable_w - gap) // (tile + gap))
                rows = max(1, (usable_h - gap) // (tile + gap))
                # 上限：别让一屏塞上百个码 —— 解码端会累，画面也会糊。
                # RGB 三通道时一个 tile 里就有 3 个码，所以按码数算。
                while columns * rows * channels > max_codes:
                    if columns >= rows and columns > 1:
                        columns -= 1
                    elif rows > 1:
                        rows -= 1
                    else:
                        break

            capacity = byte_capacity(version, ecc) - HEADER_SIZE
            if capacity <= 0:
                continue

            results.append(
                Layout(
                    version=version,
                    ecc=ecc,
                    module_px=px,
                    margin=margin,
                    gap=gap,
                    columns=columns,
                    rows=rows,
                    screen_width=screen_width,
                    screen_height=screen_height,
                    status_height=status_height,
                    channels=channels,
                )
            )

    return results


def choose_layout(
    screen_width: int,
    screen_height: int,
    *,
    ecc: str = "M",
    mode: str = "auto",
    module_px: int | None = None,
    min_module_px: int = MIN_MODULE_PX,
    margin: int = 4,
    gap: int = 8,
    status_height: int = 48,
    max_codes: int = 64,
    channels: int = 1,
) -> Layout:
    """选出单屏净载荷最大的布局。

    ``mode="single"`` 强制一屏一码（最稳，适合手机拍屏）；
    ``mode="auto"`` 在一屏多码里找最优。

    ``channels=3`` 表示 RGB 三通道：每个物理 tile 同时承载 3 个独立二维码，
    单屏净载荷和 ``per_screen`` 都按 3 倍计算。
    """
    ecc = ecc.upper()
    if ecc not in ECC_LEVELS:
        raise InputError(f"纠错等级必须是 {'/'.join(ECC_LEVELS)} 之一，收到 {ecc!r}")
    if mode not in ("auto", "single"):
        raise InputError(f"mode 只能是 auto 或 single，收到 {mode!r}")
    if channels not in (1, 3):
        raise InputError(f"channels 只能是 1（普通）或 3（RGB 三通道），收到 {channels!r}")
    if screen_width < 200 or screen_height < 200:
        raise InputError(f"屏幕分辨率太小：{screen_width}×{screen_height}")
    if max_codes < 1:
        raise InputError("--max-codes 必须 ≥ 1")
    if channels > max_codes:
        raise InputError(
            f"--max-codes={max_codes} 小于通道数 {channels}，放不下任何一个 tile"
        )

    candidates = _layouts_for(
        screen_width,
        screen_height,
        ecc=ecc,
        mode=mode,
        module_px=module_px,
        min_module_px=min_module_px,
        margin=margin,
        gap=gap,
        status_height=status_height,
        max_codes=max_codes,
        channels=channels,
    )
    if not candidates:
        raise InputError(
            f"在 {screen_width}×{screen_height} 上放不下任何 {ecc} 级二维码。"
            f"试试 --module-px 3 或 --mode single。"
        )

    # 评分：单屏净载荷最大；同分时优先"模块更大"（更抗缩放），再优先低版本
    def score(layout: Layout) -> tuple[int, int, int]:
        return (layout.bytes_per_screen, layout.module_px, -layout.version)

    return max(candidates, key=score)


def render_variants(
    screen_width: int, screen_height: int, **kwargs
) -> list[Layout]:
    """列出前几种候选布局 —— 给 ``send --dry-run`` 做对比用。"""
    mode = kwargs.pop("mode", "auto")
    ecc = kwargs.pop("ecc", "M").upper()
    kwargs.setdefault("module_px", None)
    kwargs.setdefault("min_module_px", MIN_MODULE_PX)
    kwargs.setdefault("margin", 4)
    kwargs.setdefault("gap", 8)
    kwargs.setdefault("status_height", 48)
    kwargs.setdefault("max_codes", 64)
    kwargs.setdefault("channels", 1)
    candidates = _layouts_for(screen_width, screen_height, ecc=ecc, mode=mode, **kwargs)
    seen: set[tuple[int, int, int, int]] = set()
    unique: list[Layout] = []
    for layout in sorted(candidates, key=lambda l: (l.bytes_per_screen, l.module_px), reverse=True):
        key = (layout.version, layout.module_px, layout.columns, layout.rows)
        if key not in seen:
            seen.add(key)
            unique.append(layout)
        if len(unique) >= 5:
            break
    return unique
