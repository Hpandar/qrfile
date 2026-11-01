"""文本 / 二进制判定（方案第五章）。

**关键认识**：因为我们按裸字节编码，这个判定**只影响"收不收这个文件"，不影响
"还原出来一不一样"**。所以可以放心用启发式 —— 判错了最坏是漏收或多收一个文件，
永远不会破坏一致性。这也意味着：宁可偶尔漏收一个怪文件并明确报出来，也不要为了
"多收"而放松护栏去冒把二进制当文本的风险。

判定顺序（只读开头 8192 字节）::

    0. 空文件(0 字节)        → 文本   （很多检测器会误判，显式处理）
    1. BOM 嗅探              → 文本   （先测 4 字节 UTF-32，再测 2 字节 UTF-16）
    2. 已知二进制 magic      → 二进制 （给出精确的格式名）
    3. 含 NUL？
         ├ UTF-16/32 交错零字节模式 → 文本
         └ 否则                     → 二进制   （Git 的规则）
    4. 控制字符占比 > 1%     → 二进制   （Git convert.c 的阈值）
    5. 严格 UTF-8 解码成功   → 文本
    6. 熵 > 6.0 bit/byte     → 二进制   ← 关键护栏
    7. charset-normalizer 白名单 → 文本（低置信度会给警告）
    8. 扩展名提示（仅用于打平手）
    9. 兜底                  → 二进制

第 4 步**必须排在第 5 步前面**：所有 ASCII 控制字节（0x01-0x1F）本身都是合法
UTF-8，所以一个通篇控制字符的文件在第 5 步就会被"成功解码"成文本，永远走不到
控制字符检查。Git 的 convert.c 也是不管 UTF-8 是否合法、先看控制字符比例的。

第 6 步的熵阈值是实测标定的（2026-10，本机）：

    ==============  ======  ==========================
    样本             熵      charset-normalizer 判定
    ==============  ======  ==========================
    英文 ASCII       4.44    ascii
    UTF-8 中文       5.16    utf_8
    GBK 中文         5.16    gb18030
    base64(压缩后)   5.60    ascii      ← 文本，必须放行
    zlib 压缩        6.37    None       ← 二进制，必须拦住
    随机字节         7.98    None
    ==============  ======  ==========================

6.0 正好卡在"base64 文本(5.60)"和"压缩数据(6.37)"中间，两边余量都够。

注意这一步**只会看到非 UTF-8 的字节** —— 任何纯 ASCII / UTF-8 文件在第 4 步就
被认成文本了，根本走不到熵检查。所以"高熵的合法文本"（压缩过的 base64、超长
十六进制转储等）不会被误伤。
"""

from __future__ import annotations

import codecs
import math
import os
from dataclasses import dataclass

from charset_normalizer import from_bytes

from .readonly import read_prefix

SAMPLE_SIZE = 8192

#: 实测标定（见模块文档）。低于"base64 文本"，高于"压缩数据"。
MAX_TEXT_ENTROPY = 6.0

#: Git convert.c 的阈值，不是拍脑袋的 30%
MAX_CONTROL_RATIO = 0.01

#: 判定为"没有 BOM 的 UTF-16/32"时，零字节必须占这么大比例
_UTF32_ZERO_RANGE = (0.70, 0.85)  # 4 字节里 3 个是 0
_UTF16_ZERO_RANGE = (0.35, 0.65)  # 2 字节里 1 个是 0
_ZERO_ALIGNMENT = 0.90  # 固定位置上"是零"的比例

#: BOM 表。**顺序关键**：``FF FE`` 是 UTF-32LE BOM 的前缀，
#: 先测 2 字节的 UTF-16 会把 UTF-32 文件认成 UTF-16。
BOMS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)

