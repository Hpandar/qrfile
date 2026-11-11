"""Qt 界面：命令入口、窗口构建、事件路由、子进程解析。

没有 PySide6 或没有显示环境时自动跳过；设置 ``QT_QPA_PLATFORM=offscreen``
后可以在无头 CI 中构建窗口。
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from qrfile.ui.app import MainWindow  # noqa: E402
from qrfile.ui.pages import IntervalDialog  # noqa: E402
from qrfile.ui.runner import ChildJob, command_prefix  # noqa: E402


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    yield instance


def test_command_prefix_source_mode(monkeypatch, tmp_path) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert command_prefix() == [sys.executable, "-m", "qrfile"]

    fake_exe = tmp_path / "qrfile-gui"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    assert command_prefix() == [str(fake_exe)]

    cli = tmp_path / ("qrfile.exe" if sys.platform == "win32" else "qrfile")
    cli.write_text("", encoding="utf-8")
    assert command_prefix() == [str(cli)]


def test_gui_command_invokes_ui(monkeypatch) -> None:
    from click.testing import CliRunner

    from qrfile import cli
    import qrfile.ui.app as app_module

    called: list[bool] = []
    monkeypatch.setattr(
        app_module, "main", lambda: called.append(True) or 0
    )
    result = CliRunner().invoke(cli.main, ["gui"])

    assert result.exit_code == 0, result.output
    assert called == [True]


def test_main_window_builds_and_navigates(app) -> None:
    window = MainWindow()
    try:
        assert window.stack.count() == 7
        window._navigate("send")
        assert window.stack.currentWidget() is window.send_page
        window._navigate("recv")
        assert window.stack.currentWidget() is window.receive_page
        window._navigate("diag")
        assert window.stack.currentWidget() is window.diagnostics
        assert window.send_page.mode.findData("rgb") >= 0
        assert window.receive_page.rgb.isChecked() is False
    finally:
        window.close()


def test_send_rgb_mode_passes_flag(app, tmp_path) -> None:
    window = MainWindow()
    captured: dict = {}
    window._start_job = lambda args, **kwargs: captured.update(args=args, kwargs=kwargs)
    sample = tmp_path / "a.txt"
    sample.write_text("x", encoding="utf-8")
    try:
        window._start_send({"path": str(sample), "mode": "rgb", "duration": 10})
        assert "--rgb" in captured["args"]
    finally:
        window.close()


def test_receive_rgb_mode_passes_flag(app, tmp_path) -> None:
    window = MainWindow()
    captured: dict = {}
    window._start_job = lambda args, **kwargs: captured.update(args=args, kwargs=kwargs)
    try:
        window._launch_receive(
            {"output": str(tmp_path), "safe": False, "rgb": True}
        )
        assert "--rgb" in captured["args"]
    finally:
        window.close()


def test_send_monitor_event_routing(app) -> None:
    window = MainWindow()
    window.active_kind = "send"
    try:
        window._on_event(
            {
                "type": "plan",
                "total": 100,
                "pages": 10,
                "session": "abc",
                "fps": 5.0,
                "layout": {"per_screen": 10, "description": "v33-M"},
            }
        )
        window._on_event({"type": "page", "page": 2, "total_pages": 10, "fps": 5.0})

        assert window.send_monitor.total_frames == 100
        assert window.send_monitor.total_pages == 10
        assert window.send_monitor.ring.value() == 30
        assert "第 3 / 10 屏" in window.send_monitor.page_label.text()
    finally:
        window.close()


def test_receive_monitor_event_routing(app) -> None:
    window = MainWindow()
    window.active_kind = "recv"
    try:
        window._on_event(
            {"type": "window", "description": "远程桌面", "rect": [0, 0, 800, 600]}
        )
        window._on_event(
            {
                "type": "progress",
                "received": 9,
                "total": 10,
                "percent": 90.0,
                "duplicates": 1,
                "damaged": 0,
            }
        )
        window._on_event(
            {"type": "missing", "indices": [3], "ranges": "3", "total": 10}
        )

        assert window.receive_monitor.ring.value() == 90
        assert window.receive_monitor.missing_input.text() == "3"

        window._on_event({"type": "state", "state": "completed"})
        assert window.receive_monitor.ring.value() == 100
        assert window.receive_monitor.header.chip.text() == "完成"
    finally:
        window.close()


def test_pull_accepts_screen_option(monkeypatch) -> None:
    from click.testing import CliRunner

    from qrfile import live_cli

    captured: dict = {}
    monkeypatch.setattr(
        live_cli,
        "_pull_impl",
        lambda output, **kwargs: captured.update(kwargs, output=output),
    )
    result = CliRunner().invoke(live_cli.pull_cmd, ["out", "--screen", "2"])

    assert result.exit_code == 0, result.output
    assert captured["screen"] == 2


def test_receive_page_has_screen_selector(app) -> None:
    from PySide6.QtCore import QSettings

    from qrfile.ui.pages import ReceivePage

    page = ReceivePage(QSettings("qrfile", "qrfile"))
    try:
        assert page.screen.count() >= 1
        assert page.screen.itemData(0) is None
    finally:
        page.close()


def test_send_monitor_shows_resume_after_finish(app) -> None:
    from qrfile.ui.pages import SendMonitorPage

    page = SendMonitorPage()
    try:
        page.finish(0)
        assert not page.resume_button.isHidden()
        assert str(page.header.chip.text()) == "播放结束"
    finally:
        page.close()


def test_receive_monitor_restore_progress_and_output(app) -> None:
    from qrfile.ui.pages import ReceiveMonitorPage

    page = ReceiveMonitorPage()
    try:
        page.set_restore_progress(
            {"written": 5, "total": 10, "path": "tests/a.py"}
        )
        assert page.restore_bar.value() == 5

        page.set_state(
            {
                "state": "completed",
                "output": r"C:\Users\10911\Desktop\out",
                "content_path": r"C:\Users\10911\Desktop\out\tests",
                "top_level": ["tests"],
                "written": 10,
            }
        )
        assert "C:\\Users\\10911\\Desktop\\out\\tests" in page.done_label.text()
        assert not page.open_button.isHidden()
    finally:
        page.close()


def test_interval_dialog_preview(app) -> None:
    dialog = IntervalDialog(total=100, per_screen=8, text="0-9")
    try:
        assert "10 个分片" in dialog.preview.text()
        assert "2 页" in dialog.preview.text()
    finally:
        dialog.close()


def test_child_job_parses_ndjson_and_stderr(app, tmp_path) -> None:
    script = tmp_path / "fake_child.py"
    script.write_text(
        "import json, sys\n"
        "print(json.dumps({'type': 'state', 'state': 'receiving'}))\n"
        "print(json.dumps({'type': 'progress', 'received': 1, 'total': 2}))\n"
        "print('人类可读文本', file=sys.stderr)\n",
        encoding="utf-8",
    )

    job = ChildJob()
    received_events: list[dict] = []
    received_logs: list[str] = []
    codes: list[int] = []

    job.event.connect(received_events.append)
    job.log.connect(received_logs.append)

    loop = QEventLoop()
    job.finished.connect(lambda code: (codes.append(code), loop.quit()))
    assert job.start(
        [],
        cwd=str(tmp_path),
        events_mode=True,
        prefix=[sys.executable, str(script)],
    )
    QTimer.singleShot(5000, loop.quit)
    loop.exec()

    assert codes == [0]
    assert [event["type"] for event in received_events] == ["state", "progress"]
    assert any("人类可读文本" in line for line in received_logs)
