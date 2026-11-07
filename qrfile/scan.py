"""目录递归扫描：遍历源树、判定文本、过滤、记录跳过原因。

**全程只读** —— 所有文件读取都走 :func:`qrfile.readonly.read_bytes`。

两类"超限"的处理方式**刻意不一样**：

* **单个文件的问题**（本身太大、路径太长、名字非法、不是文本）⇒ 跳过它、
  记下原因、继续跑（方案里用户选的"跳过 + 警告"策略）。
* **整个会话的预算超了**（文件数、总字节）⇒ **直接报错**，并给出可操作的
  建议（加大上限 / 分几次编码不同子目录）。

这两者不能混为一谈：前者只是少收一个文件，用户能从报告里看到；后者会**悄悄
改变这个归档的含义** —— 用户以为整棵树都进去了，实际只进去了一半。这种事必须
大声说出来。
"""

from __future__ import annotations

import fnmatch
import os
import stat
from dataclasses import dataclass
from typing import Iterator, Sequence

from .archive import FileSource
from .capacity import Profile
from .errors import InputError, LimitExceeded
from .manifest import display_path
from .pathsafe import find_collisions
from .readonly import DEFAULT_EXCLUDES, read_bytes
from .textdetect import SAMPLE_SIZE, classify

DEFAULT_MAX_DEPTH = 64

#: 遍历时每个条目的种类
_FILE = "file"
_DIR = "dir"
_SYMLINK = "symlink"


@dataclass(frozen=True, slots=True)
class ScanLimits:
    """一次会话的各项上限（方案第七章的默认值）。"""

    max_file_size: int = 8 * 1024 * 1024  # 8 MiB
    max_files: int = 5000
    max_total_bytes: int = 64 * 1024 * 1024  # 64 MiB
    max_depth: int = DEFAULT_MAX_DEPTH
    max_path_len: int = 1024  # 相对路径的 UTF-8 字节数
    max_name_len: int = 255  # 单个名字的 UTF-8 字节数（NTFS 上限）


@dataclass(frozen=True, slots=True)
class ScanIssue:
    """一个被跳过的条目及其原因。"""

    path: str
    reason: str


