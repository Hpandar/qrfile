"""压缩器探测与选择。

分层策略，**默认绝不引入额外软件依赖**：

* ``python`` 层：``zlib`` 与 ``lzma``（Python 标库，任何机器都有）。
* ``auto``（默认）：只跑 python 层，取最小的那个。
* ``max``：额外调用本机的外部工具（``brotli`` / ``zstd`` / ``7z``）。
  此时压缩方法名会写进分片头，**解码端也必须有同一个工具**，否则报错而不是
  静默出错 —— 这是刻意的：宁可明确失败，不要还原出一个坏文件。

外部工具统一走临时文件而不是管道。``7z`` 的 ``-si``/``-so`` 组合需要指定
归档名，行为随版本变化，临时文件是最稳的写法；对 10 MB 级别的载荷，多一次
落盘的开销可以忽略。
"""

from __future__ import annotations

import lzma
import os
import shutil
import subprocess
import tempfile
import zlib
from dataclasses import dataclass

from .errors import InputError, VerificationError

#: 低于这个体积不值得压：压缩头本身就要几十字节
MIN_COMPRESS_BYTES = 128

#: 外部工具的默认超时（秒）。10 MB 文本用 q11 brotli 大约十几秒，留足余量。
EXTERNAL_TIMEOUT = 300

#: Git for Windows 自带 brotli / xz，但只有从 Git Bash 启动时才会进 PATH。
#: 从 cmd 或双击 exe 启动时 which() 找不到 —— 这些是常见的兜底路径。
_EXTRA_PATHS = {
    "brotli": [
        r"C:\Program Files\Git\mingw64\bin\brotli.exe",
        r"C:\Program Files\Git\usr\bin\brotli.exe",
        "/usr/bin/brotli",
        "/usr/local/bin/brotli",
    ],
    "zstd": [
        r"C:\Program Files\zstd\zstd.exe",
        r"C:\Program Files (x86)\zstd\zstd.exe",
        "/usr/bin/zstd",
        "/usr/local/bin/zstd",
    ],
    "7z": [
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
        "/usr/bin/7z",
        "/usr/bin/7zz",
        "/usr/local/bin/7z",
    ],
}


def _find_exe(name: str, extra: list[str]) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for candidate in extra:
        if os.path.isfile(candidate):
            return candidate
    return None


@dataclass(frozen=True, slots=True)
class Compressor:
    """一个压缩器的描述。``kind`` 决定它是否会被默认启用。"""

    name: str
    kind: str  # "python" | "external"
    exe: str | None = None
    note: str = ""

    @property
    def is_python(self) -> bool:
        return self.kind == "python"


# --------------------------------------------------------------------------
# Python 标库
# --------------------------------------------------------------------------


def _zlib_compress(data: bytes) -> bytes:
    return zlib.compress(data, 9)


def _zlib_decompress(data: bytes) -> bytes:
    return zlib.decompress(data)


def _lzma_compress(data: bytes) -> bytes:
    # preset 6：8 MB 字典、压缩时约 100 MB 内存。preset 9 只多省 1~2%，
    # 却要 700 MB 内存 —— 在受限服务器上不值得。
    return lzma.compress(data, preset=6)


def _lzma_decompress(data: bytes) -> bytes:
    return lzma.decompress(data)


# --------------------------------------------------------------------------
# 外部工具
# --------------------------------------------------------------------------


def _run(cmd: list[str], *, timeout: int = EXTERNAL_TIMEOUT) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InputError(f"调用外部压缩工具失败：{cmd[0]}：{exc}") from exc


