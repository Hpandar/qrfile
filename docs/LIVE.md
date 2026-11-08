# 实时链路：屏幕播放 → 自动采集 → 还原

文本链路（`encode` / `decode`）把二维码落成 PNG，适合打印和逐张扫描。
**实时链路**解决另一种场景：两台机器之间只有一条"看得见屏幕"的通道，没有共享
目录、没有网络、也不能插 U 盘。

```
发送机器                                   接收机器
┌──────────────┐   屏幕 / 远程桌面   ┌──────────────────┐
│ qrfile push  │ ────────────────→  │ qrfile pull      │
│ 全屏循环播放  │                    │ 截屏 → 解码 → 收齐 │
└──────────────┘                    │ → 校验 → 还原目录 │
                                    └──────────────────┘
```

编码端只负责"不停地循环播放所有分片"；接收机器漏掉的帧，下一轮自然会再出现，
**不需要任何反馈通道**。

## 最简用法：两条命令

**发送机器（被隔离的那台）**

```bash
qrfile push D:\data
```

**接收机器（你手边这台）**

```bash
qrfile pull D:\out
```

每条只带一个路径，其余全部自动：

* **A**：扫描文件 → 选压缩 → 选布局 → 全屏循环播放，不落任何文件；
* **B**：自动找远程桌面窗口（找不到就整屏）→ 截屏 → 解码 → 收齐后校验并还原。

`push` 会在播放前打印准确的压缩率、分片数、屏数和每轮耗时，类似：

```
输入    3 个文件 / 2 个空目录，180.2 KB
压缩    lzma：180.2 KB -> 117.7 KB（65.3%）
布局    v40-M · 4px/模块 · 2×1=2 码/屏 · 单码 2299 B · 单屏 4598 B
分片    54 片 · 27 屏 · 渲染 14 ms/屏 → 实际 5.0 fps · 约 5 秒/轮
```

### 图形界面（可选）

```bash
pip install 'qrfile[gui]'
qrfile gui
```

独立产物是 `qrfile-gui` + `qrfile` 两个文件（必须同目录）：前者是 Qt 控制台，
后者是传输子进程。界面覆盖首页、发送、接收、监控、诊断和设置；
发送监控可以输入播放区间，接收监控可以一键复制缺片范围。
UI 只消费 CLI 输出的 NDJSON 事件，不解析人类日志。

设计、效果图与排期见 [UI-PLAN.html](UI-PLAN.html)。

### 只需要记住三个开关

| 场景 | 发送机器 | 接收机器 |
|---|---|---|
| 默认（一屏多码，最快） | `qrfile push D:\data` | `qrfile pull D:\out` |
| 远端画面被缩放 / 识别率低 | `qrfile push D:\data --safe` | `qrfile pull D:\out --safe` |
| 手机拍屏 | `qrfile push D:\data --camera` | 录视频后用 `qrfile decode-video` |

默认就是 `--fast`（一屏多码）；`--safe` 会退回一屏一码。`--fast` 和 `--safe`
只能二选一，保留 `--fast` 只是为了兼容旧命令。

### 等价的高级写法（出问题时才用）

```bash
# A：手动指定布局 / 帧率 / 压缩
qrfile send D:\data --mode auto --fps 6 --compression auto

# B：手动指定采集窗口或区域
qrfile recv D:\out --window "远程桌面连接"
qrfile recv D:\out --region 100,80,1280,720
```

`recv` 会在收齐所有分片后**自动停止**，然后逐文件校验、还原目录结构。
中途按 Ctrl+C 放弃；下次再跑一次即可，已收到的分片会重新收到，不影响结果。

### 先录屏、事后解码

```bash
# 录成无损 PNG 帧序列
qrfile record D:\frames --window "远程桌面"

# 事后还原
qrfile decode D:\frames -o D:\out
```

已经有视频文件（手机录屏 / OBS）时：

```bash
pip install opencv-python-headless
qrfile decode-video recording.mp4 -o D:\out
```

## 它保证什么

1. **逐字节一致**：每个文件 SHA-256 + 整个文件集合的 `archive_sha256`，
   任何一项不通过都**不写盘**。
2. **目录结构完整**：嵌套目录、空目录、符号链接、中文/emoji 文件名都保留。
   还原时还会保留编码时指定的**根目录**：`push /data` → `pull D:\out` 得到
   `D:\out\data\...`，而不是把文件散在输出目录顶层。
