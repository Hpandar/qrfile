"""分片头：固定 32 字节，自描述。

设计目标：解码端**只读这 32 字节**就知道这片属于哪个会话、是第几片、共几片、
载荷多长、有没有坏 —— 不依赖文件名、不依赖扫描顺序、不依赖任何旁挂文件。

布局（大端）::

    偏移  长度  字段
    0     4    magic b"QRFS"
    4     1    format_version
    5     1    flags           b0=已压缩  b1=清单片  b2=末片
    6     8    session_id      同一会话所有分片相同
    14    2    index           uint16 BE, 0-based
    16    2    total           uint16 BE
    18    4    body_len        uint32 BE
    22    4    crc32(body)     uint32 BE
    26    2    crc16(前 26 字节)
    28    4    reserved        当前忽略（留给未来，靠 format_version 区分）
"""

from __future__ import annotations

import binascii
import os
import struct
from dataclasses import dataclass

from .errors import ChunkError

MAGIC = b"QRFS"
#: 格式版本 2：分片头的 reserved[0] 携带压缩方法 id，支持
#: none/zlib/lzma/brotli/zstd/7z。版本 1 的码仍然能解（方法从 flags 推断）。
FORMAT_VERSION = 2
SUPPORTED_VERSIONS = (1, 2)
HEADER_SIZE = 32
SESSION_ID_SIZE = 8

FLAG_COMPRESSED = 0x01
FLAG_MANIFEST = 0x02
FLAG_LAST = 0x04
#: 与 FLAG_COMPRESSED 同时置位时表示用的是 lzma 而不是 zlib（仅版本 1 需要）。
#: 单独一个 bit 而不是改 FLAG_COMPRESSED 的含义 —— 这样老码仍然读得出来。
FLAG_LZMA = 0x08

# 压缩方法 id —— 版本 2 起写在 reserved[0]
METHOD_NONE = 0
METHOD_ZLIB = 1
METHOD_LZMA = 2
METHOD_BROTLI = 3
METHOD_ZSTD = 4
METHOD_7Z = 5

METHOD_NAMES: dict[int, str] = {
    METHOD_NONE: "none",
    METHOD_ZLIB: "zlib",
    METHOD_LZMA: "lzma",
    METHOD_BROTLI: "brotli",
    METHOD_ZSTD: "zstd",
    METHOD_7Z: "7z",
}
METHOD_IDS: dict[str, int] = {name: ident for ident, name in METHOD_NAMES.items()}

#: 索引是 uint16 ⇒ 单个会话最多 65535 片（含校验片）
MAX_TOTAL = 0xFFFF

_HEAD = struct.Struct(">4sBB8sHHII")  # 4+1+1+8+2+2+4+4 = 26
_CRC16 = struct.Struct(">H")
_RESERVED = struct.Struct(">4s")

assert _HEAD.size == 26, "分片头前段必须是 26 字节"


@dataclass(frozen=True, slots=True)
class ChunkHeader:
    session_id: bytes
    index: int
    total: int
    flags: int
    body_len: int
    crc32: int
    format_version: int = FORMAT_VERSION
    method: int = METHOD_NONE

    @property
    def compressed(self) -> bool:
        return bool(self.flags & FLAG_COMPRESSED) or self.method != METHOD_NONE

    @property
    def compression(self) -> str:
        """这一段用的是哪种压缩。

        版本 2 起以 ``reserved[0]`` 里的方法 id 为准；版本 1 的码没有这个字段，
        回落到 flags（zlib / lzma）。
        """
        if self.format_version >= 2 and self.method != METHOD_NONE:
            return METHOD_NAMES.get(self.method, "unknown")
        if self.flags & FLAG_COMPRESSED:
            return "lzma" if self.flags & FLAG_LZMA else "zlib"
        return "none"

    @property
    def is_manifest(self) -> bool:
        return bool(self.flags & FLAG_MANIFEST)

    @property
    def is_last(self) -> bool:
        return bool(self.flags & FLAG_LAST)

    @property
    def session_hex(self) -> str:
        return self.session_id.hex()


def new_session_id() -> bytes:
    """生成一个会话号。8 字节随机 —— 用来把混在一起的多个会话分开。"""
    return os.urandom(SESSION_ID_SIZE)


