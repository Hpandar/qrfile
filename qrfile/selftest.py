"""内置自检（方案 V4）：合成语料 → 编码 → 解码 → 逐字节 + 逐目录结构比对。

这是用户能随手跑的一条命令，也是"程序自己做验证"这条需求的最终体现：
不用相信任何单测，直接跑一遍真实的完整往返。

``--full`` 还会做故障注入 —— 故意删一张码、涂坏一张码、复制一张码、篡改内容，
断言每一种都被**精确报出来**且**不写任何半成品**。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .archive import build_session, manifest_for_sources, reassemble
from .capacity import DEFAULT_PROFILE, get_profile
from .chunk import new_session_id
from .decode import RestoreOptions, decode_file, find_image_files, restore_session
from .manifest import COMPRESSION_AUTO
from .errors import QrFileError
from .output import OutputOptions, write_session
from .readonly import compare_snapshots, snapshot_tree
from .render import Renderer
from .scan import ScanLimits, scan_paths

#: 深层嵌套的层数。**刻意保守** —— 本机 LongPathsEnabled=0，MAX_PATH 是 260，
#: 100 层目录在这里根本建不出来。10 层足以证明递归遍历正确。
DEEP_LEVELS = 10

#: 随机字节文件的体积（用来验证"二进制会被正确跳过"）
NOISE_BYTES = 512


@dataclass(frozen=True, slots=True)
class CorpusSpec:
    """合成语料：哪些该进归档、哪些该被跳过、哪些空目录要保留。"""

    files: dict[str, bytes] = field(default_factory=dict)
    empty_dirs: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


def build_corpus(root: Path) -> CorpusSpec:
    """在 ``root`` 下造出一份覆盖各种边界情况的语料。"""
    files: dict[str, bytes] = {}
    skipped: list[str] = []
    empty_dirs: list[str] = []
    notes: list[str] = []

    def write(rel: str, data: bytes) -> None:
        target = root / rel.replace("/", os.sep)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        files[rel] = data

    # --- 基本文本
    write("plain.txt", b"hello world\n")
    write("empty.txt", b"")  # 空文件
    write("one_byte.txt", b"x")
    write("no_trailing_newline.txt", b"no newline at end")

    # --- 换行风格：CRLF / LF / CR / 混合，必须原样保留
    write("crlf.txt", b"line1\r\nline2\r\n")
    write("lf.txt", b"line1\nline2\n")
    write("cr.txt", b"line1\rline2\r")
    write("mixed_newlines.txt", b"a\r\nb\nc\rd\r\n\n")

    # --- 编码边界
    write("utf8_bom.txt", "带 BOM 的中文".encode("utf-8-sig"))
    write("utf16.txt", "UTF-16 内容 with ASCII".encode("utf-16"))
    write("gbk.txt", "这是 GBK 编码的中文内容".encode("gbk"))
    write("utf8_chinese.txt", "中文、标点：，。！？以及 emoji 🎉📦\n".encode())
    write("tabs_and_spaces.txt", b"\t\tindented\r\n    spaced\r\n")

    # --- 文件名边界
    write("名字里有空格 的文件.md", "# 标题\n".encode())
    write("sub/中文目录/深层文件.txt", "深层内容\n".encode())
    write("emoji_📁_name.txt", b"emoji in filename\n")

    # CON 在 Windows 上有时允许、有时不允许 —— 能建就建，不能建不算失败
    try:
        con = root / "CON.txt"
        con.write_bytes(b"reserved name\n")
        files["CON.txt"] = b"reserved name\n"
    except OSError:
        notes.append("本机建不了 CON.txt（Windows 保留名），已跳过这一项")

    # --- 内容完全相同的两个文件（验证按内容独立处理）
    write("dup_a.txt", b"identical content\n")
    write("dup_b.txt", b"identical content\n")

    # --- 空目录（含嵌套空目录）
    for rel in ("empty_dir", "empty_dir/nested_empty"):
        (root / rel.replace("/", os.sep)).mkdir(parents=True, exist_ok=True)
        empty_dirs.append(rel)

    # --- 深层嵌套
    deep_parts = [f"d{i}" for i in range(DEEP_LEVELS)]
    write("/".join(deep_parts) + "/deep.txt", b"deep down\n")

    # --- 应当被跳过的：各类二进制
    binaries = {
        "image.png": b"\x89PNG\r\n\x1a\n" + os.urandom(NOISE_BYTES),
        "archive.zip": b"PK\x03\x04" + os.urandom(NOISE_BYTES),
        "doc.docx": b"PK\x03\x04" + os.urandom(NOISE_BYTES),
        "document.pdf": b"%PDF-1.7\n" + os.urandom(NOISE_BYTES),
        "font.woff2": b"wOF2" + os.urandom(NOISE_BYTES),
        "random.bin": os.urandom(NOISE_BYTES),
        "with_nul.txt": b"text\x00then binary" + b"\x01\x02" * 60,
        "fake.png.txt": b"\x89PNG\r\n\x1a\n" + os.urandom(NOISE_BYTES),  # 名字像文本
    }
    for rel, data in binaries.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        skipped.append(rel)

    # --- 系统垃圾文件，应当被默认排除（既不入档也不算"跳过"）
    for junk in (".DS_Store", "Thumbs.db", "desktop.ini"):
        (root / junk).write_bytes(b"junk")

    # --- 被 --exclude 之外、但应当跳过的：超限文件在 full 模式单独测
    return CorpusSpec(
        files=files,
        empty_dirs=tuple(sorted(empty_dirs)),
        skipped=tuple(sorted(skipped)),
        notes=tuple(notes),
    )


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class SelftestReport:
    checks: tuple[Check, ...] = ()
    codes: int = 0
    files: int = 0

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def failures(self) -> tuple[Check, ...]:
        return tuple(check for check in self.checks if not check.passed)


def _digest(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        out[rel] = "<dir>" if path.is_dir() else _sha256(path)
    return out


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def run_selftest(
    *,
    workdir: Path | None = None,
    profile_name: str = DEFAULT_PROFILE,
    full: bool = False,
) -> SelftestReport:
    """跑一遍完整的 encode → decode → 比对。``full=True`` 时附加故障注入。"""
    checks: list[Check] = []
    profile = get_profile(profile_name)

    owns_workdir = workdir is None
    base = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="qrfile-selftest-"))

    try:
        source = base / "src"
        if source.exists():
            shutil.rmtree(source)
        source.mkdir(parents=True)

        spec = build_corpus(source)
        codes = 0

        # ---- 编码
        before = snapshot_tree(str(source))
        scanned = scan_paths([str(source)], profile=profile, limits=ScanLimits())

        checks.append(
            Check(
                "文本文件全部收下",
                {s.path for s in scanned.sources} == set(spec.files),
                f"期望 {len(spec.files)} 个，收到 {len(scanned.sources)} 个；"
                f"差集 {sorted(set(spec.files) ^ {s.path for s in scanned.sources})[:5]}",
            )
        )
        checks.append(
            Check(
                "二进制文件全部跳过",
                {i.path for i in scanned.skipped} == set(spec.skipped),
                f"期望跳过 {len(spec.skipped)} 个，实际 {len(scanned.skipped)} 个",
            )
        )
        checks.append(
            Check(
                "空目录全部识别",
                set(scanned.empty_dirs) == set(spec.empty_dirs),
                f"期望 {sorted(spec.empty_dirs)}，实际 {sorted(scanned.empty_dirs)}",
            )
        )

        manifest, blob = manifest_for_sources(
            root=scanned.root_label,
            sources=scanned.sources,
            dirs=scanned.empty_dirs,
            symlinks=scanned.symlinks,
            skipped=[(i.path, i.reason) for i in scanned.skipped],
            chunk_size=profile.body_capacity,
        )
        plan = build_session(
            manifest=manifest,
            blob=blob,
            session_id=new_session_id(),
            chunk_size=profile.body_capacity,
            compression=COMPRESSION_AUTO,
        )
        codes = plan.total

        out_dir = base / "qrout"
        write_session(
            plan,
            output_dir=out_dir,
            renderer=Renderer(profile),
            options=OutputOptions(contact_sheet=True),
        )
        checks.append(Check("编码期自解码比对（V1）", True, f"{codes} 张码全部通过"))

        # ---- 源文件只读（指纹比对）
        changes = compare_snapshots(before, snapshot_tree(str(source)))
        checks.append(Check("源文件指纹零差异（只读）", not changes, "；".join(changes[:3])))

        # ---- 解码
        paths = find_image_files([str(out_dir)])
        payloads: list[bytes] = []
        for path in paths:
            payloads.extend(s.payload for s in decode_file(path))
        session, _ = reassemble(payloads, verify=True)
        checks.append(Check("还原级校验 V2+V3", True, "缺片/CRC/逐文件 SHA-256/集合哈希全过"))

        restored = base / "restored"
        if restored.exists():
            shutil.rmtree(restored)
        restore_session(session, RestoreOptions(output_dir=restored))

        # ---- 逐条目比对
        got = _digest(restored)
        want: dict[str, str] = {}
        for rel, data in spec.files.items():
            import hashlib

            want[rel] = hashlib.sha256(data).hexdigest()
            parent = Path(rel).parent.as_posix()
            while parent not in ("", "."):
                want.setdefault(parent, "<dir>")
                parent = Path(parent).parent.as_posix()
        for rel in spec.empty_dirs:
            want[rel] = "<dir>"

        missing = sorted(set(want) - set(got))
        extra = sorted(set(got) - set(want))
        differing = sorted(k for k in set(want) & set(got) if want[k] != got[k])

        checks.append(
            Check(
                "还原后逐文件逐字节一致",
                not differing,
                f"{len(differing)} 个文件内容不符：{differing[:5]}",
            )
        )
        checks.append(
            Check("目录结构完整（含空目录）", not missing and not extra,
                  f"缺 {missing[:5]}；多 {extra[:5]}")
        )
        leaked = [s for s in spec.skipped if s in got]
        checks.append(
            Check(
                "被跳过的文件确实没进归档",
                not leaked,
                f"有 {len(leaked)} 个二进制文件混进了归档：{leaked[:5]}" if leaked
                else f"{len(spec.skipped)} 个二进制文件都没进归档",
            )
        )

        # ---- 故障注入（--full）
        if full:
            checks.extend(_fault_injection(out_dir, base, spec))

        return SelftestReport(checks=tuple(checks), codes=codes, files=len(spec.files))
    finally:
        if owns_workdir:
            shutil.rmtree(base, ignore_errors=True)


def _fault_injection(out_dir: Path, base: Path, spec: CorpusSpec) -> list[Check]:
    """故意破坏，断言每一种都被抓住、且不写任何半成品。"""
    checks: list[Check] = []

    def _decode_all(directory: Path) -> list[bytes]:
        payloads: list[bytes] = []
        for path in find_image_files([str(directory)]):
            payloads.extend(s.payload for s in decode_file(path))
        return payloads

    def _copy_tree(name: str) -> Path:
        """复制一份输出目录来做破坏。

        **刻意丢掉总览图** —— 总览图里带着每一张码的副本，等于一份内置冗余，
        留着它删掉单张码根本丢不了数据（这本身是好性质，但会让故障注入失去意义）。
        所以这里只针对单张码做注入。
        """
        target = base / name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(out_dir, target)
        for sheet in list(target.glob("contact_sheet*.png")):
            sheet.unlink()
        return target

    # 从**不含总览图**的副本取干净载荷，否则每个分片都有一份副本，
    # 篡改那一份会被"重复且内容不一致"抢先抓到，就演示不出逐文件哈希这一层了
    clean = _decode_all(_copy_tree("fault_clean"))

    # 1) 删一张数据码 → 必须报出精确缺号，且不写盘
    victim_dir = _copy_tree("fault_missing")
    victims = sorted(victim_dir.rglob("data/*.png"))
    if victims:
        victims[0].unlink()
        try:
            reassemble(_decode_all(victim_dir), verify=True)
            checks.append(Check("故障注入：缺片被抓住", False, "居然没报错"))
        except QrFileError as exc:
            checks.append(
                Check("故障注入：缺片被抓住", "缺少分片" in str(exc), str(exc)[:80])
            )

    # 2) 涂坏一张码 → 必须报错或至少不产出错误内容
    corrupt_dir = _copy_tree("fault_corrupt")
    victims = sorted(corrupt_dir.rglob("data/*.png"))
    if victims:
        raw = bytearray(victims[0].read_bytes())
        raw[len(raw) // 2] ^= 0xFF  # 破坏 PNG 中段
        victims[0].write_bytes(bytes(raw))
        try:
            reassemble(_decode_all(corrupt_dir), verify=True)
            checks.append(Check("故障注入：损坏的码没能混过去", False, "篡改后居然还通过了校验"))
        except QrFileError:
            checks.append(Check("故障注入：损坏的码被抓住", True, "报错并拒绝产出"))

    # 3) 复制一张码（内容一致）→ 必须容忍
    dup_dir = _copy_tree("fault_duplicate")
    victims = sorted(dup_dir.rglob("data/*.png"))
    if victims:
        shutil.copy2(victims[0], dup_dir / "duplicate_copy.png")
        try:
            session, stats = reassemble(_decode_all(dup_dir), verify=True)
            checks.append(
                Check("故障注入：重复的码被容忍", stats.duplicates >= 1,
                      f"识别出 {stats.duplicates} 张重复")
            )
        except QrFileError as exc:
            checks.append(Check("故障注入：重复的码被容忍", False, str(exc)[:80]))

    # 4) 混入完全无关的二维码 → 必须忽略且不影响还原
    foreign_dir = _copy_tree("fault_foreign")
    stray = Renderer(get_profile(DEFAULT_PROFILE)).render(b"https://example.com/unrelated")
    (foreign_dir / "stray.png").write_bytes(stray)
    try:
        session, stats = reassemble(_decode_all(foreign_dir), verify=True)
        checks.append(
            Check("故障注入：无关的码被忽略", stats.foreign >= 1, f"识别出 {stats.foreign} 张外来码")
        )
    except QrFileError as exc:
        checks.append(Check("故障注入：无关的码被忽略", False, str(exc)[:80]))

    # 5) 内容被换掉，但分片结构、CRC、压缩流全都合法
    #    → 只有 **逐文件 SHA-256** 这一层能抓到，专门演示它
    try:
        reference, _ = reassemble(clean, verify=True)
        forged_blob = bytearray(reference.blob)
        if forged_blob:
            forged_blob[0] ^= 0xFF  # 长度不变，所以偏移和 blob_size 依然自洽

        forged_plan = build_session(
            manifest=reference.manifest,  # 清单里写的还是原来的 SHA-256
            blob=bytes(forged_blob),
            session_id=reference.session_id,
            chunk_size=reference.manifest.chunk_size,
            compression=COMPRESSION_AUTO,
        )
        try:
            reassemble(list(forged_plan.frames), verify=True)
            checks.append(
                Check("故障注入：内容篡改被逐文件哈希抓住", False, "内容换了居然通过了校验")
            )
        except QrFileError as exc:
            message = str(exc)
            caught_by_hash = "SHA-256" in message or "archive_sha256" in message
            checks.append(
                Check("故障注入：内容篡改被逐文件哈希抓住", caught_by_hash, message[:100])
            )
    except QrFileError as exc:
        checks.append(Check("故障注入：内容篡改被逐文件哈希抓住", False, str(exc)[:80]))

    return checks
