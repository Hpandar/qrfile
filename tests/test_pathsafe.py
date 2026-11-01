"""路径安全：穿越拒绝、Windows 怪癖转义、大小写冲突、符号链接逃逸。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from qrfile.errors import InputError
from qrfile.pathsafe import (
    WINDOWS_RESERVED,
    check_output_isolated,
    collision_key,
    find_collisions,
    has_overlap,
    is_within,
    safe_join,
    sanitize_component,
    sanitize_relpath,
    to_long_path,
    validate_relpath,
)


def _can_symlink(tmp_path: Path) -> bool:
    """本机能不能创建符号链接（Windows 上需要开发者模式或管理员）。"""
    probe = tmp_path / "_probe_link"
    target = tmp_path / "_probe_target"
    target.mkdir()
    try:
        probe.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    finally:
        if probe.exists() or probe.is_symlink():
            try:
                probe.unlink()
            except OSError:
                pass
    return True


# --------------------------------------------------------------------------
# 穿越 —— 无论什么平台都必须拒绝
# --------------------------------------------------------------------------

TRAVERSAL = [
    "/etc/passwd",
    "\\Windows\\System32",
    "//server/share/file",
    "C:/Users/x/secret",
    "c:\\boot.ini",
    "../../etc/passwd",
    "a/../../b",
    "a/./b",
    "..",
    ".",
    "",
    "a//b",
    "a/b/",
    "x\x00y",
]


@pytest.mark.parametrize("bad", TRAVERSAL)
def test_traversal_rejected(bad: str) -> None:
    with pytest.raises(InputError):
        validate_relpath(bad)


@pytest.mark.parametrize(
    "good", ["a.txt", "a/b.txt", "深/目录/文件.md", "with space.txt", ".hidden", "a-b_c.d"]
)
def test_normal_paths_accepted(good: str) -> None:
    validate_relpath(good)


# --------------------------------------------------------------------------
# Windows 怪癖只在 Windows 上处理
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["CON", "con", "NUL.txt", "COM5.foo", "LPT9", "AUX"])
def test_windows_reserved_names_escaped(name: str) -> None:
    escaped, warning = sanitize_component(name, windows=True)
    assert warning is not None
    assert escaped != name
    assert escaped.startswith("_")
    # 转义之后不再是保留名
    assert escaped.split(".", 1)[0].rstrip(" ").upper() not in WINDOWS_RESERVED


@pytest.mark.parametrize("name", ["a<b", "a>b", 'a"b', "a|b", "a?b", "a*b"])
def test_windows_invalid_chars_escaped(name: str) -> None:
    escaped, warning = sanitize_component(name, windows=True)
    assert warning is not None
    assert not any(ch in escaped for ch in '<>:"|?*')


@pytest.mark.parametrize("name", ["trailing.", "trailing ", "trailing. "])
def test_windows_trailing_dot_space_stripped(name: str) -> None:
    escaped, warning = sanitize_component(name, windows=True)
    assert warning is not None
    assert not escaped.endswith((".", " "))


def test_legal_names_untouched_on_windows() -> None:
    """合法名字不该被无谓改动 —— 否则每次解码都会产生一个"新"目录名。"""
    for name in ["report.txt", "中文文件.md", "a b c.py", "v1.2.3"]:
        escaped, warning = sanitize_component(name, windows=True)
        assert escaped == name
        assert warning is None


def test_non_windows_keeps_quirky_names() -> None:
    """Linux 上 `CON` 和 `a<b` 都是合法名字，不该动它们。"""
    for name in ["CON", "a<b", "trailing."]:
        escaped, warning = sanitize_component(name, windows=False)
        assert escaped == name
        assert warning is None


def test_sanitize_relpath_reports_each_component() -> None:
    cleaned, warnings = sanitize_relpath("CON/a<b.txt", windows=True)
    assert warnings and len(warnings) == 2
    assert cleaned.startswith("_CON/")


def test_sanitize_relpath_still_rejects_traversal() -> None:
    """转义不能成为绕过穿越检查的后门。"""
    with pytest.raises(InputError):
        sanitize_relpath("../CON", windows=True)


def test_sanitize_component_rejects_separators() -> None:
    with pytest.raises(InputError):
        sanitize_component("a/b", windows=False)
    with pytest.raises(InputError):
        sanitize_component("", windows=False)


# --------------------------------------------------------------------------
# 大小写 / Unicode 冲突
# --------------------------------------------------------------------------


def test_collision_key_folds_case_and_normalization() -> None:
    assert collision_key("File.TXT") == collision_key("file.txt")
    # NFD 的 é 和 NFC 的 é 必须折叠到一起
    assert collision_key("cafe\u0301.txt") == collision_key("caf\u00e9.txt")


def test_find_collisions_detects_case_clash() -> None:
    found = find_collisions(["File.txt", "file.txt", "other.txt"])
    assert len(found) == 1
    key = next(iter(found))
    assert found[key] == ["File.txt", "file.txt"]


def test_find_collisions_empty_when_clean() -> None:
    assert find_collisions(["a.txt", "b.txt", "dir/c.txt"]) == {}


# --------------------------------------------------------------------------
# safe_join / 包含性判断
# --------------------------------------------------------------------------


def test_safe_join_basic(tmp_path: Path) -> None:
    joined = safe_join(str(tmp_path), "a/b/c.txt")
    assert Path(joined) == tmp_path / "a" / "b" / "c.txt"


def test_safe_join_rejects_traversal(tmp_path: Path) -> None:
    with pytest.raises(InputError):
        safe_join(str(tmp_path), "../escape.txt")


def test_safe_join_rejects_symlink_escape(tmp_path: Path) -> None:
    """中间某一段是符号链接且指向根目录之外 —— 必须拒绝（方案 0.3）。"""
    if not _can_symlink(tmp_path):
        pytest.skip("本机无法创建符号链接（Windows 需开发者模式）：该防护靠审计钩子兜底")

    out = tmp_path / "out"
    out.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (out / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(InputError, match="逃出"):
        safe_join(str(out), "link/pwned.txt")


def test_is_within_and_overlap(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    assert is_within(str(root), str(root))
    assert is_within(str(root), str(root / "sub"))
    assert not is_within(str(root / "sub"), str(root))
    assert not is_within(str(root), str(tmp_path / "other"))


def test_check_output_isolated_rejects_same_dir(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    with pytest.raises(InputError, match="重叠"):
        check_output_isolated(str(src), [str(src)])


def test_check_output_isolated_rejects_inside_source(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    with pytest.raises(InputError, match="重叠"):
        check_output_isolated(str(src / "qrout"), [str(src)])


def test_check_output_isolated_rejects_ancestor_of_source(tmp_path: Path) -> None:
    """输出指向源目录的父目录也要拒绝 —— 还原时树会正好落在源树上面。"""
    src = tmp_path / "src"
    src.mkdir()
    with pytest.raises(InputError, match="重叠"):
        check_output_isolated(str(tmp_path), [str(src)])


def test_check_output_isolated_allows_sibling(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    check_output_isolated(str(tmp_path / "qrout"), [str(src)])


def test_has_overlap_symmetry(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "a" / "b"
    assert has_overlap(str(a), str(b))
    assert has_overlap(str(b), str(a))


# --------------------------------------------------------------------------
# 长路径
# --------------------------------------------------------------------------


def test_to_long_path_noop_on_short_path(tmp_path: Path) -> None:
    short = str(tmp_path / "short.txt")
    assert to_long_path(short) == short


@pytest.mark.skipif(os.name != "nt", reason="只对 Windows 生效")
def test_to_long_path_prefixes_long_windows_path(tmp_path: Path) -> None:
    deep = tmp_path
    for i in range(12):
        deep = deep / ("a_rather_long_directory_name_%02d" % i)
    result = to_long_path(str(deep))
    assert result.startswith("\\\\?\\")
    assert ".." not in result
