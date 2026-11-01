"""内置自检本身要可信 —— 它失败了才有意义，所以也要测它会失败。"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from qrfile.cli import main
from qrfile.selftest import build_corpus, run_selftest

runner = CliRunner()


@pytest.fixture(scope="module")
def report():
    """跑一次常规自检，模块内共用（它不便宜）。"""
    return run_selftest()


def test_selftest_roundtrip_passes(report) -> None:
    assert report.passed, [c for c in report.failures]
    assert report.codes >= 1
    assert report.files >= 15


def test_selftest_full_passes() -> None:
    """--full 含故障注入，全部要通过。"""
    result = run_selftest(full=True)
    assert result.passed, [(c.name, c.detail) for c in result.failures]


def test_selftest_covers_the_important_categories(report) -> None:
    """自检必须真的覆盖到这些点，否则它给的安全感是虚的。"""
    names = {check.name for check in report.checks}
    for required in (
        "文本文件全部收下",
        "二进制文件全部跳过",
        "空目录全部识别",
        "编码期自解码比对（V1）",
        "源文件指纹零差异（只读）",
        "还原级校验 V2+V3",
        "还原后逐文件逐字节一致",
        "目录结构完整（含空目录）",
    ):
        assert required in names, f"自检缺少这一项：{required}"


def test_full_selftest_includes_all_fault_injections() -> None:
    names = {check.name for check in run_selftest(full=True).checks}
    for required in (
        "故障注入：缺片被抓住",
        "故障注入：损坏的码被抓住",
        "故障注入：重复的码被容忍",
        "故障注入：无关的码被忽略",
        "故障注入：内容篡改被逐文件哈希抓住",
    ):
        assert required in names, f"故障注入缺少这一项：{required}"


def test_corpus_contains_tricky_content(tmp_path: Path) -> None:
    """语料必须包含那些"一看就容易出字节差异"的东西。"""
    spec = build_corpus(tmp_path)
    for rel in (
        "empty.txt",  # 空文件
        "crlf.txt",  # CRLF
        "cr.txt",  # CR
        "mixed_newlines.txt",  # 混合换行
        "utf8_bom.txt",  # UTF-8 BOM
        "utf16.txt",  # UTF-16
        "gbk.txt",  # GBK
        "dup_a.txt",  # 内容重复
    ):
        assert rel in spec.files, f"语料里缺 {rel}"
    assert spec.files["empty.txt"] == b""
    assert spec.files["crlf.txt"] == b"line1\r\nline2\r\n"
    assert spec.empty_dirs
    assert spec.skipped


def test_selftest_leaves_no_temp_dirs_behind() -> None:
    """不传 workdir 时，自检必须自己收拾干净。"""
    before = {p.name for p in Path(tempfile.gettempdir()).glob("qrfile-selftest-*")}
    run_selftest()
    after = {p.name for p in Path(tempfile.gettempdir()).glob("qrfile-selftest-*")}
    assert before == after


def test_selftest_cli_command() -> None:
    result = runner.invoke(main, ["selftest"])
    assert result.exit_code == 0, result.output
    assert "自检全部通过" in result.output


def test_selftest_cli_full_command() -> None:
    result = runner.invoke(main, ["selftest", "--full"])
    assert result.exit_code == 0, result.output
    assert "故障注入" in result.output


def test_selftest_cli_keep_leaves_workdir(tmp_path: Path) -> None:
    result = runner.invoke(main, ["selftest", "--keep"])
    assert result.exit_code == 0, result.output
    assert "--keep" in result.output
    # 清理掉它留下的目录
    for leftover in Path(tempfile.gettempdir()).glob("qrfile-selftest-*"):
        import shutil

        shutil.rmtree(leftover, ignore_errors=True)


# --------------------------------------------------------------------------
# 自检必须真的会失败 —— 否则它只是个装饰
# --------------------------------------------------------------------------


def test_selftest_fails_when_content_is_silently_altered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """注入一个"还原结果被悄悄改了一个字节"的故障，自检必须报出来。

    做法：把 restore_session 换成一个会篡改内容的版本。
    """
    import qrfile.selftest as selftest_module
    from qrfile.decode import RestoreReport

    real_restore = selftest_module.restore_session

    def tampering_restore(session, options):
        report: RestoreReport = real_restore(session, options)
        # 悄悄改掉一个已还原文件的内容
        victim = Path(options.output_dir) / report.written[0]
        victim.write_bytes(victim.read_bytes() + b"TAMPERED")
        return report

    monkeypatch.setattr(selftest_module, "restore_session", tampering_restore)

    result = run_selftest()
    assert not result.passed
    assert any("逐字节一致" in check.name for check in result.failures)


def test_selftest_fails_when_a_file_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """注入"还原时少写一个文件"的故障，自检必须报出来。"""
    import qrfile.selftest as selftest_module
    from qrfile.decode import RestoreReport

    real_restore = selftest_module.restore_session

    def dropping_restore(session, options):
        report: RestoreReport = real_restore(session, options)
        victim = Path(options.output_dir) / report.written[0]
        victim.unlink()
        return report

    monkeypatch.setattr(selftest_module, "restore_session", dropping_restore)

    result = run_selftest()
    assert not result.passed
    assert any("目录结构" in check.name for check in result.failures)
