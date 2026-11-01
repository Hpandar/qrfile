"""qrfile 命令行入口。

退出码（方案第十二章）::

    0  成功
    1  部分跳过但成功
    2  校验失败
    3  输入错误
    4  内部错误（含只读护栏被触发 —— 正常运行中永远不该出现）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click

from . import __version__
from .archive import build_session, manifest_for_sources, reassemble
from .capacity import (
    DEFAULT_PROFILE,
    PROFILES,
    SCAN_SECONDS_PER_CODE,
    format_duration,
    get_profile,
)
from .chunk import new_session_id
from .decode import (
    RestoreOptions,
    decode_file,
    find_image_files,
    restore_session,
)
from .errors import (
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_PARTIAL,
    EXIT_VERIFY_FAILED,
    InputError,
    QrFileError,
)
from .manifest import COMPRESSION_AUTO, COMPRESSION_CHOICES, COMPRESSION_NONE, display_path
from .output import OutputOptions, write_session
from .pathsafe import check_output_isolated
from .readonly import (
    activate_source_guard,
    assert_paths_untouched,
    deactivate_source_guard,
    noatime_degraded,
    snapshot_paths,
)
from .render import DEFAULT_MARGIN, DEFAULT_SCALE, Renderer
from .scan import ScanLimits, scan_paths

PROFILE_CHOICES = sorted(PROFILES)


# --------------------------------------------------------------------------
# 输出编码：绝不因为"控制台编码打不出某个字符"而崩掉
# --------------------------------------------------------------------------
#
# 踩过的坑：在中文 Windows 上把输出重定向到文件或管道时，stdout 的编码是 gbk，
# 而 ✓ (U+2713) 不在 gbk 里 —— 程序会直接抛 UnicodeEncodeError 崩掉。
# 交互式控制台没事（Python 用 WriteConsoleW 直写），**只有重定向时才犯**，
# 所以特别容易被漏掉：自己测的时候一路 `| tail`，恰好就踩上了。
#
# 两道防线：
#   1. 把流的错误处理器换成 replace —— 保证任何字符都不会让程序崩
#      （中文在 gbk 下没问题；实在打不出的字符变成 ? 而不是异常）
#   2. 状态符号按目标流的编码能力挑：打不出 ✓ 就用 [OK]
#      （不这么做的话，中文用户会看到一堆 ?，比崩了好但也很难看）


def _can_encode(stream, text: str) -> bool:
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return False
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


_marker_cache: dict[str, tuple[str, str]] = {}


def _markers_for(stream) -> tuple[str, str]:
    """返回该流能安全打印的 ``(成功符号, 失败符号)``。"""
    key = str(getattr(stream, "encoding", "") or "")
    if key not in _marker_cache:
        _marker_cache[key] = (
            ("✓", "✗") if _can_encode(stream, "✓✗") else ("[OK]", "[!!]")
        )
    return _marker_cache[key]


def _setup_output() -> None:
    """给 stdout / stderr 装上 replace 错误处理器。

    在**导入时**就执行 —— 不能放在 main() 里，因为 ``qrfile --help`` 是 click
    在解析参数阶段就打印并退出的，根本走不到 main() 的函数体。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            # 流被替换过（比如测试里捕获输出）或者不支持 reconfigure
            pass
    _marker_cache.clear()


_setup_output()

#: 上限的默认值。必须是**实例** —— ScanLimits 用了 slots=True，
#: 类属性是 member_descriptor 而不是默认值，直接取会拿到描述符对象。
_DEFAULT_LIMITS = ScanLimits()

#: 供 --help 展示用的可读默认值（真值见 _DEFAULT_LIMITS）
_DEFAULT_LIMITS_TEXT = {
    "max_file_size": "8M",
    "max_total_bytes": "64M",
}

#: plan 在输入不超过这个体积时，会真的压一遍算出**准确**张数，而不是估算。
#: 超过就退回估算，免得"只算不生成"的 plan 自己跑很久。
PLAN_EXACT_LIMIT = 32 * 1024 * 1024


def _size(value: str) -> int:
    """把 ``8M`` / ``512K`` / ``1048576`` 这类写法解析成字节数。

    新人面对 ``--max-file-size 8388608`` 是没法用的，所以允许带单位。
    """
    text = str(value).strip().upper().replace("B", "")
    factor = 1
    for suffix, multiplier in (("K", 1024), ("M", 1024 ** 2), ("G", 1024 ** 3)):
        if text.endswith(suffix):
            factor = multiplier
            text = text[:-1]
            break
    try:
        number = float(text)
    except ValueError:
        raise click.BadParameter(
            f"看不懂的大小写法：{value!r}（可以写 8M / 512K / 1048576）"
        ) from None
    if number < 0:
        raise click.BadParameter("大小不能是负数")
    return int(number * factor)


# --------------------------------------------------------------------------
# 输出小工具
# --------------------------------------------------------------------------


def _echo(message: str = "") -> None:
    click.echo(message)


def _warn(message: str) -> None:
    click.secho(f"  ! {message}", fg="yellow", err=True)


def _ok(message: str) -> None:
    marker = _markers_for(sys.stdout)[0]
    click.secho(f"  {marker} {message}", fg="green")


def _run(action) -> None:
    """跑一个命令体，把 qrfile 的异常翻译成退出码和人话。"""
    try:
        action()
    except QrFileError as exc:
        click.secho(f"\n{_markers_for(sys.stderr)[1]} {exc}", fg="red", err=True)
        sys.exit(exc.exit_code)
    except KeyboardInterrupt:  # pragma: no cover
        click.secho("\n已中断。", fg="red", err=True)
        sys.exit(EXIT_INTERNAL)


def _report_skips(skipped, *, limit: int = 25) -> None:
    if not skipped:
        return
    click.secho(f"  跳过 {len(skipped)} 个条目：", fg="yellow")
    for issue in skipped[:limit]:
        _warn(f"{issue.path} —— {issue.reason}")
    if len(skipped) > limit:
        _warn(f"…… 另有 {len(skipped) - limit} 个未列出")


def _limits_from_options(**kwargs) -> ScanLimits:
    return ScanLimits(
        max_file_size=kwargs["max_file_size"],
        max_files=kwargs["max_files"],
        max_total_bytes=kwargs["max_total_bytes"],
        max_depth=kwargs["max_depth"],
        max_path_len=kwargs["max_path_len"],
        max_name_len=kwargs["max_name_len"],
    )


