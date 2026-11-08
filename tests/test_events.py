"""结构化事件：CLI 与 GUI 的协议层。"""

from __future__ import annotations

import json

from qrfile import events
from qrfile.live_cli import _progress_line


class _Progress:
    total = 10
    received = 4
    percent = 40.0
    duplicates = 1
    damaged = 0


def test_emit_writes_ndjson_when_enabled(monkeypatch, capsys) -> None:
    monkeypatch.setattr(events, "_enabled", True)

    events.emit("state", state="playing")
    line = capsys.readouterr().out.strip()

    payload = json.loads(line)
    assert payload["type"] == "state"
    assert payload["state"] == "playing"
    assert "ts" in payload


def test_emit_is_silent_when_disabled(monkeypatch, capsys) -> None:
    monkeypatch.setattr(events, "_enabled", False)
    events.emit("state", state="playing")
    assert capsys.readouterr().out == ""


def test_emit_error_payload(monkeypatch, capsys) -> None:
    monkeypatch.setattr(events, "_enabled", True)

    events.emit_error("missing_chunks", "还缺 3 片", hint="应用播放区间")
    payload = json.loads(capsys.readouterr().out.strip())

    assert payload["type"] == "error"
    assert payload["code"] == "missing_chunks"
    assert payload["hint"] == "应用播放区间"


def test_progress_line_emits_event(monkeypatch, capsys) -> None:
    monkeypatch.setattr(events, "_enabled", True)

    _progress_line(_Progress())
    payload = json.loads(capsys.readouterr().out.strip())

    assert payload["type"] == "progress"
    assert payload["received"] == 4
    assert payload["total"] == 10
    assert payload["percent"] == 40.0
