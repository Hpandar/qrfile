"""静态地证明"源文件只读"，而不是靠自觉。

只读保证有三层，各自的证据形式不同：

* **运行时** —— 审计钩子，由 test_readonly.py 动态验证。
* **事后** —— 源树指纹比对，由 test_readonly.py 动态验证。
* **代码本身** —— 就是这个文件：直接把源码解析成 AST 来断言，
  触碰源文件的模块里**根本不存在**写操作。这一层不依赖运行路径被覆盖到，
  所以能挡住"新加了一条没人测到的写入分支"这种退化。
"""

from __future__ import annotations

import ast
import os
import stat
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "qrfile"

#: 会直接触碰**源文件**的模块。这三个模块之外的地方只允许操作输出目录。
#: livescan.py 是实时链路的全类型扫描器，同样必须走 read_bytes。
SOURCE_TOUCHING = {"readonly.py", "scan.py", "livescan.py"}

#: 任何以写意图打开的模式
_WRITE_MODE_CHARS = set("wax+")

#: 破坏性的 os / shutil 调用；出现在触碰源文件的模块里就是 bug
_DESTRUCTIVE_CALLS = {
    "os.remove",
    "os.unlink",
    "os.rename",
    "os.replace",
    "os.chmod",
    "os.chown",
    "os.utime",
    "os.truncate",
    "os.mkdir",
    "os.makedirs",
    "os.rmdir",
    "os.removedirs",
    "os.symlink",
    "os.link",
    "shutil.copy",
    "shutil.copyfile",
    "shutil.copy2",
    "shutil.copytree",
    "shutil.move",
    "shutil.rmtree",
}


def _modules() -> list[Path]:
    return sorted(PACKAGE.glob("*.py"))


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _called_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return f"{func.value.id}.{func.attr}"
    return None


def _open_mode(node: ast.Call) -> str | None:
    """取出 open() 的模式参数；取不到字面量就返回 None。"""
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
        value = node.args[1].value
        return value if isinstance(value, str) else None
    for keyword in node.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            value = keyword.value.value
            return value if isinstance(value, str) else None
    return None


def _iter_calls(tree: ast.Module):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node


# --------------------------------------------------------------------------
# 纪律一：整个包里不允许出现文本模式的 open()
# --------------------------------------------------------------------------


def test_every_open_call_is_explicitly_binary() -> None:
    """包内所有内置 open() 都必须显式二进制模式。

    这是字节一致的第一前提 —— 本机 locale 是 cp936 且 Windows 文本模式会把
    ``\\n`` 变成 ``\\r\\n``，任何一次 `open(p, 'r')` 或省略模式都可能损坏数据。

    （``Image.open`` 这类不是内置 open，不会被这个检查误伤。）
    """
    offenders: list[str] = []

    for path in _modules():
        for node in _iter_calls(_parse(path)):
            if _called_name(node) != "open":
                continue
            mode = _open_mode(node)
            if mode is None:
                offenders.append(f"{path.name}: open() 没写模式参数（默认是文本模式）")
            elif "b" not in mode:
                offenders.append(f"{path.name}: open(..., {mode!r}) 不是二进制模式")

    assert offenders == [], "发现非二进制 open()：\n" + "\n".join(offenders)


# --------------------------------------------------------------------------
# 纪律二：触碰源文件的模块里不允许有任何写操作
# --------------------------------------------------------------------------


def test_source_touching_modules_have_no_write_operations() -> None:
    """readonly.py 和 scan.py 里不能有任何写意图的 open() 或破坏性调用。"""
    offenders: list[str] = []

    for path in _modules():
        if path.name not in SOURCE_TOUCHING:
            continue
        for node in _iter_calls(_parse(path)):
            name = _called_name(node)

            if name == "open":
                mode = _open_mode(node) or ""
                if any(ch in mode for ch in _WRITE_MODE_CHARS):
                    offenders.append(f"{path.name}: 用 {mode!r} 打开了文件")

            if name in _DESTRUCTIVE_CALLS:
                offenders.append(f"{path.name}: 调用了 {name}")

    assert offenders == [], (
        "触碰源文件的模块里出现了写操作：\n" + "\n".join(offenders)
    )


