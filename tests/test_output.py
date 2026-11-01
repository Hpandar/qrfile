"""输出形态：PNG 序列、总览图、PDF，以及"从磁盘读回来还能还原"。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PIL import Image

from qrfile.archive import FileSource, build_session, manifest_for_sources, reassemble
from qrfile.capacity import get_profile
from qrfile.decode import decode_file, decode_image, find_image_files
from qrfile.manifest import COMPRESSION_AUTO, COMPRESSION_NONE
from qrfile.output import (
    OutputOptions,
    build_sheets,
    chunk_filename,
    write_pdf,
    write_sequence,
    write_session,
)
from qrfile.render import Renderer

SESSION = b"\x0a\x0b\x0c\x0d\x0e\x0f\x10\x11"
SCREEN = get_profile("screen")


@pytest.fixture
def renderer() -> Renderer:
    return Renderer(SCREEN)


def _plan(files: dict[str, bytes], *, chunk_size: int = 300, compression: str = COMPRESSION_AUTO):
    sources = [FileSource(path=p, data=d, mode=0o644, mtime_ns=None) for p, d in sorted(files.items())]
    manifest, blob = manifest_for_sources(root="src", sources=sources, chunk_size=chunk_size)
    return build_session(
        manifest=manifest,
        blob=blob,
        session_id=SESSION,
        chunk_size=chunk_size,
        compression=compression,
    )


def _decode_dir(directory: Path) -> list[bytes]:
    paths = find_image_files([str(directory)])
    payloads: list[bytes] = []
    for path in paths:
        payloads.extend(s.payload for s in decode_file(path))
    return payloads


# --------------------------------------------------------------------------
# 文件名
# --------------------------------------------------------------------------


def test_chunk_filename_format() -> None:
    assert chunk_filename(0, 17, is_manifest=False) == "data.p0001of0017.png"
    assert chunk_filename(16, 17, is_manifest=False) == "data.p0017of0017.png"
    assert chunk_filename(0, 2, is_manifest=True) == "manifest.p0001of0002.png"


def test_chunk_filename_pads_to_at_least_four_digits() -> None:
    assert chunk_filename(0, 5, is_manifest=False) == "data.p0001of0005.png"


# --------------------------------------------------------------------------
# PNG 序列
# --------------------------------------------------------------------------


def test_write_sequence_creates_expected_layout(tmp_path: Path, renderer: Renderer) -> None:
    plan = _plan({"a.txt": b"hello\n", "b.txt": os.urandom(1000)})
    artifacts = write_sequence(plan, output_dir=tmp_path / "out", renderer=renderer)

    assert len(artifacts) == plan.total
    assert (tmp_path / "out" / "manifest").is_dir()
    assert (tmp_path / "out" / "data").is_dir()
    assert all(a.path.exists() for a in artifacts)
    assert sum(1 for a in artifacts if a.is_manifest) == plan.manifest_count


def test_no_temp_files_left_behind(tmp_path: Path, renderer: Renderer) -> None:
    plan = _plan({"a.txt": os.urandom(900)})
    write_sequence(plan, output_dir=tmp_path / "out", renderer=renderer)
    leftovers = [p for p in (tmp_path / "out").rglob("*.tmp")]
    assert leftovers == []


def test_png_sequence_roundtrips_from_disk(tmp_path: Path, renderer: Renderer) -> None:
    """最重要的一条：真的写到盘上、再从盘上读回来，内容必须一致。"""
    files = {"a.txt": b"aaa\n", "dir/b.md": b"# b\n", "dir/sub/c.txt": os.urandom(2000)}
    plan = _plan(files)
    write_sequence(plan, output_dir=tmp_path / "out", renderer=renderer)

    session, stats = reassemble(_decode_dir(tmp_path / "out"))
    recovered = {e.path: session.file_bytes(e) for e in session.manifest.files}
    assert recovered == files
    assert stats.foreign == 0 and stats.damaged == 0


def test_renaming_files_does_not_break_decoding(tmp_path: Path, renderer: Renderer) -> None:
    """解码完全靠分片头里的 index，文件名只是给人看的。"""
    files = {"a.txt": os.urandom(800)}
    plan = _plan(files)
    write_sequence(plan, output_dir=tmp_path / "out", renderer=renderer)

    flat = tmp_path / "flat"
    flat.mkdir()
    for path in find_image_files([str(tmp_path / "out")]):
        os.rename(path, flat / f"renamed_{len(list(flat.iterdir()))}.png")

    session, _ = reassemble(_decode_dir(flat))
    assert session.file_bytes(session.manifest.files[0]) == files["a.txt"]


def test_progress_callback_reports_every_chunk(tmp_path: Path, renderer: Renderer) -> None:
    plan = _plan({"a.txt": os.urandom(900)})
    seen: list[tuple[int, int]] = []
    write_sequence(
        plan,
        output_dir=tmp_path / "out",
        renderer=renderer,
        progress=lambda done, total: seen.append((done, total)),
    )
    assert len(seen) == plan.total
    assert seen[-1] == (plan.total, plan.total)


# --------------------------------------------------------------------------
# 总览图
# --------------------------------------------------------------------------


def test_contact_sheet_is_written_and_decodable(tmp_path: Path, renderer: Renderer) -> None:
    """总览图必须**真的能一次解出全部码** —— 这是主链路的提速手段。"""
    files = {"a.txt": os.urandom(1500)}
    plan = _plan(files, chunk_size=200)
    result = write_session(
        plan,
        output_dir=tmp_path / "out",
        renderer=renderer,
        options=OutputOptions(contact_sheet=True, sheet_columns=4),
    )
    assert len(result.sheets) == 1

    payloads = [s.payload for s in decode_file(result.sheets[0])]
    assert len(payloads) == plan.total

    session, _ = reassemble(payloads)
    assert session.file_bytes(session.manifest.files[0]) == files["a.txt"]


def test_contact_sheet_splits_when_too_large(tmp_path: Path, renderer: Renderer) -> None:
    plan = _plan({"a.txt": os.urandom(3000)}, chunk_size=200)
    artifacts = write_sequence(plan, output_dir=tmp_path / "out", renderer=renderer)

    images = [Image.open(a.path) for a in artifacts]
    options = OutputOptions(contact_sheet=True, sheet_columns=4, max_sheet_px=900)
    sheets = build_sheets(images, options)

    assert len(sheets) > 1
    for sheet in sheets:
        assert sheet.size[0] <= 900 + 200 and sheet.size[1] <= 900 + 200


def test_build_sheets_on_empty_input() -> None:
    assert build_sheets([], OutputOptions()) == []


def _decode_image_payloads(image: Image.Image) -> list[bytes]:
    from qrfile.decode import decode_image

    return [s.payload for s in decode_image(image)]


def test_sheet_covers_every_code(tmp_path: Path, renderer: Renderer) -> None:
    """分张之后总数不能丢 —— 每张码都要出现在某一页上，且不重不漏。"""
    plan = _plan({"a.txt": os.urandom(2500)}, chunk_size=200)
    artifacts = write_sequence(plan, output_dir=tmp_path / "out", renderer=renderer)
    images = [Image.open(a.path) for a in artifacts]

    found: list[bytes] = []
    for sheet in build_sheets(images, OutputOptions(sheet_columns=3, max_sheet_px=1000)):
        found.extend(_decode_image_payloads(sheet))

    assert len(found) == plan.total
    assert sorted(found) == sorted(plan.frames)


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------


def test_pdf_is_created_and_wellformed(tmp_path: Path, renderer: Renderer) -> None:
    plan = _plan({"a.txt": os.urandom(1500)}, chunk_size=200)
    artifacts = write_sequence(plan, output_dir=tmp_path / "out", renderer=renderer)

    pdf_path = write_pdf(
        plan, artifacts, output_path=tmp_path / "out" / "print.pdf", options=OutputOptions()
    )
    assert pdf_path.exists()
    data = pdf_path.read_bytes()
    assert data.startswith(b"%PDF-")
    assert len(data) > 2000
    assert b"%%EOF" in data[-64:]


def test_pdf_paginates_when_many_codes(tmp_path: Path, renderer: Renderer) -> None:
    plan = _plan({"a.txt": os.urandom(3000)}, chunk_size=200)
    artifacts = write_sequence(plan, output_dir=tmp_path / "out", renderer=renderer)

    pdf_path = write_pdf(
        plan,
        artifacts,
        output_path=tmp_path / "out" / "print.pdf",
        options=OutputOptions(pdf_columns=2, pdf_rows=2),
    )
    # 每页 4 张，片数多于 4 就该有多页
    assert pdf_path.read_bytes().count(b"/Type /Page\n") >= 2 or len(artifacts) <= 4


# --------------------------------------------------------------------------
# 一站式
# --------------------------------------------------------------------------


def test_write_session_defaults_to_png_sequence_only(tmp_path: Path, renderer: Renderer) -> None:
    result = write_session(
        _plan({"a.txt": b"x"}), output_dir=tmp_path / "out", renderer=renderer
    )
    assert result.sheets == ()
    assert result.pdf is None
    assert result.png_count >= 1


def test_write_session_with_all_extras(tmp_path: Path, renderer: Renderer) -> None:
    result = write_session(
        _plan({"a.txt": os.urandom(1200)}, chunk_size=250),
        output_dir=tmp_path / "out",
        renderer=renderer,
        options=OutputOptions(contact_sheet=True, pdf=True),
    )
    assert result.sheets
    assert result.pdf is not None and result.pdf.exists()
    assert result.total_bytes > 0


def test_output_goes_only_into_output_dir(tmp_path: Path, renderer: Renderer) -> None:
    """输出目录之外（尤其是源树）不能有任何文件被创建。"""
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "a.txt").write_bytes(b"hello\n")
    before = {p.name for p in source_dir.rglob("*")}

    out = tmp_path / "out"
    plan = _plan({"a.txt": b"hello\n"})
    write_session(
        plan,
        output_dir=out,
        renderer=renderer,
        options=OutputOptions(contact_sheet=True, pdf=True),
    )

    assert {p.name for p in source_dir.rglob("*")} == before
    # 所有产物都落在输出目录里
    for path in out.rglob("*"):
        assert str(path).startswith(str(out))


def test_write_sequence_verifies_each_chunk_by_default(tmp_path: Path, renderer: Renderer) -> None:
    """写盘走的是 render_verified —— 编码期自验证不是可选项。"""
    plan = _plan({"a.txt": os.urandom(600)})
    artifacts = write_sequence(plan, output_dir=tmp_path / "out", renderer=renderer)
    # 每一张都应当能被独立解出，且内容跟原始帧一致
    for artifact in artifacts:
        payloads = [s.payload for s in decode_file(artifact.path)]
        assert payloads == [plan.frames[artifact.index]]


# --------------------------------------------------------------------------
# 整屏排版 —— 一屏多码，把"430 张照片"变成"21 张照片"
# --------------------------------------------------------------------------


def test_plan_screen_layout_fits_the_grid() -> None:
    from qrfile.output import plan_screen_layout

    # v20 码在 3px/模块下是 (97+8)*3 = 315px
    layout = plan_screen_layout(315, 315, screen_width=1920, screen_height=1080, gap=8)
    assert (layout.columns, layout.rows) == (5, 3)
    assert layout.per_screen == 15


def test_plan_screen_layout_never_returns_zero() -> None:
    """码比屏幕还大时也得给 1×1，不能算出 0 导致死循环/空输出。"""
    from qrfile.output import plan_screen_layout

    layout = plan_screen_layout(5000, 5000, screen_width=1920, screen_height=1080, gap=8)
    assert layout.columns == 1 and layout.rows == 1
    assert layout.per_screen == 1


def test_plan_screen_layout_describe() -> None:
    from qrfile.output import plan_screen_layout

    text = plan_screen_layout(315, 315, screen_width=1920, screen_height=1080, gap=8).describe(
        1920, 1080
    )
    assert "5×3" in text and "15 张/屏" in text


def test_build_screen_pages_covers_every_code_exactly_once() -> None:
    from qrfile.output import build_screen_pages, plan_screen_layout

    # 造 40 张假"码"，每屏放 15 张 → 应该 3 页
    tiles = [Image.new("1", (315, 315), 1) for _ in range(40)]
    layout = plan_screen_layout(315, 315, screen_width=1920, screen_height=1080, gap=8)
    pages = build_screen_pages(
        tiles, layout, screen_width=1920, screen_height=1080, gap=8
    )
    assert len(pages) == 3
    assert all(p.size == (1920, 1080) for p in pages)


def test_build_screen_pages_empty() -> None:
    from qrfile.output import build_screen_pages, plan_screen_layout

    layout = plan_screen_layout(315, 315, screen_width=1920, screen_height=1080, gap=8)
    assert build_screen_pages([], layout, screen_width=1920, screen_height=1080, gap=8) == []


def test_screen_pages_decode_every_chunk(tmp_path: Path, renderer: Renderer) -> None:
    """整屏页必须**一张不落地**能把所有分片解出来。"""
    from qrfile.output import OutputOptions, write_session

    # 用不可压缩的数据，片数才可预期（文本会被压到只剩几片，凑不够多屏）
    files = {"a.txt": os.urandom(15000)}
    plan = _plan(files, chunk_size=826)
    assert plan.total > 10, f"需要足够多的片才能跨多屏，现在只有 {plan.total} 片"

    # 用整屏模式渲染：模块小、一屏多码
    small = Renderer(SCREEN, scale=3, margin=4)
    result = write_session(
        plan,
        output_dir=tmp_path / "out",
        renderer=small,
        options=OutputOptions(screens=True),
    )

    assert len(result.screens) < plan.total, "整屏模式应该显著减少图的数量"
    assert result.screen_layout is not None
    assert result.screen_layout.per_screen > 1

    payloads: list[bytes] = []
    for page in result.screens:
        payloads.extend(s.payload for s in decode_file(page))

    session, _ = reassemble(payloads)
    assert session.blob == files["a.txt"]


def test_screen_pages_survive_a_simulated_photo(tmp_path: Path) -> None:
    """回归：模拟手机拍屏（斜一点 + 相机 2 倍采样 + 模糊 + 噪声 + JPEG）后仍能还原。

    这是这个功能存在的全部意义 —— 不测这条就等于没做。
    """
    import io
    import random

    from PIL import ImageFilter

    from qrfile.output import OutputOptions, write_session

    files = {"a.txt": os.urandom(12000)}
    plan = _plan(files, chunk_size=826)
    small = Renderer(SCREEN, scale=3, margin=4)
    result = write_session(
        plan,
        output_dir=tmp_path / "out",
        renderer=small,
        options=OutputOptions(screens=True),
    )

    collected: list[bytes] = []

    def shoot(image: Image.Image) -> Image.Image:
        image = image.convert("L").rotate(0.6, expand=True, fillcolor=255)
        w, h = image.size
        image = image.resize((int(w * 2), int(h * 2)), Image.LANCZOS)
        image = image.filter(ImageFilter.GaussianBlur(1.0))
        pixels = bytearray(image.tobytes())
        rng = random.Random(11)
        for i in range(0, len(pixels), 5):
            pixels[i] = max(0, min(255, pixels[i] + rng.randint(-10, 10)))
        image = Image.frombytes("L", image.size, bytes(pixels))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=75)
        return Image.open(io.BytesIO(buffer.getvalue())).convert("L")

    for page in result.screens:
        collected.extend(s.payload for s in decode_image(shoot(Image.open(page))))

    session, _ = reassemble(collected)
    assert session.blob == files["a.txt"]


def test_parse_screen_size_accepts_common_spellings() -> None:
    from qrfile.cli import _parse_screen_size

    assert _parse_screen_size("1920x1080") == (1920, 1080)
    assert _parse_screen_size("2560X1440") == (2560, 1440)
    assert _parse_screen_size("3840×2160") == (3840, 2160)


def test_parse_screen_size_rejects_garbage() -> None:
    from qrfile.cli import _parse_screen_size
    from qrfile.errors import InputError

    for bad in ("1920", "abc", "1920x", "10x10"):
        with pytest.raises(InputError):
            _parse_screen_size(bad)
