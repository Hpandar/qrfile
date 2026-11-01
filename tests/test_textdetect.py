"""文本 / 二进制判定。

重点是**两个方向都不能错得太离谱**：
  - 把文本判成二进制 → 文件被静默跳过（不好，但会明确报告）
  - 把二进制判成文本 → 白费力气编码一堆垃圾（也不好）

而最危险的是"latin-1 能解码任意字节序列"这个陷阱 —— 它会让任何东西都"看起来
像文本"。熵护栏就是专门拦它的，所以要重点测。
"""

from __future__ import annotations

import os
import zlib
from pathlib import Path

import pytest

from qrfile.textdetect import (
    MAX_TEXT_ENTROPY,
    SAMPLE_SIZE,
    classify,
    control_ratio,
    detect,
    shannon_entropy,
)

CHINESE = "这是一段中文测试文本，用来验证 GBK 和 UTF-8 都能被正确识别。" * 40


# --------------------------------------------------------------------------
# 必须判为文本
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("纯 ASCII", b"hello world\n"),
        ("UTF-8 无 BOM", "中文 UTF-8 内容\n".encode()),
        ("UTF-8 带 BOM", "中文 UTF-8 带 BOM".encode("utf-8-sig")),
        ("UTF-16LE 带 BOM", "hello 世界".encode("utf-16")),
        ("UTF-16BE 带 BOM", "hello 世界".encode("utf-16-be").join([b"\xfe\xff", b""])),
        ("UTF-32LE 带 BOM", "hello".encode("utf-32")),
        ("GBK 中文", CHINESE.encode("gbk")),
        ("GB18030 中文", CHINESE.encode("gb18030")),
        ("CRLF 换行", b"line one\r\nline two\r\n"),
        ("CR 换行", b"line one\rline two\r"),
        ("混合换行", b"a\r\nb\nc\r"),
        ("带 tab 和 ESC 的日志", b"\x1b[31merror\x1b[0m\tat line 1\n"),
        ("JSON", b'{"a": 1, "b": [true, null]}\n'),
        ("XML", b'<?xml version="1.0"?>\n<root/>\n'),
        ("SVG（是文本不是图片）", b'<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>'),
        ("base64 长串", b"aGVsbG8gd29ybGQ=" * 200),
        ("十六进制转储", b"deadbeef" * 500),
        ("空文件", b""),
        ("只有一个换行", b"\n"),
        ("全是空格的缩进代码", b"    def f():\n        return 1\n"),
    ],
)
def test_classified_as_text(name: str, data: bytes) -> None:
    verdict = classify(data, filename="sample")
    assert verdict.is_text, f"{name} 被判成了二进制：{verdict.reason}"
    assert verdict.skip_reason is None


# --------------------------------------------------------------------------
# 必须判为二进制
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("PNG", b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + os.urandom(200)),
        ("JPEG", b"\xff\xd8\xff\xe0" + os.urandom(200)),
        ("GIF", b"GIF89a" + os.urandom(200)),
        ("PDF", b"%PDF-1.7\n" + os.urandom(200)),
        ("ZIP", b"PK\x03\x04" + os.urandom(200)),
        ("docx（本质是 zip）", b"PK\x03\x04" + os.urandom(200)),
        ("GZIP", b"\x1f\x8b\x08" + os.urandom(200)),
        ("7z", b"7z\xbc\xaf\x27\x1c" + os.urandom(200)),
        ("ELF", b"\x7fELF\x02\x01\x01" + os.urandom(200)),
        ("SQLite", b"SQLite format 3\x00" + os.urandom(200)),
        ("OLE2（旧 doc）", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + os.urandom(200)),
        ("WOFF2", b"wOF2" + os.urandom(200)),
        ("含 NUL 的无 magic 数据", b"some text\x00then binary" + b"\x01\x02" * 50),
        ("纯随机字节", os.urandom(4096)),
        ("zlib 压缩流", zlib.compress(CHINESE.encode("utf-8"), 9)),
        ("zlib 压缩 ASCII", zlib.compress(b"the quick brown fox " * 400, 9)),
    ],
)
def test_classified_as_binary(name: str, data: bytes) -> None:
    verdict = classify(data, filename="sample.bin")
    assert not verdict.is_text, f"{name} 被判成了文本（{verdict.reason}）"
    assert verdict.skip_reason is not None
    assert verdict.skip_reason.startswith("binary:")


def test_skip_reason_names_the_format() -> None:
    """跳过原因要说清楚是什么格式，不能只说"二进制"。"""
    verdict = classify(b"\x89PNG\r\n\x1a\n" + os.urandom(100))
    assert "image/png" in verdict.reason
    assert "89504e47" in verdict.reason  # magic 的十六进制

    zip_verdict = classify(b"PK\x03\x04" + os.urandom(100))
    assert "docx" in zip_verdict.reason  # OOXML 的坑要提示到