#: 这些模块会读文件，但读的是**输出目录 / 自己造的临时语料**，不是用户的源树，
#: 所以不受"不许写"的纪律约束（它们本来就要写输出）。
#:
#:   cli.py       —— 编排层，读写都经过底下那些模块
#:   textdetect.py—— 只调 read_prefix，自己不碰路径
#:   archive.py   —— 在内存里拼装，落盘交给 output.py
#:   selftest.py  —— 自己造临时语料再读它，写的也是自己的临时目录
_READS_BUT_IS_NOT_SOURCE_TREE = {"cli.py", "textdetect.py", "archive.py", "selftest.py"}


def test_source_touching_modules_are_the_expected_ones() -> None:
    """防止有人新加一个模块去读源文件、绕开这套纪律。

    加了新模块读源文件就会在这里失败 —— 这是有意的：它逼着人来这里显式确认
    "这个新模块读的是什么、有没有可能写到源树"，而不是悄悄绕过去。
    （写 selftest.py 的时候就真的被它拦了一次。）
    """
    readers: set[str] = set()
    for path in _modules():
        source = path.read_text(encoding="utf-8")
        if "from .readonly import" in source or "from qrfile.readonly import" in source:
            if "read_bytes" in source or "read_prefix" in source or "iter_file_bytes" in source:
                readers.add(path.name)

    unknown = readers - SOURCE_TOUCHING - _READS_BUT_IS_NOT_SOURCE_TREE
    assert not unknown, (
        f"这些模块读了源文件但没登记：{sorted(unknown)}。"
        f"如果它们只读不写，请加进 _READS_BUT_IS_NOT_SOURCE_TREE 并写明理由。"
    )


# --------------------------------------------------------------------------
# 纪律三：唯一的读取入口确实只用只读标志
# --------------------------------------------------------------------------


def test_only_readonly_module_uses_os_open() -> None:
    """``os.open`` 能绕过内置 open 的模式检查，所以只允许 readonly.py 用它。"""
    users = [
        path.name
        for path in _modules()
        if any(
            _called_name(node) == "os.open"
            for node in _iter_calls(_parse(path))
        )
    ]
    assert users == ["readonly.py"], f"os.open 只该出现在 readonly.py，实际出现在 {users}"


_WRITE_FLAGS = {"O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND", "O_TMPFILE"}


def _os_flag_attrs(expr: ast.AST) -> set[str]:
    """表达式中出现的 ``os.O_*`` 常量名。"""
    found: set[str] = set()
    for node in ast.walk(expr):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
            and node.attr.startswith("O_")
        ):
            found.add(node.attr)
    return found