3. **二进制安全**：不做文本判定，任意字节直接进二维码（字节模式，不经过
   base64 —— 省掉 33% 的帧数）。
4. **失败可诊断**：缺哪一片、坏了哪一片、收到了多少重复，都实时显示。

## 自适应布局：一屏放几个码

默认是 `--mode auto`（一屏多码，即 `push` 的 fast 模式）。`send` 会根据
**发送机器的屏幕分辨率**枚举 `(版本, 每模块像素)` 的所有可行组合，
按 **单屏净载荷 = 格子数 × 单码载荷** 取最大。实测结果（`--mode auto`）：

| 屏幕 | 自动选择 | 每屏码数 | 单屏净载荷 |
|---|---|---|---|
| 1366×768 | v35-M · 4px | 2×1 = 2 | 3,554 B |
| 1600×900 | v40-M · 4px | 2×1 = 2 | 4,598 B |
| **1920×1080** | **v23-M · 4px** | **4×2 = 8** | **6,600 B** |
| 2560×1440 | v33-M · 4px | 4×2 = 8 | 12,768 B |
| 3840×2160 | v37-M · 4px | 5×3 = 15 | 29,355 B |

注意 1080p 上它没有选 v40 —— 因为 v40 一张就占 740px，一屏只放得下 2 张；
换成 v23 能放 8 张，总吞吐反而高 43%。**这就是"自动"的意义：码大不等于快。**

三个旋钮：

| 参数 | 作用 | 什么时候改 |
|---|---|---|
| `--mode single` | 强制一屏一码（默认是 auto） | 远端画面被缩放、拍照链路、追求最稳 |
| `--module-px N` | 固定每模块像素 | 远端缩放比例未知时，直接从 6 开始试 |
| `--max-codes N` | 限制一屏码数 | 解码端 CPU 弱、画面太密 |

### ⚠️ 远程桌面的缩放是最大的坑

远程桌面客户端默认会把远端画面**缩放到本地窗口大小**，这一步是插值重采样，
会把 4px/模块的二维码糊掉。判断方法：在接收机器看二维码的模块边缘是不是清晰
的方块。

如果被缩放了，按优先级：

1. 把远程桌面设成**全屏、100% 缩放**，远端分辨率与本地一致；
2. 或者 `qrfile send ... --module-px 6`（模块更大，抗缩放更强）；
3. 或者 `--mode single`（一屏一码，代码最大）；
4. 最后再加 `recv --robust`（每帧多策略尝试，慢但通过率高）。

## 压缩：默认可移植，`max` 更小

| 模式 | 行为 | 解码端要求 |
|---|---|---|
| `--compression auto`（默认） | 在 zlib / lzma 里取最小的 | **无**，Python 标库即可 |
| `--compression max` | 再尝试 brotli / zstd / 7z，取最小的 | 必须装同一个工具 |
| `--compression none` | 不压 | 无 |

压缩方法会写进每个分片头（格式版本 2 的 `reserved[0]`），所以解码端不需要
用户手动指定 —— 但**用了 `max` 就必须保证两端都有那个工具**。查看本机能力：

```bash
qrfile compressors
```

已经压过的数据（zip / jpg / mp4）会自动退回 `none`，不会越压越大。

## 可靠性设计

- **分片头 32 字节**：magic、会话号、序号、总数、载荷长度、CRC32、头部 CRC16。
- **重复帧无害**：接收机器按序号去重；同号不同内容会报冲突而不是猜。
- **无限循环**：不需要 ACK，漏帧的代价只是时间。
- **失败即关闭**：缺片、CRC 不符、`archive_sha256` 不符，一律不写盘。

## 采集后端 发送/接收 诊断（隐藏参数）

如果怀疑是采集后端或"阻止息屏"导致的问题，可以逐个排除：

```bash
qrfile pull D:\out --capture imagegrab --no-keep-awake   # 旧版行为
qrfile pull D:\out --capture mss       --no-keep-awake   # 跨平台改造后的默认
qrfile pull D:\out --capture dxcam     --no-keep-awake   # Windows DXGI（推荐）
```

哪个能出图，就说明问题在哪个后端。`--capture` 和 `--no-keep-awake` 都不出现在
`--help` 里，属于诊断开关，日常不用。

> **实测结论（Windows + VNC 场景）**：`dxcam` 和 `mss` 正常；`ImageGrab` 在
> DPI 缩放下可能只截到半张码，**不建议使用**。Windows 默认顺序是
> `dxcam → mss → ImageGrab`，日常直接用 `pull` 即可。

