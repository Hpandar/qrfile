"""会话组装 / 拆解，以及 V2 / V3 两层验证和故障注入。

这里是"转码解码前后文件完全一致"这个硬需求的实现所在，所以失败路径的测试比成功
路径更重要 —— 每一种损坏都必须被**精确报出来**，而且**绝不返回半成品**。
"""

from __future__ import annotations

import hashlib
import os
import zlib

import pytest

from qrfile.archive import (
    FileSource,
    build_session,
    compress_segment,
    decompress_segment,
    manifest_for_sources,
    pack_blob,
    reassemble,
    split_segment,
)
from qrfile.chunk import FLAG_COMPRESSED, FLAG_MANIFEST, new_session_id, pack_frame, unpack_frame
from qrfile.errors import ChunkError, LimitExceeded, QrFileError, VerificationError
from qrfile.manifest import (
    COMPRESSION_AUTO,
    COMPRESSION_LZMA,
    COMPRESSION_NONE,
    COMPRESSION_ZLIB,
    verify_layout,
)

SESSION = b"\x11\x22\x33\x44\x55\x66\x77\x88"


def _sources(*specs: tuple[str, bytes]) -> list[FileSource]:
    return [FileSource(path=path, data=data, mode=0o644, mtime_ns=None) for path, data in specs]


def _session(sources, *, chunk_size: int = 300, compression: str = COMPRESSION_AUTO, session_id=SESSION):
    manifest, blob = manifest_for_sources(root="src", sources=sources, chunk_size=chunk_size)
    return build_session(
        manifest=manifest,
        blob=blob,
        session_id=session_id,
        chunk_size=chunk_size,
        compression=compression,
    )


def _payloads(plan) -> list[bytes]:
    """把分片帧走一遍真正的编解码往返，模拟"扫回来的码"。"""
    from qrfile.render import Renderer
    from qrfile.capacity import get_profile
    from qrfile.decode import decode_bytes

    renderer = Renderer(get_profile("screen"))
    payloads = []
    for frame in plan.frames:
        decoded = decode_bytes(renderer.render(frame))
        payloads.extend(d.payload for d in decoded)
    return payloads


# --------------------------------------------------------------------------
# 基本往返
# --------------------------------------------------------------------------


def test_roundtrip_single_small_file() -> None:
    sources = _sources(("a.txt", b"hello world\n"))
    plan = _session(sources)
    session, stats = reassemble(_payloads(plan))

    assert session.manifest.files[0].path == "a.txt"
    assert session.file_bytes(session.manifest.files[0]) == b"hello world\n"
    assert stats.foreign == 0 and stats.damaged == 0


def test_roundtrip_multiple_files_with_structure() -> None:
    sources = _sources(
        ("a.txt", b"aaa\n"),
        ("dir/b.txt", b"bbb\n"),
        ("dir/sub/c.txt", b"ccc\n"),
    )
    plan = _session(sources)
    session, _ = reassemble(_payloads(plan))

    recovered = {
        entry.path: session.file_bytes(entry) for entry in session.manifest.files
    }
    assert recovered == {"a.txt": b"aaa\n", "dir/b.txt": b"bbb\n", "dir/sub/c.txt": b"ccc\n"}


def test_roundtrip_spans_many_chunks() -> None:
    """数据大到需要多片 —— 顺序必须靠分片头还原，不能靠帧的顺序。"""
    sources = _sources(("big.txt", os.urandom(5000)))
    plan = _session(sources, chunk_size=300)
    assert plan.data_count > 10

    payloads = _payloads(plan)
    session, _ = reassemble(payloads)
    assert session.blob == sources[0].data


def test_chunk_order_does_not_matter() -> None:
    """把扫到的顺序打乱 —— 还原结果必须一样。"""
    sources = _sources(("big.txt", os.urandom(3000)))
    plan = _session(sources, chunk_size=300)
    payloads = _payloads(plan)

    shuffled = list(reversed(payloads))
    forward, _ = reassemble(payloads)
    backward, _ = reassemble(shuffled)
    assert forward.blob == backward.blob
    assert forward.manifest.archive_sha256 == backward.manifest.archive_sha256


