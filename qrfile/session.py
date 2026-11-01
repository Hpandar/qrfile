"""会话存档：让「补播缺失分片」能重建出与原始播放完全一致的分片。

为什么需要存档而不是"重新跑一遍同样的命令"：

* 清单里有 ``created`` 时间戳，重新构建会变；
* ``--compression auto`` 会让两段各自挑压缩器，重新跑可能挑出不同结果；
* 会话号是随机的。

任何一处不同，分片边界就会移位，接收端手里的旧分片就对不上了。所以正常播放
时把「清单原文 + 会话号 + 分片大小 + 两段的压缩方法 + 布局」落一个 JSON，
补播时按存档原样重建。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

from .errors import InputError, VerificationError
from .livelayout import Layout
from .manifest import Manifest

SESSION_FORMAT = "qrfile-session/1"
#: 默认写在当前工作目录。补播命令要在同一个目录下执行。
DEFAULT_SESSION_FILE = ".qrfile-session.json"


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """一次播放的完整身份。"""

    session_id: bytes
    chunk_size: int
    data_compression: str
    manifest_compression: str
    layout: Layout
    manifest: Manifest
    sources: tuple[str, ...]
    total_frames: int
    #: 每个源文件的 ``(相对路径, 绝对路径)``。补播时按这个精确读回，
    #: 不受 --include/--exclude 或目录变化影响。
    files: tuple[tuple[str, str], ...] = ()

    def to_json(self) -> dict:
        return {
            "format": SESSION_FORMAT,
            "session_id": self.session_id.hex(),
            "chunk_size": self.chunk_size,
            "data_compression": self.data_compression,
            "manifest_compression": self.manifest_compression,
            "layout": asdict(self.layout),
            "manifest": self.manifest.to_json(),
            "sources": list(self.sources),
            "total_frames": self.total_frames,
            "files": [
                {"path": relative, "absolute": absolute}
                for relative, absolute in self.files
            ],
        }

    @classmethod
    def from_json(cls, raw: dict) -> "SessionRecord":
        if raw.get("format") != SESSION_FORMAT:
            raise VerificationError(
                f"会话文件格式不对：期望 {SESSION_FORMAT!r}，收到 {raw.get('format')!r}"
            )
        try:
            return cls(
                session_id=bytes.fromhex(raw["session_id"]),
                chunk_size=int(raw["chunk_size"]),
                data_compression=str(raw["data_compression"]),
                manifest_compression=str(raw["manifest_compression"]),
                layout=Layout(**raw["layout"]),
                manifest=Manifest.from_json(raw["manifest"]),
                sources=tuple(raw["sources"]),
                total_frames=int(raw["total_frames"]),
                files=tuple(
                    (str(item["path"]), str(item["absolute"]))
                    for item in raw.get("files", [])
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise VerificationError(f"会话文件内容不完整或格式不对：{exc}") from exc


def save_session(path: str | os.PathLike[str], record: SessionRecord) -> Path:
    """原子写入会话存档。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(record.to_json(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return target


def load_session(path: str | os.PathLike[str]) -> SessionRecord:
    target = Path(path)
    if not target.exists():
        raise InputError(
            f"找不到会话存档：{target}。先正常完整播放一次（会自动生成），"
            f"或用 --session-file 指定。"
        )
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InputError(f"读不了会话存档 {target}：{exc}") from exc
    if not isinstance(raw, dict):
        raise InputError(f"会话存档 {target} 的顶层必须是 JSON 对象")
    return SessionRecord.from_json(raw)


# --------------------------------------------------------------------------
# 分片范围：给「缺片」和「补播」用
# --------------------------------------------------------------------------


def parse_ranges(text: str) -> list[int]:
    """``"12-14,204,900-901"`` → ``[12,13,14,204,900,901]``。"""
    indices: set[int] = set()

    for part in text.replace("，", ",").split(","):
        piece = part.strip()
        if not piece:
            continue
        if "-" in piece:
            left, _, right = piece.partition("-")
            try:
                start, end = int(left), int(right)
            except ValueError:
                raise InputError(f"分片范围写法看不懂：{piece!r}（应该像 12-14）") from None
            if start < 0 or end < start:
                raise InputError(f"分片范围非法：{piece!r}")
            indices.update(range(start, end + 1))
        else:
            try:
                value = int(piece)
            except ValueError:
                raise InputError(f"分片号写法看不懂：{piece!r}") from None
            if value < 0:
                raise InputError(f"分片号不能为负：{value}")
            indices.add(value)

    if not indices:
        raise InputError("没有给出任何分片范围")
    return sorted(indices)


def format_ranges(indices: Sequence[int], *, limit: int = 40) -> str:
    """``[12,13,14,204,900,901]`` → ``"12-14,204,900-901"``。

    ``limit`` 是**区间个数**上限，超出时末尾加省略号，避免进度输出刷屏。
    """
    values = sorted(set(indices))
    if not values:
        return ""

    ranges: list[tuple[int, int]] = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
        else:
            ranges.append((start, previous))
            start = previous = value
    ranges.append((start, previous))

    parts = [str(a) if a == b else f"{a}-{b}" for a, b in ranges]
    if len(parts) > limit:
        parts = parts[:limit] + ["…"]
    return ",".join(parts)


def pages_for_indices(indices: Sequence[int], per_screen: int) -> list[int]:
    """把分片号映射成页号（补播时只播这些页）。"""
    if per_screen < 1:
        raise ValueError(f"per_screen 必须为正，收到 {per_screen}")
    return sorted({index // per_screen for index in indices})
