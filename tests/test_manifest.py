"""清单：确定性、path_b64 权威性、archive_sha256 的语义。"""

from __future__ import annotations

import json
import os

import pytest

from qrfile.errors import VerificationError
from qrfile.manifest import (
    COMPRESSION_ZLIB,
    FORMAT,
    DirEntry,
    FileEntry,
    Manifest,
    SkippedEntry,
    SymlinkEntry,
    build_manifest,
    compute_archive_sha256,
    decode_path_b64,
    display_path,
    encode_path_b64,
    verify_archive_sha256,
    verify_layout,
)

CREATED = "2026-10-10T12:00:00Z"


def _file(path: str, *, size: int = 10, offset: int = 0, sha: str = "aa" * 32) -> FileEntry:
    return FileEntry.create(path, size=size, offset=offset, sha256=sha, mode=0o644, mtime_ns=None)


def _manifest(**kwargs) -> Manifest:
    kwargs.setdefault("root", "src")
    kwargs.setdefault("chunk_size", 826)
    kwargs.setdefault("compression", COMPRESSION_ZLIB)
    kwargs.setdefault("created", CREATED)
    return build_manifest(**kwargs)


# --------------------------------------------------------------------------
# 路径的两种表示
# --------------------------------------------------------------------------


def test_path_b64_roundtrip_ascii() -> None:
    assert decode_path_b64(encode_path_b64("a/b/c.txt")) == "a/b/c.txt"


def test_path_b64_roundtrip_chinese_and_emoji() -> None:
    for path in ["文档/说明.md", "📁/🎉.txt", "с пробелом.txt", "日本語/ファイル.txt"]:
        assert decode_path_b64(encode_path_b64(path)) == path


#: 带代理对的路径。用字面量构造而不是 os.fsdecode(b"...")，因为两个平台的
#: os.fsdecode 语义不同（POSIX 是 surrogateescape，Windows 是 surrogatepass 且
#: 会直接抛错）—— 详见 manifest.encode_path_b64 的文档。
MESSY_PATH = "caf\udce9.txt"


def test_path_b64_roundtrip_non_utf8_bytes() -> None:
    """带代理对的路径在**任何平台**都必须无损往返（清单自洽的前提）。"""
    assert decode_path_b64(encode_path_b64(MESSY_PATH)) == MESSY_PATH


@pytest.mark.skipif(os.name == "nt", reason="Windows 的 fsencode 是 surrogatepass，不做原始字节还原")
def test_path_b64_recovers_raw_bytes_on_posix() -> None:
    """POSIX 上更强的保证：真的还原出文件系统上的原始字节。"""
    assert os.fsencode(decode_path_b64(encode_path_b64(MESSY_PATH))) == b"caf\xe9.txt"


def test_display_path_strips_surrogates_so_json_works() -> None:
    """代理对不是合法 JSON 字符 —— 显示路径必须清理掉，否则序列化会炸。"""
    cleaned = display_path(MESSY_PATH)
    json.dumps(cleaned)  # 不该抛
    assert "\udce9" not in cleaned

    entry = FileEntry.create(
        MESSY_PATH, size=1, offset=0, sha256="00" * 32, mode=0o644, mtime_ns=None
    )
    entry.to_json()  # 不该抛
    assert entry.resolve_path() == MESSY_PATH  # 权威路径仍然无损


def test_display_path_normalizes_to_nfc() -> None:
    """macOS 存 NFD，Windows/Linux 用 NFC —— 显示路径统一成 NFC。"""
    nfd = "café.txt"
    assert display_path(nfd) == "café.txt"


# --------------------------------------------------------------------------
# 确定性 —— 这是 archive_sha256 有意义的前提
# --------------------------------------------------------------------------


def test_same_input_produces_byte_identical_manifest() -> None:
    files = [_file("b.txt", sha="bb" * 32), _file("a.txt", sha="aa" * 32)]
    first = _manifest(files=files).to_bytes()
    second = _manifest(files=files).to_bytes()
    assert first == second


def test_input_order_does_not_matter() -> None:
    """传进来的顺序不同，清单字节必须相同。"""
    a = _file("a.txt", sha="aa" * 32)
    b = _file("b.txt", sha="bb" * 32)
    c = _file("c.txt", sha="cc" * 32)
    assert _manifest(files=[a, b, c]).to_bytes() == _manifest(files=[c, a, b]).to_bytes()