def _external_temp(exe: str, data: bytes, compress_cmd: list[str], decompress_cmd: list[str], *,
                   compress: bool) -> bytes:
    """通用的临时文件封装：写输入 → 跑工具 → 读输出。"""
    with tempfile.TemporaryDirectory(prefix="qrfile-comp-") as tmp:
        src = os.path.join(tmp, "in.bin")
        dst = os.path.join(tmp, "out.bin")
        with open(src, "wb") as handle:
            handle.write(data)
        template = compress_cmd if compress else decompress_cmd
        cmd = [part.format(exe=exe, src=src, dst=dst) for part in template]
        proc = _run(cmd)
        if proc.returncode != 0:
            raise InputError(
                f"{os.path.basename(exe)} 返回 {proc.returncode}："
                f"{proc.stderr.decode('utf-8', 'replace').strip()[:300]}"
            )
        try:
            with open(dst, "rb") as handle:
                return handle.read()
        except OSError as exc:
            raise InputError(f"{os.path.basename(exe)} 没有产出文件：{exc}") from exc


def _brotli_compress(exe: str, data: bytes) -> bytes:
    return _external_temp(
        exe, data,
        ["{exe}", "-q", "11", "-f", "-o", "{dst}", "{src}"],
        ["{exe}", "-d", "-f", "-o", "{dst}", "{src}"],
        compress=True,
    )


def _brotli_decompress(exe: str, data: bytes) -> bytes:
    return _external_temp(
        exe, data,
        ["{exe}", "-q", "11", "-f", "-o", "{dst}", "{src}"],
        ["{exe}", "-d", "-f", "-o", "{dst}", "{src}"],
        compress=False,
    )


def _zstd_compress(exe: str, data: bytes) -> bytes:
    return _external_temp(
        exe, data,
        ["{exe}", "-19", "-q", "-f", "-o", "{dst}", "{src}"],
        ["{exe}", "-d", "-q", "-f", "-o", "{dst}", "{src}"],
        compress=True,
    )


def _zstd_decompress(exe: str, data: bytes) -> bytes:
    return _external_temp(
        exe, data,
        ["{exe}", "-19", "-q", "-f", "-o", "{dst}", "{src}"],
        ["{exe}", "-d", "-q", "-f", "-o", "{dst}", "{src}"],
        compress=False,
    )


def _sevenz_compress(exe: str, data: bytes) -> bytes:
    # PPMd 对文本的表现通常优于 LZMA2；这里明确指定，结果可复现。
    with tempfile.TemporaryDirectory(prefix="qrfile-7z-") as tmp:
        src = os.path.join(tmp, "in.bin")
        dst = os.path.join(tmp, "out.7z")
        with open(src, "wb") as handle:
            handle.write(data)
        proc = _run([exe, "a", "-t7z", "-mx=9", "-m0=PPMd", dst, src])
        if proc.returncode != 0:
            raise InputError(
                f"7z 压缩失败（返回 {proc.returncode}）："
                f"{proc.stderr.decode('utf-8', 'replace').strip()[:300]}"
            )
        with open(dst, "rb") as handle:
            return handle.read()


def _sevenz_decompress(exe: str, data: bytes) -> bytes:
    with tempfile.TemporaryDirectory(prefix="qrfile-7z-") as tmp:
        src = os.path.join(tmp, "in.7z")
        out_dir = os.path.join(tmp, "out")
        os.makedirs(out_dir, exist_ok=True)
        with open(src, "wb") as handle:
            handle.write(data)
        proc = _run([exe, "x", "-y", f"-o{out_dir}", src])
        if proc.returncode != 0:
            raise VerificationError(
                f"7z 解压失败（返回 {proc.returncode}）："
                f"{proc.stderr.decode('utf-8', 'replace').strip()[:300]}"
            )
        entries = [n for n in os.listdir(out_dir) if os.path.isfile(os.path.join(out_dir, n))]
        if len(entries) != 1:
            raise VerificationError(f"7z 解出了 {len(entries)} 个文件，预期 1 个")
        with open(os.path.join(out_dir, entries[0]), "rb") as handle:
            return handle.read()


# --------------------------------------------------------------------------
# 注册表
# --------------------------------------------------------------------------