## 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 一帧都解不出 | 采集区域不含二维码 / 远端被缩放 / 播放端暂停 | 先确认 `send` 在动；改用 `--window` 或 `--region`；加 `--module-px 6` |
| 收了一部分就卡住 | 某些屏没被采集到 | 让 `send` 继续循环；`recv` 再跑一次 |
| 解码很慢、掉帧 | 一屏码太多或分辨率太高 | `--max-codes 4` 或 `--mode single` |
| `send` 报没有图形界面 | 只有 SSH / 无桌面 | 用 `--dump-pages <目录>` 导出整屏 PNG，再想办法显示 |
| `max` 压缩后解码失败 | 接收机器没有同名压缩工具 | 用 `auto` 重新编码，或两端安装同一工具 |

## 上限与已知边界

- 单会话最多 **65,535 片**（分片索引是 uint16）。按 2,299 B/片算约 **150 MB**，
  按 4K 的 29 KB/屏算是另一回事 —— 真正的限制是**时间**，不是格式。
- `send` 需要图形界面；无头环境用 `--dump-pages`。
- `decode-video` 需要 OpenCV；`--window` 需要 pygetwindow。两者都是可选依赖。
- 屏幕采集的帧率受解码速度限制：一帧里有 8 个码时，zxing 大约要 20~40 ms。

## 丢帧 / 屏幕卡顿：只播放缺失的区间

长文件播一轮要几分钟，中途卡一下就可能漏几页。**不需要整轮重来**。

接收机器（B）会实时打印缺片，例如：

```
缺 37 片：12-18,204,900-905
```

**保持接收机器窗口不要关**。此时有两种播放区间方式：

**方式一：直接在播放器底部输入（推荐，不用退出）**

播放器底部常驻一条「播放区间」输入框。把上面的范围（或接收机器整行
`缺 37 片：12-18,204,900-905`）粘贴进去，回车即可：

* 播放器立刻切换为只循环缺片所在的页，接收机器继续收；
* 终端显示 `[播放区间] 缺 N 片 / M 页：...`，页脚也会标记“播放区间”；
* 点击输入框或按 `r` 聚焦；留空回车 = 恢复完整播放；
* 输入框里按 `Esc` 只结束编辑，再按 `Esc`（或 `q`）退出播放器。

**一屏多码时自动适配**：播放器按当前布局的 `per_screen` 把分片号映射成整页。
例如一屏 8 码时缺第 12-14 片，只需重播第 2 页，不会退回一屏一码。

**方式二：退出后用命令行**

按 Esc 停掉原播放，然后执行：

```bash
qrfile push --repair "12-18,204,900-905"
```

播放器退出（Esc / 30 分钟到点）后，发送机器终端也会自动打印区间播放模板，
把接收机器显示的数字填进引号即可：

```
本次共 7640 片。如果接收机器提示缺片：
  播放器还没退出时，在底部「播放区间」输入框里填缺片范围，回车生效。
  已经退出则执行区间播放命令：
    ./qrfile-rhel8 push --repair "<缺片范围>"
    例：./qrfile-rhel8 push --repair "12-14,204"
```

发送机器只循环播放覆盖这些分片的那几页，接收机器继续收，收齐后自动还原。
一轮几分钟的文件，补几页通常只要几秒到几十秒。

命令行区间播放的前提：

- 正常播放时会在**当前目录**自动生成 `.qrfile-session.json`；
- 区间播放命令要在同一个目录执行；
- 源文件内容不能变（存档里有集合级 SHA-256，变了会拒绝）；
- 接收机器不能重启（否则已收的分片就丢了；仍需重收全部）。

播放器底部输入框没有这些限制：分片帧还在内存里，会话存档丢了也照样能用。

### 另外两个降低丢帧的手段

1. **顺序播放（固定）**：按 0,1,2… 播放，便于观察和定位；漏片用播放区间补齐。
2. **放慢发送机器**：`--fps 3` 或 `--fps 2`。当远程桌面 / 屏幕采集来不及刷新时，
   降低帧率通常比反复重播更划算；接收机器默认 8 fps，足够接住。
3. **自动退出**：`push` / `send` 默认 30 分钟后自动停止，防止播放器卡住；
   `--duration 0` 可关闭，`--duration 3600` 可延长。

## 息屏 / 锁屏 / RDP 断开

**任何一端息屏或锁屏，二维码都收不到。** 屏幕亮了不等于内容还在：息屏后系统
显示的是黑屏或锁屏界面，采集端拿到的也是这个。