def _scope_assignments(tree: ast.Module) -> tuple[dict, dict]:
    """按**作用域**收集赋值。返回 ``(每个函数节点的局部赋值, 模块级赋值)``。

    不能用一个扁平的 ``名字 -> 最后一次赋值`` —— readonly.py 里有 4 个不同作用域
    各自给 ``flags`` 赋值（审计钩子里那个是 ``args[2] if ... else None``），
    扁平表只会留下最后一个，解析结果就完全错了。
    """
    scopes: dict[ast.AST, dict[str, ast.AST]] = {}

    def visit(node: ast.AST, current: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
            current = node
            scopes.setdefault(current, {})
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    scopes.setdefault(current, {})[target.id] = node.value
        for child in ast.iter_child_nodes(node):
            visit(child, current)

    visit(tree, tree)
    module_scope = scopes.pop(tree, {})
    return scopes, module_scope


def _enclosing_function(tree: ast.Module, target: ast.AST) -> ast.AST | None:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    node = parents.get(target)
    while node is not None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
        node = parents.get(node)
    return None


def test_readonly_only_requests_read_flags() -> None:
    """真正传给 ``os.open`` 的 flags，必须由 ``O_RDONLY`` 及其只读伙伴拼成。

    注意**不能**用源码文本 grep —— readonly.py 里本来就出现 ``O_WRONLY`` 等字样，
    那是审计钩子用来**识别**写意图的常量表，是检测器，不是违规。

    也不能用扁平的变量表 —— 同名变量在不同作用域有不同值（见
    :func:`_scope_assignments`）。所以这里按作用域解析。
    """
    tree = _parse(PACKAGE / "readonly.py")
    scopes, module_scope = _scope_assignments(tree)

    checked = 0
    for call in _iter_calls(tree):
        if _called_name(call) != "os.open":
            continue
        checked += 1
        assert len(call.args) == 2, "os.open 必须显式给出 flags（不能依赖默认值）"

        flags_expr = call.args[1]
        collected = _os_flag_attrs(flags_expr)

        enclosing = _enclosing_function(tree, call)
        local = scopes.get(enclosing, {}) if enclosing is not None else {}

        # 定点展开：顺着引用的变量名找它们的赋值（先本函数作用域，再模块级）
        pending = [n.id for n in ast.walk(flags_expr) if isinstance(n, ast.Name)]
        seen: set[str] = set()
        while pending:
            name = pending.pop()
            if name in seen:
                continue
            seen.add(name)
            value = local.get(name) or module_scope.get(name)
            if value is None:
                continue
            collected |= _os_flag_attrs(value)
            pending.extend(n.id for n in ast.walk(value) if isinstance(n, ast.Name))

        assert "O_RDONLY" in collected, (
            f"os.open 的 flags 里没有 O_RDONLY：{sorted(collected)}"
        )
        offending = collected & _WRITE_FLAGS
        assert not offending, f"os.open 的 flags 里混进了写标志：{sorted(offending)}"

    assert checked >= 2, "期望 readonly.py 里有多处只读 os.open（读取入口）"


def test_no_shutil_imported_anywhere_that_touches_sources() -> None:
    """scan.py / readonly.py 不该 import shutil —— 那里全是复制/移动/删除。"""
    for name in SOURCE_TOUCHING:
        source = (PACKAGE / name).read_text(encoding="utf-8")
        assert "import shutil" not in source, f"{name} 不该 import shutil"


# --------------------------------------------------------------------------
# 动态补充：真的把源树设成只读，encode 仍然要能跑
# --------------------------------------------------------------------------


def test_encode_succeeds_on_a_readonly_source_tree(tmp_path: Path) -> None:
    """把源文件设成只读属性后跑 encode —— 成功本身就是"没写源文件"的证据。

    局限（如实说明）：Windows 上目录的只读属性**不阻止**在目录内新建文件，
    所以这项在 Windows 只覆盖"没有改动已有文件"。目录内不许新建文件这条由
    审计钩子的动态测试（test_readonly.py）覆盖。
    """
    from click.testing import CliRunner

    from qrfile.cli import main

    root = tmp_path / "src"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_bytes(b"hello\r\nworld\r\n")
    (root / "sub" / "b.md").write_bytes("# 标题\n".encode())
    (root / "empty_dir").mkdir()

    originals = {
        p: stat.S_IMODE(p.stat().st_mode) for p in root.rglob("*") if p.is_file()
    }
    for path, mode in originals.items():
        os.chmod(path, mode & ~0o222)  # 去掉所有写权限位

    try:
        # 先确认这个环境里"只读"是真的生效了，否则测试没意义
        probe = next(iter(originals))
        effective = True
        try:
            probe.open("wb").close()
            effective = False
        except (PermissionError, OSError):
            pass
        if not effective:
            pytest.skip("本机不支持按文件设置只读（或当前用户无视权限位）")

        result = CliRunner().invoke(main, ["encode", str(root), "-o", str(tmp_path / "qrout")])
        assert result.exit_code in (0, 1), result.output
        assert "指纹比对：零差异" in result.output
    finally:
        for path, mode in originals.items():
            try:
                os.chmod(path, mode)
            except OSError:
                pass