def test_roundtrip_all_byte_values_across_chunks() -> None:
    data = bytes(range(256)) * 20
    sources = _sources(("data.bin", data))
    plan = _session(sources, chunk_size=256)
    session, _ = reassemble(_payloads(plan))
    assert session.blob == data


def test_roundtrip_empty_file() -> None:
    sources = _sources(("empty.txt", b""), ("full.txt", b"content"))
    plan = _session(sources)
    session, _ = reassemble(_payloads(plan))
    entries = {e.path: e for e in session.manifest.files}
    assert session.file_bytes(entries["empty.txt"]) == b""
    assert session.file_bytes(entries["full.txt"]) == b"content"


def test_empty_file_set_produces_no_data_chunks() -> None:
    """一个文件都没有 —— 只有清单段，数据段是零片。"""
    plan = _session([])
    assert plan.data_count == 0
    assert plan.manifest_count >= 1
    session, _ = reassemble(_payloads(plan))
    assert session.blob == b""
    assert session.manifest.files == ()


def test_roundtrip_preserves_crlf_and_bom() -> None:
    data = "﻿line1\r\nline2\r\n".encode("utf-8")
    sources = _sources(("f.txt", data))
    plan = _session(sources)
    session, _ = reassemble(_payloads(plan))
    assert session.file_bytes(session.manifest.files[0]) == data


# --------------------------------------------------------------------------
# 压缩
# --------------------------------------------------------------------------


def test_compression_shrinks_text() -> None:
    text = ("这是一段会重复很多次的中文文本。" * 200).encode()
    plan = _session(_sources(("t.txt", text)), chunk_size=500)
    plain = _session(_sources(("t.txt", text)), chunk_size=500, compression=COMPRESSION_NONE)
    assert plan.total < plain.total
    assert plan.manifest.compression in {COMPRESSION_ZLIB, COMPRESSION_LZMA}

    session, _ = reassemble(_payloads(plan))
    assert session.blob == text


def test_incompressible_data_is_left_alone() -> None:
    """随机数据压不动，不该硬压（压了反而变大）。"""
    packed, method = compress_segment(os.urandom(4096))
    assert method == COMPRESSION_NONE, "随机数据不该被「压缩」"
    assert packed is not None
    plan = _session(_sources(("r.bin", os.urandom(4096))), chunk_size=500)
    assert plan.manifest.compression == COMPRESSION_NONE


def test_compress_flag_off_is_respected() -> None:
    text = b"aaaaaaaaaa" * 500
    plan = _session(_sources(("t.txt", text)), chunk_size=500, compression=COMPRESSION_NONE)
    assert plan.manifest.compression == COMPRESSION_NONE
    session, _ = reassemble(_payloads(plan))
    assert session.blob == text


def test_embedded_manifest_agrees_with_actual_compression() -> None:
    """回归：清单里的 compression 字段不能跟实际行为自相矛盾。

    早期实现是先序列化清单、再改 compression 字段，导致码里带的清单说谎。
    """
    text = ("重复内容" * 300).encode()
    plan = _session(_sources(("t.txt", text)), chunk_size=500)

    # 从真正写进码里的字节把清单读回来
    header, body = unpack_frame(plan.manifest_frames[0])
    assert header.is_manifest
    assert header.compressed is True

    session, _ = reassemble(_payloads(plan))
    assert session.manifest.compression in {COMPRESSION_ZLIB, COMPRESSION_LZMA}


def test_compress_roundtrip_is_lossless_for_random_text() -> None:
    data = os.urandom(100).hex().encode() * 50
    plan = _session(_sources(("x.txt", data)), chunk_size=200)
    session, _ = reassemble(_payloads(plan))
    assert session.blob == data


def test_decompress_rejects_garbage() -> None:
    with pytest.raises(VerificationError, match="解压失败"):
        decompress_segment(b"not zlib data at all", COMPRESSION_ZLIB)


def test_split_segment_empty() -> None:
    assert split_segment(b"", 100) == []


# --------------------------------------------------------------------------
# V2：分片级完整性 —— 缺片必须精确报号
# --------------------------------------------------------------------------