# --------------------------------------------------------------------------
# 熵护栏 —— 防"latin-1 能解码一切"这个陷阱
# --------------------------------------------------------------------------


def test_entropy_threshold_sits_between_base64_and_compressed() -> None:
    """这个断言是标定结果的固化，防止有人随手改阈值把护栏改坏。

    实测（本机）：base64 文本 5.60、zlib 压缩 6.37。阈值必须在两者之间。
    """
    base64_text = b"aGVsbG8gd29ybGQ=" * 200
    compressed = zlib.compress(CHINESE.encode("utf-8"), 9)
    assert shannon_entropy(base64_text[:8192]) < MAX_TEXT_ENTROPY
    assert shannon_entropy(compressed[:8192]) > MAX_TEXT_ENTROPY


def test_high_entropy_data_is_not_rescued_by_latin1() -> None:
    """核心回归：随机字节恰好是合法的 latin-1，绝不能被当成文本收下。"""
    noise = os.urandom(4096)
    noise.decode("latin-1")  # 证明它"能解码" —— 这正是陷阱所在
    assert classify(noise).is_text is False


def test_compressed_chinese_is_rejected() -> None:
    """压缩后的中文熵 6.37，必须拦住。"""
    compressed = zlib.compress(CHINESE.encode("utf-8"), 9)
    assert classify(compressed).is_text is False


def test_gbk_chinese_entropy_is_comfortably_below_threshold() -> None:
    """GBK 中文熵实测 5.16 —— 曾经担心它逼近阈值导致误判，这里固化下来。"""
    entropy = shannon_entropy(CHINESE.encode("gbk")[:8192])
    assert entropy < MAX_TEXT_ENTROPY - 0.5, "GBK 中文离阈值太近了，阈值需要重新标定"


# --------------------------------------------------------------------------
# BOM 顺序（先 4 字节 UTF-32，再 2 字节 UTF-16）
# --------------------------------------------------------------------------


def test_utf32_bom_is_not_mistaken_for_utf16() -> None:
    """``FF FE`` 是 UTF-32LE BOM 的前缀 —— 顺序反了就会认错。"""
    verdict = classify("hi".encode("utf-32"))  # 开头是 FF FE 00 00
    assert verdict.encoding == "utf-32-le"


def test_utf16_bom_detected() -> None:
    assert classify("hi".encode("utf-16")).encoding == "utf-16-le"  # FF FE
    assert classify(b"\xfe\xff\x00h\x00i").encoding == "utf-16-be"


def test_utf8_bom_detected() -> None:
    assert classify("hi".encode("utf-8-sig")).encoding == "utf-8-sig"


# --------------------------------------------------------------------------
# 无 BOM 的 UTF-16/32
# --------------------------------------------------------------------------


def test_bomless_utf16le_detected() -> None:
    verdict = classify(b"".join(bytes([c, 0]) for c in b"plain ascii text here"))
    assert verdict.is_text
    assert verdict.encoding == "utf-16-le"


def test_bomless_utf16be_detected() -> None:
    verdict = classify(b"".join(bytes([0, c]) for c in b"plain ascii text here"))
    assert verdict.is_text
    assert verdict.encoding == "utf-16-be"


def test_bomless_utf32le_detected() -> None:
    verdict = classify(b"".join(bytes([c, 0, 0, 0]) for c in b"plain ascii text"))
    assert verdict.is_text
    assert verdict.encoding == "utf-32-le"


def test_bomless_utf16_chinese_is_a_known_gap() -> None:
    """已知局限：无 BOM 且非 ASCII 的 UTF-16 探不到，会被判成二进制。

    这种文件极少见；判成二进制后会明确报出来，不会静默出错。这里把行为固化，
    免得以后有人以为它是"bug"而乱改。
    """
    verdict = classify("中文内容".encode("utf-16-le"))
    assert verdict.is_text is False


# --------------------------------------------------------------------------
# 控制字符
# --------------------------------------------------------------------------


def test_control_ratio_ignores_normal_whitespace() -> None:
    assert control_ratio(b"a\tb\nc\rd\x0ce\x1bf") == 0.0


def test_control_ratio_counts_real_control_bytes() -> None:
    assert control_ratio(b"\x01\x02\x03\x04") == 1.0
    assert control_ratio(b"\x7f") == 1.0


def test_mostly_control_bytes_is_binary() -> None:
    # 注意要有足够多的控制字节，且没有 NUL、熵足够低，才能走到第 5 步
    data = bytes([1, 2, 3, 4, 5, 6, 7, 8, 0x1C, 0x1D, 0x1E, 0x1F]) * 100
    assert classify(data).is_text is False


def test_ansi_escapes_are_still_text() -> None:
    """带 ANSI 颜色的终端日志是合法文本。"""
    data = b"\x1b[1;31mFAILED\x1b[0m tests/test_x.py::test_y\n" * 50
    assert classify(data).is_text


