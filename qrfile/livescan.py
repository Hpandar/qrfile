"""全类型扫描器 —— 给实时二维码链路用。

和 :mod:`qrfile.scan` 的区别只有一个，但很关键：**这里不做文本判定**。

``scan`` 是"文本文件 ⇄ 二维码"那个产品的入口，它会按 magic number / 编码探测
把图片、压缩包、可执行文件统统跳过。实时链路面对的可能是任意二进制（一个 10 MB
的日志包、一段录屏、一个安装程序），跳过它们毫无意义。所以这里只做三件事：

1. 遍历目录、保留相对路径与空目录；
2. 执行与 ``scan`` 相同的**安全与体积上限**（防路径穿越、防 OOM）；
3. 用 :func:`qrfile.readonly.read_bytes` 读取 —— 全代码库唯一的源文件读取入口。
"""

from __future__ import annotations

import os
import stat
from typing import Sequence

from .archive import FileSource
from .errors import InputError, LimitExceeded
from .manifest import display_path
from .readonly import DEFAULT_EXCLUDES, read_bytes
from .scan import ScanIssue, ScanLimits, ScanResult, _check_names, _matches_any

#: 实时链路的默认上限。比文本链路的 8 MiB 宽松得多 —— 二进制文件本来就可能大。
LIVE_LIMITS = ScanLimits(
    max_file_size=256 * 1024 * 1024,      # 单文件 256 MiB
    max_files=5000,
    max_total_bytes=512 * 1024 * 1024,    # 单会话 512 MiB
    max_depth=64,
    max_path_len=1024,
    max_name_len=255,
)


def read_files(files: Sequence[tuple[str, str]]) -> list[FileSource]:
    """按 ``(相对路径, 绝对路径)`` 精确读取一批文件 —— 补播用。

    不重新遍历目录、不重新套 include/exclude，保证读到的就是原始会话里的
    同一批文件、同样的相对路径。
    """
    sources: list[FileSource] = []
    for relative, absolute in files:
        try:
            info = os.stat(absolute)
            data = read_bytes(absolute)
        except OSError as exc:
            raise InputError(f"读不了源文件 {absolute}：{exc}") from exc
        sources.append(
            FileSource(
                path=relative,
                data=data,
                mode=stat.S_IMODE(info.st_mode),
                mtime_ns=getattr(info, "st_mtime_ns", None),
            )
        )
    return sources


def scan_all(
    roots: Sequence[str],
    *,
    limits: ScanLimits | None = None,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    follow_symlinks: bool = False,
) -> ScanResult:
    """扫描一组文件 / 目录，产出待编码的源文件列表。

    返回 :class:`qrfile.scan.ScanResult` —— 与文本链路同构，便于复用后续的
    manifest / session 构建。
    """
    if limits is None:
        limits = LIVE_LIMITS
    if not roots:
        raise InputError("至少要给一个输入路径")

    for root in roots:
        if not os.path.exists(root):
            raise InputError(f"路径不存在：{root}")

    multi = len(roots) > 1
    root_label = "archive" if multi else os.path.basename(os.path.abspath(roots[0]))

    sources: list[FileSource] = []
    absolute_paths: list[str] = []
    empty_dirs: list[str] = []
    symlinks: list[tuple[str, str]] = []
    skipped: list[ScanIssue] = []
    total_bytes = 0

    def _add_file(rel: str, full_path: str) -> None:
        nonlocal total_bytes

        problem = _check_names(rel, rel.rsplit("/", 1)[-1], limits)
        if problem:
            skipped.append(ScanIssue(display_path(rel), problem))
            return
        if exclude and _matches_any(rel, exclude):
            skipped.append(ScanIssue(display_path(rel), "命中 --exclude"))
            return
        if include and not _matches_any(rel, include):
            skipped.append(ScanIssue(display_path(rel), "未命中 --include"))
            return

        try:
            info = os.stat(full_path)
        except OSError as exc:
            skipped.append(ScanIssue(display_path(rel), f"读不了文件属性：{exc}"))
            return

        if info.st_size > limits.max_file_size:
            skipped.append(
                ScanIssue(
                    display_path(rel),
                    f"文件 {info.st_size} 字节，超过上限 {limits.max_file_size}",
                )
            )
            return
        if len(sources) >= limits.max_files:
            raise LimitExceeded(
                f"文件数超过上限 {limits.max_files}。请用 --max-files 放宽，"
                f"或分多次编码不同的子目录。"
            )
        if total_bytes + info.st_size > limits.max_total_bytes:
            raise LimitExceeded(
                f"总字节数超过上限 {limits.max_total_bytes}。请用 --max-total-bytes "
                f"放宽，或分多次编码。"
            )

        try:
            data = read_bytes(full_path)
        except OSError as exc:
            skipped.append(ScanIssue(display_path(rel), f"读不了文件：{exc}"))
            return

        sources.append(
            FileSource(
                path=rel,
                data=data,
                mode=stat.S_IMODE(info.st_mode),
                mtime_ns=getattr(info, "st_mtime_ns", None),
            )
        )
        absolute_paths.append(os.path.abspath(full_path))
        total_bytes += len(data)

    def _walk(directory: str, prefix: str, depth: int) -> bool:
        """递归遍历。返回"这棵子树里有没有任何内容"。"""
        if depth > limits.max_depth:
            skipped.append(
                ScanIssue(display_path(prefix or "."), f"目录深度超过上限 {limits.max_depth}")
            )
            return False

        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError as exc:
            skipped.append(ScanIssue(display_path(prefix or "."), f"读不了目录：{exc}"))
            return False

        has_content = False

        for entry in entries:
            if entry.name in DEFAULT_EXCLUDES:
                continue

            rel = f"{prefix}/{entry.name}" if prefix else entry.name

            if entry.is_symlink() and not follow_symlinks:
                try:
                    target = os.readlink(entry.path)
                except OSError as exc:
                    skipped.append(ScanIssue(display_path(rel), f"读不了符号链接：{exc}"))
                    continue
                symlinks.append((rel, target))
                has_content = True
                continue

            try:
                is_dir = entry.is_dir(follow_symlinks=follow_symlinks)
            except OSError:
                is_dir = False

            if is_dir:
                if exclude and _matches_any(rel, exclude):
                    skipped.append(ScanIssue(display_path(rel), "命中 --exclude"))
                    continue
                child_has_content = _walk(entry.path, rel, depth + 1)
                if child_has_content:
                    has_content = True
                else:
                    empty_dirs.append(rel)
                continue

            try:
                is_file = entry.is_file(follow_symlinks=follow_symlinks)
            except OSError:
                is_file = False

            if is_file:
                _add_file(rel, entry.path)
                has_content = True
            else:
                skipped.append(ScanIssue(display_path(rel), "既不是普通文件也不是目录"))

        return has_content

    for root in roots:
        absolute = os.path.abspath(root)
        # 根目录名始终作为前缀：还原时才能把编码时指定的文件夹一起重建。
        # 单文件输入时前缀就是文件名，行为不变。
        prefix = os.path.basename(absolute.rstrip("\\/")) or "root"

        if os.path.isdir(root):
            _walk(root, prefix, 0)
        elif os.path.isfile(root):
            _add_file(prefix, root)
        else:
            skipped.append(ScanIssue(display_path(root), "既不是普通文件也不是目录"))

    return ScanResult(
        root_label=root_label,
        sources=tuple(sources),
        empty_dirs=tuple(empty_dirs),
        symlinks=tuple(symlinks),
        skipped=tuple(skipped),
        collisions=(),
        total_bytes=total_bytes,
        absolute_paths=tuple(absolute_paths),
    )