def test_missing_chunk_reports_exact_indices() -> None:
    sources = _sources(("big.txt", os.urandom(3000)))
    plan = _session(sources, chunk_size=300)
    payloads = _payloads(plan)

    holed = [p for i, p in enumerate(payloads) if i not in (3, 7, 12)]
    with pytest.raises(ChunkError) as excinfo:
        reassemble(holed)

    message = str(excinfo.value)
    assert "缺少分片" in message
    assert "3" in message and "7" in message and "12" in message


def test_missing_chunk_fails_closed_no_partial_result() -> None:
    """缺片时必须抛异常，绝不能返回一个残缺的会话让调用方误以为成功。"""
    sources = _sources(("big.txt", os.urandom(3000)))
    plan = _session(sources, chunk_size=300)
    payloads = _payloads(plan)[:-1]

    with pytest.raises(ChunkError):
        reassemble(payloads)


def test_duplicate_chunk_with_same_content_is_tolerated() -> None:
    """同一张码被扫两次很常见 —— 内容一致就容忍。"""
    sources = _sources(("big.txt", os.urandom(1500)))
    plan = _session(sources, chunk_size=300)
    payloads = _payloads(plan)

    session, stats = reassemble(payloads + [payloads[2], payloads[5]])
    assert stats.duplicates == 2
    assert session.blob == sources[0].data


def test_duplicate_chunk_with_different_content_is_rejected() -> None:
    """同一个 index 出现两个不同内容 —— 有一张码被改过，必须报错。"""
    sources = _sources(("big.txt", os.urandom(1500)))
    plan = _session(sources, chunk_size=300)
    payloads = _payloads(plan)

    header, body = unpack_frame(payloads[2])
    forged = pack_frame(
        session_id=SESSION, index=header.index, total=header.total, body=b"X" * len(body)
    )
    with pytest.raises(ChunkError, match="内容不一致"):
        reassemble(payloads + [forged])


def test_damaged_chunk_is_counted_not_silently_ignored() -> None:
    """CRC 坏掉的片要被记数，且不能顶替掉一张好片。"""
    sources = _sources(("f.txt", b"x" * 500))
    plan = _session(sources, chunk_size=200)
    payloads = _payloads(plan)

    broken = bytearray(payloads[0])
    broken[-1] ^= 0xFF  # 改坏载荷，CRC32 必然对不上

    # 好片和坏片同时在场：坏的那张被丢弃并记数，好的那张照常还原
    session, stats = reassemble(payloads + [bytes(broken)])
    assert stats.damaged == 1
    assert stats.accepted == len(payloads)
    assert session.blob == sources[0].data


def test_damaged_chunk_alone_means_missing_chunk() -> None:
    """只有坏片、没有好片 —— 那一格就是缺片，必须报出来而不是当作没看见。"""
    sources = _sources(("f.txt", b"x" * 500))
    plan = _session(sources, chunk_size=200)
    payloads = _payloads(plan)

    broken = bytearray(payloads[0])
    broken[-1] ^= 0xFF
    with pytest.raises(ChunkError, match="缺少分片"):
        reassemble([bytes(broken)] + payloads[1:])


def test_foreign_qr_payloads_are_counted_and_ignored() -> None:
    """照片里混进别的二维码很正常 —— 忽略它，但要如实报数。"""
    sources = _sources(("f.txt", b"hello"))
    plan = _session(sources)
    payloads = _payloads(plan)

    session, stats = reassemble(payloads + [b"https://example.com", b"WIFI:S:ssid;T:WPA;"])
    assert stats.foreign == 2
    assert session.blob == b"hello"


def test_only_foreign_payloads_gives_a_clear_error() -> None:
    with pytest.raises(ChunkError, match="没有找到任何 qrfile 分片"):
        reassemble([b"https://example.com", b"not ours"])


# --------------------------------------------------------------------------
# 多会话
# --------------------------------------------------------------------------


