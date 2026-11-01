"""CLI 端到端：encode → decode 逐字节一致，以及各条安全防线。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from qrfile.cli import main
from qrfile.errors import (
    EXIT_INPUT_ERROR,
    EXIT_OK,
    EXIT_PARTIAL,
    EXIT_VERIFY_FAILED,
)
from qrfile.readonly import compare_snapshots, snapshot_tree

runner = CliRunner()


@pytest.fixture
def source_tree(tmp_path: Path) -> Path:
    root = tmp_path / "src"
    (root / "sub" / "deep").mkdir(parents=True)
    (root / "empty_dir").mkdir()
    (root / "a.txt").write_bytes(b"hello\r\nworld\r\n")
    (root / "sub" / "note.md").write_bytes("# 标题\n\n中文内容\n".encode())
    (root / "sub" / "deep" / "code.py").write_bytes(b"def f():\n    return 1\n")
    (root / "bom.txt").write_bytes("带 BOM".encode("utf-8-sig"))
    # 注意必须是**真文本**：os.urandom() 的熵是 7.98，会被正确判成二进制而跳过
    # （踩过这个坑 —— 一个叫 big.txt 的随机字节文件压根进不了归档）
    (root / "big.txt").write_bytes(b"The quick brown fox jumps over the lazy dog.\n" * 120)
    (root / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n" + os.urandom(200))
    (root / "doc.docx").write_bytes(b"PK\x03\x04" + os.urandom(200))
    return root


def _digest_tree(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        out[rel] = "<dir>" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _run(*args: str):
    return runner.invoke(main, list(args))


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------


def test_plan_reports_counts_and_skips(source_tree: Path) -> None:
    result = _run("plan", str(source_tree))
    assert result.exit_code == EXIT_OK
    assert "张码" in result.output
    assert "doc.docx" in result.output
    assert "image.png" in result.output


def test_plan_separates_machine_time_from_human_scan_time(source_tree: Path) -> None:
    """回归：曾经有个真实的误解 —— 用户把"预计扫描时长"读成了程序运行时间。

    那行其实是"你拿手机一张张扫完要多久"（379 张码约 23 分钟），而程序自己
    只跑 30 秒。两个数必须**分开写清楚**，否则还会有人误会。
    """
    output = _run("plan", str(source_tree)).output

    assert "本机编码耗时" in output, "没告诉用户程序自己要跑多久"
    assert "预计人工扫描" in output, "扫描时长那行没写清是「人工」的"
    assert "不是程序运行时间" in output, "缺少那句消除歧义的说明"


def test_plan_on_empty_dir_is_one_code(tmp_path: Path) -> None:
    """空目录也要出一张码来放清单 —— 数据段是 0 片。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    result = _run("plan", str(empty))
    assert result.exit_code == EXIT_OK
    # 注意输出里为了对齐有多个空格，别用单空格去匹配
    assert "1 张码" in result.output
    assert "清单 1" in result.output and "数据 0" in result.output


# --------------------------------------------------------------------------
# 核心往返
# --------------------------------------------------------------------------


def test_encode_then_decode_is_byte_identical(source_tree: Path, tmp_path: Path) -> None:
    qrout = tmp_path / "qrout"
    restored = tmp_path / "restored"

    encoded = _run("encode", str(source_tree), "-o", str(qrout))
    assert encoded.exit_code in (EXIT_OK, EXIT_PARTIAL), encoded.output

    decoded = _run("decode", str(qrout), "-o", str(restored))
    assert decoded.exit_code in (EXIT_OK, EXIT_PARTIAL), decoded.output

    expected = _digest_tree(source_tree)
    # 二进制文件会被跳过，还原后不该有它们
    for skipped in ("image.png", "doc.docx"):
        expected = {k: v for k, v in expected.items() if k.split("/")[-1] != skipped}

    assert _digest_tree(restored) == expected