# --------------------------------------------------------------------------
# 扩展名只在打平手时生效
# --------------------------------------------------------------------------


def test_extension_never_overrides_content_evidence() -> None:
    """一个叫 a.txt 的 PNG 仍然是二进制 —— 扩展名不能推翻内容。"""
    verdict = classify(b"\x89PNG\r\n\x1a\n" + os.urandom(100), filename="a.txt")
    assert verdict.is_text is False


def test_content_beats_extension_in_both_directions() -> None:
    """扩展名只是打平手用的提示，永远不能推翻内容证据。

    注意第二行是**刻意**的行为：一个内容是纯文本的 .exe 会被当文本收下。这没问题
    —— 它照样能字节一致地还原出来。用户真想排除就用 ``--exclude '*.exe'``。
    """
    # .txt 里装着 PNG → 仍然是二进制
    assert classify(b"\x89PNG\r\n\x1a\n" + os.urandom(100), filename="a.txt").is_text is False
    # .exe 里装着纯文本 → 仍然是文本
    assert classify(b"this is plainly text\n" * 20, filename="payload.exe").is_text is True


def test_svg_extension_is_text_not_binary() -> None:
    """SVG 是文本 —— 很容易被当成图片误排除。"""
    assert ".svg" not in __import__("qrfile.textdetect", fromlist=["x"]).BINARY_EXTENSIONS


def test_json_yaml_toml_are_not_in_binary_extensions() -> None:
    from qrfile.textdetect import BINARY_EXTENSIONS

    for ext in (".json", ".yaml", ".yml", ".toml", ".xml", ".csv", ".md"):
        assert ext not in BINARY_EXTENSIONS, f"{ext} 是文本类型，不该在二进制列表里"


def test_pickle_and_msgpack_are_binary_but_json_is_not() -> None:
    """最容易混的一组：Pickle/MsgPack 是二进制，JSON/XML/YAML 是文本。"""
    from qrfile.textdetect import BINARY_EXTENSIONS, TEXT_EXTENSIONS

    for ext in (".pickle", ".pkl", ".msgpack", ".bson", ".parquet"):
        assert ext in BINARY_EXTENSIONS
    for ext in (".json", ".xml", ".yaml", ".toml"):
        assert ext in TEXT_EXTENSIONS
        assert ext not in BINARY_EXTENSIONS


def test_ooxml_extensions_are_binary() -> None:
    from qrfile.textdetect import BINARY_EXTENSIONS

    for ext in (".docx", ".xlsx", ".pptx", ".odt", ".epub", ".jar"):
        assert ext in BINARY_EXTENSIONS


# --------------------------------------------------------------------------
# 扩展编码需要显式开启
# --------------------------------------------------------------------------


def test_big5_needs_explicit_opt_in() -> None:
    data = "這是一段繁體中文測試內容".encode("big5") * 20
    assert classify(data).is_text is False
    assert classify(data, extended_encodings=True).is_text is True


def test_latin1_guess_comes_with_a_warning() -> None:
    """低置信度的判定必须给出警告，不能悄悄收下。"""
    data = "café résumé naïve".encode("latin-1") * 40
    verdict = classify(data, extended_encodings=True)
    if verdict.is_text and verdict.encoding in {"cp1252", "latin_1", "iso8859_1"}:
        assert verdict.warning is not None
        assert verdict.confidence == "guessed"


# --------------------------------------------------------------------------
# detect() 走文件路径
# --------------------------------------------------------------------------


def test_detect_reads_from_disk(tmp_path: Path) -> None:
    src = tmp_path / "hello.txt"
    src.write_bytes("中文内容\n".encode())
    verdict = detect(src)
    assert verdict.is_text
    assert verdict.encoding == "utf-8"


def test_detect_only_reads_a_prefix(tmp_path: Path) -> None:
    """判定只看开头 8192 字节 —— 超过这个范围的内容不参与判定。

    构造：前 8192 字节全是干净的文本字节，之后才是二进制。因为只读前缀，判为文本。
    （注意这不影响字节一致性：编码时读的是完整文件。）
    """
    src = tmp_path / "mixed.txt"
    src.write_bytes(b"a" * SAMPLE_SIZE + os.urandom(50000))
    assert detect(src).is_text


def test_binary_within_the_sample_window_is_caught(tmp_path: Path) -> None:
    """反面对照：二进制落在取样窗口之内，就必须判成二进制。"""
    src = tmp_path / "mixed2.txt"
    src.write_bytes(b"valid utf-8 header\n" + os.urandom(5000))
    assert detect(src).is_text is False


def test_detect_on_empty_file(tmp_path: Path) -> None:
    src = tmp_path / "empty.txt"
    src.write_bytes(b"")
    verdict = detect(src)
    assert verdict.is_text
    assert verdict.reason == "空文件"