def _common_scan_options(func):
    """encode 和 plan 共用的扫描参数。"""
    options = [
        click.option("--include", multiple=True, metavar="GLOB",
                     help="只收匹配这个通配符的文件（可重复）"),
        click.option("--exclude", multiple=True, metavar="GLOB",
                     help="排除匹配这个通配符的文件（可重复）"),
        click.option("--text-encodings", is_flag=True,
                     help="额外接受 Big5 / Shift-JIS / EUC-KR（默认只认 UTF-8 系和 GB18030）"),
        click.option("--follow-symlinks", is_flag=True,
                     help="跟随符号链接。默认只把链接本身记进清单，不跟随"),
        click.option("--strict", is_flag=True,
                     help="只要有任何跳过或文件名冲突就中止（默认是跳过并继续）"),
        click.option("--max-file-size", type=_size, metavar="SIZE",
                     default=_DEFAULT_LIMITS_TEXT["max_file_size"], show_default=True,
                     help="单个文件超过这个大小就跳过。可写 8M / 512K / 1048576"),
        click.option("--max-files", type=int, default=_DEFAULT_LIMITS.max_files, show_default=True,
                     help="整批文件数超过上限就报错（不是跳过）"),
        click.option("--max-total-bytes", type=_size, metavar="SIZE",
                     default=_DEFAULT_LIMITS_TEXT["max_total_bytes"], show_default=True,
                     help="整批总字节超过上限就报错（不是跳过）"),
        click.option("--max-depth", type=int, default=_DEFAULT_LIMITS.max_depth, show_default=True,
                     help="目录递归的最大层数"),
        click.option("--max-path-len", type=int, default=_DEFAULT_LIMITS.max_path_len, show_default=True,
                     help="相对路径超过这么多字节就跳过（按 UTF-8 字节数算）"),
        click.option("--max-name-len", type=int, default=_DEFAULT_LIMITS.max_name_len, show_default=True,
                     help="单个文件名超过这么多字节就跳过（NTFS 上限 255）"),
    ]
    for option in reversed(options):
        func = option(func)
    return func


# --------------------------------------------------------------------------
# 顶层
# --------------------------------------------------------------------------


_CHEAT_SHEET = """\
qrfile —— 日常只需要两条命令，每条只带一个路径：

  A 机器（被隔离的那台）：   qrfile push  <文件或目录>
  B 机器（你手边这台）：     qrfile pull  <输出目录>

  · 远端画面被缩放：两边都加 --safe
  · 想快 2~3 倍：     A 机器加 --fast
  · 用手机拍屏：      A 机器加 --camera
  · 漏了几片：        接收端会打印补播命令，照抄执行即可

  其他命令（一般不用记）：qrfile --help
"""


@click.group(
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
)
@click.version_option(__version__, prog_name="qrfile")
@click.pass_context
def main(ctx: click.Context) -> None:
    """qrfile —— 把文本文件变成二维码，再还原回逐字节一致的文件。

    \b
    【新人三步】先跑这三条，就知道这件事要花多少功夫
        qrfile plan   ./你的目录                看要出多少张码、要扫多久
        qrfile encode ./你的目录 -o ./qrout     编码
        qrfile decode ./qrout    -o ./restore   还原

    \b
    【只收文本文件】
      收：源码 / 配置 / 日志 / Markdown / CSV / JSON / XML / YAML / TOML / SVG /
          i18n 资源；UTF-8（有无 BOM 都行）、UTF-16/32、GBK / GB18030 中文。
      不收：图片 / 音视频 / 压缩包 / PDF / 字体 / 可执行文件 / Pickle / MsgPack，
            以及 docx xlsx pptx（它们本质是 zip，不是文本）。
      每一个被跳过的都会打印原因，不会悄悄丢。

    \b
    【最要紧的一个选择：--profile】
      screen  默认｜每张码 826 字节｜屏幕显示 + 手机拍
      camera  每张 260 字节｜手机实时扫，最抗拍歪
      print   每张 1338 字节｜打印到纸上再扫
      max     每张 2921 字节｜纯数字归档，不经摄像头
      模块越少越抗拍歪。手机拍屏请正对着拍、别太斜（超过约 10 度就扫不出来）。

    \b
    【扫得太慢怎么办】
      · plan 会直接告诉你预计要扫多久
      · encode 加 --contact-sheet：出一张总览图，拍一张照片扫下一整片
      · 文本压缩后通常只剩 20~35%，码数直接降到三分之一

    \b
    【命令】
      plan      预检：多少张码、扫多久、会跳过什么（只算不生成）
      encode    编码成二维码图片
      decode    还原成文件
      verify    复核源与码是否一致（还原到内存里比，不写任何文件）
      info      只看分片头，查缺哪几片
      selftest  内置自检：一条命令验证工具没坏
      guide     完整参考手册 —— profile 实测数据 / 文件类型细则 / 上限 / 已知边界

    \b
    三条承诺：源文件只读 · 还原逐字节一致 · 目录结构完整保留。
    每条都有可验证的证据，见 qrfile guide safety。
    """
    if ctx.invoked_subcommand is None:
        _setup_output()
        click.echo(_CHEAT_SHEET)


# --------------------------------------------------------------------------
# guide
# --------------------------------------------------------------------------

_GUIDE_PROFILES = """\
【选 profile —— 这一个选择决定"能不能扫出来"】

  profile  模块数  每张码载荷  抗梯形畸变  用途
  camera   77     260 字节    5%          手机实时扫屏幕，最抗拍歪
  screen   97     826 字节    5%          屏幕显示 + 手机拍（默认）
  print    137    1338 字节   0%（须正视） 打印到纸上再扫
  max      177    2921 字节   0%（须正视） 纯数字归档，不经摄像头

  核心规律：模块越少越抗畸变 —— 模块少意味着单个模块在画面里更大，
  容错余量更大。屏幕取景难控制，所以默认用 screen；打印时纸面的几何形状
  你能完全控制，可以放心用 print（每张码装 1.6 倍的数据）。

  实测（本机，BICUBIC 透视变换模拟拍照）：
    梯形畸变 0%、5%   → camera / screen 能解
    梯形畸变 ≥10%     → 怎么都解不出（四种二值化、放大、四个 profile 全试过）

  所以：手机拍屏请正对着拍、别太斜，并让码在画面里占够大（单模块 ≥4 像素）。

【实际吞吐量 —— plan 命令会替你算】

  链路          单码载荷  估计吞吐        实用上限
  屏幕 + 手机   826 字节  ~20 KB/分钟     压缩后约 300 KB
  打印 + 手机   1338 字节 ~35 KB/分钟     压缩后约 1 MB
  纯数字归档    2921 字节 CPU 限制        单文件 8 MiB

  注意区分两个时间 —— plan 会分别算给你，别搞混（有人真的搞混过）：

    本机编码耗时  程序自己要跑多久。1.5 MB 文本约 43 秒，很轻快。
    预计人工扫描  你拿手机一张张扫完要多久。同一个文件约 23 分钟。

  真正卡人的是后面那个。程序本身不是瓶颈，瓶颈是"人拿着手机一张张扫"。
  所以 plan 里那句"预计人工扫描"说的是你的时间，不是电脑的时间。

  屏幕是瓶颈，不是打印：v40 的码在 380px（约 2 像素/模块）下手机读不出来，
  1080px 才行；而 A4 上 0.4mm/模块就能装下 v40。

  扫得慢的三个办法：开总览图一次拍一整片；开压缩（文本通常压到 1/3）；
  改用 print 打印出来扫。
"""

