"""从图片里解出二维码载荷。

解码器选择（方案第六章，这个不能选错）—— 实测数据（1113 字符的密集码，65 个样本）：

    ================  ==========  ================================
    解码器             通过率      结论
    ================  ==========  ================================
    zxing-cpp 3.1.1   **59/65**   ✅ 唯一可用，设为硬依赖
    wechat-qr          48/65      不采用
    opencv-python      **0/65**   ❌ 灾难，绝不能进降级链
    pyzbar            未进评测     弱，且 Linux 还要 apt install libzbar0
    ================  ==========  ================================

把 0% 通过率的 opencv 放进"降级链"只会制造**静默失败** —— 用户以为解不出来是因为
图糊了，其实是解码器根本不行。所以这里**不做降级链**：zxing-cpp 是硬依赖，缺了
就明确报错让用户去装。

已知接口坑（实测）：``zxingcpp.read_barcodes`` **不接受 PNG 文件字节** ——
它会把 bytes 当成像素缓冲区，报 "Incompatible buffer dimension"。必须传
PIL Image 或 numpy 数组。
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import zxingcpp
from PIL import Image

from .errors import InputError

#: 能喂给 decode 的图片扩展名（默认）。照片（jpg）必须支持 —— 主链路就是手机拍屏。
DEFAULT_IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff"}
)


@dataclass(frozen=True, slots=True)
class DecodedSymbol:
    """从一张图里解出来的一个二维码。"""

    payload: bytes
    format: str
    source: str  # 来源描述，用于报错时指名道姓

    @property
    def size(self) -> int:
        return len(self.payload)


def decode_image(image: Image.Image, *, source: str = "") -> list[DecodedSymbol]:
    """从一张已打开的图片里解出**所有**二维码。

    支持一张图里多个码 —— 总览图（contact sheet）和打印拼版就是靠这个一次扫下一片，
    这是主链路（手机拍屏）最实用的提速手段。

    照片（透视畸变、旋转、反光）也走这里；预处理在 :mod:`qrfile.imageprep`。
    """
    try:
        results = zxingcpp.read_barcodes(image)
    except Exception as exc:  # zxing-cpp 对某些畸形图会抛原生异常
        raise InputError(f"解码 {source or '图片'} 时出错：{exc}") from exc

    symbols: list[DecodedSymbol] = []
    for result in results:
        if result.format != zxingcpp.BarcodeFormat.QRCode:
            continue  # 只认二维码；其他条码类型不是我们生成的
        symbols.append(
            DecodedSymbol(
                payload=bytes(result.bytes),
                format=str(result.format),
                source=source,
            )
        )
    return symbols


def decode_rgb_image(
    image: Image.Image, *, source: str = "", robust: bool = False
) -> list[DecodedSymbol]:
    """从 RGB 三通道图里解出所有二维码。

    发送端把 3 张独立的 QR 分别写进 R/G/B 通道，合成一张彩色图。这里把图拆回
    三个灰度通道，逐个交给 zxing 解码。**不要**直接解合成图 —— 彩色像素对
    QR 解码器来说是噪声，拆通道才是正确的读法。

    同一份载荷如果出现在多个通道里（理论上不该发生），只保留一份；分片去重
    最终仍由 :class:`qrfile.receiver.LiveCollector` 负责。
    """
    rgb = image.convert("RGB")
    bands = rgb.split()
    names = ("R", "G", "B")

    symbols: list[DecodedSymbol] = []
    seen: set[bytes] = set()

    for name, band in zip(names, bands):
        channel_source = f"{source}[{name}]" if source else name
        if robust:
            from .imageprep import decode_robust

            found = decode_robust(band, source=channel_source).symbols
        else:
            found = decode_image(band, source=channel_source)

        for symbol in found:
            if symbol.payload in seen:
                continue
            seen.add(symbol.payload)
            symbols.append(symbol)

    return symbols


def load_image(path: str | os.PathLike[str]) -> Image.Image:
    """打开一张图（只读）。"""
    try:
        image = Image.open(path)
        image.load()
    except (OSError, ValueError) as exc:
        raise InputError(f"读不了图片 {path}：{exc}") from exc
    return image


def decode_file(
    path: str | os.PathLike[str], *, robust: bool = False, rgb: bool = False
) -> list[DecodedSymbol]:
    """从一张图片文件里解出所有二维码。

    ``robust=True`` 时走 :func:`qrfile.imageprep.decode_robust` —— 对照片做多种
    渲染变体和二值化的多次尝试。干净 PNG 用默认的快路径就够了。
    ``rgb=True`` 时按 RGB 三通道拆分，分别解码。
    """
    image = load_image(path)
    if rgb:
        return decode_rgb_image(
            image, source=os.fspath(path), robust=robust
        )
    if robust:
        from .imageprep import decode_robust

        return list(decode_robust(image, source=os.fspath(path)).symbols)
    return decode_image(image, source=os.fspath(path))


def decode_bytes(
    data: bytes,
    *,
    source: str = "<内存里的图>",
    robust: bool = False,
    rgb: bool = False,
) -> list[DecodedSymbol]:
    """从编码后的图片字节（PNG/JPEG 等）里解出所有二维码。

    注意：这里先把字节当**图片文件**解析，再交给 zxing-cpp —— 直接把 bytes 丢给
    ``read_barcodes`` 是错的（见模块文档）。
    """
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (OSError, ValueError) as exc:
        raise InputError(f"解析图片字节失败（{source}）：{exc}") from exc
    if rgb:
        return decode_rgb_image(image, source=source, robust=robust)
    if robust:
        from .imageprep import decode_robust

        return list(decode_robust(image, source=source).symbols)
    return decode_image(image, source=source)


def find_image_files(
    paths: list[str],
    *,
    suffixes: frozenset[str] = DEFAULT_IMAGE_SUFFIXES,
) -> list[str]:
    """把一组路径展开成图片文件列表，目录会递归展开。结果已排序。

    结果排序是为了让报错信息稳定可复现（跟还原的正确性无关 —— 分片顺序由
    32 字节头里的 index 决定，与文件顺序无关）。
    """
    found: list[str] = []
    for raw in paths:
        path = Path(raw)
        if not path.exists():
            raise InputError(f"路径不存在：{raw}")
        if path.is_file():
            found.append(str(path))
            continue
        for child in sorted(path.rglob("*")):
            if child.is_file() and child.suffix.lower() in suffixes:
                found.append(str(child))
    return sorted(found)


# --------------------------------------------------------------------------
# 还原文件树
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RestoreOptions:
    output_dir: Path
    overwrite: bool = False
    restore_times: bool = True
    restore_modes: bool = True


@dataclass(frozen=True, slots=True)
class RestoreReport:
    written: tuple[str, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()  # (路径, 原因)
    dirs_created: tuple[str, ...] = ()
    symlinks_created: tuple[str, ...] = ()
    symlinks_failed: tuple[tuple[str, str], ...] = ()
    collisions: tuple[tuple[str, ...], ...] = ()
    renamed: tuple[tuple[str, str], ...] = ()  # (原名, 转义后) —— 名字被迫改了
    warnings: tuple[str, ...] = ()

    @property
    def written_count(self) -> int:
        return len(self.written)

    @property
    def is_byte_identical(self) -> bool:
        """名字有没有被迫改动。内容一致性由 decode 的校验保证，这里是名字。"""
        return not self.renamed


def _create_symlink(target: str, link_path: str, *, is_dir: bool) -> str | None:
    """重建一个符号链接。返回警告文本，或 None 表示成功。

    Windows 上创建符号链接需要开发者模式或管理员权限（本机实测没有），会抛
    ``WinError 1314``。目录链接有不需要特权的替代品 —— **junction**，这里会
    自动回退过去。文件链接没有等价物，只能如实报"没能重建"，绝不伪造一个普通
    文件冒充链接。
    """
    try:
        os.symlink(target, link_path, target_is_directory=is_dir)
        return None
    except (OSError, NotImplementedError) as exc:
        if os.name == "nt" and is_dir:
            junction_error = _try_junction(target, link_path)
            if junction_error is None:
                return None
            return f"符号链接和 junction 都建不了（{exc}；junction：{junction_error}）"
        return f"建不了符号链接（{exc}）"


def _try_junction(target: str, link_path: str) -> str | None:
    """用 ``mklink /J`` 建目录 junction（Windows 上不需要特权）。成功返回 None。"""
    import subprocess

    absolute = os.path.abspath(os.path.join(os.path.dirname(link_path), target))
    if not os.path.isdir(absolute):
        return "目标不是已存在的目录"
    try:
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", link_path, absolute],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return str(exc)
    if result.returncode != 0:
        return (result.stderr or result.stdout or "mklink 失败").strip()
    return None


def restore_session(
    session,
    options: RestoreOptions,
    *,
    progress: Callable[[int, int, str], None] | None = None,
) -> RestoreReport:
    """把还原出来的会话写成一个文件树。

    安全防线（方案 0.3）全在这一层：

    * 每个目标路径都过 :func:`pathsafe.safe_join` —— 拒绝穿越，也拒绝中间某段
      是符号链接且指向输出目录之外的情况。
    * 每个文件写之前过 :func:`readonly.check_write_target` —— 拒绝覆盖已存在的
      文件（除非显式 --overwrite）、拒绝写穿**硬链接**（写它会连带改掉别处的同一
      份内容，那可能就是源文件）、拒绝跟随已存在的符号链接。
    * **从不删除**输出目录里的任何东西 —— 只创建和写入。
    """
    from .manifest import display_path
    from .pathsafe import find_collisions, safe_join, sanitize_relpath
    from .readonly import check_write_target

    output_dir = Path(options.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    skipped: list[tuple[str, str]] = []
    dirs_created: list[str] = []
    links_created: list[str] = []
    links_failed: list[tuple[str, str]] = []
    renames: list[tuple[str, str]] = []
    warnings: list[str] = []

    def _candidates(rel: str) -> list[tuple[str, list[str]]]:
        """每个相对路径给出最多两个候选：先原样，再转义版。

        **顺序很关键。** 先试原名 —— Windows 11 其实允许 `CON.txt` 这类名字，
        无条件转义会把它还原成 `_CON.txt`，就违反了"完全一致"。所以转义只是
        写不进去时的兜底。原名和转义版相同时只给一个候选。
        """
        out: list[tuple[str, list[str]]] = [(rel, [])]
        try:
            escaped, notes = sanitize_relpath(rel)
        except InputError:
            return out
        if escaped != rel:
            out.append((escaped, notes))
        return out

    def _resolve(rel: str, candidate: str) -> str | None:
        try:
            return safe_join(str(output_dir), candidate)
        except InputError as exc:
            skipped.append((display_path(rel), str(exc)))
            return None

    def _note_rename(rel: str, final: str, notes: list[str]) -> None:
        renames.append((display_path(rel), final))
        warnings.extend(notes)

    # ---- 目录（含空目录）
    for entry in session.manifest.dirs:
        rel = entry.resolve_path()
        for candidate, notes in _candidates(rel):
            target = _resolve(rel, candidate)
            if target is None:
                break
            try:
                os.makedirs(target, exist_ok=True)
            except OSError:
                continue  # 名字在这个系统上非法，试转义版
            if candidate != rel:
                _note_rename(rel, candidate, notes)
            dirs_created.append(candidate)
            break
        else:
            skipped.append((display_path(rel), "目录建不出来（名字在目标系统上非法？）"))

    # ---- 文件
    total_files = len(session.manifest.files)
    for index, entry in enumerate(session.manifest.files, 1):
        rel = entry.resolve_path()
        data = session.file_bytes(entry)
        done = False

        for candidate, notes in _candidates(rel):
            target = _resolve(rel, candidate)
            if target is None:
                break
            try:
                parent = os.path.dirname(target)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                check_write_target(target, overwrite=options.overwrite)
            except InputError as exc:
                skipped.append((display_path(rel), str(exc)))
                break
            except OSError:
                continue  # 父目录名字非法，试转义版

            try:
                temp = target + ".qrfile-tmp"
                with open(temp, "wb") as handle:
                    handle.write(data)
                os.replace(temp, target)
            except OSError:
                continue  # 文件名非法，试转义版

            if options.restore_modes:
                try:
                    os.chmod(target, entry.mode)
                except OSError:
                    pass
            if options.restore_times and entry.mtime:
                try:
                    from datetime import datetime

                    moment = datetime.strptime(entry.mtime, "%Y-%m-%dT%H:%M:%S.%fZ")
                    stamp = moment.timestamp()
                    os.utime(target, (stamp, stamp))
                except (OSError, ValueError):
                    pass

            if candidate != rel:
                _note_rename(rel, candidate, notes)
            written.append(candidate)
            done = True
            break

        if not done and not any(p == display_path(rel) for p, _ in skipped):
            skipped.append((display_path(rel), "写不进去（名字在目标系统上非法？）"))

        if progress is not None:
            progress(index, total_files, display_path(rel))

    # ---- 符号链接
    for entry in session.manifest.symlinks:
        rel = entry.resolve_path()
        for candidate, notes in _candidates(rel):
            target = _resolve(rel, candidate)
            if target is None:
                break
            parent = os.path.dirname(target)
            try:
                if parent:
                    os.makedirs(parent, exist_ok=True)
            except OSError:
                continue
            is_dir = os.path.isdir(os.path.join(parent, entry.target))
            problem_text = _create_symlink(entry.target, target, is_dir=is_dir)
            if problem_text is None:
                if candidate != rel:
                    _note_rename(rel, candidate, notes)
                links_created.append(candidate)
            else:
                # 符号链接失败多半是权限问题（不是名字问题），换名字也没用
                links_failed.append((display_path(rel), problem_text))
            break

    collisions = tuple(tuple(group) for group in find_collisions(written).values())

    return RestoreReport(
        written=tuple(written),
        skipped=tuple(skipped),
        dirs_created=tuple(dirs_created),
        symlinks_created=tuple(links_created),
        symlinks_failed=tuple(links_failed),
        collisions=collisions,
        renamed=tuple(renames),
        warnings=tuple(warnings),
    )