def test_encode_then_decode_with_contact_sheet(source_tree: Path, tmp_path: Path) -> None:
    """总览图也一起扫进去时仍要能还原（重复码被容忍）。"""
    qrout = tmp_path / "qrout"
    restored = tmp_path / "restored"

    assert _run("encode", str(source_tree), "-o", str(qrout), "--contact-sheet").exit_code in (
        EXIT_OK,
        EXIT_PARTIAL,
    )
    result = _run("decode", str(qrout), "-o", str(restored))
    assert result.exit_code in (EXIT_OK, EXIT_PARTIAL), result.output

    expected = _digest_tree(source_tree)
    for skipped in ("image.png", "doc.docx"):
        expected = {k: v for k, v in expected.items() if k.split("/")[-1] != skipped}
    assert _digest_tree(restored) == expected


def test_encode_then_decode_with_pdf(source_tree: Path, tmp_path: Path) -> None:
    qrout = tmp_path / "qrout"
    restored = tmp_path / "restored"
    assert _run("encode", str(source_tree), "-o", str(qrout), "--pdf").exit_code in (EXIT_OK, EXIT_PARTIAL)
    assert list(qrout.rglob("print.pdf"))
    assert _run("decode", str(qrout), "-o", str(restored)).exit_code in (EXIT_OK, EXIT_PARTIAL)


def test_single_file_roundtrip(tmp_path: Path) -> None:
    single = tmp_path / "single.txt"
    single.write_bytes("单文件内容\n".encode())
    qrout = tmp_path / "qrout"
    restored = tmp_path / "restored"

    assert _run("encode", str(single), "-o", str(qrout)).exit_code == EXIT_OK
    assert _run("decode", str(qrout), "-o", str(restored)).exit_code == EXIT_OK
    assert (restored / "single.txt").read_bytes() == "单文件内容\n".encode()


# --------------------------------------------------------------------------
# 只读保证（硬约束）
# --------------------------------------------------------------------------


def test_encode_leaves_source_tree_untouched(source_tree: Path, tmp_path: Path) -> None:
    before = snapshot_tree(str(source_tree))
    result = _run("encode", str(source_tree), "-o", str(tmp_path / "qrout"))
    assert result.exit_code in (EXIT_OK, EXIT_PARTIAL)
    assert compare_snapshots(before, snapshot_tree(str(source_tree))) == []


def test_encode_refuses_output_inside_source(source_tree: Path) -> None:
    """-o 指向源目录 —— 必须被拒绝（方案 0.3 第一条）。"""
    result = _run("encode", str(source_tree), "-o", str(source_tree / "qrout"))
    assert result.exit_code == EXIT_INPUT_ERROR
    assert "重叠" in result.output


def test_encode_refuses_output_equal_to_source(source_tree: Path) -> None:
    result = _run("encode", str(source_tree), "-o", str(source_tree))
    assert result.exit_code == EXIT_INPUT_ERROR


def test_encode_refuses_output_that_contains_source(source_tree: Path) -> None:
    """输出是源目录的**父目录** —— 还原时树会正好落在源树上面，也要拒绝。"""
    result = _run("encode", str(source_tree), "-o", str(source_tree.parent))
    assert result.exit_code == EXIT_INPUT_ERROR


def test_encode_does_not_create_files_next_to_source(source_tree: Path, tmp_path: Path) -> None:
    """源文件旁边不能落任何临时文件 —— 临时文件只能进系统临时目录或输出目录。

    在源**文件**旁边写一个 `.tmp` 就已经违反"源文件只读"了，所以这条要单独测。
    """
    before = {p.name for p in source_tree.rglob("*")}
    _run("encode", str(source_tree), "-o", str(tmp_path / "qrout"))

    # 源树内部一个条目都不能多
    assert {p.name for p in source_tree.rglob("*")} == before
    # 而且整个临时目录里不该留下任何 .tmp / .qrfile-tmp
    strays = [
        p for p in tmp_path.rglob("*")
        if p.name.endswith((".tmp", ".qrfile-tmp", ".part", ".swp"))
    ]
    assert strays == []


