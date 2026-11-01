"""源文件只读护栏（方案第零章，优先级最高）。

三层，缺一不可：

1. **唯一的读取入口** —— :func:`read_bytes` 是全代码库唯一允许打开源文件的
   函数，永远是只读。
2. **审计钩子** —— :func:`activate_source_guard` 装上 ``sys.addaudithook``，
   运行期任何想以写意图触碰源路径的操作**当场抛异常**。这是"运行时可证明"，
   不是"我们小心点"。
3. **源树指纹前后比对** —— :func:`snapshot_tree` 采指纹、
   :func:`compare_snapshots` 比对。事后可证明没被改，顺带覆盖 TOCTOU
   （读的时候源文件被别人动了）。

已知边界（不藏着）：审计钩子盖不住 C 扩展内部的直接 syscall，也管不了**其他
进程**。所以第 3 层是必要的补充，不是冗余。
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from .errors import InputError, SourceGuardError

# --------------------------------------------------------------------------
# 第 1 层：唯一的读取入口
# --------------------------------------------------------------------------

_O_BINARY = getattr(os, "O_BINARY", 0)
_O_NOATIME = getattr(os, "O_NOATIME", 0)

_READ_BLOCK = 1 << 20

#: 默认排除的、明显是别的系统留下的垃圾文件（方案第十章）
DEFAULT_EXCLUDES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini", "__MACOSX"})

_noatime_state = {"warned": False}


def noatime_degraded() -> bool:
    """是否发生过 O_NOATIME 降级（读源文件会更新 atime）。"""
    return _noatime_state["warned"]


def read_bytes(path: str | os.PathLike[str]) -> bytes:
    """读取文件内容 —— **全代码库唯一允许触碰源文件的读取函数**。

    永远以 ``O_RDONLY`` 打开。Linux 上尽力用 ``O_NOATIME`` 避免更新访问时间
    （需要文件属主或 ``CAP_FOWNER``，拿不到就静默降级并记一笔）。
    """
    flags = os.O_RDONLY | _O_BINARY
    fd = -1
    if _O_NOATIME:
        try:
            fd = os.open(path, flags | _O_NOATIME)
        except OSError:
            # 不是属主 / 没有 CAP_FOWNER —— 退回普通只读
            _noatime_state["warned"] = True
    if fd < 0:
        fd = os.open(path, flags)

    try:
        blocks: list[bytes] = []
        while True:
            block = os.read(fd, _READ_BLOCK)
            if not block:
                break
            blocks.append(block)
        return b"".join(blocks)
    finally:
        os.close(fd)


def read_prefix(path: str | os.PathLike[str], limit: int) -> bytes:
    """只读文件开头的 ``limit`` 字节 —— 给文本判定用，不必把整个文件读进来。"""
    flags = os.O_RDONLY | _O_BINARY
    fd = -1
    if _O_NOATIME:
        try:
            fd = os.open(path, flags | _O_NOATIME)
        except OSError:
            _noatime_state["warned"] = True
    if fd < 0:
        fd = os.open(path, flags)
    try:
        return os.read(fd, limit)
    finally:
        os.close(fd)


def iter_file_bytes(path: str | os.PathLike[str]) -> Iterator[bytes]:
    """流式读一个文件，不整个装进内存。用于给大文件算哈希。"""
    flags = os.O_RDONLY | _O_BINARY
    fd = -1
    if _O_NOATIME:
        try:
            fd = os.open(path, flags | _O_NOATIME)
        except OSError:
            _noatime_state["warned"] = True
    if fd < 0:
        fd = os.open(path, flags)
    try:
        while True:
            block = os.read(fd, _READ_BLOCK)
            if not block:
                return
            yield block
    finally:
        os.close(fd)


def sha256_file(path: str | os.PathLike[str]) -> str:
    """流式算文件的 SHA-256。只读。"""
    digest = hashlib.sha256()
    for block in iter_file_bytes(path):
        digest.update(block)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# 第 2 层：审计钩子
# --------------------------------------------------------------------------

_WRITE_INTENT_FLAGS = (
    os.O_WRONLY
    | os.O_RDWR
    | os.O_CREAT
    | os.O_TRUNC
    | os.O_APPEND
    | getattr(os, "O_EXCL", 0)
)

#: 这些审计事件即使不带"写意图"标志也可能改文件，一律拦。
_BLOCKED_EVENTS = frozenset(
    {
        "os.remove",
        "os.rename",
        "os.replace",
        "os.link",
        "os.symlink",
        "os.mkdir",
        "os.rmdir",
        "os.chmod",
        "os.chown",
        "os.utime",
        "os.truncate",
        "shutil.copyfile",
        "shutil.copymode",
        "shutil.copystat",
        "shutil.move",
        "shutil.rmtree",
    }
)

_guard_state: dict[str, object] = {
    "roots": (),
    "enabled": False,
    "installed": False,
}


def _normalize_for_compare(path: object) -> str | None:
    """把审计事件里的路径参数归一化；不是路径就返回 None。"""
    if isinstance(path, int):
        return None  # 文件描述符，判不了归属
    try:
        raw = os.fspath(path)  # type: ignore[arg-type]
    except TypeError:
        return None
    if isinstance(raw, bytes):
        raw = os.fsdecode(raw)
    try:
        return os.path.normcase(os.path.abspath(raw))
    except (OSError, ValueError):
        return None


def _is_under_source(path: object) -> bool:
    normalized = _normalize_for_compare(path)
    if normalized is None:
        return False
    for root in _guard_state["roots"]:  # type: ignore[union-attr]
        if normalized == root or normalized.startswith(root + os.sep):
            return True
    return False


def _open_has_write_intent(mode: object, flags: object) -> bool:
    if isinstance(mode, str) and any(ch in mode for ch in "wax+"):
        return True
    if isinstance(flags, int) and (flags & _WRITE_INTENT_FLAGS):
        return True
    return False


def _audit_hook(event: str, args: tuple[object, ...]) -> None:
    if not _guard_state["enabled"]:
        return

    if event == "open":
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else None
        if _open_has_write_intent(mode, flags) and _is_under_source(args[0] if args else None):
            raise SourceGuardError(
                f"只读护栏拦截：有操作试图以写模式打开源路径 —— {args[0]!r}"
            )
        return

    if event in _BLOCKED_EVENTS:
        for arg in args:
            if _is_under_source(arg):
                raise SourceGuardError(
                    f"只读护栏拦截：有操作试图改动源路径（{event}）—— {arg!r}"
                )


def activate_source_guard(roots: Iterable[str]) -> None:
    """对给定的源根目录开启只读护栏。

    幂等：钩子只装一次，之后调用只是更新受保护的根目录集合。
    注意 ``sys.addaudithook`` **装上去就撤不掉** —— 所以用一个开关变量来控制
    生效与否，而不是反复装卸。
    """
    normalized = tuple(
        sorted({os.path.normcase(os.path.abspath(os.fspath(r))) for r in roots})
    )
    _guard_state["roots"] = normalized
    _guard_state["enabled"] = bool(normalized)
    if normalized and not _guard_state["installed"]:
        sys.addaudithook(_audit_hook)
        _guard_state["installed"] = True


def deactivate_source_guard() -> None:
    """关掉护栏（钩子本身留在进程里，但不再拦截）。"""
    _guard_state["enabled"] = False
    _guard_state["roots"] = ()


def guard_is_active() -> bool:
    return bool(_guard_state["enabled"])


# --------------------------------------------------------------------------
# 第 3 层：源树指纹
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EntrySnapshot:
    """源树里一个条目的指纹。"""

    path: str  # 绝对路径（快照的键也是它）
    kind: str  # "file" | "dir" | "symlink" | "other"
    size: int
    mtime_ns: int
    mode: int
    link_target: str | None = None
    sha256: str | None = None  # 只有普通文件才有

    def describe(self) -> str:
        return f"{self.kind}:{self.path}"


def _kind_of(entry: os.DirEntry[str]) -> str:
    if entry.is_symlink():
        return "symlink"
    if entry.is_dir(follow_symlinks=False):
        return "dir"
    if entry.is_file(follow_symlinks=False):
        return "file"
    return "other"


def _snapshot_entry(entry: os.DirEntry[str]) -> EntrySnapshot:
    st = entry.stat(follow_symlinks=False)
    kind = _kind_of(entry)
    target = None
    digest = None
    if kind == "symlink":
        try:
            target = os.readlink(entry.path)
        except OSError:
            target = None
    elif kind == "file":
        digest = sha256_file(entry.path)
    return EntrySnapshot(
        path=entry.path,
        kind=kind,
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
        mode=stat.S_IMODE(st.st_mode),
        link_target=target,
        sha256=digest,
    )


def snapshot_tree(root: str, *, excludes: frozenset[str] | None = None) -> dict[str, EntrySnapshot]:
    """对整棵源树采一次指纹。**纯只读**。

    返回 ``{绝对路径: 指纹}``。符号链接只记录不跟随；不跟随也就不会成环。
    """
    if excludes is None:
        excludes = DEFAULT_EXCLUDES

    out: dict[str, EntrySnapshot] = {}
    root = os.path.abspath(root)

    def walk(directory: str) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError as exc:
            raise InputError(f"无法读取目录 {directory}：{exc}") from exc
        for entry in entries:
            if entry.name in excludes:
                continue
            out[entry.path] = _snapshot_entry(entry)
            if _kind_of(entry) == "dir":
                walk(entry.path)

    walk(root)
    return out


def compare_snapshots(
    before: dict[str, EntrySnapshot], after: dict[str, EntrySnapshot]
) -> list[str]:
    """比对两次指纹，返回人类可读的差异列表。空列表 = 完全一致。

    目录**只比对存在性，不比对属性**。理由：目录的 mtime / mode 是派生状态 ——
    它随条目的增删改而变化，而条目的增删改已经由键集合差异**直接且精确**地抓到了
    （见上面的"消失了"/"凭空出现了"）。所以比对目录属性不带来任何额外信息，却会
    带来误报：Windows 上目录时间戳会自己漂移，实测出现过两次快照之间目录 mtime
    变化但条目集合完全一致的情况。

    误报的代价很高 —— 这一层一旦报错就是告诉用户"你的源文件被改了"，而那是假话。
    一个会喊狼来了的保证层，比没有保证层更糟。
    """
    changes: list[str] = []

    removed = sorted(set(before) - set(after))
    added = sorted(set(after) - set(before))
    for path in removed:
        changes.append(f"消失了：{path}")
    for path in added:
        changes.append(f"凭空出现了：{path}")

    for path in sorted(set(before) & set(after)):
        old, new = before[path], after[path]

        if old.kind != new.kind:
            changes.append(f"类型变了：{path}（{old.kind} → {new.kind}）")
            continue

        if old.kind == "dir":
            continue  # 见上面的说明：目录属性不比对

        if old.sha256 != new.sha256:
            changes.append(f"内容变了：{path}")
            continue  # 内容变了就够严重了，不用再报元数据
        if old.size != new.size:
            changes.append(f"大小变了：{path}（{old.size} → {new.size}）")
        if old.mtime_ns != new.mtime_ns:
            changes.append(f"修改时间变了：{path}")
        if old.mode != new.mode:
            changes.append(f"权限变了：{path}（{old.mode:o} → {new.mode:o}）")
        if old.link_target != new.link_target:
            changes.append(f"符号链接目标变了：{path}")

    return changes


def snapshot_paths(paths: Iterable[str]) -> dict[str, EntrySnapshot]:
    """对一组**混合的文件/目录**路径采指纹，结果合并成一个字典。

    ``snapshot_tree`` 只管目录；用户也可能直接把单个文件当输入，这时需要对那个
    文件本身采指纹。
    """
    merged: dict[str, EntrySnapshot] = {}
    for raw in paths:
        path = os.path.abspath(os.fspath(raw))
        if os.path.isdir(path):
            merged.update(snapshot_tree(path))
        elif os.path.isfile(path):
            st = os.stat(path)
            merged[path] = EntrySnapshot(
                path=path,
                kind="file",
                size=st.st_size,
                mtime_ns=st.st_mtime_ns,
                mode=stat.S_IMODE(st.st_mode),
                sha256=sha256_file(path),
            )
    return merged


def assert_paths_untouched(paths: Iterable[str], before: dict[str, EntrySnapshot]) -> None:
    """重新采指并比对；有差异就抛 :class:`SourceGuardError`。"""
    paths = list(paths)
    changes = compare_snapshots(before, snapshot_paths(paths))
    if changes:
        detail = "\n".join(f"  - {line}" for line in changes[:20])
        more = f"\n  …… 另有 {len(changes) - 20} 处未列出" if len(changes) > 20 else ""
        raise SourceGuardError(
            f"源文件在本次操作前后发生了变化（{len(changes)} 处）：\n{detail}{more}\n"
            f"这可能是外部程序改动，也可能是 qrfile 自己的 bug —— 两种情况都要查。"
        )


def assert_source_untouched(root: str, before: dict[str, EntrySnapshot]) -> None:
    """重新采一次指纹并比对；有差异就抛 :class:`SourceGuardError`。"""
    changes = compare_snapshots(before, snapshot_tree(root))
    if changes:
        detail = "\n".join(f"  - {line}" for line in changes[:20])
        more = f"\n  …… 另有 {len(changes) - 20} 处未列出" if len(changes) > 20 else ""
        raise SourceGuardError(
            f"源目录在本次操作前后发生了变化（{len(changes)} 处）：\n{detail}{more}\n"
            f"目录：{root}\n"
            f"这可能是外部程序改动，也可能是 qrfile 自己的 bug —— 两种情况都要查。"
        )


# --------------------------------------------------------------------------
# 写盘前的目标检查（方案 0.3 的后三行）
# --------------------------------------------------------------------------


def check_write_target(path: str, *, overwrite: bool = False) -> None:
    """写文件之前的检查。挡住"从输出侧改到源文件"的三条路。

    抛 :class:`InputError` 表示拒绝；调用方可以选择跳过这一个文件继续跑
    （对应方案里"跳过 + 警告"的默认策略）。
    """
    if os.path.islink(path):
        raise InputError(
            f"拒绝写入：目标已存在且是符号链接，跟随写入会逃出输出目录 —— {path}"
        )

    if not os.path.lexists(path):
        return

    st = os.stat(path)
    if st.st_nlink > 1:
        raise InputError(
            f"拒绝写入：目标已存在且是硬链接（nlink={st.st_nlink}），"
            f"写入会连带改掉其他位置的同一份内容 —— {path}"
        )
    if not overwrite:
        raise InputError(f"拒绝覆盖已存在的文件（要覆盖请显式加 --overwrite）—— {path}")