_GUIDE_FILES = """\
【收哪些文件 —— 按内容判断，不看扩展名】

  判定基于 magic number + 编码探测 + 熵护栏。扩展名只在内容判不出来时当平手
  的提示，永远不能推翻内容证据（一个叫 a.txt 的 PNG 仍然是二进制）。

  收：
    · ASCII / UTF-8（有无 BOM 都行）
    · UTF-16 LE·BE / UTF-32 LE·BE（带 BOM；不带 BOM 且非 ASCII 的探测不到）
    · GB18030 / GBK / GB2312 —— 中文环境刚需
    · Big5 / Shift-JIS / EUC-KR（需要 --text-encodings 显式开启）
    · CP1252 / Latin-1（低置信度兜底，会给警告）
    · 任意换行风格 CRLF / LF / CR / 混合 —— 原样保留
    · 源码 / 配置 / 日志 / CSV / Markdown / JSON / XML / YAML / TOML / SVG

  跳过并说明原因：
    · 图片 PNG JPEG GIF BMP WebP HEIC TIFF
    · 音视频 MP3 MP4 MKV WAV FLAC AVI
    · 压缩包 ZIP RAR 7z GZ BZ2 XZ TAR
    · docx xlsx pptx jar apk odt epub  ← 本质是 zip，不是文本
    · 二进制文档 PDF DOC XLS PPT(旧) SQLite
    · 可执行/库 EXE DLL SO DYLIB A O CLASS PYC WASM
    · 字体 TTF OTF WOFF WOFF2
    · 序列化二进制 裸 Protobuf Avro Parquet Pickle MsgPack BSON
    · 符号链接（默认只记录不跟随，除非 --follow-symlinks）
    · FIFO / 设备节点 / socket

  最容易混的三组：
    1. Pickle / MsgPack / BSON 是二进制要跳过；JSON / XML / YAML / TOML 是文本要收
    2. SVG 是文本，不是图片
    3. .docx 是 zip，不是文本

  每个被跳过的都会打印 SKIP <路径> — <原因>，并写进清单里，解码端也能看到。

【判定里的熵护栏 —— 这是防误收的关键】实测标定：

    英文 ASCII 4.44 ｜ UTF-8 中文 5.16 ｜ GBK 中文 5.16
    base64 文本 5.60（是文本，放行）｜ zlib 压缩 6.37（是二进制，拦住）｜ 随机 7.98

  阈值取 6.00，正好卡在"base64 文本"和"压缩数据"中间。这一条是必须的 ——
  Latin-1 能"解码"任意字节序列且永不报错，不能拿"能解码"当接受的理由，
  否则一张小 JPEG 也能被当成文本收下。
"""

_GUIDE_LIMITS = """\
【上限】都可以改，默认值如下

  单文件大小      8M        --max-file-size（可写 512K / 8M / 1G）
  整批文件数      5000      --max-files
  整批总字节      64M       --max-total-bytes
  目录深度        64        --max-depth
  相对路径长度    1024 字节 --max-path-len
  文件名长度      255 字节  --max-name-len（NTFS 上限）
  分片索引        uint16，单文件最多 65535 片（物理硬上限）

  两类超限的处理方式刻意不同：

    单个文件的问题（太大 / 路径太长 / 名字非法 / 不是文本）
      → 跳过它、记下原因、继续跑，最后退出码 1

    整批的预算超了（文件数 / 总字节）
      → 直接报错。因为这会悄悄改变归档的含义：你以为整棵树都进去了，
        实际只进去一半。这种事必须大声说出来。

  压缩默认开启（zlib level 9），文本通常压到 20~35%。压不动的数据
  （已压缩的、随机的）会自动不压，不会越压越大。

  没有实现的功能（大文件 / 大目录场景少见，复杂度不值当）：
    分包（--split-by-dir）、纠删冗余（--redundancy）、喷泉码、并行（--jobs）。
    超限时会明确报错并建议你分几次编码不同的子目录。
"""

_GUIDE_SAFETY = """\
【源文件只读 —— 三层，不是口头承诺】

  1. 唯一的读取入口
     全代码库只有 readonly.py 里的 read_bytes / read_prefix / iter_file_bytes
     能打开源文件，永远是 O_RDONLY（Linux 上尽力用 O_NOATIME）。
     临时文件一律进系统临时目录或输出目录，绝不在源文件旁边落 .tmp。

  2. 运行期审计钩子
     通过 sys.addaudithook 在系统调用级拦截：编码期间任何以写意图触碰源路径的
     操作当场抛异常。这给的是"运行时可证明"，不是"我们小心点"。

  3. 事后指纹比对
     读之前和写完之后各采一次源树全量指纹（路径 / 大小 / mtime_ns / 权限 /
     SHA-256）逐项比对，哪怕只是 mtime 变了也报错，并指名道姓说哪个文件的
     哪个字段变了。顺带覆盖读取期间被外部改动的 TOCTOU 情况。

  另外，解码端专门防"从输出侧改到源文件"：

    · -o 指向源目录、或其祖先/后代        → 拒绝
    · 目标已存在且是硬链接                → 拒绝（写它会连带改掉别处的同一份内容）
    · 目标位置已存在符号链接              → 拒绝跟随（会逃出输出目录）
    · 输出目录里已存在同名文件            → 默认拒绝覆盖（需 --overwrite）
    · 输出目录里的"多余"文件              → 一律不删，只创建和写入

  这一套还有静态检查守着（tests/test_readonly_enforcement.py 直接解析源码的
  AST，断言触碰源文件的模块里根本不存在写操作），所以它不是靠自觉。

【如实说明的边界】

  · atime：Linux 上读取会更新访问时间（除非挂载选项是 noatime / relatime）。
    工具尽力用 O_NOATIME 规避，拿不到权限时会警告。
  · 符号链接：Windows 上建符号链接需要开发者模式。目录链接会自动回退到
    junction（不需要特权）；文件链接没有等价物，会如实报"没能重建"，
    绝不伪造一个普通文件冒充链接。
  · Windows 保留名（CON / NUL / COM1 等）：先按原名建，建不进去才转义。
    Windows 11 其实允许 CON.txt，无条件转义会把它改名，那就违反"完全一致"了。
  · 大小写冲突：File.txt 和 file.txt 在 Linux 是两个文件、Windows 是一个。
    编码阶段就会检测并警告（--strict 下中止）。
"""

_GUIDE_VERIFY = """\
【验证体系 —— 四层，失败即关闭】

  原则：验证不是可选项。解码端任何一片不合格，就一个字节都不写盘。

  V0 输入指纹（编码入口）
     全程 open(p,'rb') 读原始字节，从不文本模式、从不转编码、从不规范换行、
     从不剥 BOM。逐文件记 SHA-256。
     （本机 locale 是 cp936 且 Windows 文本模式会把 \\n 变成 \\r\\n —— 用文本
     模式打开一次就足以损坏数据，所以这条是硬纪律，有静态检查守着。）

  V1 编码期自验证（编码出口，强制）
     每张 PNG 写出后立刻解回来逐字节比对。能抓到编码器 bug、纠错等级容不下
     载荷、缩放/插值导致的模块畸变。

  V2 分片级完整性（解码期）
     每片自带 8 字节会话号 + 索引 + 总数 + CRC32。精确报错：
       缺少分片 3, 7, 12（共 17 片）
       分片 5 出现了两次且内容不一致
       发现 2 个不同会话，请用 --session 指定
     重复片内容一致则容忍（同一张码扫两次很常见）。

  V3 还原级校验（解码出口，强制）
     逐文件重算 SHA-256 与大小 + 偏移布局自洽 + 集合级 archive_sha256
     （证明"有哪些文件"这件事本身没被动过）。

  一条命令跑全套：qrfile selftest --full
    合成语料（空文件 / CRLF / BOM / UTF-16 / GBK / 中文名 / emoji 名 / 深目录 /
    空目录 / 重复内容 / 各类二进制）跑完整往返，外加故障注入：删片 / 涂改 /
    复制 / 无关码 / 篡改内容，断言每一种都被精确报出。

  这一层真的抓到过东西：首次端到端跑就报了"清单偏移不自洽"—— BLOB 按原始路径
  顺序拼接，而清单按 path_b64 排序，两处顺序不一致导致偏移整体错位（big.txt 和
  bom.txt 在两种排序下位置相反）。V3 的布局校验在写盘前就拦下了它。
"""