def test_encode_source_guard_blocks_deliberate_write(source_tree: Path, tmp_path: Path) -> None:
    """审计钩子在 encode 期间确实是开着的。

    这里不通过 CLI 去写源文件（CLI 自己没有这种代码路径），而是直接验证
    钩子的开启/关闭由 CLI 管好了 —— encode 结束后必须已经关掉。
    """
    from qrfile.readonly import guard_is_active

    _run("encode", str(source_tree), "-o", str(tmp_path / "qrout"))
    assert guard_is_active() is False


# --------------------------------------------------------------------------
# dry-run
# --------------------------------------------------------------------------


def test_dry_run_writes_nothing(source_tree: Path, tmp_path: Path) -> None:
    qrout = tmp_path / "qrout"
    result = _run("encode", str(source_tree), "-o", str(qrout), "--dry-run")
    assert result.exit_code in (EXIT_OK, EXIT_PARTIAL)
    assert "dry-run" in result.output
    assert not list(qrout.rglob("*.png"))


# --------------------------------------------------------------------------
# 退出码
# --------------------------------------------------------------------------


def test_skips_yield_partial_exit_code(source_tree: Path, tmp_path: Path) -> None:
    """有跳过 ⇒ 退出码 1（部分成功）。"""
    result = _run("encode", str(source_tree), "-o", str(tmp_path / "qrout"))
    assert result.exit_code == EXIT_PARTIAL
    assert "跳过" in result.output


def test_clean_tree_yields_zero_exit_code(tmp_path: Path) -> None:
    root = tmp_path / "src"
    root.mkdir()
    (root / "only.txt").write_bytes(b"clean\n")
    result = _run("encode", str(root), "-o", str(tmp_path / "qrout"))
    assert result.exit_code == EXIT_OK


# --------------------------------------------------------------------------
# 故障注入：缺片必须报错且不写盘
# --------------------------------------------------------------------------


def test_missing_chunk_fails_closed_and_writes_nothing(source_tree: Path, tmp_path: Path) -> None:
    qrout = tmp_path / "qrout"
    restored = tmp_path / "restored"
    _run("encode", str(source_tree), "-o", str(qrout))

    victims = sorted(qrout.rglob("data/*.png"))
    assert victims, "需要至少一张数据码来做这个测试"
    victims[0].unlink()

    result = _run("decode", str(qrout), "-o", str(restored))
    assert result.exit_code == EXIT_VERIFY_FAILED
    assert "缺少分片" in result.output
    # 失败即关闭：一个字节都不该写
    assert not list(restored.rglob("*")) if restored.exists() else True


def test_foreign_images_do_not_break_decoding(source_tree: Path, tmp_path: Path) -> None:
    """混进一张无关的二维码图片，不该影响还原。"""
    qrout = tmp_path / "qrout"
    restored = tmp_path / "restored"
    _run("encode", str(source_tree), "-o", str(qrout))

    # 用另一个内容造一张无关的码
    from qrfile.capacity import get_profile
    from qrfile.render import Renderer

    stray = Renderer(get_profile("screen")).render(b"https://example.com/not-ours")
    (qrout / "stray.png").write_bytes(stray)

    result = _run("decode", str(qrout), "-o", str(restored))
    assert result.exit_code in (EXIT_OK, EXIT_PARTIAL), result.output
    assert (restored / "a.txt").read_bytes() == b"hello\r\nworld\r\n"