`push` / `pull` / `record` 运行期间会自动阻止系统息屏和休眠：

| 平台 | 机制 |
|---|---|
| Windows | `SetThreadExecutionState(ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED)` |
| Linux | `systemd-inhibit --what=idle:sleep` + `xdg-screensaver reset` |
| macOS | `caffeinate` |

启动时会打印 `已阻止息屏（...）`。但它**管不了**：

- 手动锁屏（Win+L / Super+L）；
- 关闭或最小化 RDP 窗口；
- 切换用户、注销；
- 组策略强制断开空闲 RDP 会话（"Set time limit for active but idle Remote
  Desktop Services sessions"）。

如果工具没有可用后端（例如极简 Linux 没有 systemd-inhibit），手动设置：

### Windows（接收机器）

```bat
powercfg /change monitor-timeout-ac 0
powercfg /change standby-timeout-ac 0
powercfg /change monitor-timeout-dc 0
powercfg /change standby-timeout-dc 0
```

再在「设置 → 个性化 → 锁屏界面 → 屏幕保护程序」里选「无」。
传完记得把 0 改回原来的值（例如 10 / 30 分钟）。

### RHEL 8 / GNOME（发送机器）

```bash
gsettings set org.gnome.desktop.session idle-delay 0
gsettings set org.gnome.desktop.screensaver lock-enabled false
gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing'
gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-battery-type 'nothing'

# X11 会话还可以：
xset s off -dpms
```

`gsettings` 是持久设置，传完记得改回来；`xset` 只影响当前 X 会话。

### RDP

- 保持客户端窗口打开、在前台，不要最小化；
- 组策略若设置了空闲会话超时，把时间调大或临时禁用；
- 用 `--fps 2~3` 降低对画面刷新的要求。

## Windows / Linux

### Windows

直接用 `dist\qrfile.exe`，目标机器不需要 Python。要求：Windows 10/11 **x64**、
可写 TEMP、交互式桌面（`push` / `pull` / `record` 需要）。

### Linux

代码本身跨平台，抓屏和窗口定位都有 Linux 实现：

| 能力 | Linux 实现 | 依赖 |
|---|---|---|
| 抓屏（X11） | `mss` | 已列为核心依赖 |
| 抓屏（Wayland） | `grim` | `sudo apt install grim` |
| 抓屏兜底 | `scrot` / ImageMagick `import` | 可选 |
| 窗口定位 `--window` | `wmctrl` 或 `xdotool` | 可选；没有就用整屏或 `--region` |
| 全屏播放 | tkinter | `sudo apt install python3-tk` |

源码安装：

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
qrfile doctor
```

构建独立二进制（目标机不需要 Python）：

```bash
bash packaging/build_linux.sh            # 单文件 dist/qrfile
bash packaging/build_linux.sh --onedir   # 目录版，启动更快
```

**PyInstaller 不能跨平台交叉编译**：Linux 版必须在 Linux 上构建。建议在目标
发行版或更老的发行版上构建，产物依赖构建机的 glibc 版本。

### WSLg 限制

WSLg 的 Weston 不支持 X11 `XGetImage`，也不支持 grim 使用的
`wlr-screencopy` 协议，因此 **Linux 版无法在 WSLg 里抓屏接收**。表现是
`mss: X11 Protocol Error` / `X Error of failed request: 8`。

解决办法：用 **Windows 版接收端**抓取 WSLg 窗口（它就是 Windows 桌面上的
普通窗口）。已验证“Linux 发送（WSLg）→ Windows 接收”逐字节还原成功。
真实 Linux 桌面（X11/Wayland）不受这个限制。

无图形界面的纯服务器上：

```bash
qrfile send /data --dump-pages ./pages   # 只生成整屏 PNG，不需要 DISPLAY
qrfile decode ./pages -o ./out           # 解码也不需要 DISPLAY
```

`push` / `pull` / `record` 需要桌面会话；`decode` / `decode-video` /
`send --dump-pages` 不需要。

## 合规提醒

"只能往里传、不能往外拿"通常是**刻意配置的数据防泄漏策略**，不是环境没配好。
这条链路是公开的通用技术（与 txqr / AirFerry / QueQiao 同一类），但它同样是一条
**绕过出站管控的通道**。只在你有权处置这些数据、且已获得必要授权时使用；
企业环境里的满屏二维码和录屏行为很容易被审计系统标记。
