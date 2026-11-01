"""实时接收：收集分片、报进度、诊断缺片，集齐后交给容器层还原。

编码端是**无限循环播放**的，所以接收端不需要任何反馈通道：漏掉的片下一轮
自然会再出现。这里只做去重和计数，收够 ``total`` 片就完成。
"""

from __future__ import annotations

from dataclasses import dataclass

from .archive import ReassembledSession, ReassemblyStats, reassemble
from .chunk import MAGIC, ChunkError, unpack_frame
from .errors import VerificationError


@dataclass(frozen=True, slots=True)
class LiveProgress:
    session_hex: str
    received: int
    total: int
    duplicates: int
    damaged: int
    foreign: int
    other_session: int
    conflicts: int

    @property
    def percent(self) -> float:
        return 100.0 * self.received / self.total if self.total else 0.0

    @property
    def complete(self) -> bool:
        return self.total > 0 and self.received >= self.total


class LiveCollector:
    """边收边拼。线程不安全 —— 设计上只在采集循环的单线程里用。"""

    def __init__(self) -> None:
        self._frames: dict[int, bytes] = {}
        self._total = 0
        self._session: bytes | None = None
        self.duplicates = 0
        self.damaged = 0
        self.foreign = 0
        self.other_session = 0
        self.conflicts = 0

    # ---------------------------------------------------------------- 状态

    @property
    def session_id(self) -> bytes | None:
        return self._session

    @property
    def total(self) -> int:
        return self._total

    @property
    def received(self) -> int:
        return len(self._frames)

    @property
    def complete(self) -> bool:
        return self._total > 0 and len(self._frames) >= self._total

    def progress(self) -> LiveProgress:
        return LiveProgress(
            session_hex=self._session.hex() if self._session else "-",
            received=len(self._frames),
            total=self._total,
            duplicates=self.duplicates,
            damaged=self.damaged,
            foreign=self.foreign,
            other_session=self.other_session,
            conflicts=self.conflicts,
        )

    def missing(self) -> list[int]:
        return sorted(set(range(self._total)) - set(self._frames))

    # ---------------------------------------------------------------- 投喂

    def feed(self, payload: bytes) -> str:
        """收一个二维码载荷。返回分类结果，便于上层统计。"""
        if len(payload) < len(MAGIC) or not payload.startswith(MAGIC):
            self.foreign += 1
            return "foreign"

        try:
            header, _ = unpack_frame(payload)
        except ChunkError:
            self.damaged += 1
            return "damaged"

        if self._session is None:
            self._session = header.session_id
            self._total = header.total
        elif header.session_id != self._session:
            self.other_session += 1
            return "other-session"

        if header.total != self._total:
            self.damaged += 1
            return "inconsistent"

        if header.index in self._frames:
            if self._frames[header.index] == payload:
                self.duplicates += 1
                return "duplicate"
            # 同号不同内容 —— 两张码里有一张被改过，或混了两次编码。
            # 不覆盖、不猜，留给最终校验报错。
            self.conflicts += 1
            return "conflict"

        self._frames[header.index] = payload
        return "accepted"

    # ---------------------------------------------------------------- 收尾

    def payloads(self) -> list[bytes]:
        return [self._frames[index] for index in range(self._total) if index in self._frames]

    def finalize(self) -> tuple[ReassembledSession, ReassemblyStats]:
        """校验并返回完整会话。缺片时抛 :class:`VerificationError`。"""
        if not self.complete:
            missing = self.missing()
            shown = "、".join(str(i) for i in missing[:20])
            more = f" 等 {len(missing)} 片" if len(missing) > 20 else ""
            raise VerificationError(
                f"还缺 {len(missing)}/{self._total} 片（{shown}{more}），无法还原"
            )
        if self.conflicts:
            raise VerificationError(
                f"有 {self.conflicts} 个分片号收到了不一致的内容 —— "
                f"这批码可能混了两次编码，或其中一张被篡改"
            )
        return reassemble(self.payloads(), verify=True)