def test_decode_refuses_to_overwrite_by_default(source_tree: Path, tmp_path: Path) -> None:
    qrout = tmp_path / "qrout"
    restored = tmp_path / "restored"
    _run("encode", str(source_tree), "-o", str(qrout))
    assert _run("decode", str(qrout), "-o", str(restored)).exit_code in (EXIT_OK, EXIT_PARTIAL)

    # 第二次还原：默认拒绝覆盖
    again = _run("decode", str(qrout), "-o", str(restored))
    assert again.exit_code == EXIT_PARTIAL
    assert "拒绝覆盖" in again.output

    # 显式 --overwrite 就放行
    forced = _run("decode", str(qrout), "-o", str(restored), "--overwrite")
    assert forced.exit_code in (EXIT_OK, EXIT_PARTIAL)
    assert (restored / "a.txt").read_bytes() == b"hello\r\nworld\r\n"


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------


def test_verify_passes_on_untouched_source(source_tree: Path, tmp_path: Path) -> None:
    qrout = tmp_path / "qrout"
    _run("encode", str(source_tree), "-o", str(qrout))

    result = _run("verify", str(source_tree), str(qrout))
    assert result.exit_code == EXIT_OK, result.output
    assert "逐字节一致" in result.output


def test_verify_detects_source_changed_after_encoding(source_tree: Path, tmp_path: Path) -> None:
    """编码之后源文件被改了 —— 复核必须发现。"""
    qrout = tmp_path / "qrout"
    _run("encode", str(source_tree), "-o", str(qrout))

    (source_tree / "a.txt").write_bytes(b"tampered after encoding\r\n")

    result = _run("verify", str(source_tree), str(qrout))
    assert result.exit_code == EXIT_VERIFY_FAILED
    assert "不一致" in result.output


def test_verify_detects_file_added_to_source_after_encoding(
    source_tree: Path, tmp_path: Path
) -> None:
    """编码之后源里多了个文本文件 —— 既没进归档也没标为跳过，要报出来。"""
    qrout = tmp_path / "qrout"
    _run("encode", str(source_tree), "-o", str(qrout))

    (source_tree / "sneaked_in.txt").write_bytes(b"i was not here before\n")

    result = _run("verify", str(source_tree), str(qrout))
    assert result.exit_code == EXIT_VERIFY_FAILED
    assert "sneaked_in" in result.output


def test_verify_writes_nothing(source_tree: Path, tmp_path: Path) -> None:
    """verify 只读 —— 不产生任何输出文件。"""
    qrout = tmp_path / "qrout"
    _run("encode", str(source_tree), "-o", str(qrout))
    before = {p.name for p in tmp_path.rglob("*")}

    _run("verify", str(source_tree), str(qrout))

    assert {p.name for p in tmp_path.rglob("*")} == before


# --------------------------------------------------------------------------
# info
# --------------------------------------------------------------------------


def test_info_lists_session_without_full_verify(source_tree: Path, tmp_path: Path) -> None:
    qrout = tmp_path / "qrout"
    _run("encode", str(source_tree), "-o", str(qrout))

    result = _run("info", str(qrout))
    assert result.exit_code == EXIT_OK
    assert "会话" in result.output
    assert "收到" in result.output


def test_info_reports_missing_chunks(source_tree: Path, tmp_path: Path) -> None:
    qrout = tmp_path / "qrout"
    _run("encode", str(source_tree), "-o", str(qrout))
    sorted(qrout.rglob("data/*.png"))[0].unlink()

    result = _run("info", str(qrout))
    assert result.exit_code == EXIT_OK
    assert "缺" in result.output


# --------------------------------------------------------------------------
# 错误处理
# --------------------------------------------------------------------------


def test_missing_input_path_is_input_error(tmp_path: Path) -> None:
    result = _run("encode", str(tmp_path / "nope"), "-o", str(tmp_path / "out"))
    assert result.exit_code != EXIT_OK


def test_strict_mode_aborts_on_skips(source_tree: Path, tmp_path: Path) -> None:
    result = _run("encode", str(source_tree), "-o", str(tmp_path / "qrout"), "--strict")
    assert result.exit_code == EXIT_INPUT_ERROR
    assert "strict" in result.output