_GUIDE_SCREENS = """\
【一次采集能拿多少 —— 这决定你要拍多少张照片】

  默认是"一张码 = 一次扫描"，2MB 文本要 430 张照片，显然不合理。
  解法是按整屏排版：一屏塞满码，拍一张照片解一屏。

  实测（本机；模拟手机拍屏：斜 0.6 度 + 相机 2 倍采样 + 镜头模糊 +
  传感器噪声 + JPEG）。决定成败的是「每个模块分到几个相机像素」，阈值约 2.5，
  跟它在屏幕上多大无关：

      <= 2.0 相机像素/模块   → 一律解不出来（被摩尔纹和非整数重采样打垮）
      >= 2.5 相机像素/模块   → 稳定

  手机拍照时屏幕在画面里占满、相机分辨率通常是屏幕像素数的 2 倍左右，
  所以屏幕上 3 像素/模块 ≈ 相机 6 像素/模块，有 2.4 倍余量。

  1920×1080 屏实测：
      3 px/模块 → 5×3 = 15 张/屏 = 12.1 KB   （稳妥，默认）
      2 px/模块 → 8×4 = 32 张/屏 = 25.8 KB   （激进；真实屏幕有 RGB 次像素，
                                              我的模拟没建模，未经真机验证）

  2MB 文本（lzma 压到约 338 KB）需要多少：
      现在（一次拍一张）           430 张照片
      --screens 默认（3px/模块）    29 张照片
      --screens --profile max       更少

  用法：
      qrfile plan   big.json                          先看要拍几张
      qrfile encode big.json -o out --screens         产出 screen_001.png...
      （把 screen_*.png 全屏显示，一张张拍下来）
      qrfile decode ./照片目录 -o restored             照片丢进去就能还原

  解码端本来就支持一图多码（zxing 的多符号检测），所以照片里有多少张码都能
  一次解出来；而且分片格式是顺序无关的，漏拍某一张也能精确告诉你是第几片。

【为什么不做彩色码】

  QR 的数据密度已经是 0.73 bit/模块（用掉约 73% 的面积），换成自研黑白码
  最多只快 23%。真正的杠杆是「颜色」（1 bit/模块 → 2~3 bit），但过摄像头要
  对抗白平衡漂移、屏幕伽马、摩尔纹、颜色渗漏，风险很高；而收益是把 29 张
  照片变成 14 张，远不如"改用视频"（430 张 → 一段十几秒的录像）。

  真要再降一个数量级，正确的方向是视频 + 喷泉码：屏幕播放动画、手机录一段像，
  每帧就是一整屏；喷泉码让丢帧无所谓。这是 qrsend 那类工具的做法，
  代价是要实现视频生成、抽帧和纠删编码。
"""

_GUIDE_SECTIONS = {
    "profiles": ("选 profile 与吞吐量", _GUIDE_PROFILES),
    "screens": ("一次采集能拿多少（一屏多码）", _GUIDE_SCREENS),
    "files": ("收哪些文件", _GUIDE_FILES),
    "limits": ("上限与超限处理", _GUIDE_LIMITS),
    "safety": ("源文件只读保证与已知边界", _GUIDE_SAFETY),
    "verify": ("验证体系", _GUIDE_VERIFY),
}


@main.command()
@click.argument(
    "topic",
    required=False,
    type=click.Choice(sorted(_GUIDE_SECTIONS)),
)
def guide(topic) -> None:
    """完整参考手册：实测数据、文件类型细则、上限、已知边界。

    \b
    带一个可选的小节名就只打印那一节：
        qrfile guide screens    一屏多码能省多少、实测的密度上限（最该先看）
        qrfile guide profiles   选 profile 与吞吐量
        qrfile guide files      收哪些文件、为什么跳过那些
        qrfile guide limits     各项上限、超限怎么处理
        qrfile guide safety     源文件只读的三层保证 + 如实说明的边界
        qrfile guide verify     四层验证体系
    """
    topics = [topic] if topic else list(_GUIDE_SECTIONS)

    _echo()
    for name in topics:
        title, body = _GUIDE_SECTIONS[name]
        # 用 ASCII 的 = 而不是 ═ —— 后者在 cp1252 的控制台上打不出来
        click.secho(f"==== {title} ====", fg="cyan")
        _echo()
        _echo(body.rstrip())
        _echo()


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------


@main.command()
@click.argument("sources", nargs=-1, required=True, type=click.Path(exists=True))
@click.option("--profile", "profile_name", type=click.Choice(PROFILE_CHOICES), default=DEFAULT_PROFILE,
              show_default=True, help="二维码尺寸/纠错档位")
@click.option("--chunk-size", type=int, default=None, help="每张码的净载荷字节数（默认取 profile 上限）")
@click.option("--compression", type=click.Choice(COMPRESSION_CHOICES),
              default=COMPRESSION_AUTO, show_default=True,
              help="压缩方式。auto 会把 zlib 和 lzma 都试一遍取最小的")
@click.option("--pdf", is_flag=True, help="估算同时出 PDF 的页数")
@click.option("--screen-size", default="auto", show_default="自动探测", metavar="WxH",
              help="屏幕分辨率。auto = 自动探测主屏（探测到的值直接决定要拍几张）")
@click.option("--module-px", type=int, default=3, show_default=True,
              help="算整屏采集张数时，每模块占几个屏幕像素")