#: 已知二进制格式的 magic。只用 ≥3 字节、含义明确的 ——
#: ``BM``（BMP）和 ``MZ``（PE）只有 2 字节，会把 "BMW 报告.txt" 之类的
#: 正常文本误判掉，所以不放进这张表，交给扩展名提示去处理。
BINARY_MAGICS: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"\x00\x00\x01\x00", "image/x-icon"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    (b"RIFF", "riff（webp / wav / avi）"),
    (b"8BPS", "image/vnd.adobe.photoshop"),
    (b"%PDF-", "application/pdf"),
    (b"PK\x03\x04", "zip 容器（zip / jar / docx / xlsx / pptx / odt / epub）"),
    (b"PK\x05\x06", "zip 容器（空档案）"),
    (b"PK\x07\x08", "zip 容器（分卷）"),
    (b"Rar!\x1a\x07", "application/vnd.rar"),
    (b"7z\xbc\xaf\x27\x1c", "application/x-7z-compressed"),
    (b"\x1f\x8b", "application/gzip"),
    (b"BZh", "application/x-bzip2"),
    (b"\xfd7zXZ\x00", "application/x-xz"),
    (b"\x28\xb5\x2f\xfd", "application/zstd"),
    (b"\x7fELF", "application/x-elf"),
    (b"\xca\xfe\xba\xbe", "Mach-O / Java class"),
    (b"\xcf\xfa\xed\xfe", "Mach-O 64"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "OLE2 复合文档（doc / xls / ppt 旧格式）"),
    (b"SQLite format 3\x00", "application/vnd.sqlite3"),
    (b"\x00\x01\x00\x00\x00", "font/ttf"),
    (b"OTTO", "font/otf"),
    (b"wOFF", "font/woff"),
    (b"wOF2", "font/woff2"),
    (b"OggS", "audio/ogg"),
    (b"fLaC", "audio/flac"),
    (b"ID3", "audio/mpeg"),
    (b"\x1a\x45\xdf\xa3", "video/x-matroska 或 webm"),
    (b"FLV\x01", "video/x-flv"),
    (b"\x30\x26\xb2\x75\x8e\x66\xcf\x11", "video/x-ms-wmv"),
)

#: tar 的 magic 不在开头 —— ``ustar`` 在偏移 257 处，另外单独处理
_TAR_MAGIC = b"ustar"
_TAR_OFFSET = 257

#: 明确是二进制的扩展名。**只在打平手时用作提示**，不会覆盖内容证据。
BINARY_EXTENSIONS = frozenset(
    {
        # 图片（注意：.svg 是文本，不在这里）
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tif", ".tiff",
        ".heic", ".heif", ".avif", ".psd", ".ai", ".eps", ".raw", ".cr2", ".nef",
        # 音视频
        ".mp3", ".mp4", ".mkv", ".avi", ".mov", ".wav", ".flac", ".ogg", ".webm",
        ".m4a", ".aac", ".wma", ".flv", ".mpg", ".mpeg", ".3gp",
        # 压缩包与容器
        ".zip", ".rar", ".7z", ".gz", ".bz2", ".xz", ".zst", ".tar", ".cab", ".iso",
        # Office / 电子书（本质是 zip 或 OLE）
        ".doc", ".xls", ".ppt", ".docx", ".xlsx", ".pptx",
        ".odt", ".ods", ".odp", ".epub", ".mobi",
        # 二进制文档
        ".pdf", ".db", ".sqlite", ".sqlite3", ".mdb",
        # 可执行 / 库 / 中间产物
        ".exe", ".dll", ".so", ".dylib", ".a", ".o", ".obj", ".lib", ".class",
        ".jar", ".war", ".ear", ".pyc", ".pyo", ".pyd", ".wasm", ".bin", ".dat",
        ".lnk", ".msi", ".dmg", ".img", ".vhd", ".vhdx", ".vmdk",
        # 字体
        ".ttf", ".otf", ".woff", ".woff2", ".eot", ".ttc",
        # 序列化二进制（注意：JSON / XML / YAML / TOML 是文本，**不在这里**）
        ".pickle", ".pkl", ".msgpack", ".bson", ".parquet", ".avro", ".orc",
        ".npy", ".npz", ".h5", ".hdf5", ".pb", ".pt", ".pth", ".onnx", ".safetensors",
    }
)

#: 明确是文本的扩展名。同样只在打平手时用。
TEXT_EXTENSIONS = frozenset(
    {
        ".txt", ".text", ".md", ".markdown", ".rst", ".adoc", ".org", ".tex",
        ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".java",
        ".c", ".h", ".cc", ".cpp", ".hpp", ".cxx", ".cs", ".go", ".rs", ".rb",
        ".php", ".pl", ".pm", ".lua", ".sh", ".bash", ".zsh", ".fish", ".ps1",
        ".bat", ".cmd", ".sql", ".r", ".jl", ".swift", ".kt", ".kts", ".scala",
        ".html", ".htm", ".xhtml", ".css", ".scss", ".sass", ".less", ".svg",
        ".xml", ".xsl", ".xsd", ".dtd", ".json", ".json5", ".jsonl", ".ndjson",
        ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".properties", ".env",
        ".csv", ".tsv", ".log", ".diff", ".patch", ".po", ".pot", ".srt", ".vtt",
        ".gitignore", ".gitattributes", ".editorconfig", ".dockerignore",
        ".gradle", ".cmake", ".mk", ".makefile", ".lock", ".sum", ".mod",
    }
)