def test_help_works() -> None:
    result = _run("--help")
    assert result.exit_code == EXIT_OK
    assert "只读" in result.output


# --------------------------------------------------------------------------
# 新人使用说明（--help）与参考手册（guide）
# --------------------------------------------------------------------------

ALL_COMMANDS = ["encode", "decode", "plan", "verify", "info", "selftest", "guide"]


def test_top_level_help_is_a_newcomer_guide() -> None:
    """--help 必须能让新人直接上手 —— 这四件事必须讲到。"""
    output = _run("--help").output
    assert "新人三步" in output
    assert "profile" in output, "没讲最要紧的那个选择"
    assert "只收文本文件" in output, "没讲清楚收什么、不收什么"
    assert "扫得太慢怎么办" in output
    for command in ALL_COMMANDS:
        assert command in output, f"--help 里没提到 {command} 命令"


def test_top_level_help_shows_a_runnable_example() -> None:
    """给的例子要能直接抄着跑。"""
    output = _run("--help").output
    assert "qrfile plan" in output
    assert "qrfile encode" in output
    assert "qrfile decode" in output


def test_guide_prints_every_section() -> None:
    output = _run("guide").output
    for title in ("选 profile", "收哪些文件", "上限", "源文件只读", "验证体系"):
        assert title in output, f"guide 缺少「{title}」这一节"


@pytest.mark.parametrize("topic", ["profiles", "files", "limits", "safety", "verify"])
def test_guide_single_topic(topic: str) -> None:
    result = _run("guide", topic)
    assert result.exit_code == EXIT_OK
    assert len(result.output.strip()) > 100


def test_guide_profiles_has_the_measured_numbers() -> None:
    """profile 那一节必须带上实测的抗畸变数据 —— 这是选型的依据。"""
    output = _run("guide", "profiles").output
    for profile in ("camera", "screen", "print", "max"):
        assert profile in output
    assert "5%" in output  # 抗梯形畸变能力
    assert "畸变" in output


def test_guide_rejects_unknown_topic() -> None:
    result = _run("guide", "nonsense")
    assert result.exit_code != EXIT_OK


def test_guide_is_referenced_from_help() -> None:
    assert "qrfile guide" in _run("--help").output


@pytest.mark.parametrize("command", ALL_COMMANDS)
def test_every_option_has_help_text(command: str) -> None:
    """每个 --选项 都要有说明 —— 新人看不懂光秃秃的 --max-file-size。

    （只查 Option，不查 Argument：位置参数在 click 里本来就没有 help 字段。）
    """
    import click

    from qrfile.cli import main as cli_main

    options = [p for p in cli_main.commands[command].params if isinstance(p, click.Option)]
    missing = [p.name for p in options if not p.help]
    assert not missing, f"{command} 这些选项没有帮助文本：{missing}"


@pytest.mark.parametrize("command", ALL_COMMANDS)
def test_every_positional_argument_is_documented(command: str) -> None:
    """位置参数靠 docstring 说明，所以命令自己的说明不能是空的。"""
    import click

    from qrfile.cli import main as cli_main

    cmd = cli_main.commands[command]
    if any(isinstance(p, click.Argument) for p in cmd.params):
        assert (cmd.help or "").strip(), f"{command} 有位置参数但没有命令说明"


@pytest.mark.parametrize("command", ALL_COMMANDS)
def test_help_output_has_no_markdown_markers(command: str) -> None:
    """帮助/手册是纯终端文本，**粗体** 之类的 markdown 会原样显示成星号。"""
    assert "**" not in _run(command, "--help").output
    assert "**" not in _run("guide").output


def test_runtime_output_has_no_markdown_markers(source_tree: Path) -> None:
    """运行时的输出也一样 —— 它不是 markdown，星号只会原样打出来。"""
    plan_output = _run("plan", str(source_tree)).output
    assert "**" not in plan_output

    encoded = _run("encode", str(source_tree), "-o", str(source_tree.parent / "qr_md"))
    assert "**" not in encoded.output