def test_multiple_sessions_require_explicit_choice() -> None:
    a = _session(_sources(("a.txt", b"aaa")), session_id=b"\x01" * 8)
    b = _session(_sources(("b.txt", b"bbb")), session_id=b"\x02" * 8)
    mixed = _payloads(a) + _payloads(b)

    with pytest.raises(ChunkError, match="2 个不同会话"):
        reassemble(mixed)


def test_session_can_be_selected_explicitly() -> None:
    a = _session(_sources(("a.txt", b"aaa")), session_id=b"\x01" * 8)
    b = _session(_sources(("b.txt", b"bbb")), session_id=b"\x02" * 8)
    mixed = _payloads(a) + _payloads(b)

    session_a, _ = reassemble(mixed, session_id=b"\x01" * 8)
    session_b, _ = reassemble(mixed, session_id=b"\x02" * 8)
    assert session_a.blob == b"aaa"
    assert session_b.blob == b"bbb"


def test_unknown_session_id_is_rejected() -> None:
    plan = _session(_sources(("a.txt", b"aaa")))
    with pytest.raises(ChunkError, match="不在这批码里"):
        reassemble(_payloads(plan), session_id=b"\xff" * 8)


def test_inconsistent_total_within_one_session_is_rejected() -> None:
    sources = _sources(("f.txt", b"x" * 400))
    plan = _session(sources, chunk_size=200)
    payloads = _payloads(plan)

    forged = pack_frame(session_id=SESSION, index=99, total=9999, body=b"y")
    with pytest.raises(ChunkError, match="分片总数不一致"):
        reassemble(payloads + [forged])


# --------------------------------------------------------------------------
# V3：还原级校验
# --------------------------------------------------------------------------


def test_verify_all_passes_on_clean_session() -> None:
    plan = _session(_sources(("a.txt", b"hello"), ("b.txt", os.urandom(500))))
    session, _ = reassemble(_payloads(plan))
    session.verify_all()  # 不该抛


def test_verify_all_catches_tampered_file_content() -> None:
    """内容被改了 —— 逐文件 SHA-256 必须抓到。"""
    sources = _sources(("a.txt", b"original content"))
    manifest, blob = manifest_for_sources(root="src", sources=sources, chunk_size=400)
    plan = build_session(manifest=manifest, blob=blob, session_id=SESSION, chunk_size=400)

    session, _ = reassemble(_payloads(plan), verify=False)
    tampered = type(session)(
        session_id=session.session_id, manifest=session.manifest, blob=b"tampered content!!"
    )
    problems = tampered.verify_files()
    assert problems and "SHA-256 不符" in problems[0]
    with pytest.raises(VerificationError, match="校验不通过"):
        tampered.verify_all()


def test_verify_all_catches_removed_file_entry() -> None:
    """清单里偷偷删掉一个文件条目，**并且把 blob_size 也改成一致的**。

    只删条目会被 verify_layout 先抓到（blob_size 对不上）。但攻击者当然会顺手把
    blob_size 也改了 —— 那时布局和逐文件哈希都过得去，**只有 archive_sha256
    能抓到**。这才是这层校验真正的价值，所以要测这个版本。
    """
    sources = _sources(("a.txt", b"aaa"), ("b.txt", b"bbb"))
    manifest, blob = manifest_for_sources(root="src", sources=sources, chunk_size=400)
    plan = build_session(manifest=manifest, blob=blob, session_id=SESSION, chunk_size=400)

    session, _ = reassemble(_payloads(plan), verify=False)
    from dataclasses import replace

    kept = session.manifest.files[:1]
    shrunk = replace(
        session.manifest,
        files=kept,
        blob_size=sum(e.size for e in kept),  # 顺手把 blob_size 也凑一致
    )
    tampered = type(session)(
        session_id=session.session_id, manifest=shrunk, blob=session.blob
    )

    # 布局和逐文件哈希都过得去……
    assert tampered.verify_files() == []
    # ……但集合级哈希把它揪出来了
    with pytest.raises(VerificationError, match="archive_sha256"):
        tampered.verify_all()