#: 默认就接受的文本编码。**中文环境（本机 locale = cp936）是刚需**。
DEFAULT_TEXT_ENCODINGS = frozenset(
    {
        "ascii",
        "utf_8",
        "utf_8_sig",
        "gb18030",
        "gbk",
        "gb2312",
        "cp936",
        "cp1252",
        "latin_1",
        "iso8859_1",
    }
)

#: 需要显式 ``--text-encodings`` 才接受的（避免误判）。
EXTENDED_TEXT_ENCODINGS = frozenset(
    {
        "big5",
        "cp950",
        "shift_jis",
        "cp932",
        "euc_jp",
        "euc_kr",
        "cp949",
        "cp1250",
        "cp1251",
        "cp1253",
        "cp1254",
        "iso8859_2",
        "iso8859_5",
        "iso8859_7",
        "iso8859_9",
        "iso8859_15",
        "koi8_r",
        "mac_roman",
    }
)

#: 统计"控制字符"时要排除的字节。``\x1b``(ESC) 也排除 —— 带 ANSI 颜色的
#: 终端日志是合法文本，很常见。
_ALLOWED_CONTROL = frozenset({0x09, 0x0A, 0x0C, 0x0D, 0x1B})
_IS_CONTROL = bytes(
    1 if (b < 0x20 and b not in _ALLOWED_CONTROL) or b == 0x7F else 0 for b in range(256)
)


@dataclass(frozen=True, slots=True)
class TextVerdict:
    """一个文件的文本判定结果。

    ``encoding`` **只用于报告**，绝不参与任何编码转换 —— 我们始终按裸字节处理。
    """

    is_text: bool
    encoding: str | None
    reason: str
    confidence: str = "certain"  # "certain" | "likely" | "guessed"
    warning: str | None = None

    @property
    def skip_reason(self) -> str | None:
        """非文本时给用户看的一句话（进清单的 ``skipped`` 字段）。"""
        return None if self.is_text else f"binary:{self.reason}"


def shannon_entropy(data: bytes) -> float:
    """样本的香农熵（bit/byte）。"""
    if not data:
        return 0.0
    counts = [0] * 256
    for byte in data:
        counts[byte] += 1
    size = len(data)
    total = 0.0
    for count in counts:
        if count:
            probability = count / size
            total -= probability * math.log2(probability)
    return total


def control_ratio(data: bytes) -> float:
    """不可打印控制字符的占比（排除 tab / LF / FF / CR / ESC）。"""
    if not data:
        return 0.0
    hits = sum(_IS_CONTROL[b] for b in data)
    return hits / len(data)


def _match_bom(data: bytes) -> str | None:
    for bom, name in BOMS:  # 顺序已在表里排好
        if data.startswith(bom):
            return name
    return None


def _match_binary_magic(data: bytes) -> str | None:
    for magic, name in BINARY_MAGICS:
        if data.startswith(magic):
            return f"{name} (magic {magic.hex()})"
    if data[_TAR_OFFSET : _TAR_OFFSET + 5] == _TAR_MAGIC:
        return f"application/x-tar (magic ustar@{_TAR_OFFSET})"
    return None


def _detect_bomless_utf16_32(data: bytes) -> str | None:
    """没有 BOM 的 UTF-16 / UTF-32 文本。

    判据是**零字节的固定周期分布**，并且用"零字节总占比"做二次校验 ——
    随机二进制里零字节只占约 0.4%，而 UTF-16LE 的 ASCII 文本约占 50%、
    UTF-32LE 约占 75%，区分度很高。

    已知局限：无 BOM 且非 ASCII 的 UTF-16（比如中文）高位字节不为零，探测不到。
    这种文件极少见，且会被判成二进制后明确报出来。
    """
    if len(data) < 32:
        return None

    probe = data[:512]
    zero_fraction = probe.count(0) / len(probe)

    aligned = len(probe) - (len(probe) % 4)
    if aligned >= 32:
        quads = aligned // 4
        positions = [probe[i::4][:quads].count(0) / quads for i in range(4)]
        low, high = _UTF32_ZERO_RANGE
        if low <= zero_fraction <= high:
            if all(p >= _ZERO_ALIGNMENT for p in positions[1:]):
                return "utf-32-le"
            if all(p >= _ZERO_ALIGNMENT for p in positions[:3]):
                return "utf-32-be"

    paired = len(probe) - (len(probe) % 2)
    if paired >= 32:
        pairs = paired // 2
        low, high = _UTF16_ZERO_RANGE
        if low <= zero_fraction <= high:
            if probe[1::2][:pairs].count(0) / pairs >= _ZERO_ALIGNMENT:
                return "utf-16-le"
            if probe[0::2][:pairs].count(0) / pairs >= _ZERO_ALIGNMENT:
                return "utf-16-be"

    return None