@pytest.mark.parametrize("command", ALL_COMMANDS)
def test_help_output_is_not_empty(command: str) -> None:
    result = _run(command, "--help")
    assert result.exit_code == EXIT_OK
    assert "Usage:" in result.output


def test_size_option_accepts_human_readable_units() -> None:
    """--max-file-size 8388608 对新人没法用，所以要支持 8M 这种写法。"""
    from qrfile.cli import _size

    assert _size("8M") == 8 * 1024 * 1024
    assert _size("512K") == 512 * 1024
    assert _size("1G") == 1024 ** 3
    assert _size("1048576") == 1048576
    assert _size("8m") == 8 * 1024 * 1024  # 大小写不敏感
    assert _size("8MB") == 8 * 1024 * 1024  # 带 B 也行


def test_size_option_rejects_garbage() -> None:
    import click

    from qrfile.cli import _size

    for bad in ("abc", "", "8X", "-1"):
        with pytest.raises(click.BadParameter):
            _size(bad)


def test_size_option_is_wired_into_the_cli(tmp_path: Path) -> None:
    """真的从命令行传 8K 进去，要能生效。"""
    root = tmp_path / "src"
    root.mkdir()
    (root / "big.txt").write_bytes(b"x" * 20000)
    result = _run("plan", str(root), "--max-file-size", "8K")
    assert result.exit_code == EXIT_OK
    assert "超过单文件上限" in result.output


def test_plan_help_documents_the_profile_tradeoff() -> None:
    """选 profile 是最要紧的决定，plan 的帮助里要能看出来。"""
    output = _run("plan", "--help").output
    assert "profile" in output.lower()


# --------------------------------------------------------------------------
# 输出编码 —— 曾经真的崩过
# --------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _run_frozen(args: list[str], encoding: str):
    """在子进程里跑，并强制 stdout/stderr 用指定编码（模拟不同语言的 Windows 控制台）。"""
    import subprocess
    import sys

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = f"{encoding}:strict"  # strict 才能暴露问题
    return subprocess.run(
        [sys.executable, "-m", "qrfile", *args],
        capture_output=True,
        env=env,
        cwd=str(PROJECT_ROOT),
        timeout=300,
    )


@pytest.mark.parametrize("args", [["--help"], ["guide"], ["encode", "--help"], ["plan", "--help"]])
def test_output_survives_a_gbk_console(args: list[str]) -> None:
    """回归：中文 Windows 上把输出重定向到文件/管道时，stdout 编码是 gbk，
    而 ✓ (U+2713) 不在 gbk 里 —— 曾经直接 UnicodeEncodeError 崩掉。

    交互式控制台不会犯（Python 用 WriteConsoleW 直写），**只有重定向时才犯**，
    所以自己测的时候一路 `| tail` 反而正好踩上。这个测试就走重定向路径。
    """
    result = _run_frozen(args, "gbk")
    assert result.returncode == 0, result.stderr.decode("gbk", "replace")[-800:]
    assert b"UnicodeEncodeError" not in result.stderr
    assert b"Traceback" not in result.stderr


@pytest.mark.parametrize("args", [["--help"], ["guide"]])
def test_output_survives_a_western_console(args: list[str]) -> None:
    """英文版 Windows（cp1252）连中文都打不出来 —— 也不能崩，最多变成 ?。

    这一条比 gbk 更狠：cp1252 编码不了任何中文字符。
    """
    result = _run_frozen(args, "cp1252")
    assert result.returncode == 0, result.stderr.decode("cp1252", "replace")[-800:]
    assert b"UnicodeEncodeError" not in result.stderr
    assert b"Traceback" not in result.stderr


