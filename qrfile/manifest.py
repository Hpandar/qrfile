"""会话清单：规范 JSON，字节级确定。

清单是**权威描述**：每个文件在 BLOB 里的偏移、大小、SHA-256，加上空目录、符号
链接、被跳过的文件。解码端完全靠它来逐文件校验并还原目录结构。

两个关键设计（方案第三章）：

**一、``path_b64`` 是权威，``path`` 只是给人看的。**
路径的真实身份是文件系统上的原始字节。用 base64 存原始字节，一次性解决三个
难题：Linux 上非 UTF-8 文件名的 surrogateescape、macOS 的 NFD vs NFC 分解差异、
Windows 保留名的转义。排序和 ``archive_sha256`` 一律基于 ``path_b64``。

**二、确定性。**
同样的输入必须产生字节完全相同的清单，否则 ``archive_sha256`` 就失去意义。
所以：紧凑分隔符、键排序、固定字段顺序、基于 path_b64 排序。

``archive_sha256`` 覆盖"文件集合的构成"—— 有哪些文件、各自多大、内容哈希是多少、
有哪些空目录和符号链接。它**不**覆盖 ``created`` / ``chunk_size`` / ``compression``
这类传输元数据：同样的内容换个分片大小重新编码，身份理应不变。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Sequence

from .errors import VerificationError

FORMAT = "QRFS/1"

COMPRESSION_NONE = "none"
COMPRESSION_ZLIB = "zlib"
COMPRESSION_LZMA = "lzma"
#: "auto" 不是一种真实算法，而是让 compress_segment 在 Python 标库里挑最小的那个
COMPRESSION_AUTO = "auto"
#: "max" 会额外尝试本机的 brotli / zstd / 7z。解码端必须有同一个工具。
COMPRESSION_MAX = "max"

#: 用户能选的压缩方式
COMPRESSION_CHOICES = (
    COMPRESSION_AUTO,
    COMPRESSION_MAX,
    COMPRESSION_ZLIB,
    COMPRESSION_LZMA,
    COMPRESSION_NONE,
)


# --------------------------------------------------------------------------
# 路径的两种表示
# --------------------------------------------------------------------------


def encode_path_b64(path: str) -> str:
    """把路径转成权威表示：文件系统原始字节的 base64。

    两个平台的 ``os.fsencode`` 语义**不一样**，这里如实说明而不是假装统一：

    * **POSIX** 用 ``surrogateescape`` —— 非 UTF-8 的文件名会带上代理对，
      ``fsencode`` 把它们还原成**原始字节**。所以 Linux 上 ``caf\\xe9.txt``
      这种非 UTF-8 文件名能真正无损往返。
    * **Windows** 用 ``surrogatepass`` —— 文件名本来就是 UTF-16，``fsdecode``
      遇到非法 UTF-8 字节会**直接抛错**，所以 Windows 上根本产生不了这种路径。
      人工构造的代理对会被按 CESU-8 编码，同样是可逆的，但拿到的是编码后的字节
      而不是"原始字节"。

    结论：**同一个平台上编码再解码永远无损**（这是清单自洽所必需的）。
    而 Linux 上带非 UTF-8 文件名的清单拿到 Windows 上还原，会得到一个合法但
    名字略有出入的路径 —— 这是真实的跨平台限制，不藏着。
    """
    return base64.b64encode(os.fsencode(path)).decode("ascii")


def decode_path_b64(encoded: str) -> str:
    """从权威表示还原路径。"""
    return os.fsdecode(base64.b64decode(encoded.encode("ascii")))


def display_path(path: str) -> str:
    """给人看的路径：NFC 规范化 + 替换掉代理对。

    代理对（Linux 上非 UTF-8 文件名会产生）不是合法 JSON 字符串，直接序列化会
    炸。这里替换成 U+FFFD —— **仅用于显示**，还原一律走 ``path_b64``。
    """
    cleaned = "".join(
        "�" if 0xD800 <= ord(ch) <= 0xDFFF else ch for ch in path
    )
    return unicodedata.normalize("NFC", cleaned)


def _iso_utc(timestamp_ns: int) -> str:
    seconds, nanos = divmod(timestamp_ns, 1_000_000_000)
    moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + f".{nanos:09d}Z"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# 条目
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FileEntry:
    path: str  # 显示用（NFC，可能有损）
    path_b64: str  # 权威：原始文件系统字节
    size: int
    offset: int  # 在 BLOB 里的字节偏移
    sha256: str
    mode: int = 0o644
    mtime: str | None = None  # UTC ISO-8601

    @classmethod
    def create(
        cls, path: str, *, size: int, offset: int, sha256: str, mode: int, mtime_ns: int | None
    ) -> FileEntry:
        return cls(
            path=display_path(path),
            path_b64=encode_path_b64(path),
            size=size,
            offset=offset,
            sha256=sha256,
            mode=mode,
            mtime=_iso_utc(mtime_ns) if mtime_ns is not None else None,
        )

    def resolve_path(self) -> str:
        """权威路径（还原磁盘文件时用这个，不要用 ``path``）。"""
        return decode_path_b64(self.path_b64)

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "path_b64": self.path_b64,
            "size": self.size,
            "offset": self.offset,
            "sha256": self.sha256,
            "mode": self.mode,
            "mtime": self.mtime,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> FileEntry:
        return cls(
            path=raw["path"],
            path_b64=raw["path_b64"],
            size=raw["size"],
            offset=raw["offset"],
            sha256=raw["sha256"],
            mode=raw.get("mode", 0o644),
            mtime=raw.get("mtime"),
        )


@dataclass(frozen=True, slots=True)
class DirEntry:
    """空目录。非空目录可以靠文件的父路径推出来，但空目录必须显式记录。"""

    path: str
    path_b64: str

    @classmethod
    def create(cls, path: str) -> DirEntry:
        return cls(path=display_path(path), path_b64=encode_path_b64(path))

    def resolve_path(self) -> str:
        return decode_path_b64(self.path_b64)

    def to_json(self) -> dict[str, Any]:
        return {"path": self.path, "path_b64": self.path_b64}

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> DirEntry:
        return cls(path=raw["path"], path_b64=raw["path_b64"])


@dataclass(frozen=True, slots=True)
class SymlinkEntry:
    path: str
    path_b64: str
    target: str

    @classmethod
    def create(cls, path: str, target: str) -> SymlinkEntry:
        return cls(path=display_path(path), path_b64=encode_path_b64(path), target=target)

    def resolve_path(self) -> str:
        return decode_path_b64(self.path_b64)

    def to_json(self) -> dict[str, Any]:
        return {"path": self.path, "path_b64": self.path_b64, "target": self.target}

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> SymlinkEntry:
        return cls(path=raw["path"], path_b64=raw["path_b64"], target=raw["target"])


@dataclass(frozen=True, slots=True)
class SkippedEntry:
    """被跳过的文件。写进清单，好让解码端告诉用户"当初跳过了什么"。"""

    path: str
    reason: str

    @classmethod
    def create(cls, path: str, reason: str) -> SkippedEntry:
        return cls(path=display_path(path), reason=reason)

    def to_json(self) -> dict[str, Any]:
        return {"path": self.path, "reason": self.reason}

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> SkippedEntry:
        return cls(path=raw["path"], reason=raw["reason"])


# --------------------------------------------------------------------------
# 清单
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Manifest:
    root: str
    chunk_size: int
    compression: str
    files: tuple[FileEntry, ...] = ()
    dirs: tuple[DirEntry, ...] = ()
    symlinks: tuple[SymlinkEntry, ...] = ()
    skipped: tuple[SkippedEntry, ...] = ()
    created: str = ""
    blob_size: int = 0
    archive_sha256: str = ""

    @property
    def total_file_bytes(self) -> int:
        return sum(entry.size for entry in self.files)

    def to_json(self) -> dict[str, Any]:
        return {
            "format": FORMAT,
            "created": self.created,
            "root": self.root,
            "compression": self.compression,
            "chunk_size": self.chunk_size,
            "blob_size": self.blob_size,
            "files": [entry.to_json() for entry in self.files],
            "dirs": [entry.to_json() for entry in self.dirs],
            "symlinks": [entry.to_json() for entry in self.symlinks],
            "skipped": [entry.to_json() for entry in self.skipped],
            "archive_sha256": self.archive_sha256,
        }

    def to_bytes(self) -> bytes:
        """规范化 JSON 字节。紧凑分隔符 + 键排序 ⇒ 同样输入必得同样字节。"""
        return json.dumps(
            self.to_json(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Manifest:
        fmt = raw.get("format")
        if fmt != FORMAT:
            raise VerificationError(
                f"清单格式不对：期望 {FORMAT!r}，收到 {fmt!r}"
            )
        return cls(
            root=raw["root"],
            chunk_size=raw["chunk_size"],
            compression=raw["compression"],
            files=tuple(FileEntry.from_json(r) for r in raw.get("files", [])),
            dirs=tuple(DirEntry.from_json(r) for r in raw.get("dirs", [])),
            symlinks=tuple(SymlinkEntry.from_json(r) for r in raw.get("symlinks", [])),
            skipped=tuple(SkippedEntry.from_json(r) for r in raw.get("skipped", [])),
            created=raw.get("created", ""),
            blob_size=raw.get("blob_size", 0),
            archive_sha256=raw.get("archive_sha256", ""),
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> Manifest:
        try:
            raw = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VerificationError(f"清单不是合法 JSON：{exc}") from exc
        if not isinstance(raw, dict):
            raise VerificationError("清单顶层必须是 JSON 对象")
        return cls.from_json(raw)


# --------------------------------------------------------------------------
# archive_sha256
# --------------------------------------------------------------------------


def compute_archive_sha256(
    files: Sequence[FileEntry],
    dirs: Sequence[DirEntry] = (),
    symlinks: Sequence[SymlinkEntry] = (),
) -> str:
    """整个文件集合的身份哈希。

    覆盖：有哪些文件（权威路径）、各自多大、内容 SHA-256；有哪些空目录；有哪些
    符号链接及其目标。**不**覆盖 created / chunk_size / compression —— 同样内容
    换个分片大小重新编码，身份理应不变。

    每条记录都做长度前缀，避免拼接歧义（"ab"+"c" 和 "a"+"bc" 必须不同）。
    记录排序后入哈希，所以调用方传进来的顺序不影响结果。
    """
    records: list[bytes] = []

    for entry in files:
        records.append(
            b"F\x00"
            + entry.path_b64.encode("ascii")
            + b"\x00"
            + str(entry.size).encode("ascii")
            + b"\x00"
            + entry.sha256.encode("ascii")
        )
    for directory in dirs:
        records.append(b"D\x00" + directory.path_b64.encode("ascii"))
    for link in symlinks:
        records.append(
            b"L\x00"
            + link.path_b64.encode("ascii")
            + b"\x00"
            + base64.b64encode(os.fsencode(link.target))
        )

    records.sort()

    digest = hashlib.sha256()
    for record in records:
        digest.update(struct.pack(">I", len(record)))
        digest.update(record)
    return digest.hexdigest()


def build_manifest(
    *,
    root: str,
    chunk_size: int,
    compression: str,
    files: Sequence[FileEntry] = (),
    dirs: Sequence[DirEntry] = (),
    symlinks: Sequence[SymlinkEntry] = (),
    skipped: Sequence[SkippedEntry] = (),
    created: str | None = None,
    blob_size: int | None = None,
) -> Manifest:
    """组装清单：排序、算 archive_sha256、填 metadata。

    排序基准一律是 ``path_b64``（原始字节）而不是显示路径 —— 这样不同平台上
    不会因为 Unicode 归一化差异而排出不同顺序。
    """
    ordered_files = tuple(sorted(files, key=lambda e: e.path_b64))
    ordered_dirs = tuple(sorted(dirs, key=lambda e: e.path_b64))
    ordered_links = tuple(sorted(symlinks, key=lambda e: e.path_b64))
    ordered_skipped = tuple(sorted(skipped, key=lambda e: display_path(e.path)))

    manifest = Manifest(
        root=root,
        chunk_size=chunk_size,
        compression=compression,
        files=ordered_files,
        dirs=ordered_dirs,
        symlinks=ordered_links,
        skipped=ordered_skipped,
        created=created if created is not None else utc_now_iso(),
        blob_size=blob_size if blob_size is not None else sum(f.size for f in ordered_files),
        archive_sha256=compute_archive_sha256(ordered_files, ordered_dirs, ordered_links),
    )
    return manifest


# --------------------------------------------------------------------------
# 校验（V3 的一部分）
# --------------------------------------------------------------------------


def verify_archive_sha256(manifest: Manifest) -> None:
    """重算 ``archive_sha256`` 并跟清单里存的值比对。

    证明"文件集合的构成"本身没被动过 —— 比如有人偷偷从清单里删掉一个文件条目，
    或者改了某个文件的路径。逐文件的 SHA-256 校验抓不到这类改动。
    """
    actual = compute_archive_sha256(manifest.files, manifest.dirs, manifest.symlinks)
    if actual != manifest.archive_sha256:
        raise VerificationError(
            f"archive_sha256 校验失败：清单里写着 {manifest.archive_sha256}，"
            f"重算得到 {actual}。清单被改动过。"
        )


def verify_layout(manifest: Manifest) -> None:
    """校验清单内部自洽：偏移量首尾相接、总长对得上 blob_size。

    偏移错位会导致把 A 文件的内容当成 B 文件的 —— 逐文件 SHA-256 能抓到，但在这里
    先报错能给出更清楚的定位。
    """
    expected_offset = 0
    for entry in manifest.files:
        if entry.offset != expected_offset:
            raise VerificationError(
                f"清单偏移不自洽：{entry.path!r} 的 offset 是 {entry.offset}，"
                f"按前面的文件推算应该是 {expected_offset}"
            )
        expected_offset += entry.size

    if manifest.blob_size != expected_offset:
        raise VerificationError(
            f"清单 blob_size 不自洽：写着 {manifest.blob_size}，"
            f"按各文件大小求和是 {expected_offset}"
        )


def with_created(manifest: Manifest, created: str) -> Manifest:
    """换掉 created 时间戳（测试里用来构造确定性输入）。"""
    return replace(manifest, created=created)