def test_verify_default_is_on_and_fails_closed() -> None:
    """默认就该校验。篡改过的载荷必须让 reassemble 直接抛异常。"""
    sources = _sources(("a.txt", b"a" * 600))
    plan = _session(sources, chunk_size=200)
    payloads = _payloads(plan)

    # 把数据段的片拼起来，改一个字节，再重新打包成合法分片
    header, body = unpack_frame(payloads[-1])
    mutated = bytearray(body)
    mutated[0] ^= 0xFF
    forged = pack_frame(
        session_id=SESSION,
        index=header.index,
        total=header.total,
        body=bytes(mutated),
        flags=header.flags,
    )
    with pytest.raises(QrFileError):
        reassemble(payloads[:-1] + [forged])


# --------------------------------------------------------------------------
# 上限
# --------------------------------------------------------------------------


def test_too_many_chunks_is_rejected_with_actionable_advice() -> None:
    sources = _sources(("huge.txt", b"x" * 100000))
    manifest, blob = manifest_for_sources(root="src", sources=sources, chunk_size=1)
    # 必须关掉压缩 —— 全 "x" 的内容压完只剩一百多字节，就触不到分片上限了
    with pytest.raises(LimitExceeded, match="profile|子目录"):
        build_session(
            manifest=manifest, blob=blob, session_id=SESSION, chunk_size=1, compression=COMPRESSION_NONE
        )


def test_zero_chunk_size_is_rejected() -> None:
    manifest, blob = manifest_for_sources(root="src", sources=[], chunk_size=100)
    with pytest.raises(LimitExceeded):
        build_session(manifest=manifest, blob=blob, session_id=SESSION, chunk_size=0)


# --------------------------------------------------------------------------
# pack_blob
# --------------------------------------------------------------------------


def test_pack_blob_assigns_contiguous_offsets() -> None:
    entries, blob = pack_blob(_sources(("a", b"12345"), ("b", b"678"), ("c", b"")))
    assert [e.offset for e in entries] == [0, 5, 8]  # 空文件不占偏移
    assert [e.size for e in entries] == [5, 3, 0]
    assert blob == b"12345678"


def test_offsets_follow_manifest_sorted_order() -> None:
    """回归测试：BLOB 偏移必须跟清单里（按 path_b64 排序）的文件顺序一致。

    真实踩过的坑 —— 对一个含 big.txt / bom.txt 的目录编码后解码，报
    "清单偏移不自洽：'bom.txt' 的 offset 是 4079，按前面的文件推算应该是 26"。
    原因是 pack_blob 按原始路径顺序拼 BLOB，而 build_manifest 按 base64 排序：
    ``big.txt`` 的原始路径在前，但 base64 后 ``Ym9t``(bom) < ``Ymln``(big)
    （``'9' < 'l'``），两处顺序不一致，偏移量整体错位。

    这个 bug 是被 verify_layout 这层校验当场抓住的 —— 一个字节都没写错盘。
    """
    names = ["big.txt", "bom.txt", "CON.txt", "a.txt", "Z.txt", "中文.txt"]
    sources = _sources(*[(n, f"content-of-{n}".encode()) for n in names])

    # 先证明这组名字确实能让两种排序产生分歧，否则这个测试是空的
    from qrfile.manifest import encode_path_b64

    raw_order = [s.path for s in sources]
    b64_order = [s.path for s in sorted(sources, key=lambda s: encode_path_b64(s.path))]
    assert raw_order != b64_order, "这组名字没造出排序分歧，测试失去意义"

    manifest, blob = manifest_for_sources(root="src", sources=sources, chunk_size=200)
    verify_layout(manifest)  # 不该抛

    # 而且每个条目的偏移必须正好指向它自己的内容
    for entry in manifest.files:
        data = blob[entry.offset : entry.offset + entry.size]
        assert data == f"content-of-{entry.path}".encode(), f"{entry.path} 的偏移指错了地方"


def test_pack_blob_hashes_match_content() -> None:
    entries, _ = pack_blob(_sources(("a", b"hello")))
    assert entries[0].sha256 == hashlib.sha256(b"hello").hexdigest()