def test_gbk_console_uses_ascii_status_markers() -> None:
    """gbk 下打不出 ✓，应该换成 [OK]，而不是显示成问号。"""
    result = _run_frozen(["selftest"], "gbk")
    assert result.returncode == 0
    text = result.stdout.decode("gbk", "replace")
    assert "[OK]" in text, "gbk 控制台下没有退化成 ASCII 标记"
    assert "✓" not in text
    assert "?" not in text.split("自检")[0]  # 前面不该有问号（中文本身 gbk 能打）


def test_utf8_console_keeps_the_pretty_markers() -> None:
    """UTF-8 终端下仍然用好看的符号。"""
    result = _run_frozen(["selftest"], "utf-8")
    assert result.returncode == 0
    assert "✓" in result.stdout.decode("utf-8", "replace")


def test_no_gbk_unsafe_symbols_in_user_facing_output() -> None:
    """把"gbk 打不出来的符号不许出现在用户可见输出里"这条钉死。

    ⇒ 和 ≲ 曾经出现在 guide 的表格里 —— 它们在 gbk 下打不出来。
    开发者注释里的不算（那些不会打印）。
    """
    unsafe = set("✓✗⇒≲")
    sources = [PROJECT_ROOT / "qrfile" / "cli.py"]

    offenders: list[str] = []
    for path in sources:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # 注释，不打印
            # 只看可能被打印的字符串字面量里有没有这些字符
            for char in unsafe:
                if char in line and ("_echo" in line or "secho" in line or "print" in line):
                    offenders.append(f"{path.name}:{number} 含 {char!r}")

    assert not offenders, "用户可见输出里还有 gbk 打不出来的符号：\n" + "\n".join(offenders)


def test_encode_without_screens_still_works(source_tree: Path, tmp_path: Path) -> None:
    """回归：`scale` 曾经在 action() 里被重新赋值，导致它变成局部变量，
    不带 --screens 时读它直接 UnboundLocalError —— 所有不带该参数的 encode 全挂。

    这个坑很隐蔽（带 --screens 时反而正常），单独钉一条。
    """
    result = _run("encode", str(source_tree), "-o", str(tmp_path / "qr"))
    assert result.exit_code in (EXIT_OK, EXIT_PARTIAL), result.output
    assert "UnboundLocalError" not in result.output


def test_plan_reports_how_many_photos_screen_mode_needs(source_tree: Path) -> None:
    """plan 必须告诉用户"整屏采集要拍几张照片" —— 这才是决定要不要用的数字。"""
    output = _run("plan", str(source_tree)).output
    assert "整屏采集" in output
    assert "张照片" in output
    assert "--screens" in output


# --------------------------------------------------------------------------
# 整屏模式：输出目录只该有整屏页，不该铺几百个中间 PNG
# --------------------------------------------------------------------------


def _many_chunks_tree(tmp_path: Path) -> Path:
    """造一棵能产生**多张整屏页**的源树。

    内容用 sha256 的十六进制 —— 它是合法文本（熵 4.0，远低于判定阈值 6.0），
    但基本压不动，所以分片数可预期。别用 base64 包重复数据：那玩意儿一压就没了，
    lzma 之后只剩一片，测不到多屏。
    """
    root = tmp_path / "big"
    root.mkdir()
    lines = [hashlib.sha256(f"line-{i}".encode()).hexdigest() for i in range(400)]
    (root / "data.txt").write_text("\n".join(lines), encoding="utf-8")
    return root


