"""会话的组装与拆解 —— 容器格式 ``QRFS/1`` 的实现。

一个会话的载荷分**两段**，各自切片：

    段 1（清单段）：manifest JSON              → 切成 M 片，标 FLAG_MANIFEST
    段 2（数据段）：各文件原始字节按清单顺序拼接 → 切成 N 片

分片头的 ``index`` 覆盖整个序列（``0..M+N-1``），``total = M+N``。

**为什么跟方案里写的"拼成一个流"不一样**：分开之后清单是自定界的（它本来就是
JSON），解码端可以**先只解清单**，在任何数据片到位之前就知道该有哪些文件、各自
多大、内容哈希多少、总共该有多少片。于是能立刻报出精确的"缺第 3、7、12 片"，
而不是等拼完才发现对不上。代价是两段各自的 zlib 头（几十字节），而清单通常只有
几 KB —— 换来的可诊断性远超这点压缩损失。

压缩是**整个会话一个决定**（由数据段压不压得动决定），清单段的 zlib 标志也一
致。解码时以每片自己的 ``FLAG_COMPRESSED`` 为准，清单里的 ``compression`` 字段
只是参考。
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, replace
from typing import Sequence

from . import compressors as _compressors
from .chunk import (
    FLAG_COMPRESSED,
    FLAG_LZMA,
    FLAG_MANIFEST,
    MAGIC,
    MAX_TOTAL,
    METHOD_IDS,
    METHOD_NONE,
    pack_frame,
    unpack_frame,
)
from .errors import ChunkError, InputError, LimitExceeded, VerificationError
from .manifest import (
    COMPRESSION_AUTO,
    COMPRESSION_LZMA,
    COMPRESSION_MAX,
    COMPRESSION_NONE,
    COMPRESSION_ZLIB,
    DirEntry,
    FileEntry,
    Manifest,
    SkippedEntry,
    SymlinkEntry,
    build_manifest,
    encode_path_b64,
    verify_archive_sha256,
    verify_layout,
)

#: 小于这个体积就不值得压（zlib 头本身就要几十字节）
MIN_COMPRESS_BYTES = 128


# --------------------------------------------------------------------------
# 压缩
# --------------------------------------------------------------------------


def compress_segment(data: bytes, method: str = COMPRESSION_AUTO) -> tuple[bytes, str]:
    """压缩一段字节。返回 ``(结果, 实际用的方法)``。

    压不动的数据（已经压过的、随机的）会原样返回并报 ``none`` —— 压了反而变大
    就没有意义，而每张码的容量是硬约束，能不浪费就不浪费。

    * ``auto``（默认）：只跑 Python 标库（zlib / lzma），**解码端不需要额外软件**。
    * ``max``：额外尝试本机的 brotli / zstd / 7z，比率更好，但解码端得有同一个工具。
    * 也可以显式指定某个方法名。
    """
    if method == COMPRESSION_NONE or len(data) < MIN_COMPRESS_BYTES:
        return data, COMPRESSION_NONE

    if method == COMPRESSION_AUTO:
        return _compressors.compress_best(data, include_external=False)
    if method == COMPRESSION_MAX:
        return _compressors.compress_best(data, include_external=True)

    known = set(_compressors._compressors_by_name(include_external=True))
    if method not in known:
        raise InputError(
            f"不认识的压缩方式 {method!r}，可选：{COMPRESSION_AUTO}、{COMPRESSION_MAX}、"
            f"{COMPRESSION_ZLIB}、{COMPRESSION_LZMA}、{COMPRESSION_NONE}，"
            f"或本机可用的外部工具（{'、'.join(sorted(known))}）"
        )
    return _compressors.compress_one(data, method), method


def decompress_segment(data: bytes, method: str) -> bytes:
    return _compressors.decompress_one(data, method)


# --------------------------------------------------------------------------
# 切片
# --------------------------------------------------------------------------


def split_segment(segment: bytes, chunk_size: int) -> list[bytes]:
    """把一段字节切成若干片。空段得到**零片**（数据段可以空，清单段不会）。"""
    if chunk_size < 1:
        raise ValueError(f"chunk_size 必须为正，收到 {chunk_size}")
    return [segment[i : i + chunk_size] for i in range(0, len(segment), chunk_size)]


# --------------------------------------------------------------------------
# 源文件与 BLOB
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FileSource:
    """一个待编码的文件。``data`` 是**原始字节**，不做任何转换。"""

    path: str  # 相对源根的路径，用 / 分隔
    data: bytes
    mode: int = 0o644
    mtime_ns: int | None = None


def pack_blob(sources: Sequence[FileSource]) -> tuple[tuple[FileEntry, ...], bytes]:
    """把各文件拼成 BLOB，同时算出每个文件的偏移和 SHA-256。

    **按 ``path_b64`` 排序后再拼** —— 这个排序不是可选的：清单里的文件列表也是
    按 ``path_b64`` 排的，而 ``verify_layout`` 要求偏移量跟这个顺序首尾相接。
    两处顺序一旦不一致，还原时就会把 A 文件的内容当成 B 文件的。

    这个坑真的踩过：``big.txt`` 按原始路径排在 ``bom.txt`` 前面，但 base64 之后
    ``Ym9t...``(bom) 排在 ``Ymln...``(big) 前面（因为 ``'9' < 'l'``），于是偏移量
    整体错位。所以在这里排序，而不是指望调用方排对。
    """
    ordered = sorted(sources, key=lambda s: encode_path_b64(s.path))

    entries: list[FileEntry] = []
    parts: list[bytes] = []
    offset = 0
    for source in ordered:
        entries.append(
            FileEntry.create(
                source.path,
                size=len(source.data),
                offset=offset,
                sha256=hashlib.sha256(source.data).hexdigest(),
                mode=source.mode,
                mtime_ns=source.mtime_ns,
            )
        )
        parts.append(source.data)
        offset += len(source.data)
    return tuple(entries), b"".join(parts)


# --------------------------------------------------------------------------
# 组装
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionPlan:
    """一个会话的完整编码计划：清单 + 所有分片帧。"""

    session_id: bytes
    manifest: Manifest
    frames: tuple[bytes, ...]
    manifest_count: int
    data_count: int
    #: 压缩后的数据段 / 清单段字节数（给 CLI 报"压到多少"用）
    data_bytes: int = 0
    manifest_bytes: int = 0
    #: 两段各自实际用的压缩方法 —— 补播时要原样复现
    data_compression: str = COMPRESSION_NONE
    manifest_compression: str = COMPRESSION_NONE

    @property
    def total(self) -> int:
        return len(self.frames)

    @property
    def manifest_frames(self) -> tuple[bytes, ...]:
        return self.frames[: self.manifest_count]

    @property
    def data_frames(self) -> tuple[bytes, ...]:
        return self.frames[self.manifest_count :]

    @property
    def session_hex(self) -> str:
        return self.session_id.hex()


def _frame_flags(method: str, *, is_manifest: bool) -> tuple[int, int]:
    """把"这段用了哪种压缩"编码进分片头。

    返回 ``(flags, method_id)``：flags 保留给版本 1 兼容，method_id 是版本 2 的
    权威来源（写在 reserved[0]）。
    """
    flags = 0
    if method != COMPRESSION_NONE:
        flags |= FLAG_COMPRESSED
    if method == COMPRESSION_LZMA:
        flags |= FLAG_LZMA
    if is_manifest:
        flags |= FLAG_MANIFEST
    return flags, METHOD_IDS.get(method, METHOD_NONE)


def build_session(
    *,
    manifest: Manifest,
    blob: bytes,
    session_id: bytes,
    chunk_size: int,
    compression: str = COMPRESSION_AUTO,
    manifest_compression: str | None = None,
) -> SessionPlan:
    """把清单和 BLOB 组装成一个会话的全部分片帧。

    ``compression`` 可以是 ``auto`` / ``zlib`` / ``lzma`` / ``none``。
    ``auto`` 会把所有压缩器都跑一遍取最小的 —— **直接决定要出多少张码**，
    所以默认就选它。

    ``manifest_compression`` 用于**补播**：正常播放时两段各自用 ``auto`` 挑了
    可能不同的方法；补播必须原样复现，否则分片边界会移位。把它设成清单段
    当时实际用的方法即可。
    """
    if chunk_size < 1:
        raise LimitExceeded(f"分片大小必须为正，收到 {chunk_size}")

    # 数据段占体积的绝对多数，它的压缩方式就是这次会话的"身份"
    blob_segment, data_method = compress_segment(blob, compression)

    # 先把真实的 compression 写回清单，再序列化 —— 否则码里带的清单会说谎
    manifest = replace(manifest, compression=data_method)
    manifest_segment, manifest_method = compress_segment(
        manifest.to_bytes(),
        compression if manifest_compression is None else manifest_compression,
    )

    manifest_chunks = split_segment(manifest_segment, chunk_size)
    data_chunks = split_segment(blob_segment, chunk_size)

    if not manifest_chunks:
        raise ChunkError("清单段为空 —— 这不该发生（清单至少有 format 字段）")

    total = len(manifest_chunks) + len(data_chunks)
    if total > MAX_TOTAL:
        raise LimitExceeded(
            f"分片总数 {total} 超出上限 {MAX_TOTAL}。"
            f"请改用每码装得更多的 --profile（max 每码 2921 字节），"
            f"或分几次编码不同的子目录（每次 -o 到不同输出目录）。"
        )

    frames: list[bytes] = []
    manifest_flags, manifest_method_id = _frame_flags(manifest_method, is_manifest=True)
    data_flags, data_method_id = _frame_flags(data_method, is_manifest=False)

    for index, body in enumerate(manifest_chunks):
        frames.append(
            pack_frame(
                session_id=session_id,
                index=index,
                total=total,
                body=body,
                flags=manifest_flags,
                method=manifest_method_id,
            )
        )
    for offset, body in enumerate(data_chunks):
        frames.append(
            pack_frame(
                session_id=session_id,
                index=len(manifest_chunks) + offset,
                total=total,
                body=body,
                flags=data_flags,
                method=data_method_id,
            )
        )

    return SessionPlan(
        session_id=session_id,
        manifest=manifest,
        frames=tuple(frames),
        manifest_count=len(manifest_chunks),
        data_count=len(data_chunks),
        data_bytes=len(blob_segment),
        manifest_bytes=len(manifest_segment),
        data_compression=data_method,
        manifest_compression=manifest_method,
    )


# --------------------------------------------------------------------------
# 拆解
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReassemblyStats:
    """拆解过程的统计，用于给用户如实的报告。"""

    total_symbols: int = 0
    accepted: int = 0
    foreign: int = 0  # magic 不对 —— 不是 qrfile 生成的码
    damaged: int = 0  # 头 CRC 或载荷 CRC 失败
    duplicates: int = 0  # 重复片（内容一致，已容忍）
    session_hex: str = ""


@dataclass(frozen=True, slots=True)
class ReassembledSession:
    session_id: bytes
    manifest: Manifest
    blob: bytes

    @property
    def session_hex(self) -> str:
        return self.session_id.hex()

    def file_bytes(self, entry: FileEntry) -> bytes:
        return self.blob[entry.offset : entry.offset + entry.size]

    def verify_files(self) -> list[str]:
        """逐文件核对大小与 SHA-256。返回不一致的描述列表（空 = 全过）。

        这是 V3 的前半。
        """
        problems: list[str] = []
        for entry in self.manifest.files:
            data = self.file_bytes(entry)
            if len(data) != entry.size:
                problems.append(
                    f"{entry.path}：大小不符（清单写 {entry.size}，实际 {len(data)}）"
                )
                continue
            actual = hashlib.sha256(data).hexdigest()
            if actual != entry.sha256:
                problems.append(
                    f"{entry.path}：SHA-256 不符"
                    f"（清单写 {entry.sha256[:16]}…，实际 {actual[:16]}…）"
                )
        return problems

    def verify_all(self) -> None:
        """V3 全套：布局自洽 + 逐文件哈希 + 集合级 archive_sha256。全过才返回。"""
        verify_layout(self.manifest)
        problems = self.verify_files()
        if problems:
            detail = "\n".join(f"  - {line}" for line in problems)
            raise VerificationError(f"{len(problems)} 个文件校验不通过：\n{detail}")
        verify_archive_sha256(self.manifest)


def reassemble(
    payloads: Sequence[bytes],
    *,
    session_id: bytes | None = None,
    verify: bool = True,
) -> tuple[ReassembledSession, ReassemblyStats]:
    """把一堆分片载荷拼回一个会话。

    **失败即关闭**（fail closed）：缺片、CRC 不符、重复片内容冲突，一律抛
    :class:`ChunkError`，绝不返回一个"凑合能用"的结果。``verify=True`` 时还会
    跑完整的 V3 校验（默认开）。
    """
    # ---- 第一遍：逐张拆帧，把非本工具的码和坏码分门别类记下来
    parsed: list[tuple[object, bytes]] = []
    foreign = 0
    damaged = 0

    for payload in payloads:
        # 先用 magic 判断"是不是我们的码"，**不要**去匹配 unpack_frame 的报错文案
        # —— 那样一个 19 字节的普通网址二维码（连头都不够长）会被误报成"已损坏的
        # qrfile 分片"，而它其实只是别人的码。
        if len(payload) < len(MAGIC) or not payload.startswith(MAGIC):
            foreign += 1
            continue
        try:
            header, body = unpack_frame(payload)
        except ChunkError:
            damaged += 1
            continue
        parsed.append((header, body))

    if not parsed:
        raise ChunkError(
            f"没有找到任何 qrfile 分片（共看了 {len(payloads)} 张码："
            f"{foreign} 张不是 qrfile 生成的，{damaged} 张已损坏）"
        )

    # ---- 分组：把所有会话列出来，让用户知道有哪些可选
    counts = Counter(header.session_id for header, _ in parsed)  # type: ignore[attr-defined]
    if len(counts) > 1 and session_id is None:
        listed = "、".join(sorted(s.hex() for s in counts))
        raise ChunkError(
            f"发现 {len(counts)} 个不同会话（{listed}）。请用 --session 指定要还原哪一个。"
        )

    target = session_id if session_id is not None else next(iter(counts))
    if target not in counts:
        raise ChunkError(
            f"指定的会话 {target.hex()} 不在这批码里"
            f"（现有：{'、'.join(sorted(s.hex() for s in counts))}）"
        )

    # ---- 第二遍：收目标会话的片，检查总数一致、重复片、缺片
    # frames[index] = (body, is_manifest, 压缩方式)
    frames: dict[int, tuple[bytes, bool, str]] = {}
    total: int | None = None
    duplicates = 0

    for header, body in parsed:
        if header.session_id != target:  # type: ignore[attr-defined]
            continue
        if total is None:
            total = header.total  # type: ignore[attr-defined]
        elif header.total != total:  # type: ignore[attr-defined]
            raise ChunkError(
                f"同一会话里的分片总数不一致：先见到 {total}，"
                f"又见到 {header.total}。这批码可能来自两次不同的编码。"  # type: ignore[attr-defined]
            )

        index = header.index  # type: ignore[attr-defined]
        existing = frames.get(index)
        if existing is not None:
            if existing[0] != body:
                raise ChunkError(
                    f"分片 {index} 出现了两次且内容不一致 —— "
                    f"有一张码被改过，或者混进了两次编码的码。"
                )
            duplicates += 1
            continue
        frames[index] = (body, header.is_manifest, header.compression)  # type: ignore[attr-defined]

    if total is None:
        raise ChunkError(f"会话 {target.hex()} 里一片都没有")

    missing = sorted(set(range(total)) - set(frames))
    if missing:
        shown = ", ".join(str(i) for i in missing[:20])
        more = f"，另有 {len(missing) - 20} 片未列出" if len(missing) > 20 else ""
        raise ChunkError(
            f"缺少分片 {shown}{more}（共 {total} 片，手上只有 {len(frames)} 片）。"
            f"请把缺的码补扫进来。"
        )

    # ---- 按 index 顺序拼回两段
    manifest_parts: list[bytes] = []
    data_parts: list[bytes] = []
    manifest_methods: set[str] = set()
    data_methods: set[str] = set()

    for index in range(total):
        body, is_manifest, method = frames[index]
        if is_manifest:
            manifest_parts.append(body)
            manifest_methods.add(method)
        else:
            data_parts.append(body)
            data_methods.add(method)

    if len(manifest_methods) > 1 or len(data_methods) > 1:
        raise ChunkError("同一段里的压缩方式不一致 —— 这些码可能来自两次不同的编码。")

    manifest_raw = decompress_segment(
        b"".join(manifest_parts),
        manifest_methods.pop() if manifest_methods else COMPRESSION_NONE,
    )
    manifest = Manifest.from_bytes(manifest_raw)
    blob = decompress_segment(
        b"".join(data_parts),
        data_methods.pop() if data_methods else COMPRESSION_NONE,
    )

    session = ReassembledSession(session_id=target, manifest=manifest, blob=blob)
    if verify:
        session.verify_all()

    stats = ReassemblyStats(
        total_symbols=len(payloads),
        accepted=len(frames),
        foreign=foreign,
        damaged=damaged,
        duplicates=duplicates,
        session_hex=target.hex(),
    )
    return session, stats


# --------------------------------------------------------------------------
# 从扫描结果组装清单
# --------------------------------------------------------------------------


def manifest_for_sources(
    *,
    root: str,
    sources: Sequence[FileSource],
    dirs: Sequence[str] = (),
    symlinks: Sequence[tuple[str, str]] = (),
    skipped: Sequence[tuple[str, str]] = (),
    chunk_size: int,
) -> tuple[Manifest, bytes]:
    """从扫描出来的源文件组装清单和 BLOB。

    ``dirs`` / ``symlinks`` / ``skipped`` 用简单元组传入，免得调用方依赖
    manifest 内部的条目类型。
    """
    entries, blob = pack_blob(sources)
    manifest = build_manifest(
        root=root,
        chunk_size=chunk_size,
        compression=COMPRESSION_NONE,  # 真实值由 build_session 填
        files=entries,
        dirs=[DirEntry.create(d) for d in dirs],
        symlinks=[SymlinkEntry.create(p, t) for p, t in symlinks],
        skipped=[SkippedEntry.create(p, r) for p, r in skipped],
        blob_size=len(blob),
    )
    return manifest, blob