def classify(
    data: bytes,
    *,
    filename: str = "",
    extended_encodings: bool = False,
) -> TextVerdict:
    """判定一段字节是不是文本。``data`` 应是文件开头（最多 8192 字节）。"""
    # 0. 空文件 —— 很多检测器会把 0 字节判成二进制，显式处理
    if not data:
        return TextVerdict(True, None, "空文件")

    # 1. BOM
    bom_encoding = _match_bom(data)
    if bom_encoding:
        return TextVerdict(True, bom_encoding, f"有 {bom_encoding} BOM")

    # 2. 已知二进制格式
    magic = _match_binary_magic(data)
    if magic:
        return TextVerdict(False, None, magic)

    # 3. NUL 字节 —— Git 的规则，但要先排除无 BOM 的 UTF-16/32
    if 0 in data:
        wide = _detect_bomless_utf16_32(data)
        if wide:
            return TextVerdict(
                True, wide, f"无 BOM 的 {wide}（零字节呈固定周期）", "likely"
            )
        return TextVerdict(False, None, "含 NUL 字节（Git 的二进制判据）")

    # 4. 控制字符占比 —— 必须排在 UTF-8 检查之前，因为 ASCII 控制字节本身
    #    就是合法 UTF-8（见模块文档）
    ratio = control_ratio(data)
    if ratio > MAX_CONTROL_RATIO:
        return TextVerdict(
            False, None, f"控制字符占比 {ratio:.1%} > {MAX_CONTROL_RATIO:.0%}"
        )

    # 5. 严格 UTF-8
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    else:
        return TextVerdict(True, "utf-8", "合法 UTF-8")

    # 6. 熵护栏 —— 拦住"latin-1 能解码一切"这个陷阱
    entropy = shannon_entropy(data)
    if entropy > MAX_TEXT_ENTROPY:
        return TextVerdict(
            False, None, f"熵 {entropy:.2f} bit/byte > {MAX_TEXT_ENTROPY}（疑似压缩/加密数据）"
        )

    # 7. 编码探测
    allowed = DEFAULT_TEXT_ENCODINGS | (EXTENDED_TEXT_ENCODINGS if extended_encodings else frozenset())
    best = from_bytes(data).best()
    if best is not None:
        encoding = best.encoding
        chaos = getattr(best, "percent_chaos", 0.0) or 0.0
        if encoding in allowed and chaos <= 10.0:
            warning = None
            confidence = "likely"
            if encoding in {"cp1252", "latin_1", "iso8859_1"}:
                confidence = "guessed"
                warning = (
                    f"编码是猜的（{encoding}）—— Latin-1 能解码任意字节序列，"
                    f"这个判定置信度低。如果这其实是个二进制文件，请加 --exclude 排除它。"
                )
            return TextVerdict(True, encoding, f"编码探测判定为 {encoding}", confidence, warning)

    # 8. 扩展名提示 —— 只在内容判不出结果时打平手，不覆盖内容证据
    suffix = os.path.splitext(filename)[1].lower()
    if suffix in TEXT_EXTENSIONS:
        return TextVerdict(
            True,
            None,
            f"内容判不出来，但扩展名 {suffix} 是文本类型",
            "guessed",
            f"没能通过编码探测，仅凭扩展名 {suffix} 判为文本，置信度低。",
        )
    if suffix in BINARY_EXTENSIONS:
        return TextVerdict(False, None, f"{suffix} 是二进制扩展名")

    # 9. 兜底
    return TextVerdict(
        False, None, f"无法判定为文本（熵 {entropy:.2f}，控制字符 {ratio:.2%}）"
    )


def detect(
    path: str | os.PathLike[str],
    *,
    extended_encodings: bool = False,
) -> TextVerdict:
    """读文件开头并判定。**只读**（走 :func:`readonly.read_prefix`）。"""
    data = read_prefix(path, SAMPLE_SIZE)
    return classify(data, filename=os.fspath(path), extended_encodings=extended_encodings)