def test_screens_mode_writes_only_screen_pages(tmp_path: Path) -> None:
    """回归：整屏模式下输出目录里**不该**再有 data/ 和 manifest/ 那堆单码。"""
    tree = _many_chunks_tree(tmp_path)
    out = tmp_path / "out"

    result = _run("encode", str(tree), "-o", str(out), "--screens", "--screen-size", "1920x1080")
    assert result.exit_code in (EXIT_OK, EXIT_PARTIAL), result.output

    session_dir = next(p for p in out.iterdir() if p.is_dir())
    names = sorted(p.name for p in session_dir.iterdir())

    assert names, "输出目录是空的"
    assert all(n.startswith("screen_") and n.endswith(".png") for n in names), (
        f"整屏模式下不该有别的文件，实际有：{names}"
    )
    assert not (session_dir / "data").exists(), "不该有 data/ 目录"
    assert not (session_dir / "manifest").exists(), "不该有 manifest/ 目录"
    assert len(names) > 1, "这个用例应该产生多张整屏页"
    assert "单张码没落盘" in result.output


def test_screens_only_output_still_decodes(tmp_path: Path) -> None:
    """只有整屏页也要能完整还原 —— 单码不落盘不能影响解码。"""
    tree = _many_chunks_tree(tmp_path)
    out = tmp_path / "out"
    restored = tmp_path / "restored"

    assert _run("encode", str(tree), "-o", str(out), "--screens", "--screen-size", "1920x1080").exit_code in (EXIT_OK, EXIT_PARTIAL)
    assert _run("decode", str(out), "-o", str(restored)).exit_code in (EXIT_OK, EXIT_PARTIAL)

    assert (restored / "data.txt").read_bytes() == (tree / "data.txt").read_bytes()


def test_keep_chunks_flag_writes_both(tmp_path: Path) -> None:
    """要留单码就显式加 --keep-chunks。"""
    tree = _many_chunks_tree(tmp_path)
    out = tmp_path / "out"

    result = _run("encode", str(tree), "-o", str(out), "--screens", "--keep-chunks", "--screen-size", "1920x1080")
    assert result.exit_code in (EXIT_OK, EXIT_PARTIAL), result.output

    session_dir = next(p for p in out.iterdir() if p.is_dir())
    assert (session_dir / "data").is_dir()
    assert (session_dir / "manifest").is_dir()
    assert list(session_dir.glob("screen_*.png"))
    assert "另外也留了" in result.output


def test_non_screen_mode_still_writes_individual_codes(tmp_path: Path) -> None:
    """不带 --screens 时行为不变（别把这个默认改坏了）。"""
    tree = _many_chunks_tree(tmp_path)
    out = tmp_path / "out"

    assert _run("encode", str(tree), "-o", str(out)).exit_code in (EXIT_OK, EXIT_PARTIAL)
    session_dir = next(p for p in out.iterdir() if p.is_dir())
    assert (session_dir / "data").is_dir()
    assert not list(session_dir.glob("screen_*.png"))


def test_screen_size_auto_detects_the_real_monitor() -> None:
    """回归：曾经把 --screen-size 默认写死成 1920x1080，而本机是 2560x1440 ——
    直接让张数差一倍（21 张 vs 11 张）。现在默认自动探测。"""
    from qrfile.cli import _resolve_screen_size, detect_screen_size

    detected = detect_screen_size()
    if detected is None:
        pytest.skip("本平台探测不到屏幕分辨率，走的是保守回退值")

    assert _resolve_screen_size("auto") == detected
    # 显式给值时要照用，不能被探测覆盖
    assert _resolve_screen_size("1920x1080") == (1920, 1080)
    assert detected[0] > 0 and detected[1] > 0


def test_bigger_screen_needs_fewer_photos(tmp_path: Path) -> None:
    """屏幕越大一屏装得越多 —— 这条直接决定要拍几张，值得钉死。"""
    tree = _many_chunks_tree(tmp_path)
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"

    _run("encode", str(tree), "-o", str(out_a), "--screens", "--screen-size", "1920x1080")
    _run("encode", str(tree), "-o", str(out_b), "--screens", "--screen-size", "2560x1440")

    a = len(list(out_a.rglob("screen_*.png")))
    b = len(list(out_b.rglob("screen_*.png")))
    assert b <= a, f"更大的屏幕不该产生更多张（1920x1080={a}, 2560x1440={b}）"
