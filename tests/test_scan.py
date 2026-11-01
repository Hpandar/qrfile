"""目录递归扫描：结构保留、跳过报告、上限、只读。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from qrfile.capacity import get_profile
from qrfile.errors import InputError, LimitExceeded
from qrfile.readonly import compare_snapshots, snapshot_tree
from qrfile.scan import ScanLimits, scan_paths

PROFILE = get_profile("screen")


def _tree(tmp_path: Path) -> Path:
    """一棵有代表性的源树。"""
    root = tmp_path / "src"
    (root / "sub" / "deep").mkdir(parents=True)
    (root / "empty_dir").mkdir()
    (root / "sub" / "empty_nested").mkdir()
    (root / "a.txt").write_bytes(b"aaa\n")
    (root / "sub" / "b.md").write_bytes("# b\n".encode())
    (root / "sub" / "deep" / "c.py").write_bytes(b"print(1)\n")
    (root / "中文.txt").write_bytes("中文内容\n".encode())
    (root / ".DS_Store").write_bytes(b"junk")
    (root / "Thumbs.db").write_bytes(b"junk")
    (root / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n" + os.urandom(100))
    (root / "archive.zip").write_bytes(b"PK\x03\x04" + os.urandom(100))
    (root / "doc.docx").write_bytes(b"PK\x03\x04" + os.urandom(100))
    return root


def _scan(*roots, **kwargs):
    kwargs.setdefault("profile", PROFILE)
    return scan_paths([str(r) for r in roots], **kwargs)


def _paths(result) -> set[str]:
    return {s.path for s in result.sources}


# --------------------------------------------------------------------------
# 结构与内容
# --------------------------------------------------------------------------


def test_single_file(tmp_path: Path) -> None:
    path = tmp_path / "one.txt"
    path.write_bytes(b"hello\n")
    result = _scan(path)
    assert _paths(result) == {"one.txt"}
    assert result.sources[0].data == b"hello\n"
    assert result.root_label == "one.txt"


def test_directory_tree_relative_paths(tmp_path: Path) -> None:
    result = _scan(_tree(tmp_path))
    assert _paths(result) == {"a.txt", "sub/b.md", "sub/deep/c.py", "中文.txt"}
    assert result.root_label == "src"


def test_empty_directories_are_recorded(tmp_path: Path) -> None:
    """空目录必须显式记录 —— 否则还原时目录结构就丢了。"""
    result = _scan(_tree(tmp_path))
    assert set(result.empty_dirs) == {"empty_dir", "sub/empty_nested"}


def test_non_empty_dirs_are_not_listed_as_empty(tmp_path: Path) -> None:
    result = _scan(_tree(tmp_path))
    for implied in ("sub", "sub/deep"):
        assert implied not in result.empty_dirs


def test_binary_files_are_skipped_with_reasons(tmp_path: Path) -> None:
    result = _scan(_tree(tmp_path))
    reasons = {issue.path: issue.reason for issue in result.skipped}

    assert "image.png" in reasons and "image/png" in reasons["image.png"]
    assert "archive.zip" in reasons and "zip" in reasons["archive.zip"]
    # OOXML 的本质是 zip —— 提示里要说到 docx，否则用户会困惑
    assert "doc.docx" in reasons and "docx" in reasons["doc.docx"]
    assert all(r.startswith("binary:") for r in reasons.values())


def test_default_excludes_are_skipped(tmp_path: Path) -> None:
    result = _scan(_tree(tmp_path))
    assert ".DS_Store" not in _paths(result)
    assert "Thumbs.db" not in _paths(result)


def test_crlf_preserved_through_scan(tmp_path: Path) -> None:
    root = tmp_path / "src"
    root.mkdir()
    (root / "crlf.txt").write_bytes(b"a\r\nb\r\n")
    result = _scan(root)
    assert result.sources[0].data == b"a\r\nb\r\n"


def test_total_bytes_matches(tmp_path: Path) -> None:
    root = tmp_path / "src"
    root.mkdir()
    (root / "a.txt").write_bytes(b"12345")
    (root / "b.txt").write_bytes(b"678")
    result = _scan(root)
    assert result.total_bytes == 8


def test_scan_is_deterministic(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    first = _scan(tree)
    second = _scan(tree)
    assert [s.path for s in first.sources] == [s.path for s in second.sources]
    assert first.empty_dirs == second.empty_dirs


# --------------------------------------------------------------------------
# 只读（硬约束）
# --------------------------------------------------------------------------


def test_scan_does_not_modify_the_source_tree(tmp_path: Path) -> None:
    """扫描全程只读 —— 用前后指纹比对来证明，而不是靠承诺。"""
    tree = _tree(tmp_path)
    before = snapshot_tree(str(tree))
    _scan(tree)
    assert compare_snapshots(before, snapshot_tree(str(tree))) == []


def test_scan_does_not_create_temp_files_in_source(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    before = {p.name for p in tree.rglob("*")}
    _scan(tree)
    after = {p.name for p in tree.rglob("*")}
    assert before == after


# --------------------------------------------------------------------------
# 过滤
# --------------------------------------------------------------------------


def test_exclude_pattern(tmp_path: Path) -> None:
    result = _scan(_tree(tmp_path), exclude=["*.md"])
    assert "sub/b.md" not in _paths(result)
    assert any("--exclude" in i.reason for i in result.skipped)


def test_exclude_by_full_relative_path(tmp_path: Path) -> None:
    result = _scan(_tree(tmp_path), exclude=["sub/deep/*"])
    assert "sub/deep/c.py" not in _paths(result)


def test_include_pattern(tmp_path: Path) -> None:
    result = _scan(_tree(tmp_path), include=["*.py"])
    assert _paths(result) == {"sub/deep/c.py"}


# --------------------------------------------------------------------------
# 上限
# --------------------------------------------------------------------------


def test_oversized_file_is_skipped_not_fatal(tmp_path: Path) -> None:
    root = tmp_path / "src"
    root.mkdir()
    (root / "big.txt").write_bytes(b"x" * 5000)
    (root / "small.txt").write_bytes(b"ok")

    result = _scan(root, limits=ScanLimits(max_file_size=1000))
    assert _paths(result) == {"small.txt"}
    assert any("超过单文件上限" in i.reason for i in result.skipped)


def test_too_many_files_is_fatal_with_advice(tmp_path: Path) -> None:
    """会话级预算超了必须**报错**，不能悄悄只收一部分（见模块文档）。"""
    root = tmp_path / "src"
    root.mkdir()
    for i in range(10):
        (root / f"f{i}.txt").write_bytes(b"x")

    with pytest.raises(LimitExceeded, match="max-files|split-by-dir"):
        _scan(root, limits=ScanLimits(max_files=5))


def test_total_bytes_limit_is_fatal(tmp_path: Path) -> None:
    root = tmp_path / "src"
    root.mkdir()
    for i in range(5):
        (root / f"f{i}.txt").write_bytes(b"x" * 100)

    with pytest.raises(LimitExceeded, match="max-total-bytes|split-by-dir"):
        _scan(root, limits=ScanLimits(max_total_bytes=250))


def test_depth_limit_records_dir_without_descending(tmp_path: Path) -> None:
    root = tmp_path / "src"
    deep = root
    for i in range(8):
        deep = deep / f"d{i}"
    deep.mkdir(parents=True)
    (root / "top.txt").write_bytes(b"x")
    (deep / "bottom.txt").write_bytes(b"y")

    result = _scan(root, limits=ScanLimits(max_depth=3))
    assert _paths(result) == {"top.txt"}
    assert "bottom.txt" not in _paths(result)


def test_long_name_is_skipped(tmp_path: Path) -> None:
    """用**调低的上限**来触发，而不是造一个真长名字。

    本机 LongPathsEnabled=0，连 304 字符的文件名都创建不出来（MAX_PATH 260），
    真造名字这条路在 Windows 上走不通。调低上限同样能验证"检查被正确接上了"。
    """
    root = tmp_path / "src"
    root.mkdir()
    (root / "ok.txt").write_bytes(b"x")
    (root / "a_much_longer_name.txt").write_bytes(b"y")

    result = _scan(root, limits=ScanLimits(max_name_len=8))
    assert _paths(result) == {"ok.txt"}
    assert any("文件名超过" in i.reason for i in result.skipped)


def test_long_relative_path_is_skipped(tmp_path: Path) -> None:
    root = tmp_path / "src"
    nested = root / "a" / "b" / "c"
    nested.mkdir(parents=True)
    (nested / "deep.txt").write_bytes(b"x")
    (root / "top.txt").write_bytes(b"y")

    # "top.txt" 是 7 字节，上限设 8 让它通过；"a/b/c/deep.txt" 是 14 字节，被跳过
    result = _scan(root, limits=ScanLimits(max_path_len=8))
    assert _paths(result) == {"top.txt"}


# --------------------------------------------------------------------------
# 符号链接
# --------------------------------------------------------------------------


def _can_symlink(tmp_path: Path) -> bool:
    probe, target = tmp_path / "_l", tmp_path / "_t"
    target.mkdir()
    try:
        probe.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    finally:
        if probe.is_symlink():
            probe.unlink()
    return True


def test_symlinks_recorded_not_followed(tmp_path: Path) -> None:
    if not _can_symlink(tmp_path):
        pytest.skip("本机无法创建符号链接（Windows 需开发者模式）")

    root = tmp_path / "src"
    root.mkdir()
    (root / "real.txt").write_bytes(b"real")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"secret")
    (root / "link.txt").symlink_to(outside / "secret.txt")
    (root / "linkdir").symlink_to(outside, target_is_directory=True)

    result = _scan(root)
    assert _paths(result) == {"real.txt"}  # 链接目标没被卷进来
    assert {rel for rel, _ in result.symlinks} == {"link.txt", "linkdir"}


def test_symlink_cycle_does_not_hang(tmp_path: Path) -> None:
    if not _can_symlink(tmp_path):
        pytest.skip("本机无法创建符号链接")

    root = tmp_path / "src"
    root.mkdir()
    (root / "a.txt").write_bytes(b"x")
    (root / "loop").symlink_to(root, target_is_directory=True)

    result = _scan(root)  # 不该死循环
    assert "a.txt" in _paths(result)


# --------------------------------------------------------------------------
# 冲突与严格模式
# --------------------------------------------------------------------------


def test_case_collision_is_reported(tmp_path: Path) -> None:
    """Linux 上 File.txt 和 file.txt 是两个文件，Windows 上是一个。
    编码阶段就要查出来，否则在 Windows 上还原时会静默覆盖。"""
    root = tmp_path / "src"
    root.mkdir()
    (root / "File.txt").write_bytes(b"one")
    (root / "file.txt").write_bytes(b"two")

    result = _scan(root)
    if len(_paths(result)) < 2:
        pytest.skip("本机文件系统大小写不敏感，造不出这个冲突")
    assert len(result.collisions) == 1


def test_strict_mode_aborts_on_skips(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="strict"):
        _scan(_tree(tmp_path), strict=True)


def test_non_strict_mode_records_skips_and_continues(tmp_path: Path) -> None:
    result = _scan(_tree(tmp_path))
    assert result.skipped  # 有跳过
    assert result.sources  # 但仍然收了文本文件


# --------------------------------------------------------------------------
# 多根路径
# --------------------------------------------------------------------------


def test_multiple_roots_get_prefixed(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "same.txt").write_bytes(b"L")
    (right / "same.txt").write_bytes(b"R")

    result = _scan(left, right)
    assert _paths(result) == {"left/same.txt", "right/same.txt"}
    assert result.root_label == "archive"
    contents = {s.path: s.data for s in result.sources}
    assert contents["left/same.txt"] == b"L"
    assert contents["right/same.txt"] == b"R"


def test_multiple_file_roots(tmp_path: Path) -> None:
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_bytes(b"A")
    b.write_bytes(b"B")
    result = _scan(a, b)
    assert _paths(result) == {"a.txt", "b.txt"}


# --------------------------------------------------------------------------
# 出错情形
# --------------------------------------------------------------------------


def test_missing_root_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="路径不存在"):
        _scan(tmp_path / "nope")


def test_no_roots_is_rejected() -> None:
    with pytest.raises(InputError, match="至少要给一个"):
        scan_paths([], profile=PROFILE)


def test_nested_binary_inside_text_named_file_is_skipped(tmp_path: Path) -> None:
    """名字叫 .txt 但内容是 PNG —— 内容说了算。"""
    root = tmp_path / "src"
    root.mkdir()
    (root / "fake.txt").write_bytes(b"\x89PNG\r\n\x1a\n" + os.urandom(200))
    result = _scan(root)
    assert _paths(result) == set()
    assert any("image/png" in i.reason for i in result.skipped)


def test_empty_tree_needs_exactly_one_code(tmp_path: Path) -> None:
    """回归：空目录只有一张清单码，不能凭空多算一个数据片。"""
    root = tmp_path / "src"
    root.mkdir()
    assert _scan(root).estimated_chunks(PROFILE) == 1


def test_more_data_means_more_codes(tmp_path: Path) -> None:
    root = tmp_path / "src"
    root.mkdir()
    (root / "small.txt").write_bytes(b"x" * 10)
    small = _scan(root).estimated_chunks(PROFILE)

    (root / "big.txt").write_bytes(b"y" * 5000)
    big = _scan(root).estimated_chunks(PROFILE)
    assert big > small