def pack_frame(
    *,
    session_id: bytes,
    index: int,
    total: int,
    body: bytes,
    flags: int = 0,
    method: int = METHOD_NONE,
) -> bytes:
    """把一个分片打包成最终写进二维码的字节串（头 32 字节 + 载荷）。

    ``method`` 是压缩方法 id（见 ``METHOD_*``）。版本 2 起它写在 reserved[0]，
    同时在 flags 里保留 COMPRESSED/LZMA 位，方便旧解码器至少能识别出"这码压过"。
    """
    if len(session_id) != SESSION_ID_SIZE:
        raise ValueError(f"session_id 必须是 {SESSION_ID_SIZE} 字节，收到 {len(session_id)}")
    if total < 1 or total > MAX_TOTAL:
        raise ChunkError(f"分片总数越界：total={total}（允许 1..{MAX_TOTAL}）")
    if not 0 <= index < total:
        raise ChunkError(f"分片索引越界：index={index} total={total}")
    if method not in METHOD_NAMES:
        raise ValueError(f"未知的压缩方法 id：{method}")

    if method != METHOD_NONE:
        flags |= FLAG_COMPRESSED
        if method == METHOD_LZMA:
            flags |= FLAG_LZMA
    if index == total - 1:
        flags |= FLAG_LAST

    head = _HEAD.pack(
        MAGIC, FORMAT_VERSION, flags, session_id, index, total, len(body), binascii.crc32(body)
    )
    tail = _CRC16.pack(binascii.crc_hqx(head, 0xFFFF)) + _RESERVED.pack(
        bytes([method]) + b"\0\0\0"
    )
    return head + tail + body


def unpack_frame(buf: bytes, *, strict_length: bool = False) -> tuple[ChunkHeader, bytes]:
    """拆开一个分片。

    载荷的完整性由 CRC32 保证，所以**尾部多出来的字节会被忽略**（某些编码器或
    传输链路可能补零）。``strict_length=True`` 时改为要求长度严丝合缝。
    """
    if len(buf) < HEADER_SIZE:
        raise ChunkError(f"数据太短：{len(buf)} 字节，连 {HEADER_SIZE} 字节的分片头都不够")

    head = buf[:26]
    magic, version, flags, session_id, index, total, body_len, body_crc = _HEAD.unpack(head)

    if magic != MAGIC:
        raise ChunkError(f"magic 不匹配（{magic!r}）—— 这不是 qrfile 生成的分片")
    if version not in SUPPORTED_VERSIONS:
        raise ChunkError(
            f"不支持的格式版本 {version}（本程序支持 "
            f"{'、'.join(str(v) for v in SUPPORTED_VERSIONS)}）"
        )

    stored_crc16 = _CRC16.unpack_from(buf, 26)[0]
    if binascii.crc_hqx(head, 0xFFFF) != stored_crc16:
        raise ChunkError("分片头 CRC16 校验失败 —— 头部已损坏")

    if total < 1 or total > MAX_TOTAL:
        raise ChunkError(f"分片总数非法：total={total}")
    if not 0 <= index < total:
        raise ChunkError(f"分片索引非法：index={index} total={total}")

    available = len(buf) - HEADER_SIZE
    if body_len > available:
        raise ChunkError(f"载荷长度对不上：头里声明 {body_len} 字节，实际只有 {available} 字节")
    if strict_length and body_len != available:
        raise ChunkError(f"载荷长度对不上：头里声明 {body_len} 字节，实际 {available} 字节")

    body = buf[HEADER_SIZE : HEADER_SIZE + body_len]
    actual_crc = binascii.crc32(body)
    if actual_crc != body_crc:
        raise ChunkError(
            f"分片 {index} 载荷 CRC32 校验失败（期望 {body_crc:08x}，实际 {actual_crc:08x}）"
        )

    method = METHOD_NONE
    if version >= 2:
        method = buf[28]
        if method not in METHOD_NAMES:
            raise ChunkError(f"未知的压缩方法 id {method} —— 这个码来自更新的版本")

    header = ChunkHeader(
        session_id=session_id,
        index=index,
        total=total,
        flags=flags,
        body_len=body_len,
        crc32=body_crc,
        format_version=version,
        method=method,
    )
    return header, body