@_common_scan_options
def plan(sources, profile_name, chunk_size, compression, pdf,
         screen_size, module_px, **kwargs) -> None:
    """预检：只算不生成，先说清楚要出多少张码、要扫多久。"""

    def action() -> None:
        profile = get_profile(profile_name)
        size = chunk_size or profile.body_capacity

        result = scan_paths(
            list(sources),
            profile=profile,
            limits=_limits_from_options(**kwargs),
            extended_encodings=kwargs["text_encodings"],
            include=kwargs["include"],
            exclude=kwargs["exclude"],
            follow_symlinks=kwargs["follow_symlinks"],
            strict=kwargs["strict"],
        )

        # 张数**算准**，不估 —— 扫描时文件内容已经在内存里了，压一遍就知道确切大小。
        # 曾经用"假设压到 30%"去估，结果 1.5MB 的文件估 547 张、实际 379 张，差 44%。
        # 输入特别大时才退回估算，免得 plan 自己跑很久。
        exact = result.total_bytes <= PLAN_EXACT_LIMIT
        if exact:
            preview_manifest, preview_blob = manifest_for_sources(
                root=result.root_label,
                sources=result.sources,
                dirs=result.empty_dirs,
                symlinks=result.symlinks,
                skipped=[(i.path, i.reason) for i in result.skipped],
                chunk_size=size,
            )
            preview = build_session(
                manifest=preview_manifest,
                blob=preview_blob,
                session_id=new_session_id(),
                chunk_size=size,
                compression=compression,
            )
            codes = preview.total
            used = preview.manifest.compression
            payload_bytes = len(preview_blob)
        else:
            codes = result.estimated_chunks(profile, compress_ratio=0.25)
            used = compression
            payload_bytes = result.total_bytes

        _echo()
        _echo(f"源：{result.root_label}")
        _echo(f"  文件        {result.file_count}")
        _echo(f"  总字节      {result.total_bytes:,}")
        _echo(f"  空目录      {len(result.empty_dirs)}")
        _echo(f"  符号链接    {len(result.symlinks)}")
        _echo(f"  跳过        {len(result.skipped)}")
        _echo()
        _echo(f"profile       {profile.name}（{profile.description}）")
        _echo(f"  每码净载荷  {size} 字节（version ≤ {profile.version_max}，纠错 {profile.ecc}）")
        if exact:
            ratio = f"{payload_bytes / result.total_bytes:.0%}" if result.total_bytes else "—"
            _echo(f"  压缩        {used}（{result.total_bytes:,} → {payload_bytes:,} 字节，{ratio}）")
        else:
            _echo(f"  压缩        {used}（输入太大，按 25% 估算）")
        _echo()
        if exact:
            _echo(f"产出         {codes} 张码（清单 {preview.manifest_count} + 数据 {preview.data_count}）")
        else:
            _echo(f"预计产出     ≈ {codes} 张码（估算）")

        per_code_px = (profile.version_max * 4 + 17 + 2 * 4) * DEFAULT_SCALE
        _echo(f"  单张尺寸    ≈ {per_code_px}×{per_code_px} px（scale={DEFAULT_SCALE}, margin=4）")
        _echo(f"  PNG 总体积  ≈ {codes * per_code_px * per_code_px / 8 / 1024:.0f} KiB（粗估）")

        if pdf:
            per_page = 5 * 7
            _echo(f"  PDF         ≈ {-(-codes // per_page)} 页 A4（5×7 布局）")

        # 整屏采集：**这才是真正决定你要拍多少次**的数字
        screen_w, screen_h = _resolve_screen_size(screen_size)
        modules = profile.version_max * 4 + 17
        tile = (modules + 8) * module_px
        gap = 8
        cols = max(1, (screen_w - gap) // (tile + gap))
        rows = max(1, (screen_h - gap) // (tile + gap))
        per_screen = cols * rows
        photos = -(-codes // per_screen)

        _echo()
        _echo(f"整屏采集     {photos} 张照片"
              f"（--screens，每屏 {cols}×{rows} = {per_screen} 张码，"
              f"{screen_w}×{screen_h} @ {module_px}px/模块）")
        if per_screen <= 1:
            _warn(f"每屏只放得下 1 张 —— 把 --module-px 调小（下限约 2.5）")
        elif module_px < 3:
            _warn(f"--module-px {module_px} 偏激进，实测下限约 2.5 个相机像素")

        # 程序自己在这台机器上要跑多久 —— 实测，不是常数
        per_code = _benchmark_render(profile, size)
        encode_seconds = codes * per_code
        _echo(f"本机编码耗时 ≈ {format_duration(encode_seconds)}"
              f"（实测单张 {per_code * 1000:.0f} ms × {codes} 张，含每张的自解码校验）")

        seconds = SCAN_SECONDS_PER_CODE.get(profile.name)
        _echo()
        if seconds is None:
            _echo("人工扫描     不适用（该 profile 不经摄像头，没有人工环节）")
        else:
            _echo(f"预计人工扫描 ≈ {format_duration(codes * seconds)}"
                  f"（每张按 {seconds} 秒算，含对焦和翻页）")
            click.secho("               这是你拿手机一张张扫完要花的时间，"
                        "不是程序运行时间。", fg="yellow")
            if codes * seconds > 1800:
                _click_advice(profile, codes, seconds)

        _echo()
        _report_skips(result.skipped)

        if result.collisions:
            _echo()
            click.secho("  大小写 / 归一化冲突：", fg="yellow")
            for group in result.collisions:
                _warn(" 与 ".join(group) + " 在大小写不敏感的文件系统上会撞车")

        if result.skipped or result.collisions:
            _echo()
            click.secho("  注意：上面这些内容不会进归档。", fg="yellow")

    _run(action)


def detect_screen_size() -> tuple[int, int] | None:
    """探测主屏的**物理**像素分辨率。探不到就返回 None。

    为什么要探测：屏幕分辨率直接决定一屏能塞多少张码，进而决定要拍几张照片。
    实测同一份 1.5MB 文件，1920×1080 要 21 张、2560×1440 只要 11 张 —— 差一倍。
    默认写死一个值必然让一半的人吃亏。

    跨平台顺序：Windows 用 ctypes（能拿到物理分辨率）→ ``mss`` → Tk。
    无图形界面的服务器上三者都会失败，返回 None，调用方回落到 1920×1080。
    """
    if os.name == "nt":
        try:
            import ctypes

            user32 = ctypes.windll.user32
            user32.SetProcessDPIAware()  # 不设的话拿到的是缩放后的逻辑分辨率
            width = int(user32.GetSystemMetrics(0))
            height = int(user32.GetSystemMetrics(1))
            if width > 0 and height > 0:
                return width, height
        except Exception:
            pass

    try:
        import mss

        with mss.mss() as session:
            monitor = session.monitors[0]
            width, height = int(monitor["width"]), int(monitor["height"])
            if width > 0 and height > 0:
                return width, height
    except Exception:
        pass

    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        width, height = root.winfo_screenwidth(), root.winfo_screenheight()
        root.destroy()
        if width > 0 and height > 0:
            return width, height
    except Exception:
        pass

    return None


def _resolve_screen_size(text: str) -> tuple[int, int]:
    """``auto`` 就探测，否则按 WxH 解析。"""
    if text.strip().lower() == "auto":
        detected = detect_screen_size()
        if detected is None:
            return 1920, 1080  # 探不到就用一个保守值
        return detected
    return _parse_screen_size(text)


def _parse_screen_size(text: str) -> tuple[int, int]:
    """``1920x1080`` / ``2560×1440`` → (1920, 1080)。"""
    cleaned = text.lower().replace("×", "x").replace("*", "x")
    parts = cleaned.split("x")
    if len(parts) != 2:
        raise InputError(f"屏幕分辨率写法看不懂：{text!r}（应该像 1920x1080）")
    try:
        width, height = int(parts[0].strip()), int(parts[1].strip())
    except ValueError:
        raise InputError(f"屏幕分辨率写法看不懂：{text!r}（应该像 1920x1080）") from None
    if width < 100 or height < 100:
        raise InputError(f"屏幕分辨率太小了：{width}x{height}")
    return width, height


def _benchmark_render(profile, chunk_size: int) -> float:
    """实测在这台机器上渲染一张码要多久。

    给用户一个**本机**的耗时估计，而不是我拍脑袋写死的常数 —— 别人机器快慢差很多。
    跑一次预热 + 一次计时，中位数级别的小开销（屏幕档约 0.2 秒）。
    """
    import time

    from .chunk import new_session_id, pack_frame
    from .render import Renderer

    payload = pack_frame(
        session_id=new_session_id(),
        index=0,
        total=1,
        body=b"x" * min(chunk_size, profile.body_capacity),
    )
    renderer = Renderer(profile)
    renderer.render_verified(payload)  # 预热，把 import 和首次分配排除掉
    start = time.perf_counter()
    renderer.render_verified(payload)
    return time.perf_counter() - start


def _click_advice(profile, codes: int, seconds: float) -> None:
    _echo()
    click.secho("  这条链路要扫很久。建议：", fg="yellow")
    if profile.name in {"screen", "camera"}:
        _warn("改用 --profile max 会让张数少 3.5 倍（但每张码难扫得多，见 qrfile guide profiles）")
    _warn("确认压缩方式用的是 auto（默认）—— 它会把 zlib 和 lzma 都试一遍取最小的")
    _warn("或分几次只编码不同的子目录，每次 -o 到不同的输出目录")


# --------------------------------------------------------------------------
# encode
# --------------------------------------------------------------------------


@main.command()
@click.argument("sources", nargs=-1, required=True, type=click.Path(exists=True))
@click.option("-o", "--output", required=True, type=click.Path(file_okay=False),
              help="输出目录（不能与源路径重叠）")
@click.option("--profile", "profile_name", type=click.Choice(PROFILE_CHOICES), default=DEFAULT_PROFILE,
              show_default=True,
              help="决定每张码装多少、有多好扫（详见 qrfile guide profiles）")
@click.option("--chunk-size", type=int, default=None, help="每张码的净载荷字节数")
@click.option("--scale", type=int, default=DEFAULT_SCALE, show_default=True, help="像素/模块")
@click.option("--margin", type=int, default=DEFAULT_MARGIN, show_default=True, help="静默区模块数")
@click.option("--compression", type=click.Choice(COMPRESSION_CHOICES),
              default=COMPRESSION_AUTO, show_default=True,
              help="压缩方式，直接决定出多少张码。auto 会把 zlib 和 lzma 都试一遍取最小的")
@click.option("--contact-sheet", is_flag=True,
              help="额外输出一张总览图 —— 拍一张照片就能扫下一整片，是最实用的提速手段")
@click.option("--sheet-columns", type=int, default=6, show_default=True,
              help="总览图每行放几张码")
@click.option("--screens", is_flag=True,
              help="【省采集次数】按整屏排版：一屏塞满码，拍一张照片解一屏。"
                   "只输出 screen_*.png，不落单张码")
@click.option("--keep-chunks", is_flag=True,
              help="整屏模式下额外把单张码也写出来（默认不写 —— 那只是中间产物）")
@click.option("--screen-size", default="auto", show_default="自动探测", metavar="WxH",
              help="屏幕分辨率（配合 --screens）。auto = 自动探测主屏")
@click.option("--module-px", type=int, default=3, show_default=True,
              help="每模块占几个屏幕像素。实测下限约 2.5 个相机像素，3 比较稳")
@click.option("--pdf", "make_pdf", is_flag=True, help="额外输出 A4 拼版 PDF，方便打印")
@click.option("--no-assert-source-untouched", is_flag=True,
              help="关闭「写完源树指纹再比对一次」这道检查（不建议）")
@click.option("--no-source-guard", is_flag=True,
              help="关闭只读审计钩子（仅调试用。关掉就没有运行期的写保护了）")
@click.option("--dry-run", is_flag=True, help="跑完整流程但一个文件都不写")
@click.option("--quiet", is_flag=True, help="只输出错误，不打印进度")
@_common_scan_options
def encode(sources, output, profile_name, chunk_size, scale, margin, compression,
           contact_sheet, sheet_columns, screens, keep_chunks, screen_size, module_px,
           make_pdf, no_assert_source_untouched,
           no_source_guard, dry_run, quiet, **kwargs) -> None:
    """把文件或目录编码成二维码图片。源文件全程只读。"""

    def action() -> None:
        profile = get_profile(profile_name)
        size = chunk_size or profile.body_capacity
        output_dir = Path(output)
        source_list = [str(s) for s in sources]

        screen_w, screen_h = _resolve_screen_size(screen_size)
        # 整屏模式下，每模块几个屏幕像素才是关键参数 —— 默认的 scale=8
        # 会让一张码占 840px，一屏只放得下 2 张，等于没省。
        #
        # 注意**不能**写 `scale = ...`：那会让 Python 把 scale 当成 action() 的
        # 局部变量，于是不带 --screens 时读它直接 UnboundLocalError（真踩过）。
        render_scale = module_px if screens else scale

        # --- 只读保证 0.3：输出目录绝不能跟源路径重叠
        check_output_isolated(str(output_dir), source_list)

        # --- 只读保证 0.2：临时文件走系统临时目录，不落在源树旁边
        #     （render/output 的所有写入都在 output_dir 内）

        # --- 只读保证 0.4：审计钩子
        if not no_source_guard:
            activate_source_guard(source_list)

        try:
            # --- 只读保证 0.5：读之前采指纹
            before = (
                {} if no_assert_source_untouched else snapshot_paths(source_list)
            )

            if not quiet:
                _echo(f"扫描 {len(sources)} 个路径…")
            result = scan_paths(
                source_list,
                profile=profile,
                limits=_limits_from_options(**kwargs),
                extended_encodings=kwargs["text_encodings"],
                include=kwargs["include"],
                exclude=kwargs["exclude"],
                follow_symlinks=kwargs["follow_symlinks"],
                strict=kwargs["strict"],
            )

            if not quiet:
                _echo(f"  收下 {result.file_count} 个文本文件，共 {result.total_bytes:,} 字节")
                _report_skips(result.skipped)
                if result.collisions:
                    click.secho("  大小写冲突（在 Windows 上还原会撞车）：", fg="yellow")
                    for group in result.collisions:
                        _warn(" 与 ".join(group))

            manifest, blob = manifest_for_sources(
                root=result.root_label,
                sources=result.sources,
                dirs=result.empty_dirs,
                symlinks=result.symlinks,
                skipped=[(i.path, i.reason) for i in result.skipped],
                chunk_size=size,
            )
            session_id = new_session_id()
            session_plan = build_session(
                manifest=manifest,
                blob=blob,
                session_id=session_id,
                chunk_size=size,
                compression=compression,
            )

            if dry_run:
                _echo(f"\n[dry-run] 将产出 {session_plan.total} 张码"
                      f"（清单 {session_plan.manifest_count} + 数据 {session_plan.data_count}）"
                      f"，没有写任何文件。")
                return

            session_dir = output_dir / session_plan.session_hex
            if not quiet:
                _echo(f"\n编码 {session_plan.total} 张码（每张都自解码比对）…")

            def progress(done: int, total: int) -> None:
                if not quiet and (done == total or done % 25 == 0):
                    _echo(f"  {done}/{total}")

            written = write_session(
                session_plan,
                output_dir=session_dir,
                renderer=Renderer(profile, scale=render_scale, margin=margin),
                options=OutputOptions(
                    contact_sheet=contact_sheet,
                    sheet_columns=sheet_columns,
                    pdf=make_pdf,
                    screens=screens,
                    keep_chunks=keep_chunks,
                    screen_width=screen_w,
                    screen_height=screen_h,
                ),
                progress=progress,
            )

            # --- 只读保证 0.5：写完之后再采一次指纹比对
            if before:
                assert_paths_untouched(source_list, before)

            _echo()
            _ok(f"会话 {session_plan.session_hex}")

            if written.screens and written.screen_layout:
                _ok(f"整屏页 {len(written.screens)} 张 → {session_dir}")
                _echo(f"     每屏 {written.screen_layout.describe(screen_w, screen_h)}")
                _echo("     把 screen_*.png 全屏显示，一张张拍下来就行")
                if written.png_count == 0:
                    _echo("     （单张码没落盘 —— 它们只是中间产物。要留着加 --keep-chunks）")
                else:
                    _echo(f"     另外也留了 {written.png_count} 张单码（--keep-chunks）")
                if written.screen_layout.per_screen <= 1:
                    _warn(
                        "每屏只放得下 1 张码，整屏模式没起到省采集的作用 —— "
                        "把 --module-px 调小，或用版本更低的 --profile"
                    )
                elif render_scale < 3:
                    _warn(
                        f"--module-px {render_scale} 偏激进（实测下限约 2.5 个相机像素）。"
                        f"如果拍出来解不出，调到 3"
                    )
            else:
                _ok(f"{written.png_count} 张码 → {session_dir}")

            if written.sheets:
                _ok(f"总览图 → {', '.join(str(s.name) for s in written.sheets)}")
            if written.pdf:
                _ok(f"打印 PDF → {written.pdf.name}")
            _ok("全部二维码已自解码比对通过")
            if before:
                _ok("源文件指纹比对：零差异")

            if noatime_degraded():
                _warn("本机无法用 O_NOATIME 读取，系统可能更新了源文件的访问时间(atime)")
            if result.skipped:
                _warn(f"有 {len(result.skipped)} 个条目被跳过（不会进归档）")

            if result.skipped:
                sys.exit(EXIT_PARTIAL)
            sys.exit(EXIT_OK)
        finally:
            deactivate_source_guard()

    _run(action)


# --------------------------------------------------------------------------
# decode
# --------------------------------------------------------------------------


@main.command()
@click.argument("qr", nargs=-1, required=True, type=click.Path(exists=True))
@click.option("-o", "--output", required=True, type=click.Path(file_okay=False), help="还原到哪个目录")
@click.option("--session", default=None,
              help="只还原指定会话（十六进制）。混了多批码时用它挑一批")
@click.option("--glob", "suffixes", default=None,
              help="只收这些扩展名的图，逗号分隔（默认 png,jpg,jpeg,bmp,gif,webp,tif）")
@click.option("--overwrite", is_flag=True,
              help="允许覆盖输出目录里已存在的文件。默认拒绝覆盖，也拒绝写穿硬链接")
@click.option("--partial", is_flag=True,
              help="缺片时也尽量还原（默认是失败即关闭：任何一片不合格就一个字节都不写）")
@click.option("--robust", is_flag=True,
              help="多渲染/多二值化反复试解。实测常见劣化没有额外收益、耗时约 3 倍，留给疑难图")
@click.option("--no-restore-times", is_flag=True, help="不恢复文件修改时间")
@click.option("--quiet", is_flag=True, help="只输出错误，不打印进度")
def decode(qr, output, session, suffixes, overwrite, partial, robust, no_restore_times, quiet) -> None:
    """把二维码图片还原成文件。默认失败即关闭：任何一片不合格就不写盘。"""

    def action() -> None:
        extra = None
        if suffixes:
            extra = frozenset(
                s if s.startswith(".") else f".{s}" for s in suffixes.split(",") if s.strip()
            )

        paths = find_image_files(list(qr), suffixes=extra) if extra else find_image_files(list(qr))
        if not paths:
            raise InputError("没找到任何图片文件")

        if not quiet:
            _echo(f"读入 {len(paths)} 张图{'（robust 模式）' if robust else ''}…")

        payloads: list[bytes] = []
        unreadable: list[tuple[str, str]] = []
        for path in paths:
            try:
                symbols = decode_file(path, robust=robust)
            except InputError as exc:
                unreadable.append((path, str(exc)))
                continue
            if not symbols:
                unreadable.append((path, "图里没有二维码"))
                continue
            payloads.extend(s.payload for s in symbols)

        if not payloads:
            # 一张都没解出来 —— 与其干巴巴地报"失败"，不如给出可操作的建议
            from .imageprep import describe_failure

            raise InputError(describe_failure([p for p, _ in unreadable] or paths))

        if unreadable and not quiet:
            click.secho(f"  {len(unreadable)} 张图解不出内容：", fg="yellow")
            for path, reason in unreadable[:10]:
                _warn(f"{Path(path).name} —— {reason}")

        target = bytes.fromhex(session) if session else None

        try:
            reassembled, stats = reassemble(payloads, session_id=target, verify=True)
        except QrFileError as exc:
            if not partial:
                raise
            # --partial：尽力还原能验证通过的部分，并明确列出缺什么
            click.secho(f"\n! 校验未通过，但指定了 --partial：\n{exc}", fg="yellow", err=True)
            reassembled, stats = reassemble(payloads, session_id=target, verify=False)

        if not quiet:
            _echo()
            _echo(f"会话 {stats.session_hex}")
            _echo(f"  分片        {stats.accepted}/{stats.accepted + 0} 片已收")
            if stats.duplicates:
                _echo(
                    f"  重复的码    {stats.duplicates} 张（内容一致，已忽略）"
                    f" —— 常见于同时扫了单张码和总览图"
                )
            if stats.foreign:
                _echo(f"  非本工具的码 {stats.foreign}")
            if stats.damaged:
                _echo(f"  损坏的片    {stats.damaged}")

        report = restore_session(
            reassembled,
            RestoreOptions(
                output_dir=Path(output),
                overwrite=overwrite,
                restore_times=not no_restore_times,
            ),
        )

        _echo()
        _ok(f"{report.written_count} 个文件 → {output}")
        if report.dirs_created:
            _ok(f"{len(report.dirs_created)} 个空目录已重建")
        if report.symlinks_created:
            _ok(f"{len(report.symlinks_created)} 个符号链接已重建")
        if report.symlinks_failed:
            click.secho(f"  {len(report.symlinks_failed)} 个符号链接没能重建：", fg="yellow")
            for path, reason in report.symlinks_failed[:10]:
                _warn(f"{path} —— {reason}")
        if report.skipped:
            click.secho(f"  {len(report.skipped)} 个条目被跳过：", fg="yellow")
            for path, reason in report.skipped[:10]:
                _warn(f"{path} —— {reason}")
        if report.collisions:
            click.secho("  ! 有文件名只靠大小写区分，在某些文件系统上会撞车", fg="yellow")

        _ok("逐文件 SHA-256 与集合级 archive_sha256 全部通过")

        if report.skipped or report.symlinks_failed:
            sys.exit(EXIT_PARTIAL)
        sys.exit(EXIT_OK)

    _run(action)


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------


@main.command()
@click.option("--profile", "profile_name", type=click.Choice(PROFILE_CHOICES),
              default=DEFAULT_PROFILE, show_default=True,
              help="用哪个 profile 跑自检（只是换个码的大小，不影响结论）")
@click.option("--full", is_flag=True, help="附加故障注入：删片 / 涂改 / 复制 / 篡改内容")
@click.option("--keep", is_flag=True, help="保留自检用的临时目录，便于排查")
def selftest(profile_name, full, keep) -> None:
    """内置自检：合成语料跑一遍完整往返，逐字节 + 逐目录结构比对。

    结论只有两种：全部通过（退出码 0），或者哪几项没过（退出码 2）。
    怀疑工具坏了、或者刚改了代码，跑这个最快。
    """

    def action() -> None:
        import tempfile

        from .selftest import run_selftest

        workdir = Path(tempfile.mkdtemp(prefix="qrfile-selftest-")) if keep else None
        if workdir:
            _echo(f"（--keep：中间产物保留在 {workdir}）")

        report = run_selftest(workdir=workdir, profile_name=profile_name, full=full)

        _echo()
        _echo(f"合成语料：{report.files} 个文本文件，产出 {report.codes} 张码")
        _echo()
        for check in report.checks:
            if check.passed:
                _ok(check.name + (f"（{check.detail}）" if check.detail else ""))
            else:
                marker = _markers_for(sys.stderr)[1]
                click.secho(f"  {marker} {check.name} —— {check.detail}", fg="red", err=True)

        _echo()
        if report.passed:
            _ok(f"自检全部通过（{len(report.checks)} 项）")
            sys.exit(EXIT_OK)

        click.secho(f"自检失败：{len(report.failures)}/{len(report.checks)} 项没过", fg="red", err=True)
        sys.exit(EXIT_VERIFY_FAILED)

    _run(action)


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------


@main.command()
@click.argument("source", type=click.Path(exists=True))
@click.argument("qr", nargs=-1, required=True, type=click.Path(exists=True))
@click.option("--session", default=None, help="只核对指定会话（十六进制）")
@click.option("--quiet", is_flag=True, help="只输出错误，不打印进度")
def verify(source, qr, session, quiet) -> None:
    """复核：把二维码还原到内存里，跟源逐个文件比对 —— 不写任何文件。

    编码之后想确认"码里装的确实是这份源"，用这个。它比 decode 多做的事是
    反查源侧：源里多出来的文件（既没收也没标为跳过）也会被报出来。
    """

    def action() -> None:
        from .readonly import sha256_file

        source_path = Path(source)
        base = source_path if source_path.is_dir() else source_path.parent

        paths = find_image_files(list(qr))
        if not paths:
            raise InputError("没找到任何图片文件")

        payloads: list[bytes] = []
        for path in paths:
            try:
                payloads.extend(s.payload for s in decode_file(path))
            except InputError:
                continue

        target = bytes.fromhex(session) if session else None
        reassembled, stats = reassemble(payloads, session_id=target, verify=True)

        if not quiet:
            _echo(f"会话 {stats.session_hex}：{stats.accepted} 片，"
                  f"{len(reassembled.manifest.files)} 个文件")

        problems: list[str] = []
        checked = 0

        for entry in reassembled.manifest.files:
            rel = entry.resolve_path()
            origin = base / rel.replace("/", os.sep)
            if not origin.is_file():
                problems.append(f"{rel}：源里找不到这个文件")
                continue
            actual = sha256_file(origin)
            if actual != entry.sha256:
                problems.append(
                    f"{rel}：与源不一致（源 {actual[:16]}…，归档 {entry.sha256[:16]}…）"
                )
                continue
            checked += 1

        # 源里多出来的文件，必须正好是被跳过的那批
        if source_path.is_dir():
            manifest_names = {e.resolve_path() for e in reassembled.manifest.files}
            skipped_names = {i.path for i in reassembled.manifest.skipped}
            for path in sorted(source_path.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(base).as_posix()
                if rel in manifest_names or rel in skipped_names:
                    continue
                if path.name in {"Thumbs.db", "desktop.ini"} or path.name.startswith("."):
                    continue
                problems.append(f"{rel}：源里有，归档里既没收也没标为跳过")

        _echo()
        if problems:
            marker = _markers_for(sys.stderr)[1]
            click.secho(f"{marker} {len(problems)} 处不一致：", fg="red", err=True)
            for line in problems[:20]:
                click.secho(f"  - {line}", fg="red", err=True)
            if len(problems) > 20:
                click.secho(f"  …… 另有 {len(problems) - 20} 处", fg="red", err=True)
            sys.exit(EXIT_VERIFY_FAILED)

        _ok(f"{checked} 个文件与源逐字节一致")
        if reassembled.manifest.skipped:
            _echo(f"  （另有 {len(reassembled.manifest.skipped)} 个条目当初被跳过，"
                  f"它们不在归档里，属预期）")
        _ok("archive_sha256 与逐文件 SHA-256 全部通过")
        sys.exit(EXIT_OK)

    _run(action)


# --------------------------------------------------------------------------
# info
# --------------------------------------------------------------------------


@main.command()
@click.argument("qr", nargs=-1, required=True, type=click.Path(exists=True))
@click.option("--quiet", is_flag=True, help="只输出错误，不打印进度")
def info(qr, quiet) -> None:
    """只看分片头，列出有哪些会话、各收了多少片 —— 不解载荷、不校验。

    排查"到底缺哪几张码"用这个，它比 decode 快得多，也不要求码齐全。
    """

    def action() -> None:
        from collections import Counter

        from .chunk import unpack_frame

        paths = find_image_files(list(qr))
        if not paths:
            raise InputError("没找到任何图片文件")

        sessions: dict[bytes, list[int]] = {}
        totals: dict[bytes, int] = {}
        foreign = damaged = 0

        for path in paths:
            try:
                symbols = decode_file(path)
            except InputError:
                continue
            for symbol in symbols:
                try:
                    header, _ = unpack_frame(symbol.payload)
                except Exception:
                    foreign += 1
                    continue
                sessions.setdefault(header.session_id, []).append(header.index)
                totals[header.session_id] = header.total

        if not sessions:
            _echo(f"看了 {len(paths)} 张图，没有 qrfile 分片（{foreign} 张不是本工具生成的）。")
            sys.exit(EXIT_OK)

        for session_id, indices in sorted(sessions.items()):
            total = totals[session_id]
            missing = sorted(set(range(total)) - set(indices))
            _echo()
            _echo(f"会话 {session_id.hex()}")
            _echo(f"  收到 {len(set(indices))}/{total} 片")
            if missing:
                shown = ", ".join(str(i) for i in missing[:20])
                _echo(f"  缺  {shown}{' …' if len(missing) > 20 else ''}")

        if foreign:
            _echo(f"\n另有 {foreign} 张不是 qrfile 生成的码，已忽略。")

    _run(action)


# --------------------------------------------------------------------------
# 实时链路（屏幕播放 / 截图 / 录屏）—— 单独放在 live_cli，避免主文件继续膨胀
# --------------------------------------------------------------------------


def _register_live_commands() -> None:
    from . import live_cli

    main.add_command(live_cli.push_cmd)
    main.add_command(live_cli.pull_cmd)
    main.add_command(live_cli.send_cmd)
    main.add_command(live_cli.recv_cmd)
    main.add_command(live_cli.record_cmd)
    main.add_command(live_cli.decode_video_cmd)
    main.add_command(live_cli.compressors_cmd)
    main.add_command(live_cli.doctor_cmd)


_register_live_commands()


if __name__ == "__main__":  # pragma: no cover
    main()
