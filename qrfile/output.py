"""输出形态：PNG 序列、单张总览图、PDF 拼版（方案第八章）。

**输出只进输出目录，绝不碰源树。** 所有写操作都发生在 ``output_dir`` 之内；
临时文件走系统临时目录。这条由 ``readonly`` 的审计钩子在运行期强制。

关于数据片的文件名：方案里写的是 ``<relpath-slug>.pNNNNofMMMM.png``，但实际
实现里数据段是**整个 BLOB 一个流**切出来的，一个分片会跨文件边界，挂不上任何
单个文件名。所以改成顺序命名 —— 反正文件名只是给人看的，解码完全靠分片头里的
``index``，改文件名不影响还原（测试会覆盖这一点）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from PIL import Image

from .archive import SessionPlan
from .errors import InputError
from .render import Renderer

A4_WIDTH_PT = 595.276
A4_HEIGHT_PT = 841.89
A4_MARGIN_PT = 28.0


@dataclass(frozen=True, slots=True)
class OutputOptions:
    contact_sheet: bool = False
    sheet_columns: int = 6
    sheet_gap: int = 20
    max_sheet_px: int = 8000
    pdf: bool = False
    pdf_columns: int = 5
    pdf_rows: int = 7
    title: str = "qrfile"

    # ---- 整屏排版：一屏塞尽可能多的码，拍一张照片解一屏
    screens: bool = False
    screen_width: int = 1920
    screen_height: int = 1080
    screen_gap: int = 8
    #: 整屏模式下**是否额外**把每张单码也写出来。
    #: 默认不写 —— 单张码只是中间产物，几百个 PNG 铺在输出目录里又乱又没用。
    keep_chunks: bool = False


@dataclass(frozen=True, slots=True)
class ChunkArtifact:
    index: int
    is_manifest: bool
    path: Path
    payload_size: int


@dataclass(frozen=True, slots=True)
class SessionOutput:
    session_dir: Path
    artifacts: tuple[ChunkArtifact, ...]
    sheets: tuple[Path, ...] = ()
    pdf: Path | None = None
    screens: tuple[Path, ...] = ()
    screen_layout: ScreenLayout | None = None

    @property
    def png_count(self) -> int:
        return len(self.artifacts)

    @property
    def total_bytes(self) -> int:
        """所有产物的总字节数（整屏模式下单码不落盘，自然只算整屏页）。"""
        paths = [a.path for a in self.artifacts]
        paths += list(self.sheets)
        paths += list(self.screens)
        if self.pdf is not None:
            paths.append(self.pdf)
        return sum(p.stat().st_size for p in paths if p.exists())


def chunk_filename(index: int, total: int, *, is_manifest: bool) -> str:
    """``data.p0003of0017.png`` 这种形式。序号从 1 开始，给人看的。"""
    kind = "manifest" if is_manifest else "data"
    width = max(4, len(str(total)))
    return f"{kind}.p{index + 1:0{width}d}of{total:0{width}d}.png"


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_sequence(
    plan: SessionPlan,
    *,
    output_dir: Path,
    renderer: Renderer,
    progress: Callable[[int, int], None] | None = None,
) -> list[ChunkArtifact]:
    """把会话所有分片写成 PNG 序列。

    每一张都走 ``renderer.render_verified`` —— 即**编码期自解码比对**（方案 V1）。
    这是默认行为，不是可选项。
    """
    manifest_dir = _ensure_dir(output_dir / "manifest")
    data_dir = _ensure_dir(output_dir / "data")

    artifacts: list[ChunkArtifact] = []
    total = plan.total

    for index, frame in enumerate(plan.frames):
        is_manifest = index < plan.manifest_count
        target_dir = manifest_dir if is_manifest else data_dir
        target = target_dir / chunk_filename(index, total, is_manifest=is_manifest)

        png = renderer.render_verified(frame)
        # 先写临时文件再原子替换：中途失败不会留下半张坏码
        temp = target.with_name(target.name + ".tmp")
        temp.write_bytes(png)
        os.replace(temp, target)

        artifacts.append(
            ChunkArtifact(
                index=index,
                is_manifest=is_manifest,
                path=target,
                payload_size=len(frame),
            )
        )
        if progress is not None:
            progress(index + 1, total)

    return artifacts


# --------------------------------------------------------------------------
# 整屏排版 —— 一屏塞尽可能多的码，拍一张照片解一屏
# --------------------------------------------------------------------------
#
# 这是"减少采集次数"最省事的一招：解码端本来就支持一图多码（zxing 的多符号
# 检测 + decode_image 返回全部），所以只要把码排满一屏输出成整屏 PNG 就行。
#
# **决定成败的是每个模块分到几个【相机像素】，实测下限约 2.5，跟它在屏幕上
# 多大无关。** 手机拍照时屏幕在画面里占满、相机分辨率通常是屏幕的 2 倍左右，
# 所以屏幕上 3 像素/模块 → 相机约 6 像素/模块，有 2.4 倍余量。低于 2 会被
# 摩尔纹和非整数重采样打垮。
#
# 实测（1920×1080 屏，v20 码）：
#     3 px/模块 → 5×3 = 15 张/屏 = 12.1 KB
#     2 px/模块 → 8×4 = 32 张/屏 = 25.8 KB   （但真实屏幕有 RGB 次像素，没验证过）


@dataclass(frozen=True, slots=True)
class ScreenLayout:
    """一屏能放几行几列。"""

    columns: int
    rows: int
    tile_width: int
    tile_height: int

    @property
    def per_screen(self) -> int:
        return self.columns * self.rows

    def describe(self, screen_width: int, screen_height: int) -> str:
        return (
            f"{self.columns}×{self.rows} = {self.per_screen} 张/屏"
            f"（单张 {self.tile_width}px，{screen_width}×{screen_height}）"
        )


def plan_screen_layout(
    tile_width: int,
    tile_height: int,
    *,
    screen_width: int,
    screen_height: int,
    gap: int,
) -> ScreenLayout:
    """算出这个尺寸的码在一屏里能放几行几列。"""
    columns = max(1, (screen_width - gap) // (tile_width + gap))
    rows = max(1, (screen_height - gap) // (tile_height + gap))
    return ScreenLayout(
        columns=columns, rows=rows, tile_width=tile_width, tile_height=tile_height
    )


def build_screen_pages(
    images: Sequence[Image.Image],
    layout: ScreenLayout,
    *,
    screen_width: int,
    screen_height: int,
    gap: int,
) -> list[Image.Image]:
    """把码按整屏排好，每屏一张图。"""
    if not images:
        return []

    per_screen = layout.per_screen
    pages: list[Image.Image] = []

    for start in range(0, len(images), per_screen):
        batch = images[start : start + per_screen]
        page = Image.new("1", (screen_width, screen_height), 1)
        for offset, image in enumerate(batch):
            row, column = divmod(offset, layout.columns)
            page.paste(
                image,
                (
                    gap + column * (layout.tile_width + gap),
                    gap + row * (layout.tile_height + gap),
                ),
            )
        pages.append(page)

    return pages


def _flush_page(page: Image.Image, output_dir: Path, number: int) -> Path:
    target = output_dir / f"screen_{number:03d}.png"
    temp = target.with_name(target.name + ".tmp")
    page.save(temp, format="PNG")
    os.replace(temp, target)
    return target


def write_screens_only(
    plan: SessionPlan,
    *,
    output_dir: Path,
    renderer: Renderer,
    options: OutputOptions,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[list[Path], ScreenLayout | None]:
    """**只**输出整屏页，一张单码都不落盘。

    每张码照样走 V1 自验证（``render_verified_image``），只是渲染完直接在内存里
    拼进整屏页 —— 于是输出目录里只有 N 张 ``screen_NNN.png``，没有几百个中间
    PNG。内存占用也是恒定的：只持有当前这一页加当前这一张码。
    """
    _ensure_dir(output_dir)

    tile = Renderer.tile_size(
        renderer.profile, scale=renderer.scale, margin=renderer.margin
    )
    layout = plan_screen_layout(
        tile,
        tile,
        screen_width=options.screen_width,
        screen_height=options.screen_height,
        gap=options.screen_gap,
    )

    written: list[Path] = []
    page = Image.new("1", (options.screen_width, options.screen_height), 1)
    placed = 0
    total = plan.total

    for index, frame in enumerate(plan.frames):
        image = renderer.render_verified_image(frame)

        row, column = divmod(placed, layout.columns)
        offset_x = options.screen_gap + column * (layout.tile_width + options.screen_gap)
        offset_y = options.screen_gap + row * (layout.tile_height + options.screen_gap)
        # 不满一格的码（最后一个分片会小一些）居中放，画面才是齐的
        offset_x += max(0, (layout.tile_width - image.size[0]) // 2)
        offset_y += max(0, (layout.tile_height - image.size[1]) // 2)
        page.paste(image, (offset_x, offset_y))

        placed += 1
        if placed == layout.per_screen:
            written.append(_flush_page(page, output_dir, len(written) + 1))
            page = Image.new("1", (options.screen_width, options.screen_height), 1)
            placed = 0

        if progress is not None:
            progress(index + 1, total)

    if placed:
        written.append(_flush_page(page, output_dir, len(written) + 1))

    return written, layout


def write_screen_pages(
    artifacts: Sequence[ChunkArtifact],
    *,
    output_dir: Path,
    options: OutputOptions,
) -> tuple[list[Path], ScreenLayout | None]:
    """把已渲染好的码排成整屏页写出去。返回 (文件列表, 布局)。"""
    from .decode import load_image

    if not artifacts:
        return [], None

    images = [load_image(a.path) for a in artifacts]
    tile_width = max(image.size[0] for image in images)
    tile_height = max(image.size[1] for image in images)

    layout = plan_screen_layout(
        tile_width,
        tile_height,
        screen_width=options.screen_width,
        screen_height=options.screen_height,
        gap=options.screen_gap,
    )
    pages = build_screen_pages(
        images,
        layout,
        screen_width=options.screen_width,
        screen_height=options.screen_height,
        gap=options.screen_gap,
    )

    written: list[Path] = []
    for number, page in enumerate(pages, start=1):
        target = output_dir / f"screen_{number:03d}.png"
        temp = target.with_name(target.name + ".tmp")
        page.save(temp, format="PNG")
        os.replace(temp, target)
        written.append(target)

    return written, layout


# --------------------------------------------------------------------------
# 总览图
# --------------------------------------------------------------------------


def build_sheets(images: Sequence[Image.Image], options: OutputOptions) -> list[Image.Image]:
    """把若干张码拼成总览图。超过 ``max_sheet_px`` 就自动拆成多张。

    总览图是主链路（手机拍屏）最实用的提速手段：**拍一张照片扫下一整片**，
    而不是一张一张拍。所以格与格之间留够空隙，免得手机把相邻的码看混。
    """
    if not images:
        return []

    columns = max(1, options.sheet_columns)
    gap = max(0, options.sheet_gap)

    tile_w = max(image.size[0] for image in images)
    tile_h = max(image.size[1] for image in images)

    per_row = max(1, (options.max_sheet_px - gap) // (tile_w + gap))
    columns = min(columns, per_row)

    cell_w = tile_w + gap
    cell_h = tile_h + gap
    rows_per_sheet = max(1, (options.max_sheet_px - gap) // cell_h)
    per_sheet = columns * rows_per_sheet

    sheets: list[Image.Image] = []
    for start in range(0, len(images), per_sheet):
        batch = images[start : start + per_sheet]
        rows = -(-len(batch) // columns)
        width = gap + columns * cell_w
        height = gap + rows * cell_h
        sheet = Image.new("1", (width, height), 1)
        for offset, image in enumerate(batch):
            row, column = divmod(offset, columns)
            sheet.paste(image, (gap + column * cell_w, gap + row * cell_h))
        sheets.append(sheet)

    return sheets


def write_sheets(
    plan: SessionPlan,
    artifacts: Sequence[ChunkArtifact],
    *,
    output_dir: Path,
    options: OutputOptions,
) -> list[Path]:
    from .decode import load_image

    del plan  # 目前不需要会话信息；留着参数是为了调用点对称
    images = [load_image(a.path) for a in artifacts]
    sheets = build_sheets(images, options)

    written: list[Path] = []
    for number, sheet in enumerate(sheets, start=1):
        suffix = f"_{number:03d}" if len(sheets) > 1 else ""
        target = output_dir / f"contact_sheet{suffix}.png"
        temp = target.with_name(target.name + ".tmp")
        sheet.save(temp, format="PNG")
        os.replace(temp, target)
        written.append(target)
    return written


# --------------------------------------------------------------------------
# PDF 拼版
# --------------------------------------------------------------------------


def write_pdf(
    plan: SessionPlan,
    artifacts: Sequence[ChunkArtifact],
    *,
    output_path: Path,
    options: OutputOptions,
) -> Path:
    """A4 拼版，每张码下面标注 ``session / index / k-n``，页脚有会话号与总片数。

    reportlab 延迟导入 —— 只有真要出 PDF 时才付这个 import 成本。
    """
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfgen import canvas
    except ImportError as exc:  # pragma: no cover - 依赖缺失时给出人话提示
        raise InputError(
            "生成 PDF 需要 reportlab：请执行 pip install reportlab"
        ) from exc

    columns = max(1, options.pdf_columns)
    rows = max(1, options.pdf_rows)
    per_page = columns * rows

    page_width, page_height = A4
    usable_width = page_width - 2 * A4_MARGIN_PT
    usable_height = page_height - 2 * A4_MARGIN_PT
    cell_width = usable_width / columns
    cell_height = usable_height / rows
    label_height = 12.0

    total = plan.total
    session_hex = plan.session_hex

    output_path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(output_path), pagesize=A4)
    pdf.setTitle(options.title)

    for page_number, start in enumerate(range(0, len(artifacts), per_page), start=1):
        batch = artifacts[start : start + per_page]

        for offset, artifact in enumerate(batch):
            row, column = divmod(offset, columns)
            cell_x = A4_MARGIN_PT + column * cell_width
            # 从上往下排
            cell_top = page_height - A4_MARGIN_PT - row * cell_height
            image = ImageReader(str(artifact.path))

            available = cell_height - label_height - 6
            side = min(cell_width, available)
            image_x = cell_x + (cell_width - side) / 2
            image_y = cell_top - label_height - side

            pdf.drawImage(
                image, image_x, image_y, width=side, height=side, preserveAspectRatio=True
            )
            kind = "M" if artifact.is_manifest else "D"
            pdf.setFont("Helvetica", 7)
            pdf.drawCentredString(
                cell_x + cell_width / 2,
                cell_top - label_height + 1,
                f"{session_hex[:8]} {kind} {artifact.index + 1}/{total}",
            )

        pdf.setFont("Helvetica", 8)
        pdf.drawString(
            A4_MARGIN_PT,
            A4_MARGIN_PT / 2,
            f"qrfile  session {session_hex}   共 {total} 片   第 {page_number} 页",
        )
        pdf.showPage()

    pdf.save()
    return output_path


# --------------------------------------------------------------------------
# 一站式写出
# --------------------------------------------------------------------------


def write_session(
    plan: SessionPlan,
    *,
    output_dir: Path,
    renderer: Renderer,
    options: OutputOptions | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> SessionOutput:
    """把会话写成 PNG 序列，并按需附上总览图和 PDF。"""
    if options is None:
        options = OutputOptions()

    output_dir = Path(output_dir)
    _ensure_dir(output_dir)

    # 整屏模式默认**只出整屏页**：单张码是中间产物，几百个 PNG 铺在目录里
    # 又乱又没用。要留就显式加 --keep-chunks。
    if options.screens and not options.keep_chunks:
        screen_files, layout = write_screens_only(
            plan,
            output_dir=output_dir,
            renderer=renderer,
            options=options,
            progress=progress,
        )
        return SessionOutput(
            session_dir=output_dir,
            artifacts=(),
            screens=tuple(screen_files),
            screen_layout=layout,
        )

    artifacts = write_sequence(
        plan, output_dir=output_dir, renderer=renderer, progress=progress
    )

    sheets: list[Path] = []
    if options.contact_sheet:
        sheets = write_sheets(plan, artifacts, output_dir=output_dir, options=options)

    pdf_path: Path | None = None
    if options.pdf:
        pdf_path = write_pdf(
            plan,
            artifacts,
            output_path=output_dir / "print.pdf",
            options=options,
        )

    screen_files: list[Path] = []
    layout: ScreenLayout | None = None
    if options.screens:
        screen_files, layout = write_screen_pages(
            artifacts, output_dir=output_dir, options=options
        )

    return SessionOutput(
        session_dir=output_dir,
        artifacts=tuple(artifacts),
        sheets=tuple(sheets),
        pdf=pdf_path,
        screens=tuple(screen_files),
        screen_layout=layout,
    )
