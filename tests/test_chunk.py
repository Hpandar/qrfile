"""分片头的打包 / 解包，以及各种损坏场景下的报错。"""

from __future__ import annotations

import binascii

import pytest

from qrfile.chunk import (
    FLAG_COMPRESSED,
    FLAG_LAST,
    FLAG_MANIFEST,
    FORMAT_VERSION,
    HEADER_SIZE,
    MAGIC,
    MAX_TOTAL,
    ChunkHeader,
    new_session_id,
    pack_frame,
    unpack_frame,
)
from qrfile.errors import ChunkError

SESSION = b"\x01\x23\x45\x67\x89\xab\xcd\xef"


def test_roundtrip_preserves_body_exactly() -> None:
    """字节级往返 —— 包括全 256 种字节值。"""
    body = bytes(range(256)) * 3
    frame = pack_frame(session_id=SESSION, index=3, total=10, body=body)
    header, decoded = unpack_frame(frame)
    assert decoded == body
    assert header.session_id == SESSION
    assert header.index == 3
    assert header.total == 10
    assert header.body_len == len(body)
    assert header.crc32 == binascii.crc32(body)


def test_header_is_exactly_32_bytes() -> None:
    frame = pack_frame(session_id=SESSION, index=0, total=1, body=b"x")
    assert len(frame) == HEADER_SIZE + 1
    assert frame[:4] == MAGIC


def test_empty_body_roundtrips() -> None:
    """空载荷也要能编码 —— 空文件是合法输入。"""
    frame = pack_frame(session_id=SESSION, index=0, total=1, body=b"")
    header, body = unpack_frame(frame)
    assert body == b""
    assert header.body_len == 0


def test_last_chunk_gets_flag_automatically() -> None:
    last = pack_frame(session_id=SESSION, index=4, total=5, body=b"z")
    assert unpack_frame(last)[0].is_last
    not_last = pack_frame(session_id=SESSION, index=3, total=5, body=b"z")
    assert not unpack_frame(not_last)[0].is_last


def test_flags_survive() -> None:
    frame = pack_frame(
        session_id=SESSION, index=0, total=2, body=b"a", flags=FLAG_COMPRESSED | FLAG_MANIFEST
    )
    header, _ = unpack_frame(frame)
    assert header.compressed
    assert header.is_manifest
    assert not header.is_last


def test_trailing_bytes_are_tolerated_by_default() -> None:
    """CRC32 已经保证载荷完整，尾部多出来的字节不该导致解码失败。"""
    frame = pack_frame(session_id=SESSION, index=0, total=1, body=b"hello")
    header, body = unpack_frame(frame + b"\x00" * 16)
    assert body == b"hello"
    with pytest.raises(ChunkError):
        unpack_frame(frame + b"\x00" * 16, strict_length=True)


# --------------------------------------------------------------------------
# 损坏场景：每一种都必须报错，绝不能静默返回错数据
# --------------------------------------------------------------------------


def test_too_short() -> None:
    with pytest.raises(ChunkError, match="太短"):
        unpack_frame(b"QRFS")


def test_wrong_magic() -> None:
    frame = bytearray(pack_frame(session_id=SESSION, index=0, total=1, body=b"x"))
    frame[0:4] = b"XXXX"
    with pytest.raises(ChunkError, match="magic 不匹配"):
        unpack_frame(bytes(frame))


def test_wrong_version() -> None:
    frame = bytearray(pack_frame(session_id=SESSION, index=0, total=1, body=b"x"))
    frame[4] = FORMAT_VERSION + 1
    with pytest.raises(ChunkError, match="格式版本"):
        unpack_frame(bytes(frame))


def test_header_bit_flip_is_caught() -> None:
    """翻掉头部一个 bit（模拟二维码被涂改）—— CRC16 必须抓到。"""
    frame = bytearray(pack_frame(session_id=SESSION, index=7, total=10, body=b"payload"))
    frame[15] ^= 0x01  # index 字段里的一个 bit
    with pytest.raises(ChunkError, match="头 CRC16|索引非法|总数非法"):
        unpack_frame(bytes(frame))


def test_body_corruption_is_caught() -> None:
    frame = bytearray(pack_frame(session_id=SESSION, index=0, total=1, body=b"payload"))
    frame[-1] ^= 0xFF
    with pytest.raises(ChunkError, match="CRC32 校验失败"):
        unpack_frame(bytes(frame))


def test_truncated_body_is_caught() -> None:
    frame = pack_frame(session_id=SESSION, index=0, total=1, body=b"payload")
    with pytest.raises(ChunkError, match="载荷长度对不上"):
        unpack_frame(frame[:-3])


def test_index_out_of_range_is_rejected_on_pack() -> None:
    with pytest.raises(ChunkError):
        pack_frame(session_id=SESSION, index=5, total=5, body=b"x")
    with pytest.raises(ChunkError):
        pack_frame(session_id=SESSION, index=0, total=0, body=b"x")
    with pytest.raises(ChunkError):
        pack_frame(session_id=SESSION, index=0, total=MAX_TOTAL + 1, body=b"x")


def test_wrong_session_id_size_rejected() -> None:
    with pytest.raises(ValueError):
        pack_frame(session_id=b"short", index=0, total=1, body=b"x")


def test_session_id_is_random_and_8_bytes() -> None:
    a, b = new_session_id(), new_session_id()
    assert len(a) == 8
    assert a != b


def test_header_is_hashable_and_frozen() -> None:
    header = ChunkHeader(SESSION, 0, 1, 0, 3, 123)
    assert header.session_hex == SESSION.hex()
    assert hash(header) is not None