@dataclass(frozen=True, slots=True)
class ScanResult:
    root_label: str
    sources: tuple[FileSource, ...] = ()
    empty_dirs: tuple[str, ...] = ()
    symlinks: tuple[tuple[str, str], ...] = ()
    skipped: tuple[ScanIssue, ...] = ()
    collisions: tuple[tuple[str, ...], ...] = ()
    total_bytes: int = 0
    #: 与 sources 一一对应的绝对路径。实时链路用它生成区间播放存档。
    absolute_paths: tuple[str, ...] = ()

    @property
    def file_count(self) -> int:
        return len(self.sources)

    def estimated_chunks(self, profile: Profile, *, compress_ratio: float = 1.0) -> int:
        """粗估需要多少张码。

        这是个**估算**，两个地方不精确，``plan`` 命令会如实说明：

        * 压缩率只能猜 —— 真实值要等内容压完才知道。
        * 清单本身也要占码。文件多了清单会很长甚至跨多张码，这里按每条目约
          120 字节 JSON 粗算。

        两个估不准的方向都是**宁可估多**，免得用户按预估时间安排了却发现码更多。
        """
        per_chunk = max(profile.body_capacity, 1)

        estimated_manifest_bytes = 200 + 120 * len(self.sources)
        manifest_chunks = max(1, -(-estimated_manifest_bytes // per_chunk))

        estimated_payload = int(self.total_bytes * compress_ratio)
        data_chunks = -(-estimated_payload // per_chunk) if estimated_payload > 0 else 0

        return manifest_chunks + data_chunks


def _matches_any(rel: str, patterns: Sequence[str]) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(name, p) for p in patterns)


def _iter_tree(
    directory: str,
    *,
    limits: ScanLimits,
    follow_symlinks: bool,
) -> Iterator[tuple[str, os.DirEntry[str], str]]:
    """深度优先遍历，产出 ``(相对路径, DirEntry, 种类)``，相对路径用 ``/`` 分隔。

    符号链接默认**只记录不跟随** —— 既避免成环，也避免把源树之外的东西卷进来。
    即使开启跟随，也用 ``(st_dev, st_ino)`` 记录已访问目录来断环。
    """
    visited: set[tuple[int, int]] = set()
    # 栈里放 (绝对路径, 相对路径, 深度)
    stack: list[tuple[str, str, int]] = [(directory, "", 0)]

    while stack:
        current, current_prefix, depth = stack.pop()

        try:
            entries = sorted(os.scandir(current), key=lambda e: e.name)
        except OSError:
            continue

        subdirs: list[tuple[str, str, int]] = []

        for entry in entries:
            if entry.name in DEFAULT_EXCLUDES:
                continue

            rel = f"{current_prefix}/{entry.name}" if current_prefix else entry.name

            if entry.is_symlink():
                yield rel, entry, _SYMLINK
                if not follow_symlinks:
                    continue
                try:
                    target_stat = entry.stat(follow_symlinks=True)
                    key = (target_stat.st_dev, target_stat.st_ino)
                except OSError:
                    continue
                if key in visited:
                    continue
                visited.add(key)

            try:
                if entry.is_dir(follow_symlinks=follow_symlinks):
                    if depth + 1 > limits.max_depth:
                        # 不再深入，但这个目录本身要如实记下来
                        yield rel, entry, _DIR
                        continue
                    yield rel, entry, _DIR
                    subdirs.append((entry.path, rel, depth + 1))
                else:
                    yield rel, entry, _FILE
            except OSError:
                continue

        # 逆序压栈，保证出栈顺序是文件名字典序
        stack.extend(reversed(subdirs))


def _check_names(rel: str, name: str, limits: ScanLimits) -> str | None:
    """单文件层面的名字/长度检查。返回跳过原因，或 None 表示通过。"""
    if len(name.encode("utf-8", "surrogateescape")) > limits.max_name_len:
        return f"文件名超过 {limits.max_name_len} 字节（NTFS 上限）"
    if len(rel.encode("utf-8", "surrogateescape")) > limits.max_path_len:
        return f"相对路径超过 {limits.max_path_len} 字节"
    return None


def scan_paths(
    roots: Sequence[str],
    *,
    profile: Profile,
    limits: ScanLimits | None = None,
    extended_encodings: bool = False,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    follow_symlinks: bool = False,
    strict: bool = False,
) -> ScanResult:
    """扫描一组路径，产出待编码的源文件列表。

    多个根路径时，每个根的名字会作为前缀加进去，避免不同根下的同名文件撞车
    （撞车了也有 :func:`find_collisions` 兜底）。
    只有一个根时，相对路径就是相对这个根本身，不加前缀。
    """
    if limits is None:
        limits = ScanLimits()
    if not roots:
        raise InputError("至少要给一个输入路径")

    for root in roots:
        if not os.path.exists(root):
            raise InputError(f"路径不存在：{root}")

    multi = len(roots) > 1
    root_label = "archive" if multi else os.path.basename(os.path.abspath(roots[0]))

    sources: list[FileSource] = []
    all_dirs: set[str] = set()
    symlinks: list[tuple[str, str]] = []
    skipped: list[ScanIssue] = []
    total_bytes = 0

    def _accept_file(rel: str, full_path: str) -> None:
        nonlocal total_bytes

        name_problem = _check_names(rel, rel.rsplit("/", 1)[-1], limits)
        if name_problem:
            skipped.append(ScanIssue(display_path(rel), name_problem))
            return
        if exclude and _matches_any(rel, exclude):
            skipped.append(ScanIssue(display_path(rel), "命中 --exclude"))
            return
        if include and not _matches_any(rel, include):
            skipped.append(ScanIssue(display_path(rel), "未命中 --include"))
            return

        try:
            st = os.stat(full_path)
        except OSError as exc:
            skipped.append(ScanIssue(display_path(rel), f"读不了文件属性：{exc}"))
            return

        if st.st_size > limits.max_file_size:
            skipped.append(
                ScanIssue(
                    display_path(rel),
                    f"文件 {st.st_size} 字节，超过单文件上限 {limits.max_file_size} 字节"
                    f"（用 --max-file-size 调整）",
                )
            )
            return

        # 会话级预算：这是硬错误，不是跳过 —— 见模块文档
        if len(sources) >= limits.max_files:
            raise LimitExceeded(
                f"文件数超过上限 {limits.max_files}。"
                f"请提高 --max-files，或分几次只编码不同的子目录。"
            )
        if total_bytes + st.st_size > limits.max_total_bytes:
            raise LimitExceeded(
                f"总字节数超过上限 {limits.max_total_bytes}（已累计 {total_bytes}）。"
                f"请提高 --max-total-bytes，或分几次只编码不同的子目录。"
            )

        try:
            data = read_bytes(full_path)
        except OSError as exc:
            skipped.append(ScanIssue(display_path(rel), f"读不了文件：{exc}"))
            return

        verdict = classify(
            data[:SAMPLE_SIZE], filename=rel, extended_encodings=extended_encodings
        )
        if not verdict.is_text:
            skipped.append(ScanIssue(display_path(rel), verdict.skip_reason or "非文本"))
            return

        sources.append(
            FileSource(
                path=rel,
                data=data,
                mode=stat.S_IMODE(st.st_mode),
                mtime_ns=st.st_mtime_ns,
            )
        )
        total_bytes += st.st_size

    for root in roots:
        absolute = os.path.abspath(root)

        if os.path.isfile(absolute):
            _accept_file(os.path.basename(absolute), absolute)
            continue

        if not os.path.isdir(absolute):
            skipped.append(
                ScanIssue(display_path(root), "既不是普通文件也不是目录（FIFO / 设备节点？）")
            )
            continue

        prefix = os.path.basename(absolute) if multi else ""

        for rel, entry, kind in _iter_tree(
            absolute, limits=limits, follow_symlinks=follow_symlinks
        ):
            prefixed = f"{prefix}/{rel}" if prefix else rel

            if kind == _DIR:
                all_dirs.add(prefixed)
                continue
            if kind == _SYMLINK:
                if follow_symlinks:
                    # 跟随了就当普通文件/目录处理，不记成链接
                    if entry.is_dir(follow_symlinks=True):
                        all_dirs.add(prefixed)
                        continue
                else:
                    try:
                        target = os.readlink(entry.path)
                    except OSError:
                        target = ""
                    symlinks.append((prefixed, target))
                    continue
            _accept_file(prefixed, entry.path)

    # 空目录 = 走到的所有目录 减去 被接受文件隐含需要创建的目录。
    # 非空目录可以靠文件的父路径推出来，空目录必须显式记录，否则还原时丢结构。
    implied: set[str] = set()
    for source in sources:
        parts = source.path.split("/")[:-1]
        for index in range(1, len(parts) + 1):
            implied.add("/".join(parts[:index]))
    for rel, target in symlinks:
        parts = rel.split("/")[:-1]
        for index in range(1, len(parts) + 1):
            implied.add("/".join(parts[:index]))

    empty_dirs = sorted(all_dirs - implied)

    if strict and (skipped or symlinks):
        raise InputError(
            f"--strict 模式：有 {len(skipped)} 个条目被跳过、"
            f"{len(symlinks)} 个符号链接未跟随，按严格模式中止。"
        )

    collisions = tuple(
        tuple(group) for group in find_collisions(s.path for s in sources).values()
    )
    if collisions and strict:
        raise InputError(f"--strict 模式：发现大小写 / 归一化冲突 {collisions}")

    return ScanResult(
        root_label=root_label,
        sources=tuple(sorted(sources, key=lambda s: s.path)),
        empty_dirs=tuple(empty_dirs),
        symlinks=tuple(sorted(symlinks)),
        skipped=tuple(skipped),
        collisions=collisions,
        total_bytes=total_bytes,
    )