def test_dirs_symlinks_skipped_also_sorted() -> None:
    d1, d2 = DirEntry.create("z"), DirEntry.create("a")
    s1, s2 = SymlinkEntry.create("z", "t"), SymlinkEntry.create("a", "t")
    k1, k2 = SkippedEntry.create("z.png", "r1"), SkippedEntry.create("a.png", "r2")
    assert _manifest(dirs=[d1, d2], symlinks=[s1, s2], skipped=[k1, k2]).to_bytes() == \
        _manifest(dirs=[d2, d1], symlinks=[s2, s1], skipped=[k2, k1]).to_bytes()


def test_manifest_json_uses_compact_separators() -> None:
    data = _manifest(files=[_file("a.txt")]).to_bytes()
    assert b", " not in data and b": " not in data
    assert b'"format":"QRFS/1"' in data


def test_manifest_bytes_are_utf8_decodable_and_parseable() -> None:
    manifest = _manifest(files=[_file("中文/文件.txt")])
    parsed = json.loads(manifest.to_bytes().decode("utf-8"))
    assert parsed["format"] == FORMAT
    assert parsed["files"][0]["path"] == "中文/文件.txt"


# --------------------------------------------------------------------------
# archive_sha256 的语义
# --------------------------------------------------------------------------


def test_archive_hash_order_independent() -> None:
    a = _file("a.txt", sha="aa" * 32)
    b = _file("b.txt", sha="bb" * 32)
    assert compute_archive_sha256([a, b]) == compute_archive_sha256([b, a])


def test_archive_hash_changes_when_content_changes() -> None:
    before = compute_archive_sha256([_file("a.txt", sha="aa" * 32)])
    after = compute_archive_sha256([_file("a.txt", sha="bb" * 32)])
    assert before != after


def test_archive_hash_changes_when_path_changes() -> None:
    assert compute_archive_sha256([_file("a.txt")]) != compute_archive_sha256([_file("b.txt")])


def test_archive_hash_changes_when_size_changes() -> None:
    assert compute_archive_sha256([_file("a.txt", size=1)]) != \
        compute_archive_sha256([_file("a.txt", size=2)])


def test_archive_hash_detects_added_and_removed_files() -> None:
    one = compute_archive_sha256([_file("a.txt")])
    two = compute_archive_sha256([_file("a.txt"), _file("b.txt")])
    assert one != two


def test_archive_hash_is_ambiguous_concat_resistant() -> None:
    """长度前缀必须防住拼接歧义：一条记录 "ab" 不等于两条 "a" + "b"。"""
    one = compute_archive_sha256([_file("a"), _file("b")])
    two = compute_archive_sha256([_file("ab")])
    assert one != two


def test_archive_hash_covers_empty_dirs() -> None:
    """空目录也必须进哈希 —— 否则目录结构可以被悄悄改掉。"""
    files = [_file("a.txt")]
    without = compute_archive_sha256(files)
    with_dir = compute_archive_sha256(files, dirs=[DirEntry.create("empty")])
    assert without != with_dir


def test_archive_hash_covers_symlink_targets() -> None:
    link = SymlinkEntry.create("link", "target-a")
    other = SymlinkEntry.create("link", "target-b")
    assert compute_archive_sha256([], symlinks=[link]) != compute_archive_sha256([], symlinks=[other])


def test_archive_hash_ignores_transport_metadata() -> None:
    """同样内容换个分片大小 / 压缩方式重新编码，身份理应不变。"""
    files = [_file("a.txt")]
    base = _manifest(files=files, chunk_size=826, compression=COMPRESSION_ZLIB)
    other = _manifest(files=files, chunk_size=260, compression="none")
    assert base.archive_sha256 == other.archive_sha256
    assert base.to_bytes() != other.to_bytes()  # 清单字节本身当然不同


def test_archive_hash_ignores_created_timestamp() -> None:
    files = [_file("a.txt")]
    a = _manifest(files=files, created="2026-01-01T00:00:00Z")
    b = _manifest(files=files, created="2026-12-31T23:59:59Z")
    assert a.archive_sha256 == b.archive_sha256


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------


