# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目定位

qrfile 把文件编码成二维码，在**没有网络、共享目录或 U 盘的隔离环境**里，仅靠
「屏幕 → 二维码 → 截图/录像」搬运文件，并要求还原后**逐字节一致**。它同时提供
两条链路，二者共享同一套容器格式、清单和校验逻辑：

- **静态/文本链路**：`encode` → PNG/PDF → `decode`，只接受文本文件（按内容判定）。
- **实时链路**：`push` 全屏循环播放 → `pull` 截屏解码并自动还原，接受任意二进制。

代码、文档和 CLI 输出都是中文。Python ≥ 3.11。

## 常用命令

### 安装

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e '.[dev]'          # 核心 + pytest/hypothesis
pip install -e '.[dev,gui]'      # 需要 Qt 界面时
```

可选 extras：`gui`（PySide6-Essentials 6.9.x）、`window`（pygetwindow）、
`video`（OpenCV，decode-video）、`fast`（numpy 渲染）。

### 测试与自检

```bash
pytest -q                                  # 全量（当前约 561 个用例）
pytest tests/test_cli.py -q                # 单个文件
pytest tests/test_cli.py::test_xxx -q      # 单个用例
pytest -k "repair or interval" -q          # 按关键字
python -m qrfile selftest --full           # 真实完整往返 + 故障注入
python -m qrfile doctor                    # 检查 tkinter / 抓屏 / 压缩器
python -m qrfile compressors               # 本机可用压缩器
```

- 根目录 `conftest.py` 让 pytest 能把项目根加入 `sys.path`，直接 `import qrfile`。
- `tests/test_ui.py` 自动设置 `QT_QPA_PLATFORM=offscreen`；未安装 PySide6 时跳过。
- 没有 lint / type-check 配置。

### 运行

```bash
python -m qrfile                    # 打印速查表
python -m qrfile push <路径>         # 发送端：编码 + 全屏播放
python -m qrfile pull <输出目录>     # 接收端：截屏 + 解码 + 还原
python -m qrfile push <路径> --rgb   # RGB 三通道：每个 tile 承载 3 张码（需无损采集）
python -m qrfile pull <目录> --rgb   # 接收端也必须开 --rgb
python -m qrfile gui                # Qt 界面
python -m qrfile encode <路径> -o <目录>
python -m qrfile decode <图片或目录> -o <目录>
```

### 构建独立产物

```bash
python packaging/build.py --all     # Windows：dist/qrfile.exe + dist/qrfile-gui.exe
python packaging/build.py --onedir  # 目录版，启动更快
bash packaging/build_linux.sh --all # Linux 构建（内部会建 .venv、装依赖）
```

- PyInstaller **不能跨平台交叉编译**：Windows exe 在 Windows 打，Linux ELF 在 Linux 打。
- 构建脚本会自动验证产物：CLI 跑一遍 `selftest --full`；GUI 用
  `QRFILE_GUI_SMOKE=job QT_QPA_PLATFORM=offscreen` 启动并调同目录 CLI 的 `doctor`。
- GUI 产物必须与同目录的 CLI 产物一起分发。

## 高层架构

### 一套容器，两条链路

核心数据流：

```
扫描 → 清单(manifest) → 分片(chunk) → 渲染/播放 → 采集/解码 → 重组(reassemble) → 还原
```

- `archive.py` 是容器核心：`manifest_for_sources()` 生成清单与 BLOB，
  `build_session()` 切成帧，`reassemble()` 按分片头拼回并做 V3 校验。
- 会话分成**两段**：清单段（JSON，`FLAG_MANIFEST`）在前，数据段（所有文件原始
  字节按清单顺序拼接）在后；分片 `index` 覆盖整个序列，`total` 是两段之和。
  先解清单即可知道缺哪片、每个文件多大、哈希多少。
- `chunk.py`：固定 32 字节分片头（`QRFS` magic、格式版本、flags、8 字节
  session_id、uint16 index/total、body_len、CRC32、头 CRC16、reserved）。
  压缩方法 id 从格式版本 2 起写在 `reserved[0]`；版本 1 的码仍可解。
- `manifest.py`：规范 JSON（键排序、紧凑分隔符、按 `path_b64` 排序）。
  `path_b64`（文件系统原始字节的 base64）是路径的权威表示，`path` 仅供显示。
  逐文件 SHA-256 + `archive_sha256`（覆盖文件集合构成，不覆盖 `created` /
  `chunk_size` / `compression`）。

### 静态/文本链路

`cli.py` 中的 `plan` / `encode` / `decode` / `verify` / `info` / `selftest`：

1. `scan.py` 递归遍历并调用 `textdetect.classify()`——按 magic、编码探测和熵护栏
   判断文本/二进制（阈值 6.0）。非文本文件跳过并记录原因。
2. `archive.py` 组装清单/BLOB。
3. `render.py` 用 segno 渲染，每张码写出后立刻解回比对（V1）。
4. `output.py` 写 PNG 序列、总览图、A4 PDF 或整屏 `screen_*.png`。
5. `decode.py` 用 zxing-cpp 解码，`reassemble()` 校验，`restore_session()` 落盘。

`capacity.py` 是 QR 版本容量表；`PROFILES`（camera/screen/print/max）决定单码
载荷与纠错等级。默认 `screen`。

### 实时链路

入口在 `live_cli.py`（`push`/`pull` 是日用入口，`send`/`recv` 是高级入口）：

- **发送端** `livescan.scan_all()` 接收任意二进制（与 `scan.py` 的关键区别），
  `livelayout.choose_layout()` 枚举 `(QR 版本, 每模块像素)`，按
  `单屏净载荷 = 每屏码数 × 单码载荷` 取最大。`player.py` 在全屏 Tk 窗口中循环
  渲染整屏页；渲染线程 + 长度 2 的有界队列把渲染与显示解耦。
  默认固定 mask=3 以换取约 4.8 倍渲染速度；`--camera` 改用最优掩码。
- **接收端** `capture.py` 按平台选择抓屏后端：
  Windows `dxcam(DXGI) → mss → ImageGrab`；Wayland `grim → mss`；X11 `mss → ...`。
  播放布局使用 `detect_play_screen_size()` 选**单块显示器**，不能用虚拟桌面总
  尺寸，否则多屏/WSLg 下右侧码会被裁掉。`receiver.LiveCollector` 按 index 去重，
  收齐前不需要任何反馈通道。
- **RGB 三通道模式**：`Layout.channels=3` 时一个物理 tile 的 R/G/B 各装一张
  独立 QR；`player._tile_image()` 用 `Image.merge("RGB", ...)` 合成，
  `decode.decode_rgb_image()` 拆通道逐个解码。`Layout.per_screen` 和
  `bytes_per_screen` 都按 `columns*rows*channels` 计算，区间播放/页映射因此
  自动成立。只适合无损 RGB 采集（VNC raw/ZRLE、本机截屏），不能和
  `--camera` 同用；`.qrfile-session.json` 记录 `channels`，`--repair` 无需
  重复指定 `--rgb`。
- **区间播放**：正常播放会在当前目录写 `.qrfile-session.json`
  （`session.py`）。`push --repair "12-14,204"` 按存档原样重建同一 session_id、
  分片大小、两段压缩方法和布局；源文件变化或重建结果不一致会拒绝播放。
  播放器底部的「播放区间」输入框只过滤内存里的页，不依赖存档。
- **阻止息屏**：`awake.KeepAwake`，Windows `SetThreadExecutionState`，
  Linux `systemd-inhibit` + `xdg-screensaver`，macOS `caffeinate`。

### GUI 与 CLI 的进程边界

Qt 界面（`qrfile/ui/`）**不执行传输循环**。它通过 `ui/runner.py` 启动同目录的
CLI 子进程，带 `--events-json`，逐行读取 stdout 上的 NDJSON 事件；人类可读日志
一律走 stderr。事件类型见 `events.py`（`state` / `plan` / `progress` / `missing`
/ `page` / `interval` / `paused` / `warning` / `error` 等）。

GUI 通过子进程 stdin 发控制命令：`interval <范围>`、`pause`、`resume`、
`toggle`、`speed_up`、`speed_down`、`stop`。子进程环境会设置
`QRFILE_UTF8_PIPE=1`、`PYTHONUTF8=1`，Windows 上加 `CREATE_NO_WINDOW`。
发送端统一蓝色、接收端统一绿色（见 `ui/theme.py`）。

### 只读与路径安全

- `readonly.py` 是**唯一**允许打开源文件的模块（`read_bytes` / `read_prefix` /
  `iter_file_bytes`，永远 `O_RDONLY`，Linux 尽量 `O_NOATIME`）。
- 编码期间 `activate_source_guard()` 安装 `sys.addaudithook`，写意图触碰源路径
  立即抛 `SourceGuardError`。
- 读取前后对源树采指纹（路径/大小/mtime/权限/SHA-256）并比对。
- `pathsafe.py` + `readonly.check_write_target()` 保护还原侧：拒绝路径穿越、
  绝对路径、Windows 保留名、写穿符号链接/硬链接、默认拒绝覆盖；输出目录里
  多余的文件一律不删除。
- `tests/test_readonly_enforcement.py` 会解析源码 AST，断言触碰源文件的模块里
  不存在写操作。

## 关键约束（修改前必读）

1. **源文件只能经 `readonly.py` 读取**，不得用文本模式 `open()` 打开源文件（会
   改写换行/BOM）；这条有静态测试守着。
2. **解码器只允许 zxing-cpp**。OpenCV 的 `QRCodeDetector` 在密集码上实测 0%
   通过率，绝不能加入降级链。
3. **`pack_blob()` 与清单都必须按 `path_b64` 排序**，偏移量必须首尾相接。
   两处排序不一致曾导致「清单偏移不自洽」和文件内容张冠李戴。
4. **失败即关闭**：缺片、CRC 不符、逐文件 SHA-256 或 `archive_sha256` 不符时，
   一个字节都不写盘。`--partial` 是用户显式选择的例外。
5. **分片头布局是兼容契约**：改布局必须提升 `FORMAT_VERSION`，并保留对旧版本的
   解码支持（当前支持 1、2）。
6. **区间播放必须能精确复现原会话**：session_id、chunk size、两段压缩方法、
   布局、清单原文都不能漂移，否则接收端手里的旧分片对不上。
7. **实时布局按单块显示器计算**，不要回退到虚拟桌面总尺寸。
8. **事件模式下 stdout 只能输出 NDJSON**；人类可读文本必须走 stderr，否则 GUI
   的事件解析会被污染。
9. **GUI 不要直接调用播放/采集函数**；传输必须留在 CLI 子进程里，否则全屏 Tk
   事件循环会和 Qt 主线程互相阻塞。
10. **静态链路与实时链路的扫描器不要合并**：`scan.py` 按内容拒绝二进制，
    `livescan.py` 故意接受任意字节。
11. **RGB 三通道模式依赖无损 RGB 链路**：发送端 `--rgb` 与接收端 `--rgb`
    必须成对使用；不要和 `--camera` 组合；`decode` / `decode-video` 处理录制的
    RGB 帧时也要带 `--rgb`。每个通道仍是标准分片，CRC 失败只会表现为缺片。
12. 退出码是 CLI 契约：`0` 成功、`1` 部分跳过、`2` 校验失败、`3` 输入错误、
    `4` 内部错误（含只读护栏触发）。

## 测试结构（改代码时优先跑相关文件）

- `tests/test_roundtrip.py`、`test_archive.py`、`test_manifest.py`、`test_chunk.py`
  —— 容器、清单、往返与校验。
- `tests/test_scan.py`、`test_textdetect.py` —— 文本判定与跳过策略。
- `tests/test_capacity.py` —— 容量表与 segno 逐版本交叉验证。
- `tests/test_live.py`、`test_imageprep.py` —— 布局、区间、采集/解码逻辑。
- `tests/test_readonly.py`、`test_readonly_enforcement.py`、`test_pathsafe.py`
  —— 只读保证与还原安全。
- `tests/test_cli.py`、`test_selftest.py`、`test_events.py` —— CLI 行为、自检、事件协议。
- `tests/test_ui.py` —— Qt 窗口与子进程桥接（无 PySide6 时跳过）。

## 文档

- `README.md`：用户视角的完整说明、平台支持、故障排查。
- `docs/LIVE.md`：实时链路参数、实测数据与踩坑。
- `docs/TEXT-LINK.md`：静态二维码 / 打印 / 文本判定细节。
- `docs/UI-PLAN.html`：界面设计与排期。