def test_manifest_for_sources_records_dirs_symlinks_skipped() -> None:
    manifest, _ = manifest_for_sources(
        root="src",
        sources=_sources(("a.txt", b"x")),
        dirs=["empty_dir", "another"],
        symlinks=[("link", "a.txt")],
        skipped=[("p.png", "binary:image/png")],
        chunk_size=400,
    )
    assert [d.path for d in manifest.dirs] == ["another", "empty_dir"]
    assert manifest.symlinks[0].target == "a.txt"
    assert manifest.skipped[0].reason == "binary:image/png"


# --------------------------------------------------------------------------
# 压缩方式（zlib / lzma / auto / none）—— lzma 是为了减少张数加的
# --------------------------------------------------------------------------


def test_auto_picks_the_smaller_compressor() -> None:
    """auto 必须真的取最小的那个，而不是固定用某一种。

    这一条直接决定要出多少张码 —— 用你这个 1.5MB 的 JSON 实测，
    zlib 压到 20.8%、lzma 压到 16.9%，差别是 107 张 vs 87 张。
    """
    text = ("这是会重复很多次的中文内容，用来让 lzma 发挥优势。" * 400).encode()
    auto_data, auto_method = compress_segment(text, COMPRESSION_AUTO)
    zlib_data, _ = compress_segment(text, COMPRESSION_ZLIB)
    lzma_data, _ = compress_segment(text, COMPRESSION_LZMA)

    assert auto_method in {COMPRESSION_ZLIB, COMPRESSION_LZMA}
    assert len(auto_data) == min(len(zlib_data), len(lzma_data))


def test_lzma_roundtrips() -> None:
    sources = _sources(("t.txt", ("中文内容" * 500).encode()))
    plan = _session(sources, chunk_size=500, compression=COMPRESSION_LZMA)
    session, _ = reassemble(_payloads(plan))
    assert session.blob == sources[0].data


def test_lzma_flag_is_set_and_read_back() -> None:
    """lzma 靠分片头里的一个独立 bit 标识 —— 老码（该位为 0）仍然读得出来。"""
    from qrfile.chunk import FLAG_COMPRESSED, FLAG_LZMA

    sources = _sources(("t.txt", ("重复" * 800).encode()))
    plan = _session(sources, chunk_size=500, compression=COMPRESSION_LZMA)
    header, _ = unpack_frame(plan.data_frames[0])
    assert header.compressed
    assert header.flags & FLAG_LZMA
    assert header.compression == COMPRESSION_LZMA

    zlib_plan = _session(sources, chunk_size=500, compression=COMPRESSION_ZLIB)
    zlib_header, _ = unpack_frame(zlib_plan.data_frames[0])
    assert zlib_header.flags & FLAG_COMPRESSED
    assert not (zlib_header.flags & FLAG_LZMA)
    assert zlib_header.compression == COMPRESSION_ZLIB


def test_uncompressed_header_reports_none() -> None:
    sources = _sources(("t.txt", b"x" * 900))
    plan = _session(sources, chunk_size=300, compression=COMPRESSION_NONE)
    header, _ = unpack_frame(plan.data_frames[0])
    assert not header.compressed
    assert header.compression == COMPRESSION_NONE


def test_unknown_compression_method_is_rejected() -> None:
    from qrfile.errors import InputError

    with pytest.raises(InputError, match="不认识的压缩方式"):
        # brotli 现在是受支持的方法（本机可能没装，但名字是合法的），
        # 所以这里用一个确定不存在的名字。
        compress_segment(b"x" * 500, "lz4-does-not-exist")


def test_lzma_corrupted_data_fails_closed() -> None:
    with pytest.raises(VerificationError, match="解压失败"):
        decompress_segment(b"definitely not lzma data", COMPRESSION_LZMA)


def test_auto_never_produces_more_codes_than_zlib_alone() -> None:
    """auto 的承诺：张数不会比只用 zlib 更多。这是它存在的全部理由。"""
    text = ("路径/重复/中文混合内容" * 300).encode()
    auto_plan = _session(_sources(("t.txt", text)), chunk_size=400, compression=COMPRESSION_AUTO)
    zlib_plan = _session(_sources(("t.txt", text)), chunk_size=400, compression=COMPRESSION_ZLIB)
    assert auto_plan.total <= zlib_plan.total