def available_compressors(*, include_external: bool = True) -> list[Compressor]:
    """列出本机可用的压缩器。python 层永远在最前面。"""
    result = [
        Compressor("lzma", "python", note="Python 标库，比率最好，解码端无需额外软件"),
        Compressor("zlib", "python", note="Python 标库，速度快，比率一般"),
    ]
    if include_external:
        brotli = _find_exe("brotli", _EXTRA_PATHS["brotli"])
        zstd = _find_exe("zstd", _EXTRA_PATHS["zstd"])
        sevenz = _find_exe("7z", _EXTRA_PATHS["7z"])
        if brotli:
            result.append(Compressor("brotli", "external", brotli, "文本上通常优于 lzma；解码端需 brotli"))
        if zstd:
            result.append(Compressor("zstd", "external", zstd, "速度极快，比率略逊；解码端需 zstd"))
        if sevenz:
            result.append(Compressor("7z", "external", sevenz, "PPMd 对文本很强；解码端需 7-Zip"))
    return result


def _compressors_by_name(*, include_external: bool) -> dict[str, Compressor]:
    return {c.name: c for c in available_compressors(include_external=include_external)}


def compress_one(data: bytes, method: str) -> bytes:
    """用指定方法压缩。方法不可用就明确报错。"""
    if method == "none":
        return data
    if method == "zlib":
        return _zlib_compress(data)
    if method == "lzma":
        return _lzma_compress(data)

    found = _compressors_by_name(include_external=True).get(method)
    if found is None or not found.exe:
        raise InputError(
            f"压缩方法 {method!r} 在本机不可用。"
            f"可用：{'、'.join(_compressors_by_name(include_external=True))}"
        )
    if method == "brotli":
        return _brotli_compress(found.exe, data)
    if method == "zstd":
        return _zstd_compress(found.exe, data)
    if method == "7z":
        return _sevenz_compress(found.exe, data)
    raise InputError(f"不认识的压缩方法：{method!r}")


def decompress_one(data: bytes, method: str) -> bytes:
    """用指定方法解压。缺工具时抛 VerificationError，绝不返回半成品。"""
    if method == "none":
        return data
    try:
        if method == "zlib":
            return _zlib_decompress(data)
        if method == "lzma":
            return _lzma_decompress(data)
    except (zlib.error, lzma.LZMAError) as exc:
        raise VerificationError(f"解压失败，数据可能已损坏：{exc}") from exc

    found = _compressors_by_name(include_external=True).get(method)
    if found is None or not found.exe:
        raise VerificationError(
            f"这批码用了 {method!r} 压缩，但本机没有对应的解压工具。"
            f"请先安装 {method}，或改用 --compression lzma 重新编码。"
        )
    if method == "brotli":
        return _brotli_decompress(found.exe, data)
    if method == "zstd":
        return _zstd_decompress(found.exe, data)
    if method == "7z":
        return _sevenz_decompress(found.exe, data)
    raise VerificationError(f"不认识的压缩方法：{method!r}")


def compress_best(data: bytes, *, include_external: bool = False) -> tuple[bytes, str]:
    """把所有可用压缩器跑一遍，返回 ``(最小的结果, 方法名)``。

    压不动（结果不比原文小）就返回原文和 ``"none"`` —— 每张码的容量是硬约束，
    浪费不起。
    """
    if len(data) < MIN_COMPRESS_BYTES:
        return data, "none"

    best_data = data
    best_method = "none"

    for compressor in available_compressors(include_external=include_external):
        try:
            packed = compress_one(data, compressor.name)
        except (InputError, VerificationError):
            # 单个工具失败不该拖垮整次编码 —— 跳过它，继续试下一个
            continue
        if len(packed) < len(best_data):
            best_data, best_method = packed, compressor.name

    return best_data, best_method


def describe_availability() -> list[tuple[str, str, str]]:
    """给 CLI 用的列表：``(名称, 是否可用, 说明)``。"""
    rows: list[tuple[str, str, str]] = []
    for compressor in available_compressors(include_external=True):
        status = "可用" if (compressor.is_python or compressor.exe) else "不可用"
        where = "" if compressor.is_python else (compressor.exe or "")
        rows.append((compressor.name, status, f"{compressor.note} {where}".strip()))
    for name in ("brotli", "zstd", "7z"):
        if not any(row[0] == name for row in rows):
            rows.append((name, "未安装", "需要本机安装对应命令行工具"))
    return rows
