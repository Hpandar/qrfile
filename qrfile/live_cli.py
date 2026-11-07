"""实时链路的命令行命令：send / recv / record / decode-video / compressors。

设计目标是**一条命令完成一件事**：

``qrfile send  <路径...>``   全屏循环播放（编码端）
``qrfile recv  <输出目录>``  自动采集 + 解码 + 还原（接收端，收齐自动停）
``qrfile record <帧目录>``   只录屏，产出无损 PNG 帧序列
``qrfile decode-video <视频...> -o <目录>``  从已有视频里还原
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import click

from .archive import build_session, manifest_for_sources
from .awake import KeepAwake
from .capture import (
    CaptureOptions,
    activate_window,
    enable_dpi_awareness,
    guess_remote_window,
    iter_frames,
    iter_video_frames,
    record as record_frames,
    resolve_region,
)
from .chunk import new_session_id
from .compressors import describe_availability
from .decode import RestoreOptions, decode_image, restore_session
from .errors import (
    EXIT_INTERNAL,
    EXIT_VERIFY_FAILED,
    InputError,
    QrFileError,
    VerificationError,
)
from .imageprep import decode_robust
from .livelayout import choose_layout, render_variants
from .livescan import LIVE_LIMITS, read_files, scan_all
from .player import Player, compose_page
from .readonly import (
    activate_source_guard,
    assert_paths_untouched,
    deactivate_source_guard,
    snapshot_paths,
)
from .receiver import LiveCollector
from .scan import ScanLimits
from .session import (
    DEFAULT_SESSION_FILE,
    SessionRecord,
    format_ranges,
    load_session,
    interval_pages,
    save_session,
)

COMPRESSION_VALUES = ["auto", "max", "zlib", "lzma", "none", "brotli", "zstd", "7z"]


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _echo(message: str = "") -> None:
    click.echo(message)


def _print_repair_hint(total_chunks: int) -> None:
    """播放器退出后打印区间播放命令模板，方便直接复制。"""
    raw = Path(sys.argv[0]).name
    if raw and not raw.endswith(".py") and "qrfile" in raw.lower():
        invocation = f"./{raw}"
    else:
        invocation = "qrfile"

    _echo("")
    _echo("=" * 60)
    _echo(f"本次共 {total_chunks} 片。如果接收端提示缺片：")
    _echo("  播放器还没退出时，在底部「播放区间」输入框里填缺片范围，回车生效。")
    _echo("  已经退出则执行区间播放命令：")
    _echo(f'    {invocation} push --repair "<缺片范围>"')
    _echo(f'    例：{invocation} push --repair "12-14,204"')
    _echo("=" * 60)


def _warn(message: str) -> None:
    click.echo(f"[!] {message}", err=True)


def _ok(message: str) -> None:
    click.echo(f"[OK] {message}")


def _human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{value} B"


def _human_seconds(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f} 秒"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.1f} 分钟"
    return f"{minutes / 60:.1f} 小时"


def _run(action) -> None:
    """统一异常 → 退出码。与文本链路的约定保持一致。"""
    # 必须在任何 GUI / 抓屏之前声明 DPI 感知，否则窗口坐标和屏幕像素会错位
    try:
        enable_dpi_awareness()
    except Exception:  # pragma: no cover
        pass

    try:
        # 复用文本链路那套输出编码处理：中文 Windows 的重定向 / 管道下
        # 不会因为某个字符打不出来而崩掉。
        from .cli import _setup_output

        _setup_output()
    except Exception:  # pragma: no cover - 输出处理失败不该影响主流程
        pass

    try:
        action()
    except KeyboardInterrupt:
        click.echo("")
        sys.exit(130)
    except QrFileError as exc:
        click.echo(f"错误：{exc}", err=True)
        sys.exit(exc.exit_code)
    except click.ClickException:
        raise
    except Exception as exc:  # pragma: no cover - 兜底
        click.echo(f"内部错误：{type(exc).__name__}: {exc}", err=True)
        sys.exit(EXIT_INTERNAL)


def _screen_size(text: str) -> tuple[int, int]:
    if text.strip().lower() == "auto":
        try:
            from .cli import detect_screen_size
        except Exception:  # pragma: no cover
            detect_screen_size = None  # type: ignore[assignment]
        found = detect_screen_size() if detect_screen_size else None
        return found if found else (1920, 1080)

    cleaned = text.lower().replace("×", "x").replace("*", "x")
    parts = cleaned.split("x")
    if len(parts) != 2:
        raise InputError(f"屏幕分辨率写法看不懂：{text!r}（应该像 1920x1080）")
    try:
        width, height = int(parts[0].strip()), int(parts[1].strip())
    except ValueError:
        raise InputError(f"屏幕分辨率写法看不懂：{text!r}（应该像 1920x1080）") from None
    if width < 200 or height < 200:
        raise InputError(f"屏幕分辨率太小：{width}×{height}")
    return width, height


def _measure_render(frames, layout, pages: int, fps: float, mask) -> tuple[float, float]:
    """实测"渲染一屏要多久"，返回 ``(实际可达 fps, 每屏秒数)``。

    ``send`` 打印的时长必须按**渲染速度**算，而不是按用户请求的 fps ——
    否则会给出一个乐观到误导人的"5 秒/轮"（实际可能要 30 秒）。
    """
    sample = max(1, min(pages, 3))
    try:
        compose_page(frames, 0, layout, fps=fps, mask=mask)  # 预热：导入、字体、缓存
        started = time.perf_counter()
        for index in range(sample):
            compose_page(frames, index, layout, fps=fps, mask=mask)
        per_page = (time.perf_counter() - started) / sample
    except Exception:
        return fps, 0.0
    if per_page <= 0:
        return fps, 0.0
    return min(fps, 1.0 / per_page), per_page


def _looks_blank(image) -> bool:
    """粗略判断一帧是不是纯色/黑屏（息屏、锁屏、窗口被遮挡的典型表现）。"""
    try:
        gray = image.convert("L").resize((32, 32))
        low, high = gray.getextrema()
        return high - low < 8
    except Exception:
        return False


def _progress_line(progress, *, width: int = 28) -> None:
    if progress.total <= 0:
        return
    filled = int(width * progress.received / progress.total)
    bar = "#" * filled + "-" * (width - filled)
    sys.stdout.write(
        f"\r[{bar}] {progress.received}/{progress.total} "
        f"({progress.percent:5.1f}%)  缺 {progress.total - progress.received}  "
        f"重复 {progress.duplicates}  坏帧 {progress.damaged}"
    )
    sys.stdout.flush()


# --------------------------------------------------------------------------
# send：全屏播放
# --------------------------------------------------------------------------


def _send_repair(
    record: SessionRecord,
    ranges_text: str,
    *,
    fps: float,
    windowed: bool,
    dump_pages: str | None,
    duration: float,
    camera: bool,
    accurate_mask: bool,
    keep_awake: bool = True,
) -> None:
    """只播放指定的分片区间 —— 不再整轮重来。"""
    if not record.files:
        raise InputError("会话存档缺少文件列表（旧版本生成），请重新完整播放一次")

    sources = read_files(record.files)

    manifest, blob = manifest_for_sources(
        root=record.manifest.root,
        sources=sources,
        dirs=[entry.resolve_path() for entry in record.manifest.dirs],
        symlinks=[(link.resolve_path(), link.target) for link in record.manifest.symlinks],
        skipped=[(item.path, item.reason) for item in record.manifest.skipped],
        chunk_size=record.chunk_size,
    )
    if manifest.archive_sha256 != record.manifest.archive_sha256:
        raise VerificationError("源文件内容已经变化，无法按区间播放。请重新完整播放一次。")
    if len(blob) != record.manifest.blob_size:
        raise VerificationError(
            f"源文件总大小变了（存档 {record.manifest.blob_size}，现在 {len(blob)}）"
        )

    plan = build_session(
        manifest=record.manifest,
        blob=blob,
        session_id=record.session_id,
        chunk_size=record.chunk_size,
        compression=record.data_compression,
        manifest_compression=record.manifest_compression,
    )
    if plan.total != record.total_frames:
        raise VerificationError(
            f"按区间重建出的分片数（{plan.total}）与原始会话（{record.total_frames}）不一致"
        )
    if plan.manifest.to_bytes() != record.manifest.to_bytes():
        raise VerificationError("按区间重建出的清单与原始会话不一致")

    indices, pages = interval_pages(ranges_text, plan.total, record.layout.per_screen)
    if not pages:
        raise InputError("给定的分片范围映射不到任何页")

    mask = None if (camera or accurate_mask) else 3
    _echo(f"播放区间：{format_ranges(indices)} → {len(pages)} 页（会话 {plan.session_hex}）")
    _echo("接收端保持运行；结束后按 Esc 退出。")
    _echo("播放中可在底部「播放区间」输入框修改（留空回车恢复完整播放）。")
    if duration > 0:
        _echo(f"自动退出：{duration / 60:.0f} 分钟后（--duration 0 可关闭）")

    if dump_pages:
        target = Path(dump_pages)
        target.mkdir(parents=True, exist_ok=True)
        for page_index in pages:
            page = compose_page(
                plan.frames, page_index, record.layout, fps=fps, mask=mask
            )
            page.save(target / f"page_{page_index:05d}.png", compress_level=1)
        _ok(f"已写出 {len(pages)} 页到 {target}")
        return

    awake = KeepAwake()
    if keep_awake:
        awake.start()
    if awake.backend:
        _echo(f"已阻止息屏（{awake.backend}）")
    try:
        Player(
            plan.frames,
            record.layout,
            fps=fps,
            windowed=windowed,
            mask=mask,
            pages=pages,
            shuffle=False,
            status_note=f"播放区间 {format_ranges(indices, limit=8)}",
            interval_text=ranges_text,
        ).run(duration=duration)
    finally:
        awake.stop()
    _print_repair_hint(plan.total)


def _send_impl(
    roots,
    *,
    ecc: str,
    mode: str,
    module_px: int | None,
    screen_size: str,
    fps: float,
    compression: str,
    max_codes: int,
    camera: bool,
    windowed: bool,
    dry_run: bool,
    dump_pages: str | None,
    max_file_size: int | None,
    max_total_bytes: int | None,
    include,
    exclude,
    no_source_guard: bool,
    accurate_mask: bool,
    duration: float,
    repair: str | None = None,
    session_file: str | None = None,
    shuffle: bool = False,
    keep_awake: bool = True,
) -> None:
    limits = ScanLimits(
        max_file_size=max_file_size or LIVE_LIMITS.max_file_size,
        max_files=LIVE_LIMITS.max_files,
        max_total_bytes=max_total_bytes or LIVE_LIMITS.max_total_bytes,
        max_depth=LIVE_LIMITS.max_depth,
        max_path_len=LIVE_LIMITS.max_path_len,
        max_name_len=LIVE_LIMITS.max_name_len,
    )

    record = None
    if repair:
        record = load_session(session_file or DEFAULT_SESSION_FILE)
        roots = [absolute for _relative, absolute in record.files] or list(record.sources)

    if not roots:
        raise InputError("请给一个文件/目录；只播放指定区间用 --repair <分片范围>")

    before = None
    if not no_source_guard:
        before = snapshot_paths(roots)
        activate_source_guard(roots)

    try:
        if record is not None:
            _send_repair(
                record,
                repair,
                fps=fps,
                windowed=windowed,
                dump_pages=dump_pages,
                duration=duration,
                camera=camera,
                accurate_mask=accurate_mask,
                keep_awake=keep_awake,
            )
            return

        result = scan_all(list(roots), limits=limits, include=include, exclude=exclude)
        if not result.sources:
            raise InputError("没有可编码的文件（检查路径、--include/--exclude）")
        if len(result.absolute_paths) != len(result.sources):
            raise InputError("内部错误：扫描结果的路径数量不一致")

        for issue in result.skipped:
            _warn(f"跳过 {issue.path} — {issue.reason}")

        width, height = _screen_size(screen_size)
        chosen_ecc = "H" if camera else ("M" if ecc.lower() == "auto" else ecc.upper())
        min_px = 6 if camera else 4

        layout = choose_layout(
            width,
            height,
            ecc=chosen_ecc,
            mode=mode,
            module_px=module_px,
            min_module_px=min_px,
            max_codes=max_codes,
        )

        manifest, blob = manifest_for_sources(
            root=result.root_label,
            sources=result.sources,
            dirs=result.empty_dirs,
            symlinks=result.symlinks,
            skipped=[(issue.path, issue.reason) for issue in result.skipped],
            chunk_size=layout.body_capacity,
        )
        plan = build_session(
            manifest=manifest,
            blob=blob,
            session_id=new_session_id(),
            chunk_size=layout.body_capacity,
            compression=compression,
        )

        if not dry_run:
            session_path = (
                Path(session_file) if session_file else Path(DEFAULT_SESSION_FILE)
            )
            files = tuple(
                (source.path, absolute)
                for source, absolute in zip(result.sources, result.absolute_paths)
            )
            try:
                save_session(
                    session_path,
                    SessionRecord(
                        session_id=plan.session_id,
                        chunk_size=layout.body_capacity,
                        data_compression=plan.data_compression,
                        manifest_compression=plan.manifest_compression,
                        layout=layout,
                        manifest=plan.manifest,
                        sources=tuple(str(Path(root).resolve()) for root in roots),
                        total_frames=plan.total,
                        files=files,
                    ),
                )
                _echo(f"会话存档：{session_path}（区间播放用）")
            except OSError as exc:
                # 写不了存档绝不能影响正常播放
                _warn(f"会话存档写不了（{exc}）；本次传输不受影响，但命令行区间播放不可用。")

        pages = layout.page_count(plan.total)
        # 固定掩码让渲染快约 4.8 倍；拍屏（--camera）换回最优掩码保稳。
        mask = None if (camera or accurate_mask) else 3
        effective_fps, per_page = _measure_render(plan.frames, layout, pages, fps, mask)
        cycle_seconds = pages / max(effective_fps, 1e-6)
        ratio = (100.0 * plan.data_bytes / len(blob)) if blob else 0.0

        _echo("")
        _echo(f"输入    {result.file_count} 个文件 / {len(result.empty_dirs)} 个空目录，"
              f"{_human_bytes(result.total_bytes)}")
        _echo(f"压缩    {plan.manifest.compression}："
              f"{_human_bytes(len(blob))} -> {_human_bytes(plan.data_bytes)}"
              f"（{ratio:.1f}%）")
        _echo(f"布局    {layout.describe()}")
        _echo(f"分片    {plan.total} 片 · {pages} 屏 · 渲染 {per_page * 1000:.0f} ms/屏 "
              f"→ 实际 {effective_fps:.1f} fps · 约 {_human_seconds(cycle_seconds)}/轮")
        _echo(f"屏幕    {width}×{height} · 掩码 "
              f"{'最优（慢）' if mask is None else f'固定 {mask}（快）'} · 会话 {plan.session_hex}")
        if duration > 0 and not dry_run:
            _echo(f"自动退出：{duration / 60:.0f} 分钟后（--duration 0 可关闭）")

        if dry_run:
            _echo("")
            _echo("候选布局（按单屏净载荷排序）：")
            for index, candidate in enumerate(
                render_variants(
                    width,
                    height,
                    ecc=chosen_ecc,
                    mode=mode,
                    module_px=module_px,
                    min_module_px=min_px,
                    max_codes=max_codes,
                ),
                1,
            ):
                _echo(f"  {index}. {candidate.describe()}")
            _echo("")
            _echo("这是 --dry-run，没有播放。")
            return

        if dump_pages:
            target = Path(dump_pages)
            target.mkdir(parents=True, exist_ok=True)
            for index in range(pages):
                page = compose_page(plan.frames, index, layout, fps=fps, mask=mask)
                page.save(target / f"page_{index:05d}.png", compress_level=1)
            _ok(f"已写出 {pages} 屏到 {target}")
            _echo(f"接收端：qrfile decode {target} -o <还原目录>")
            return

        _echo("")
        _echo("接收端命令：qrfile recv <输出目录>")
        _echo("播放中：空格 暂停，↑/↓ 调速，底部输入框填播放区间，Esc 退出")
        _echo("")
        awake = KeepAwake()
        if keep_awake:
            awake.start()
        if awake.backend:
            _echo(f"已阻止息屏（{awake.backend}）")
        try:
            try:
                Player(
                    plan.frames,
                    layout,
                    fps=fps,
                    windowed=windowed,
                    mask=mask,
                    shuffle=shuffle,
                ).run(duration=duration)
            finally:
                awake.stop()
            _print_repair_hint(plan.total)
        except ImportError as exc:
            raise InputError(
                f"没有图形界面（{exc}）。可以用 --dump-pages <目录> 导出整屏 PNG。"
            ) from exc
    finally:
        if not no_source_guard:
            deactivate_source_guard()
            if before is not None:
                assert_paths_untouched(roots, before)


@click.command("send")
@click.argument("sources", nargs=-1, required=False, type=click.Path(exists=True))
@click.option("--ecc", type=click.Choice(["auto", "L", "M", "Q", "H"]), default="auto",
              show_default=True, help="纠错等级；auto 时截图用 M、拍屏用 H")
@click.option("--mode", type=click.Choice(["auto", "single"]), default="auto",
              show_default=True,
              help="auto=一屏多码（默认，吞吐最大）；single=一屏一码（最稳）")
@click.option("--module-px", type=int, default=None, help="每模块像素；默认自动在 4~8 之间选")
@click.option("--screen", "screen_size", default="auto", show_default="自动探测", metavar="WxH")
@click.option("--fps", type=float, default=5.0, show_default=True, help="播放帧率")
@click.option("--compression", type=click.Choice(COMPRESSION_VALUES), default="auto",
              show_default=True, help="auto=只用标准库；max=再试 brotli/zstd/7z")
@click.option("--max-codes", type=int, default=64, show_default=True, help="一屏最多几个码")
@click.option("--camera", is_flag=True, help="按手机拍屏优化：ECC H + 更大模块 + 最优掩码")
@click.option("--accurate-mask", is_flag=True, hidden=True)
@click.option("--windowed", is_flag=True, hidden=True)
@click.option("--duration", type=float, default=1800.0, show_default=True,
              help="多少秒后自动停止；0=一直循环（默认 30 分钟，防卡死）")
@click.option("--repair", default=None, metavar="RANGES",
              help="只播放指定分片区间，例如 12-14,204（需先正常播放过一次）")
@click.option("--session-file", default=None, metavar="PATH", hidden=True)
@click.option("--keep-awake/--no-keep-awake", default=True, hidden=True)
@click.option("--dump-pages", type=click.Path(file_okay=False), default=None,
              help="不播放，把整屏页写成 PNG（无头环境 / 离线测试）")
@click.option("--dry-run", is_flag=True, help="只计算、不播放、不写文件")
@click.option("--max-file-size", type=int, default=None, hidden=True)
@click.option("--max-total-bytes", type=int, default=None, hidden=True)
@click.option("--include", multiple=True, hidden=True)
@click.option("--exclude", multiple=True, hidden=True)
@click.option("--no-source-guard", is_flag=True, hidden=True)
def send_cmd(**kwargs) -> None:
    """高级命令：手动调参播放。日常请直接用 ``qrfile push <路径>``。"""
    sources = kwargs.get("sources")
    if not sources and not kwargs.get("repair"):
        raise click.UsageError("请给一个文件/目录，或用 --repair 播放指定区间")
    _run(lambda: _send_impl(kwargs.pop("sources"), **kwargs))


# --------------------------------------------------------------------------
# recv：采集 + 实时解码 + 还原
# --------------------------------------------------------------------------


def _decode_symbols(image, *, robust: bool):
    if robust:
        return decode_robust(image, source="屏幕").symbols
    return decode_image(image, source="屏幕")


def _restore(collector: LiveCollector, output: str, *, overwrite: bool, restore_times: bool) -> None:
    session, stats = collector.finalize()
    _ok(
        f"收齐 {stats.accepted} 片（重复 {stats.duplicates}、坏帧 {stats.damaged}、"
        f"外来码 {stats.foreign}）"
    )
    report = restore_session(
        session,
        RestoreOptions(
            output_dir=Path(output),
            overwrite=overwrite,
            restore_times=restore_times,
        ),
    )
    for path, reason in report.skipped:
        _warn(f"跳过 {path} — {reason}")
    for original, renamed in report.renamed:
        _warn(f"名字被转义：{original} -> {renamed}")
    _ok(f"还原 {report.written_count} 个文件到 {Path(output).resolve()}")
    if not report.is_byte_identical:
        _warn("有文件名被改动（内容仍然逐字节校验通过）")


def _receive_loop(
    output: str,
    *,
    rect,
    description: str,
    fps: float,
    overwrite: bool,
    no_restore_times: bool,
    max_seconds: float,
    save_frames: str | None,
    robust: bool,
    capture: str = "auto",
    keep_awake: bool = True,
) -> None:
    options = CaptureOptions(region=rect, fps=fps, backend=capture)
    collector = LiveCollector()
    frames_dir = Path(save_frames) if save_frames else None
    if frames_dir is not None:
        frames_dir.mkdir(parents=True, exist_ok=True)

    _echo(f"采集：{description}，{fps:.1f} fps")
    if rect is not None:
        left, top, right, bottom = rect
        _echo(f"区域：{left},{top} · {right - left}x{bottom - top}")
    else:
        _echo("区域：整屏")
    _echo("集齐后自动停止；Ctrl+C 放弃。")
    _echo("")

    last_draw = 0.0
    last_missing = 0.0
    saved = 0
    frames_seen = 0
    debug_saved = False
    started = time.time()
    awake = KeepAwake()
    if keep_awake:
        awake.start()
    if awake.backend:
        _echo(f"已阻止息屏（{awake.backend}）")
    try:
        for image in iter_frames(options, max_seconds=max_seconds):
            frames_seen += 1
            if frames_seen == 1 and _looks_blank(image):
                _warn(
                    "采集到的第一帧几乎是纯色 —— 可能息屏/锁屏、窗口被遮挡，"
                    "或 --region 不对。"
                )
            if frames_dir is not None:
                saved += 1
                image.save(frames_dir / f"frame_{saved:06d}.png", compress_level=1)

            try:
                symbols = _decode_symbols(image, robust=robust)
            except InputError:
                symbols = ()

            for symbol in symbols:
                collector.feed(symbol.payload)

            now = time.time()
            if now - last_draw >= 0.25:
                _progress_line(collector.progress())
                last_draw = now

            # 每 5 秒把"缺哪几片"打出来，方便直接复制到播放器里
            if collector.total and now - last_missing >= 5.0:
                missing_now = collector.missing()
                if missing_now:
                    click.echo(
                        f"\n缺 {len(missing_now)} 片："
                        f"{format_ranges(missing_now, limit=25)}"
                    )
                last_missing = now

            # 5 秒还没解出任何码：把最后一帧存下来，方便判断是"画面错"还是"码太糊"
            if (
                not debug_saved
                and collector.total == 0
                and frames_seen >= 3
                and time.time() - started >= 5.0
            ):
                debug_path = Path.cwd() / "qrfile-debug.png"
                try:
                    image.save(debug_path)
                    _warn(f"5 秒内没解出任何码，已把最后一帧存到：{debug_path}")
                    _warn("打开看看：是黑屏/纯色，还是有二维码但模糊？")
                except OSError:
                    pass
                debug_saved = True

            if collector.complete:
                break
    except KeyboardInterrupt:
        click.echo("")
    finally:
        awake.stop()

    _progress_line(collector.progress())
    click.echo("")

    if not collector.complete:
        progress = collector.progress()
        if progress.total == 0:
            _warn(
                "一帧都没解出来。检查：播放端是否在播放？采集区域是否包含二维码？"
                "远端画面是否被缩放/压缩？可以加 --robust 再试。"
            )
            if debug_saved:
                _warn(f"诊断帧：{Path.cwd() / 'qrfile-debug.png'}（先看这张图）")
        else:
            missing = collector.missing()
            shown = format_ranges(missing, limit=1000)
            _warn(f"还缺 {len(missing)}/{progress.total} 片：{shown}")
            _warn("=" * 60)
            _warn("播放区间（保持本窗口运行）：")
            _warn("  1) 在 A 机器播放器底部的「播放区间」输入框填入上面的范围，回车；")
            _warn("  2) 或退出播放器后，在 A 机器执行：")
            _warn(f'     qrfile push --repair "{shown}"')
            _warn("=" * 60)
            _warn("区间播放结束后，本窗口会继续接收并自动还原。")
        sys.exit(EXIT_VERIFY_FAILED)

    _restore(
        collector,
        output,
        overwrite=overwrite,
        restore_times=not no_restore_times,
    )


def _recv_impl(
    output: str,
    *,
    region: str | None,
    window: str | None,
    fps: float,
    overwrite: bool,
    no_restore_times: bool,
    max_seconds: float,
    save_frames: str | None,
    robust: bool,
    capture: str = "auto",
    keep_awake: bool = True,
) -> None:
    rect, description = resolve_region(region=region, window=window)
    _receive_loop(
        output,
        rect=rect,
        description=description,
        fps=fps,
        overwrite=overwrite,
        no_restore_times=no_restore_times,
        max_seconds=max_seconds,
        save_frames=save_frames,
        robust=robust,
        capture=capture,
        keep_awake=keep_awake,
    )


def _pull_impl(
    output: str,
    *,
    safe: bool,
    window: str | None,
    region: str | None,
    capture: str = "auto",
    keep_awake: bool = True,
) -> None:
    if window or region:
        rect, description = resolve_region(region=region, window=window)
        if window and activate_window(window):
            _echo("已把目标窗口调到前台")
            time.sleep(0.4)
    else:
        guessed = guess_remote_window()
        if guessed is not None:
            rect, title = guessed
            description = f"自动定位窗口 {title!r}"
            _echo(f"自动定位到：{description}")
            if activate_window(title):
                _echo("已把目标窗口调到前台")
                time.sleep(0.4)
        else:
            rect = None
            description = "整个屏幕（没识别到远程桌面窗口，通常也能用）"

    _receive_loop(
        output,
        rect=rect,
        description=description,
        fps=6.0 if safe else 8.0,
        overwrite=False,
        no_restore_times=False,
        max_seconds=0.0,
        save_frames=None,
        robust=safe,
        capture=capture,
        keep_awake=keep_awake,
    )


# --------------------------------------------------------------------------
# 极简入口：push / pull —— 日常只需要这两条
# --------------------------------------------------------------------------


@click.command("push")
@click.argument("sources", nargs=-1, required=False, type=click.Path(exists=True))
@click.option("--fast", is_flag=True,
              help="一屏多码（现在已是默认，保留兼容）")
@click.option("--safe", is_flag=True,
              help="更稳：一屏一码 + 更大模块（远端画面被缩放时用）")
@click.option("--camera", is_flag=True, help="用手机拍屏（ECC H + 最优掩码 + 一屏一码）")
@click.option("--keep-awake/--no-keep-awake", default=True, hidden=True)
@click.option("--duration", type=float, default=1800.0, show_default=True,
              help="多少秒后自动停；0=一直循环（默认 30 分钟，防卡死）")
@click.option("--repair", default=None, metavar="RANGES",
              help="只播放指定分片区间，例如 12-14,204（保持接收端运行）")
def push_cmd(
    sources,
    fast: bool,
    safe: bool,
    camera: bool,
    duration: float,
    repair: str | None,
    keep_awake: bool,
) -> None:
    """A 机器：把文件/目录播出去。日常就一条：qrfile push <路径>"""
    if repair:
        if fast or safe or camera:
            raise click.UsageError("--repair 不能和 --fast/--safe/--camera 同时用")
        _run(
            lambda: _send_impl(
                (),
                ecc="auto",
                mode="auto",
                module_px=None,
                screen_size="auto",
                fps=5.0,
                compression="auto",
                max_codes=64,
                camera=False,
                windowed=False,
                dry_run=False,
                dump_pages=None,
                max_file_size=None,
                max_total_bytes=None,
                include=(),
                exclude=(),
                no_source_guard=False,
                accurate_mask=False,
                duration=duration,
                repair=repair,
                session_file=None,
                shuffle=False,
                keep_awake=keep_awake,
            )
        )
        return

    if not sources:
        raise click.UsageError("请给一个文件/目录，或用 --repair 播放指定区间")
    if fast and safe:
        raise click.UsageError("--fast 和 --safe 只能二选一")
    if camera and fast:
        raise click.UsageError("--camera 和 --fast 不能同时用")

    # 默认 fast：一屏多码。--safe / --camera 退回一屏一码保稳。
    mode = "single" if (safe or camera) else "auto"
    module_px = 6 if (safe or camera) else None
    ecc = "H" if (safe or camera) else "auto"
    fps = 3.0 if camera else (4.0 if safe else 5.0)

    _run(
        lambda: _send_impl(
            sources,
            ecc=ecc,
            mode=mode,
            module_px=module_px,
            screen_size="auto",
            fps=fps,
            compression="auto",
            max_codes=64,
            camera=camera,
            windowed=False,
            dry_run=False,
            dump_pages=None,
            max_file_size=None,
            max_total_bytes=None,
            include=(),
            exclude=(),
            no_source_guard=False,
            accurate_mask=camera,
            duration=duration,
            keep_awake=keep_awake,
        )
    )


@click.command("pull")
@click.argument("output", type=click.Path(file_okay=False))
@click.option("--safe", is_flag=True, help="更稳：多策略解码（画面被缩放时用）")
@click.option("--window", default=None, help="手动指定窗口标题（通常不需要）")
@click.option("--region", default=None, help="手动指定区域 x,y,w,h（通常不需要）")
@click.option("--capture", type=click.Choice(["auto", "dxcam", "mss", "imagegrab"]),
              default="auto", hidden=True, help="采集后端（诊断用）")
@click.option("--keep-awake/--no-keep-awake", default=True, hidden=True)
def pull_cmd(
    output: str,
    safe: bool,
    window: str | None,
    region: str | None,
    capture: str,
    keep_awake: bool,
) -> None:
    """B 机器：采集屏幕上的二维码并还原。日常就一条：qrfile pull <输出目录>"""
    _run(
        lambda: _pull_impl(
            output,
            safe=safe,
            window=window,
            region=region,
            capture=capture,
            keep_awake=keep_awake,
        )
    )


@click.command("recv")
@click.argument("output", type=click.Path(file_okay=False))
@click.option("--region", default=None, help="采集区域 x,y,w,h；默认整屏")
@click.option("--window", default=None, help="按窗口标题子串定位（例如远程桌面窗口）")
@click.option("--fps", type=float, default=8.0, show_default=True, help="采集帧率")
@click.option("--overwrite", is_flag=True, help="允许覆盖输出目录里已存在的文件")
@click.option("--no-restore-times", is_flag=True, help="不恢复文件修改时间")
@click.option("--max-seconds", type=float, default=0.0, show_default=True, help="最长采集时间，0=不限")
@click.option("--save-frames", type=click.Path(file_okay=False), default=None,
              help="顺手把采集到的帧也存下来（便于事后重解）")
@click.option("--robust", is_flag=True, help="每帧多策略尝试（慢，适合手机拍屏）")
@click.option("--capture", type=click.Choice(["auto", "dxcam", "mss", "imagegrab"]),
              default="auto", hidden=True, help="采集后端（诊断用）")
@click.option("--keep-awake/--no-keep-awake", default=True, hidden=True)
def recv_cmd(**kwargs) -> None:
    """采集屏幕上的二维码流，收齐后自动还原成文件树。"""
    _run(lambda: _recv_impl(kwargs.pop("output"), **kwargs))


# --------------------------------------------------------------------------
# record：只录屏
# --------------------------------------------------------------------------


def _record_impl(
    output: str,
    *,
    region: str | None,
    window: str | None,
    fps: float,
    max_seconds: float,
    capture: str = "auto",
    keep_awake: bool = True,
) -> None:
    rect, description = resolve_region(region=region, window=window)
    options = CaptureOptions(region=rect, fps=fps, backend=capture)

    _echo(f"录屏：{description}，{fps:.1f} fps，只保存画面变化的帧")
    _echo("Ctrl+C 停止。")
    _echo("")

    def progress(count: int, elapsed: float) -> None:
        sys.stdout.write(f"\r已保存 {count} 帧，{elapsed:.1f} 秒")
        sys.stdout.flush()

    saved = 0
    awake = KeepAwake()
    if keep_awake:
        awake.start()
    if awake.backend:
        _echo(f"已阻止息屏（{awake.backend}）")
    try:
        saved = record_frames(options, output, max_seconds=max_seconds, progress=progress)
    except KeyboardInterrupt:
        pass
    finally:
        awake.stop()

    click.echo("")
    _ok(f"保存 {saved} 帧到 {Path(output).resolve()}")
    _echo(f"下一步：qrfile decode {output} -o <还原目录>")


@click.command("record")
@click.argument("output", type=click.Path(file_okay=False))
@click.option("--region", default=None, help="采集区域 x,y,w,h；默认整屏")
@click.option("--window", default=None, help="按窗口标题子串定位")
@click.option("--fps", type=float, default=10.0, show_default=True, help="采集帧率")
@click.option("--max-seconds", type=float, default=0.0, show_default=True, help="最长录制时间，0=不限")
@click.option("--capture", type=click.Choice(["auto", "dxcam", "mss", "imagegrab"]),
              default="auto", hidden=True, help="采集后端（诊断用）")
@click.option("--keep-awake/--no-keep-awake", default=True, hidden=True)
def record_cmd(**kwargs) -> None:
    """把屏幕录成无损 PNG 帧序列（之后用 decode 还原）。"""
    _run(lambda: _record_impl(kwargs.pop("output"), **kwargs))


# --------------------------------------------------------------------------
# decode-video：从已有视频还原
# --------------------------------------------------------------------------


def _decode_video_impl(
    videos,
    *,
    output: str,
    overwrite: bool,
    no_restore_times: bool,
) -> None:
    collector = LiveCollector()
    for video in videos:
        _echo(f"解码 {video} …")
        frames = 0
        for image in iter_video_frames(video):
            frames += 1
            try:
                symbols = decode_image(image, source=str(video))
            except InputError:
                continue
            for symbol in symbols:
                collector.feed(symbol.payload)
            if frames % 60 == 0:
                _progress_line(collector.progress())
            if collector.complete:
                break
        click.echo("")
        if collector.complete:
            break

    if not collector.complete:
        progress = collector.progress()
        if progress.total == 0:
            _warn("视频里没有解出任何 qrfile 分片。")
        else:
            _warn(f"还缺 {len(collector.missing())}/{progress.total} 片。")
        sys.exit(EXIT_VERIFY_FAILED)

    _restore(
        collector,
        output,
        overwrite=overwrite,
        restore_times=not no_restore_times,
    )


@click.command("decode-video")
@click.argument("videos", nargs=-1, required=True, type=click.Path(exists=True))
@click.option("-o", "--output", required=True, type=click.Path(file_okay=False),
              help="还原到哪个目录")
@click.option("--overwrite", is_flag=True)
@click.option("--no-restore-times", is_flag=True)
def decode_video_cmd(**kwargs) -> None:
    """从 mp4/mkv/avi/gif 等视频里抽帧并还原（需要 OpenCV）。"""
    _run(lambda: _decode_video_impl(kwargs.pop("videos"), **kwargs))


# --------------------------------------------------------------------------
# compressors：列出本机能力
# --------------------------------------------------------------------------


@click.command("doctor")
def doctor_cmd() -> None:
    """检查本机能力：图形界面、抓屏、可选依赖、推荐布局。"""
    _run(_doctor_impl)


def _doctor_impl() -> None:
    import platform

    from . import __version__

    _echo(f"qrfile {__version__} · Python {platform.python_version()} · {platform.platform()}")
    _echo("")

    checks: list[tuple[str, str, str]] = []

    try:
        import tkinter

        checks.append(("tkinter（全屏播放）", "可用", f"Tk {tkinter.TkVersion}"))
    except Exception as exc:  # pragma: no cover - 环境相关
        checks.append(("tkinter（全屏播放）", "不可用", str(exc)[:60]))

    try:
        import numpy

        checks.append(("numpy（快速渲染）", "可用", numpy.__version__))
    except ImportError:
        checks.append(("numpy（快速渲染）", "未安装", "仍可用，只是渲染慢一点"))

    if sys.platform == "win32":
        try:
            import dxcam  # noqa: F401

            checks.append(
                ("dxcam（DXGI 抓屏）", "可用", "优先用于 VNC/RDP 硬件加速窗口")
            )
        except Exception as exc:
            checks.append(("dxcam（DXGI 抓屏）", "不可用", str(exc)[:60]))

    try:
        import pygetwindow  # noqa: F401

        checks.append(("pygetwindow（--window）", "可用", "按窗口标题定位"))
    except ImportError:
        checks.append(("pygetwindow（--window）", "未安装", "pip install pygetwindow"))

    try:
        import cv2

        checks.append(("OpenCV（decode-video）", "可用", cv2.__version__))
    except ImportError:
        checks.append(("OpenCV（decode-video）", "未安装", "pip install opencv-python-headless"))

    try:
        from .capture import grab, grab_backend

        shot = grab(None)
        checks.append(
            ("抓屏", "可用", f"{shot.size[0]}×{shot.size[1]} · 后端 {grab_backend()}")
        )
        shot.close()
    except Exception as exc:  # pragma: no cover - 环境相关
        checks.append(("抓屏", "不可用", str(exc)[:60]))

    for name, status, note in checks:
        _echo(f"{name:<26} {status:<6} {note}")

    _echo("")
    try:
        from .cli import detect_screen_size

        size = detect_screen_size()
    except Exception:  # pragma: no cover
        size = None
    if size:
        layout = choose_layout(*size)
        _echo(f"推荐布局：{layout.describe()}")
    else:
        _echo("屏幕分辨率：探测不到（可以用 send --screen WxH 手动指定）")

    _echo("")
    _echo("压缩器：")
    for name, status, note in describe_availability():
        _echo(f"  {name:<8} {status:<6} {note}")


@click.command("compressors")
def compressors_cmd() -> None:
    """列出本机可用的压缩器。"""
    _echo("压缩器                 状态    说明")
    _echo("-" * 78)
    for name, status, note in describe_availability():
        _echo(f"{name:<22} {status:<6} {note}")
    _echo("")
    _echo("默认 --compression auto 只用标准库（zlib / lzma），解码端无需额外软件。")
    _echo("--compression max 会额外尝试外部工具，但解码端也必须安装同一个。")
