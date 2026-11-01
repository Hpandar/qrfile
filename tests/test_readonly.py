"""源文件只读护栏（方案第零章）。

三层各测各的：唯一读取入口、审计钩子、指纹比对。外加写盘前的目标检查。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from qrfile.errors import InputError, SourceGuardError
from qrfile.readonly import (
    activate_source_guard,
    assert_source_untouched,
    check_write_target,
    compare_snapshots,
    deactivate_source_guard,
    guard_is_active,
    read_bytes,
    sha256_file,
    snapshot_tree,
)


@pytest.fixture
def source_tree(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_bytes(b"hello\n")
    (src / "sub" / "b.bin").write_bytes(bytes(range(256)))
    (src / "empty.txt").write_bytes(b"")
    return src


@pytest.fixture
def guarded(source_tree: Path):
    """开启护栏，测完关掉 —— 钩子装上去撤不掉，所以用开关变量控制。"""
    activate_source_guard([str(source_tree)])
    try:
        yield source_tree
    finally:
        deactivate_source_guard()


# --------------------------------------------------------------------------
# 第 1 层：唯一读取入口
# --------------------------------------------------------------------------


def test_read_bytes_is_exact(source_tree: Path) -> None:
    assert read_bytes(source_tree / "a.txt") == b"hello\n"
    assert read_bytes(source_tree / "sub" / "b.bin") == bytes(range(256))
    assert read_bytes(source_tree / "empty.txt") == b""


def test_read_bytes_does_not_translate_newlines(source_tree: Path) -> None:
    """文本模式会把 \\n 变成 \\r\\n —— 这是字节一致的头号杀手。"""
    crlf = source_tree / "crlf.txt"
    crlf.write_bytes(b"a\r\nb\nc\r")
    assert read_bytes(crlf) == b"a\r\nb\nc\r"


def test_sha256_matches_hashlib(source_tree: Path) -> None:
    import hashlib

    expected = hashlib.sha256(bytes(range(256))).hexdigest()
    assert sha256_file(source_tree / "sub" / "b.bin") == expected


def test_read_bytes_on_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_bytes(tmp_path / "nope.txt")


# --------------------------------------------------------------------------
# 第 2 层：审计钩子
# --------------------------------------------------------------------------


def test_guard_blocks_overwriting_source_file(guarded: Path) -> None:
    with pytest.raises(SourceGuardError, match="只读护栏"):
        open(guarded / "a.txt", "w")


def test_guard_blocks_appending_to_source_file(guarded: Path) -> None:
    with pytest.raises(SourceGuardError):
        open(guarded / "a.txt", "a")


def test_guard_blocks_read_write_mode(guarded: Path) -> None:
    with pytest.raises(SourceGuardError):
        open(guarded / "a.txt", "r+")


def test_guard_blocks_os_open_with_write_flags(guarded: Path) -> None:
    with pytest.raises(SourceGuardError):
        os.open(guarded / "a.txt", os.O_WRONLY)


def test_guard_blocks_creating_file_inside_source(guarded: Path) -> None:
    """在源目录里新建文件（比如落个 .tmp）同样违反需求。"""
    with pytest.raises(SourceGuardError):
        open(guarded / "new.tmp", "w")


def test_guard_blocks_remove(guarded: Path) -> None:
    with pytest.raises(SourceGuardError):
        os.remove(guarded / "a.txt")


def test_guard_blocks_rename_into_source(guarded: Path, tmp_path: Path) -> None:
    """源目录外的文件被改名进源目录 —— 目标参数会被抓到。"""
    outsider = tmp_path / "outsider.txt"
    outsider.write_bytes(b"x")
    with pytest.raises(SourceGuardError):
        os.rename(outsider, guarded / "landed.txt")


def test_guard_blocks_chmod_and_utime(guarded: Path) -> None:
    with pytest.raises(SourceGuardError):
        os.chmod(guarded / "a.txt", 0o600)
    with pytest.raises(SourceGuardError):
        os.utime(guarded / "a.txt")


def test_guard_allows_reading(guarded: Path) -> None:
    """只读操作必须放行 —— 否则工具自己就跑不动了。"""
    assert read_bytes(guarded / "a.txt") == b"hello\n"
    assert (guarded / "a.txt").read_bytes() == b"hello\n"
    assert os.path.getsize(guarded / "a.txt") == 6
    with open(guarded / "a.txt", "rb") as fh:
        assert fh.read() == b"hello\n"


def test_guard_allows_writing_outside_source(guarded: Path, tmp_path: Path) -> None:
    """输出目录在源之外，写它不该被拦。"""
    out = tmp_path / "out"
    out.mkdir()
    (out / "result.png").write_bytes(b"not really a png")
    assert (out / "result.png").read_bytes() == b"not really a png"


def test_guard_allows_creating_output_dir_sibling(guarded: Path, tmp_path: Path) -> None:
    (tmp_path / "qrout").mkdir()


def test_guard_is_inactive_after_deactivation(source_tree: Path) -> None:
    activate_source_guard([str(source_tree)])
    deactivate_source_guard()
    assert not guard_is_active()
    (source_tree / "a.txt").write_bytes(b"changed")  # 不该抛
    assert read_bytes(source_tree / "a.txt") == b"changed"


# --------------------------------------------------------------------------
# 第 3 层：指纹比对
# --------------------------------------------------------------------------


def test_snapshot_clean_compare_is_empty(source_tree: Path) -> None:
    before = snapshot_tree(str(source_tree))
    assert compare_snapshots(before, snapshot_tree(str(source_tree))) == []


def test_snapshot_covers_files_dirs_and_content(source_tree: Path) -> None:
    snapshot = snapshot_tree(str(source_tree))
    by_name = {Path(k).name: v for k, v in snapshot.items()}
    assert by_name["a.txt"].kind == "file"
    assert by_name["a.txt"].size == 6
    assert by_name["a.txt"].sha256 is not None
    assert by_name["sub"].kind == "dir"
    assert by_name["sub"].sha256 is None
    assert by_name["empty.txt"].size == 0


def test_compare_detects_content_change(source_tree: Path) -> None:
    before = snapshot_tree(str(source_tree))
    (source_tree / "a.txt").write_bytes(b"tampered")
    changes = compare_snapshots(before, snapshot_tree(str(source_tree)))
    assert any("内容变了" in c for c in changes)


def test_compare_detects_added_and_removed(source_tree: Path) -> None:
    before = snapshot_tree(str(source_tree))
    (source_tree / "added.txt").write_bytes(b"new")
    os.remove(source_tree / "empty.txt")
    changes = compare_snapshots(before, snapshot_tree(str(source_tree)))
    assert any("凭空出现了" in c for c in changes)
    assert any("消失了" in c for c in changes)


def test_compare_detects_mtime_change(source_tree: Path) -> None:
    """文件：哪怕内容没变，只是 mtime 被动了，也要报出来。"""
    before = snapshot_tree(str(source_tree))
    st = os.stat(source_tree / "a.txt")
    os.utime(source_tree / "a.txt", ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    changes = compare_snapshots(before, snapshot_tree(str(source_tree)))
    assert any("修改时间变了" in c for c in changes)


def test_directory_mtime_is_not_a_modification_signal(source_tree: Path) -> None:
    """目录 mtime 是派生状态，必须忽略。

    回归测试：Windows 上实测出现过两次快照之间目录 mtime 变化、但条目集合完全一致
    的情况，导致 SourceGuardError 误报。目录真正变了（增删条目）由键集合差异直接
    抓到，比 mtime 更准，所以比对目录属性只有坏处没有好处。
    """
    before = snapshot_tree(str(source_tree))
    sub = source_tree / "sub"
    st = os.stat(sub)
    try:
        os.utime(sub, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    except OSError:
        pytest.skip("本机不允许对目录调用 os.utime")

    assert compare_snapshots(before, snapshot_tree(str(source_tree))) == []
    # 但目录里真增删了东西，必须抓到
    (sub / "extra.txt").write_bytes(b"x")
    assert any("凭空出现了" in c for c in compare_snapshots(before, snapshot_tree(str(source_tree))))


def test_compare_detects_kind_change(source_tree: Path) -> None:
    """同一个路径从文件变成目录 —— 必须报出来，不能被属性比对漏掉。"""
    before = snapshot_tree(str(source_tree))
    os.remove(source_tree / "empty.txt")
    (source_tree / "empty.txt").mkdir()
    changes = compare_snapshots(before, snapshot_tree(str(source_tree)))
    assert any("类型变了" in c for c in changes)


def test_assert_source_untouched_passes_when_clean(source_tree: Path) -> None:
    before = snapshot_tree(str(source_tree))
    assert_source_untouched(str(source_tree), before)  # 不该抛


def test_assert_source_untouched_raises_on_change(source_tree: Path) -> None:
    before = snapshot_tree(str(source_tree))
    (source_tree / "sub" / "b.bin").write_bytes(b"clobbered")
    with pytest.raises(SourceGuardError, match="发生了变化"):
        assert_source_untouched(str(source_tree), before)


def test_excludes_are_skipped(source_tree: Path) -> None:
    (source_tree / ".DS_Store").write_bytes(b"junk")
    (source_tree / "Thumbs.db").write_bytes(b"junk")
    snapshot = snapshot_tree(str(source_tree))
    assert not any(Path(k).name in {".DS_Store", "Thumbs.db"} for k in snapshot)


# --------------------------------------------------------------------------
# 写盘前的目标检查（方案 0.3 的后三行）
# --------------------------------------------------------------------------


def test_write_target_fresh_path_ok(tmp_path: Path) -> None:
    check_write_target(str(tmp_path / "fresh.txt"))


def test_write_target_refuses_overwrite_by_default(tmp_path: Path) -> None:
    existing = tmp_path / "existing.txt"
    existing.write_bytes(b"original")
    with pytest.raises(InputError, match="拒绝覆盖"):
        check_write_target(str(existing))
    check_write_target(str(existing), overwrite=True)  # 显式允许就放行
    assert existing.read_bytes() == b"original"  # 而且检查本身不写东西


def test_write_target_refuses_hardlink(tmp_path: Path) -> None:
    """硬链接：写它就等于改到另一个位置的同一份内容 —— 可能正是源文件。"""
    original = tmp_path / "original.txt"
    original.write_bytes(b"source data")
    linked = tmp_path / "linked.txt"
    try:
        os.link(original, linked)
    except OSError:
        pytest.skip("本机文件系统不支持硬链接")

    with pytest.raises(InputError, match="硬链接"):
        check_write_target(str(linked), overwrite=True)
    assert original.read_bytes() == b"source data"


def test_write_target_refuses_symlink(tmp_path: Path) -> None:
    target = tmp_path / "real.txt"
    target.write_bytes(b"x")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("本机无法创建符号链接（Windows 需开发者模式）")

    with pytest.raises(InputError, match="符号链接"):
        check_write_target(str(link), overwrite=True)