def test_verify_archive_sha256_passes_on_clean_manifest() -> None:
    verify_archive_sha256(_manifest(files=[_file("a.txt"), _file("b.txt")]))


def test_verify_archive_sha256_catches_removed_entry() -> None:
    """从清单里偷偷删掉一个文件条目 —— 逐文件 SHA-256 抓不到，这一层要抓到。"""
    manifest = _manifest(files=[_file("a.txt"), _file("b.txt")])
    tampered = Manifest(
        root=manifest.root,
        chunk_size=manifest.chunk_size,
        compression=manifest.compression,
        files=manifest.files[:1],  # 删掉一个
        created=manifest.created,
        blob_size=manifest.blob_size,
        archive_sha256=manifest.archive_sha256,
    )
    with pytest.raises(VerificationError, match="archive_sha256"):
        verify_archive_sha256(tampered)


def test_verify_archive_sha256_catches_path_swap() -> None:
    manifest = _manifest(files=[_file("a.txt", sha="aa" * 32), _file("b.txt", sha="bb" * 32)])
    swapped = Manifest(
        root=manifest.root,
        chunk_size=manifest.chunk_size,
        compression=manifest.compression,
        files=tuple(
            FileEntry.create(
                ["b.txt", "a.txt"][i],
                size=e.size,
                offset=e.offset,
                sha256=e.sha256,
                mode=e.mode,
                mtime_ns=None,
            )
            for i, e in enumerate(manifest.files)
        ),
        created=manifest.created,
        blob_size=manifest.blob_size,
        archive_sha256=manifest.archive_sha256,
    )
    with pytest.raises(VerificationError):
        verify_archive_sha256(swapped)


def test_verify_layout_passes_when_offsets_are_contiguous() -> None:
    verify_layout(
        _manifest(
            files=[
                _file("a.txt", size=10, offset=0),
                _file("b.txt", size=20, offset=10),
                _file("c.txt", size=5, offset=30),
            ]
        )
    )


def test_verify_layout_catches_offset_gap() -> None:
    manifest = _manifest(files=[_file("a.txt", size=10, offset=0), _file("b.txt", size=20, offset=99)])
    with pytest.raises(VerificationError, match="偏移不自洽"):
        verify_layout(manifest)


def test_verify_layout_catches_blob_size_mismatch() -> None:
    manifest = build_manifest(
        root="src",
        chunk_size=826,
        compression=COMPRESSION_ZLIB,
        files=[_file("a.txt", size=10, offset=0)],
        created=CREATED,
        blob_size=9999,
    )
    with pytest.raises(VerificationError, match="blob_size 不自洽"):
        verify_layout(manifest)


# --------------------------------------------------------------------------
# 序列化往返
# --------------------------------------------------------------------------


def test_roundtrip_through_bytes() -> None:
    original = _manifest(
        files=[_file("a.txt", sha="aa" * 32), _file("中文/文档.md", size=7, offset=10, sha="dd" * 32)],
        dirs=[DirEntry.create("空目录")],
        symlinks=[SymlinkEntry.create("link", "a.txt")],
        skipped=[SkippedEntry.create("x.png", "binary:image/png")],
    )
    restored = Manifest.from_bytes(original.to_bytes())
    assert restored.to_bytes() == original.to_bytes()
    assert restored.archive_sha256 == original.archive_sha256
    assert restored.files[0].resolve_path() == original.files[0].resolve_path()
    assert restored.skipped[0].reason == "binary:image/png"


def test_from_bytes_rejects_wrong_format() -> None:
    with pytest.raises(VerificationError, match="清单格式不对"):
        Manifest.from_bytes(b'{"format":"SOMETHING/9"}')


def test_from_bytes_rejects_garbage() -> None:
    with pytest.raises(VerificationError):
        Manifest.from_bytes(b"not json at all")


def test_from_bytes_rejects_non_object() -> None:
    with pytest.raises(VerificationError, match="JSON 对象"):
        Manifest.from_bytes(b"[1,2,3]")


def test_blob_size_defaults_to_sum_of_file_sizes() -> None:
    manifest = _manifest(files=[_file("a.txt", size=10, offset=0), _file("b.txt", size=5, offset=10)])
    assert manifest.blob_size == 15
    assert manifest.total_file_bytes == 15
