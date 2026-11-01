"""跨平台路径安全。

两个方向都要防：

* **encode 侧** —— 把源树里的相对路径写进清单之前，检查大小写冲突、非法名。
* **decode 侧** —— 把清单里的路径还原成磁盘路径之前，拒绝穿越 / 绝对路径 /
  Windows 保留名 / 符号链接逃逸。

区分两类处理：

* 安全性质的问题（穿越、绝对路径）⇒ **永远拒绝**，跟平台无关。
* 平台怪癖（保留名、结尾点号）⇒ **只在 Windows 上转义**，Linux 上原样保留。
  转义是无损的 —— 原始名字始终存在清单的 ``path_b64`` 里。
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Iterable, Sequence

from .errors import InputError

_RE_DRIVE = re.compile(r"^[A-Za-z]:")

#: Windows 保留设备名。**带扩展名一样保留**（`NUL.txt`、`COM5.foo` 都不合法）。
#: 这里比 Windows 官方列表稍宽 —— 宁可多转义一个无害的名字，也不要漏掉一个。
WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$"}
    | {f"COM{i}" for i in range(10)}
    | {f"LPT{i}" for i in range(10)}
)

#: Windows 文件名里不允许出现这些字符。`/` 和 `\` 不会走到这一步（会被拆成路径段）。
_WINDOWS_INVALID = frozenset('<>:"|?*') | {chr(c) for c in range(32)}

#: 长路径前缀。加了这个前缀之后 Windows 会关掉一堆路径解析，所以只对绝对路径加。
_LONG_PATH_THRESHOLD = 240


def is_windows() -> bool:
    return os.name == "nt"


def validate_relpath(rel: str) -> None:
    """校验一个相对路径是否可以安全使用。不安全就抛 :class:`InputError`。

    安全性的底线（与平台无关）：非空、不含 NUL、不是绝对路径、不含 ``..``。
    """
    if not rel:
        raise InputError("相对路径为空")

    if "\x00" in rel:
        raise InputError(f"相对路径含 NUL 字符：{rel!r}")

    normalized = rel.replace("\\", "/")

    # 前导 / 覆盖了 POSIX 绝对路径和 UNC（//server/share）
    if normalized.startswith("/"):
        raise InputError(f"拒绝绝对路径或 UNC 路径：{rel!r}")

    if _RE_DRIVE.match(normalized):
        raise InputError(f"拒绝带盘符的路径：{rel!r}")

    for part in normalized.split("/"):
        if part in ("", ".", ".."):
            raise InputError(f"相对路径含非法段 {part!r}：{rel!r}")


def collision_key(rel: str) -> str:
    """用于检测"大小写 / Unicode 归一化后撞车"的键。

    Windows 和 macOS 大小写不敏感，Linux 敏感。``File.txt`` 和 ``file.txt``
    在 Linux 上是两个文件、在 Windows 上是一个 —— 解码时会静默覆盖。
    所以编码阶段就要用这个键查一遍。
    """
    return unicodedata.normalize("NFC", rel).casefold()


def find_collisions(rels: Iterable[str]) -> dict[str, list[str]]:
    """找出只靠大小写 / 归一化区分的路径。返回 ``{键: [原始路径, ...]}``。

    只返回真正冲突的（同一个键下有多个不同写法）。
    """
    buckets: dict[str, list[str]] = {}
    for rel in rels:
        buckets.setdefault(collision_key(rel), []).append(rel)
    return {k: sorted(v) for k, v in buckets.items() if len(v) > 1}


def _escape_windows_component(name: str) -> tuple[str, str | None]:
    """把一个 Windows 上非法的文件名转成合法的。返回 ``(新名字, 警告文本)``。"""
    original = name
    reasons: list[str] = []

    # 保留名判定看第一个点之前的部分，且忽略结尾空格
    stem = name.split(".", 1)[0].rstrip(" ").upper()
    if stem in WINDOWS_RESERVED:
        name = "_" + name
        reasons.append(f"是 Windows 保留设备名（{stem}）")

    cleaned = "".join("_" if ch in _WINDOWS_INVALID else ch for ch in name)
    if cleaned != name:
        name = cleaned
        reasons.append("含 Windows 非法字符")

    stripped = name.rstrip(". ")
    if stripped != name:
        name = stripped or "_"
        reasons.append("以点号或空格结尾")

    if not name:
        name = "_"
        reasons.append("转义后为空")

    if not reasons:
        return original, None
    return name, f"文件名在 Windows 上非法（{'；'.join(reasons)}），已转义为 {name!r}"


def sanitize_component(name: str, *, windows: bool | None = None) -> tuple[str, str | None]:
    """把单个路径段转成目标平台合法的名字。返回 ``(名字, 警告或 None)``。"""
    if windows is None:
        windows = is_windows()
    if not name or name in (".", ".."):
        raise InputError(f"非法的路径段：{name!r}")
    if "\x00" in name or "/" in name or "\\" in name:
        raise InputError(f"路径段里不允许出现分隔符或 NUL：{name!r}")
    if windows:
        return _escape_windows_component(name)
    return name, None


def sanitize_relpath(rel: str, *, windows: bool | None = None) -> tuple[str, list[str]]:
    """校验并（在需要时）转义一整个相对路径。返回 ``(新路径, 警告列表)``。

    校验先跑 —— 穿越之类的安全问题**永远拒绝**，不会走转义这条路。
    """
    validate_relpath(rel)
    parts = rel.replace("\\", "/").split("/")
    out: list[str] = []
    warnings: list[str] = []
    for part in parts:
        new_part, warning = sanitize_component(part, windows=windows)
        if warning:
            warnings.append(f"{rel}：{warning}")
        out.append(new_part)
    return "/".join(out), warnings


def to_long_path(path: str) -> str:
    """Windows 上给接近 MAX_PATH 的路径加 ``\\\\?\\`` 前缀。

    注意这个前缀会**关掉** Windows 的路径解析（不做 ``..`` 归约、不裁结尾点号/
    空格、只认反斜杠），所以只对已经 :func:`os.path.abspath` 过的路径加。
    非 Windows 或路径不长时原样返回。
    """
    if not is_windows():
        return path
    if path.startswith("\\\\?\\"):
        return path
    absolute = os.path.abspath(path)
    if len(absolute) < _LONG_PATH_THRESHOLD:
        return path
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def _real(path: str) -> str:
    """解析到真实路径并归一化大小写，用于包含性判断。

    ``strict=False``（Python 3.10+ 默认）对尚不存在的路径也能用 —— 它会尽量解析
    已存在的部分。返回绝对路径，永不抛异常。
    """
    try:
        return os.path.normcase(os.path.realpath(os.path.abspath(path)))
    except (OSError, ValueError):
        return os.path.normcase(os.path.abspath(path))


def is_within(root: str, path: str) -> bool:
    """``path`` 是否落在 ``root`` 之内（含相等）。两者都会先解析符号链接。"""
    root_real = _real(root)
    path_real = _real(path)
    return path_real == root_real or path_real.startswith(root_real + os.sep)


def safe_join(root: str, rel: str) -> str:
    """把相对路径拼到根目录下，保证结果不会逃出根目录。

    包含性判断走 :func:`os.path.realpath`，所以**中间任何一段是符号链接且指向
    根目录之外时，这里就会拒绝** —— 这正是我们想要的（方案 0.3）。
    """
    validate_relpath(rel)
    joined = os.path.join(root, rel.replace("/", os.sep))
    if not is_within(root, joined):
        raise InputError(
            f"拒绝：路径逃出了输出根目录（可能中间有符号链接指向外部）—— {rel!r}"
        )
    return os.path.join(os.path.abspath(root), rel.replace("/", os.sep))


def relative_to_root(root: str, path: str) -> str:
    """把 ``path`` 转成相对 ``root`` 的、用 ``/`` 分隔的路径。

    用于遍历源树时生成清单里的路径。
    """
    rel = os.path.relpath(path, root)
    return rel.replace(os.sep, "/")


def has_overlap(a: str, b: str) -> bool:
    """两个路径是否重叠（相等，或一个是另一个的祖先）。"""
    return is_within(a, b) or is_within(b, a)


def check_output_isolated(output: str, sources: Sequence[str]) -> None:
    """确认输出目录跟任何一个源路径都不重叠（方案 0.3 第一行）。

    这是"源文件只读"最容易被绕过的地方：``-o`` 直接指向源目录，或者指向源目录的
    父目录（那样还原出来的树会正好落在源树上面）。
    """
    for src in sources:
        if has_overlap(output, src):
            raise InputError(
                f"拒绝：输出目录与源路径重叠，会覆盖源文件 ——\n"
                f"  输出：{output}\n  源　：{src}\n"
                f"请换一个跟源路径完全不重叠的输出位置。"
            )
