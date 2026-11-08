"""结构化事件：CLI 与图形界面共用的传输状态协议。

GUI 不再解析人类可读日志。命令带 ``--events-json`` 时，stdout 只写
NDJSON（每行一个 JSON 对象），人类可读文本全部改走 stderr；GUI 逐行读取
并更新进度、缺片和状态。

事件约定（``type`` 字段）：

``state``        状态变化：preparing / playing / receiving / verifying / completed
``plan``         发送准备完成：分片数、页数、布局、会话号
``progress``     接收进度：received / total / percent / duplicates / damaged
``missing``      缺失分片：indices / ranges
``page``         发送机器当前屏：page / total_pages / fps
``interval``     播放区间已应用：indices / pages / ranges
``paused``       暂停状态变化：value
``warning``      非致命提示：code / message
``error``        失败：code / message / hint / details
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

_enabled = False


def enable_events() -> None:
    """让当前进程的 stdout 只输出 NDJSON。"""
    global _enabled
    _enabled = True


def events_enabled() -> bool:
    return _enabled


def emit(kind: str, **payload: Any) -> None:
    """写一行 JSON 事件。未启用时是空操作。"""
    if not _enabled:
        return
    record = {"type": kind, "ts": round(time.time(), 3), **payload}
    sys.stdout.write(json.dumps(record, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def emit_error(
    code: str, message: str, *, hint: str = "", details: Any = None
) -> None:
    payload: dict[str, Any] = {"code": code, "message": message}
    if hint:
        payload["hint"] = hint
    if details is not None:
        payload["details"] = details
    emit("error", **payload)
