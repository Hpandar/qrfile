"""实时链路：全类型扫描、自适应布局、压缩方法随码传递、端到端还原。

这条链路的承诺和文本链路一致：**还原后逐字节一致，目录结构完整**。
区别只在于采集方式（屏幕播放 + 截图/录屏）和输入类型（允许二进制）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from qrfile.archive import build_session, manifest_for_sources
from qrfile.capture import parse_region
from qrfile.chunk import ChunkError, new_session_id, unpack_frame
from qrfile.decode import decode_image
from qrfile.errors import InputError
from qrfile.livelayout import choose_layout
from qrfile.livescan import scan_all
from qrfile.player import compose_page
from qrfile.receiver import LiveCollector


def _make_tree(root: Path) -> Path:
    source = root / "src"
    (source / "deep" / "a").mkdir(parents=True)
    (source / "子目录" / "空目录").mkdir(parents=True)
    (source / "随机二进制.bin").write_bytes(os.urandom(12_000))
    (source / "中文文本.txt").write_text("离线二维码传输测试\r\n" * 400, encoding="utf-8")
    (source / "deep" / "a" / "note.md").write_text("# hi\n" * 200, encoding="utf-8")
    return source


def _build(source: Path, *, chunk_size: int, compression: str = "auto"):
    result = scan_all([str(source)])
    manifest, blob = manifest_for_sources(
        root=result.root_label,
        sources=result.sources,
        dirs=result.empty_dirs,
        symlinks=result.symlinks,
        skipped=[(issue.path, issue.reason) for issue in result.skipped],
        chunk_size=chunk_size,
    )
    plan = build_session(
        manifest=manifest,
        blob=blob,
        session_id=new_session_id(),
        chunk_size=chunk_size,
        compression=compression,
    )
    return result, plan


# --------------------------------------------------------------------------
# 扫描器
# --------------------------------------------------------------------------


def test_livescan_accepts_binary_and_keeps_structure(tmp_path: Path) -> None:
    source = _make_tree(tmp_path)
    result = scan_all([str(source)])

    paths = {item.path for item in result.sources}
    # 根目录名作为前缀保留，还原时才会重建 src/
    assert "src/随机二进制.bin" in paths
    assert "src/中文文本.txt" in paths
    assert "src/deep/a/note.md" in paths
    # 空目录必须显式记录，否则还原时丢失
    assert "src/子目录/空目录" in result.empty_dirs
    assert result.total_bytes > 12_000


def test_livescan_rejects_missing_path() -> None:
    with pytest.raises(InputError):
        scan_all(["definitely-not-here"])


# --------------------------------------------------------------------------
# 布局
# --------------------------------------------------------------------------


def test_layout_fits_on_screen() -> None:
    for width, height in ((1366, 768), (1920, 1080), (2560, 1440), (3840, 2160)):
        layout = choose_layout(width, height)
        assert layout.tile_px <= width
        assert layout.tile_px <= layout.usable_height
        assert layout.per_screen >= 1


def test_layout_single_mode_is_one_code() -> None:
    layout = choose_layout(1920, 1080, mode="single")
    assert layout.per_screen == 1
    assert layout.body_capacity > 0


def test_layout_bigger_screen_at_least_as_good() -> None:
    small = choose_layout(1366, 768)
    big = choose_layout(1920, 1080)
    assert big.bytes_per_screen >= small.bytes_per_screen


def test_layout_respects_max_codes() -> None:
    layout = choose_layout(3840, 2160, max_codes=3)
    assert layout.per_screen <= 3


# --------------------------------------------------------------------------
# 压缩方法随码传递
# --------------------------------------------------------------------------


def test_data_frame_carries_compression_method(tmp_path: Path) -> None:
    source = _make_tree(tmp_path)
    _result, plan = _build(source, chunk_size=1200, compression="max")

    if plan.manifest.compression == "none":
        pytest.skip("这批数据压不动，方法不会写进码")

    header, _ = unpack_frame(plan.data_frames[0])
    assert header.compression == plan.manifest.compression


def test_unknown_method_id_is_rejected() -> None:
    from qrfile.chunk import pack_frame

    frame = bytearray(
        pack_frame(session_id=b"12345678", index=0, total=1, body=b"x", method=0)
    )
    frame[28] = 200  # 方法 id 伪造
    import binascii

    head = bytes(frame[:26])
    frame[26:28] = binascii.crc_hqx(head, 0xFFFF).to_bytes(2, "big")
    with pytest.raises(ChunkError, match="压缩方法"):
        unpack_frame(bytes(frame))


# --------------------------------------------------------------------------
# 接收器
# --------------------------------------------------------------------------


def test_collector_reports_missing_and_complete() -> None:
    collector = LiveCollector()
    assert collector.feed(b"not a qrfile frame") == "foreign"

    from qrfile.chunk import pack_frame

    first = pack_frame(session_id=b"abcdefgh", index=0, total=2, body=b"a")
    second = pack_frame(session_id=b"abcdefgh", index=1, total=2, body=b"b")

    assert collector.feed(first) == "accepted"
    assert collector.feed(first) == "duplicate"
    assert not collector.complete
    assert collector.missing() == [1]

    assert collector.feed(second) == "accepted"
    assert collector.complete


def test_collector_rejects_conflicting_duplicate() -> None:
    from qrfile.chunk import pack_frame

    collector = LiveCollector()
    collector.feed(pack_frame(session_id=b"abcdefgh", index=0, total=2, body=b"a"))
    collector.feed(pack_frame(session_id=b"abcdefgh", index=0, total=2, body=b"DIFFERENT"))
    assert collector.conflicts == 1


# --------------------------------------------------------------------------
# 端到端：渲染整屏页 → 解码 → 逐文件比对
# --------------------------------------------------------------------------


@pytest.mark.parametrize("compression", ["auto", "none"])
def test_live_roundtrip_through_pages(tmp_path: Path, compression: str) -> None:
    source = _make_tree(tmp_path)
    layout = choose_layout(1280, 720, ecc="M", mode="auto", max_codes=4)
    _result, plan = _build(source, chunk_size=layout.body_capacity, compression=compression)

    collector = LiveCollector()
    for index in range(layout.page_count(plan.total)):
        page = compose_page(plan.frames, index, layout, fps=5.0)
        for symbol in decode_image(page, source=f"page {index}"):
            collector.feed(symbol.payload)

    assert collector.complete, collector.progress()
    session, _stats = collector.finalize()

    for entry in session.manifest.files:
        # entry.path 现在带根目录前缀（src/...），相对于 source 的父目录
        original = (source.parent / entry.path).read_bytes()
        assert session.file_bytes(entry) == original, entry.path

    assert session.manifest.root == "src"
    assert any(d.path.endswith("空目录") for d in session.manifest.dirs)


def test_all_frames_render_same_size(tmp_path: Path) -> None:
    """短分片也要补到布局容量 —— 否则播放时二维码忽大忽小。"""
    from qrfile.player import qr_image

    source = _make_tree(tmp_path)
    layout = choose_layout(1280, 720, ecc="M", mode="single")
    _result, plan = _build(source, chunk_size=layout.body_capacity)

    sizes = {qr_image(frame, layout).size for frame in plan.frames}
    assert len(sizes) == 1, sizes


def test_live_roundtrip_single_code_mode(tmp_path: Path) -> None:
    source = _make_tree(tmp_path)
    layout = choose_layout(1280, 720, ecc="M", mode="single")
    _result, plan = _build(source, chunk_size=layout.body_capacity)

    collector = LiveCollector()
    for index in range(layout.page_count(plan.total)):
        page = compose_page(plan.frames, index, layout, fps=5.0)
        for symbol in decode_image(page, source=f"page {index}"):
            collector.feed(symbol.payload)

    assert collector.complete
    session, _stats = collector.finalize()
    for entry in session.manifest.files:
        assert session.file_bytes(entry) == (source.parent / entry.path).read_bytes()


# --------------------------------------------------------------------------
# 采集参数
# --------------------------------------------------------------------------


def test_parse_and_format_ranges() -> None:
    from qrfile.session import format_ranges, pages_for_indices, parse_ranges

    assert parse_ranges("12-14,204,900-901") == [12, 13, 14, 204, 900, 901]
    assert parse_ranges("5, 5, 7") == [5, 7]
    assert format_ranges([12, 13, 14, 204, 900, 901]) == "12-14,204,900-901"
    assert pages_for_indices([2, 3, 4, 5], 2) == [1, 2]
    with pytest.raises(InputError):
        parse_ranges("5-1")
    with pytest.raises(InputError):
        parse_ranges("")


def test_repair_rebuild_is_byte_identical(tmp_path: Path) -> None:
    """补播必须能重建出与原始播放完全相同的分片。"""
    from qrfile.livescan import read_files, scan_all
    from qrfile.session import SessionRecord, load_session, save_session

    source = _make_tree(tmp_path)
    result = scan_all([str(source)])
    layout = choose_layout(1280, 720, ecc="M", mode="single")
    manifest, blob = manifest_for_sources(
        root=result.root_label,
        sources=result.sources,
        dirs=result.empty_dirs,
        symlinks=result.symlinks,
        skipped=[(issue.path, issue.reason) for issue in result.skipped],
        chunk_size=layout.body_capacity,
    )
    session_id = new_session_id()
    original = build_session(
        manifest=manifest,
        blob=blob,
        session_id=session_id,
        chunk_size=layout.body_capacity,
        compression="auto",
    )

    record = SessionRecord(
        session_id=session_id,
        chunk_size=layout.body_capacity,
        data_compression=original.data_compression,
        manifest_compression=original.manifest_compression,
        layout=layout,
        manifest=original.manifest,
        sources=(str(source),),
        total_frames=original.total,
        files=tuple(zip([s.path for s in result.sources], result.absolute_paths)),
    )
    path = tmp_path / "session.json"
    save_session(path, record)
    loaded = load_session(path)

    sources = read_files(loaded.files)
    rebuilt_manifest, rebuilt_blob = manifest_for_sources(
        root=loaded.manifest.root,
        sources=sources,
        dirs=[entry.resolve_path() for entry in loaded.manifest.dirs],
        symlinks=[(link.resolve_path(), link.target) for link in loaded.manifest.symlinks],
        skipped=[(item.path, item.reason) for item in loaded.manifest.skipped],
        chunk_size=loaded.chunk_size,
    )
    assert rebuilt_manifest.archive_sha256 == loaded.manifest.archive_sha256

    rebuilt = build_session(
        manifest=loaded.manifest,
        blob=rebuilt_blob,
        session_id=loaded.session_id,
        chunk_size=loaded.chunk_size,
        compression=loaded.data_compression,
        manifest_compression=loaded.manifest_compression,
    )
    assert rebuilt.total == loaded.total_frames
    assert rebuilt.frames == original.frames
    assert rebuilt.manifest.to_bytes() == loaded.manifest.to_bytes()


def test_parse_region() -> None:
    assert parse_region("100,200,800,600") == (100, 200, 900, 800)
    assert parse_region("0，0，1920，1080") == (0, 0, 1920, 1080)
    with pytest.raises(InputError):
        parse_region("1,2,3")
    with pytest.raises(InputError):
        parse_region("1,2,0,4")


# --------------------------------------------------------------------------
# 跨平台：Linux 窗口解析
# --------------------------------------------------------------------------


def test_parse_wmctrl_output() -> None:
    from qrfile.capture import _parse_wmctrl

    text = (
        "0x03a00007  0  1920  0  1280  720  host  远程桌面连接\n"
        "0x04b00001  1  0     0  800   600  host  记事本\n"
        "garbage line\n"
    )
    windows = _parse_wmctrl(text)
    assert len(windows) == 2
    assert windows[0].title == "远程桌面连接"
    assert windows[0].region == (1920, 0, 3200, 720)
    assert windows[1].region == (0, 0, 800, 600)


def test_guess_remote_window_picks_best(monkeypatch) -> None:
    from qrfile import capture

    monkeypatch.setattr(
        capture,
        "_list_windows",
        lambda: [
            capture.WindowInfo("远程桌面连接", 100, 50, 1280, 720),
            capture.WindowInfo("记事本", 0, 0, 800, 600),
            capture.WindowInfo("VNC Viewer - host", 0, 0, 1000, 700),
        ],
    )
    region, title = capture.guess_remote_window()
    assert region == (100, 50, 1380, 770)
    assert title == "远程桌面连接"


def test_resolve_window_matches_substring(monkeypatch) -> None:
    from qrfile import capture

    monkeypatch.setattr(
        capture,
        "_list_windows",
        lambda: [capture.WindowInfo("RDP - 10.0.0.5", 10, 20, 800, 600)],
    )
    region, title = capture.resolve_window("rdp")
    assert region == (10, 20, 810, 620)
    assert title == "RDP - 10.0.0.5"


# --------------------------------------------------------------------------
# 极简入口：push / pull
# --------------------------------------------------------------------------


def _fake_sender(monkeypatch):
    from qrfile import live_cli

    captured: dict = {}

    def fake_send(sources, **kwargs):
        captured["sources"] = sources
        captured.update(kwargs)

    monkeypatch.setattr(live_cli, "_send_impl", fake_send)
    return live_cli, captured


def test_push_fast_preset(tmp_path, monkeypatch) -> None:
    from click.testing import CliRunner

    live_cli, captured = _fake_sender(monkeypatch)
    sample = tmp_path / "a.txt"
    sample.write_text("hi", encoding="utf-8")

    result = CliRunner().invoke(live_cli.push_cmd, [str(sample), "--fast"])
    assert result.exit_code == 0, result.output
    assert captured["mode"] == "auto"
    assert captured["module_px"] is None
    assert captured["fps"] == 5.0
    assert captured["camera"] is False


def test_push_safe_preset(tmp_path, monkeypatch) -> None:
    from click.testing import CliRunner

    live_cli, captured = _fake_sender(monkeypatch)
    sample = tmp_path / "a.txt"
    sample.write_text("hi", encoding="utf-8")

    result = CliRunner().invoke(live_cli.push_cmd, [str(sample), "--safe"])
    assert result.exit_code == 0, result.output
    assert captured["mode"] == "single"
    assert captured["module_px"] == 6
    assert captured["ecc"] == "H"
    assert captured["fps"] == 4.0


def test_push_rejects_fast_and_safe_together(tmp_path, monkeypatch) -> None:
    from click.testing import CliRunner

    live_cli, _captured = _fake_sender(monkeypatch)
    sample = tmp_path / "a.txt"
    sample.write_text("hi", encoding="utf-8")

    result = CliRunner().invoke(live_cli.push_cmd, [str(sample), "--fast", "--safe"])
    assert result.exit_code != 0


def test_pull_auto_locates_window(monkeypatch) -> None:
    from click.testing import CliRunner
    from qrfile import live_cli

    monkeypatch.setattr(
        live_cli, "guess_remote_window", lambda: ((10, 20, 810, 620), "远程桌面连接")
    )
    captured: dict = {}
    monkeypatch.setattr(
        live_cli, "_receive_loop",
        lambda output, **kwargs: captured.update(kwargs, output=output),
    )

    result = CliRunner().invoke(live_cli.pull_cmd, ["outdir"])
    assert result.exit_code == 0, result.output
    assert captured["rect"] == (10, 20, 810, 620)
    assert captured["fps"] == 8.0
    assert captured["robust"] is False


def test_pull_safe_enables_robust(monkeypatch) -> None:
    from click.testing import CliRunner
    from qrfile import live_cli

    monkeypatch.setattr(live_cli, "guess_remote_window", lambda: None)
    captured: dict = {}
    monkeypatch.setattr(
        live_cli, "_receive_loop",
        lambda output, **kwargs: captured.update(kwargs, output=output),
    )

    result = CliRunner().invoke(live_cli.pull_cmd, ["outdir", "--safe"])
    assert result.exit_code == 0, result.output
    assert captured["rect"] is None
    assert captured["fps"] == 6.0
    assert captured["robust"] is True


def test_main_without_command_prints_cheat_sheet() -> None:
    from click.testing import CliRunner
    from qrfile.cli import main

    result = CliRunner().invoke(main, [])
    assert result.exit_code == 0
    assert "qrfile push" in result.output
    assert "qrfile pull" in result.output
