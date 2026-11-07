# qrfile

把文件从一台机器搬到另一台机器，**只靠“屏幕 → 二维码 → 截图/录像”这条链路**。

适用于：两台机器之间没有网络、没有共享目录、不能插 U 盘，但你至少能“看见”
其中一台的屏幕（远程桌面、VNC、虚拟机控制台、物理显示器都算）。

- 源机器：`qrfile push <路径>`，把文件压成分片并全屏循环播放二维码；
- 接收机器：`qrfile pull <目录>`，截屏、解码、校验、还原。

```
源机器                                     接收机器
┌──────────────────────────┐   屏幕/画面   ┌──────────────────────────┐
│ qrfile push /data        │ ───────────→ │ qrfile pull D:\out       │
│ 压缩 → 分片 → 全屏循环    │              │ 截屏 → 解码 → 校验 → 还原 │
└──────────────────────────┘              └──────────────────────────┘
```

## 特性

- **逐字节一致**：每个文件 SHA-256 + 整个文件集合的 `archive_sha256`，任何一项不通过都不写盘。
- **目录结构完整**：保留编码时指定的根目录、嵌套目录、空目录、符号链接、中文/emoji 文件名。
- **二进制安全**：不做文本判定，任意字节直接进二维码，不经过 base64。
- **自适应布局**：根据屏幕分辨率、每模块像素、纠错等级自动选择最优的码数和版本。
- **自动压缩**：默认在 zlib / lzma 里选最小的；需要更高压缩率可启用 brotli / zstd / 7z。
- **缺片可补**：播放器底部常驻「播放区间」输入框，填入缺片范围即可只播这些页，不用退出重来。
- **自动化**：自动找目标窗口、自动调到前台、运行期间阻止息屏、默认 30 分钟自动退出。
- **跨平台**：Windows 与 Linux 产物可直接互解，源机器和接收机器可以用不同系统。
- **独立二进制**：目标机器不需要安装 Python。

## 快速开始

### A 机器（被隔离的源机器）

```bash
chmod +x qrfile-rhel8
./qrfile-rhel8 doctor          # 可选：确认 tkinter / 抓屏正常
./qrfile-rhel8 push /data
```

### B 机器（接收机器）

```powershell
.\qrfile.exe doctor            # 确认抓屏后端
.\qrfile.exe pull D:\out
```

还原结果：

```
D:\out\data\...                # 根目录 data 会一起还原
```

### 10 MB 文件推荐

```bash
# A：默认就是一屏多码（fast）
./qrfile-rhel8 push /data
```

```powershell
# B：默认即可
.\qrfile.exe pull D:\out
```

如果远端画面被缩放、识别率低，两边都加 `--safe` 退回一屏一码：

```bash
./qrfile-rhel8 push /data --safe
```
```powershell
.\qrfile.exe pull D:\out --safe
```

## 命令与参数

### `push` —— A 机器（编码 + 全屏播放）

```
qrfile push <路径> [选项]
```

| 选项 | 默认 | 作用 |
|---|---|---|
| `--fast` | **开** | 一屏多码（现在已是默认，保留兼容） |
| `--safe` | 关 | 一屏一码 + 更大模块 + ECC H，画面被缩放时用 |
| `--camera` | 关 | 手机拍屏模式（ECC H + 最优掩码 + 一屏一码） |
| `--duration N` | `1800` | N 秒后自动退出；`0` = 一直循环 |
| `--repair "12-14,204"` | — | 只播放指定分片区间（不用等整轮） |

默认行为：一屏多码（fast）、顺序播放、5 fps、自动压缩、运行期间阻止息屏、
30 分钟后自动退出。

播放器快捷键 / 底部控制栏：

```
Esc / q / Q / Ctrl+Q / Ctrl+C   退出（输入框里按 Esc 只结束编辑）
空格                             暂停
点击输入框 / r                    编辑底部「播放区间」
回车                             应用播放区间（留空 = 恢复完整播放）
f                                全屏 / 窗口切换
```

### `pull` —— B 机器（截屏 + 解码 + 还原）

```
qrfile pull <输出目录> [选项]
```

| 选项 | 作用 |
|---|---|
| `--safe` | 每帧多策略尝试，画面模糊/被缩放时用 |
| `--window "标题"` | 手动指定目标窗口（自动找错时） |
| `--region x,y,w,h` | 手动指定采集区域 |

默认行为：

- 自动找 VNC / 远程桌面窗口，并把它调到前台；
- Windows 优先用 **dxcam（DXGI）**，失败自动回退 mss；
- 收齐后自动停止、校验、还原。

> 隐藏诊断参数：`--capture auto|dxcam|mss|imagegrab`、`--no-keep-awake`。
> 日常不需要使用。

### 其它命令

| 命令 | 用途 |
|---|---|
| `doctor` | 检查 tkinter、抓屏后端、压缩器、推荐布局 |
| `compressors` | 列出本机可用的压缩器 |
| `send` / `recv` | 高级版播放 / 接收，可手动调布局、帧率等 |
| `record` | 把屏幕录成无损 PNG 帧序列 |
| `decode-video` | 从 mp4/mkv/avi/gif 还原（需要 OpenCV） |
| `plan` / `encode` / `decode` / `verify` / `info` / `selftest` / `guide` | 静态 PNG / 打印链路，见 [docs/TEXT-LINK.md](docs/TEXT-LINK.md) |

实时链路的详细说明、参数和实测数据见 [docs/LIVE.md](docs/LIVE.md)。

## 播放区间（漏片补齐）

长文件播一轮可能需要几分钟，中途卡一下可能漏几页。**不需要整轮重来，也不需要
退出播放器。**

接收端（B）会实时打印：

```
缺 37 片：12-18,204,900-905
============================================================
播放区间（保持本窗口运行）：
  1) 在 A 机器播放器底部的「播放区间」输入框填入上面的范围，回车；
  2) 或退出播放器后，在 A 机器执行：
     qrfile push --repair "12-18,204,900-905"
============================================================
```

**保持 B 窗口运行**，在 A 机器播放器底部常驻的「播放区间」输入框里填入缺片
范围（也可以直接粘贴接收端整行 `缺 37 片：12-18,204,900-905`），回车即可 ——
播放器立刻改成只循环这些页，接收端继续收，收齐后自动还原。留空回车则恢复
完整播放；点击输入框或按 `r` 可聚焦，`Esc` 结束编辑。

**一屏多码时会自动适配**：播放器按当前布局的 `per_screen` 把分片号映射成整页。
例如一屏 8 码时缺第 12-14 片，只需重播第 2 页，不会退回一屏一码。命令行
`--repair` 也使用原始会话存档里的布局，和初次播放完全一致。

不需要退出播放器、不需要重新执行命令、也不需要 `.qrfile-session.json`
（帧还在内存里）。

如果已经退出了播放器，仍然可以用命令行播放区间：

```bash
./qrfile-rhel8 push --repair "12-18,204,900-905"
```

播放器退出后，A 机器终端也会自动打印区间播放模板：

```
本次共 7640 片。如果接收端提示缺片：
  播放器还没退出时，在底部「播放区间」输入框里填缺片范围，回车生效。
  已经退出则执行区间播放命令：
    ./qrfile-rhel8 push --repair "<缺片范围>"
    例：./qrfile-rhel8 push --repair "12-14,204"
```

命令行区间播放的要求：

- 正常播放时会在**当前目录**自动生成 `.qrfile-session.json`；
- 区间播放命令要在同一个目录执行；
- 源文件内容不能变（存档里有集合级 SHA-256，变了会拒绝）；
- 接收端不能重启（已收的分片在内存里，重启就丢了）。

## 还原后的目录结构

**根目录会保留**：

```
A：push /data
B：pull D:\out
结果：D:\out\data\...
```

以下内容也会保留：

- 嵌套目录；
- 空目录；
- 符号链接（Windows 上创建失败时只影响链接本身）；
- 中文 / emoji 文件名；
- 文件修改时间（可用 `--no-restore-times` 关闭）。

单文件输入的行为不变：`push /data/a.txt` → `pull D:\out` → `D:\out\a.txt`。

## 平台支持

| 平台 | 产物 | 运行要求 |
|---|---|---|
| Windows | `qrfile.exe`（约 38 MB） | Windows 10/11 x64；可写 TEMP；交互式桌面 |
| Linux | `qrfile-rhel8`（约 25 MB） | RHEL 8 / glibc ≥ 2.28；图形会话（`push` 需要） |

Windows 版包含 numpy + dxcam（DXGI 抓屏）；Linux 版使用 mss / grim，所以体积更小。

### 抓屏后端

| 平台 | 默认顺序 |
|---|---|
| Windows | **dxcam（DXGI）** → mss → ImageGrab |
| Linux / Wayland | grim → mss → ImageGrab |
| Linux / X11 | mss → ImageGrab → scrot / ImageMagick |

> Windows 的 `ImageGrab` 在 DPI 缩放下可能只截到半张码，**不建议使用**；
> 默认的 dxcam/mss 正常。目标窗口在副显示器时，dxcam 会失败并自动回退 mss。

### 可选依赖（按需安装）

| 能力 | 依赖 |
|---|---|
| Wayland 抓屏 | `grim` |
| Linux 按窗口标题定位/激活 | `wmctrl` 或 `xdotool` |
| `decode-video` | `opencv-python-headless`（并需重新打包或从源码运行） |
| `--compression max` | `brotli` / `zstd` / `7-zip`（**两端都要有**） |

默认 `--compression auto` 只用 Python 标准库的 zlib/lzma，**两端无需额外软件**。

## 从源码构建

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e .
```

### Windows 独立 exe

```bash
python -m pip install pyinstaller
python packaging/build.py            # → dist/qrfile.exe
python packaging/build.py --onedir   # 目录版，启动更快
```

### Linux 独立二进制

```bash
bash packaging/build_linux.sh            # → dist/qrfile
bash packaging/build_linux.sh --onedir
```

RHEL 8 / AlmaLinux 8 需要先安装：

```bash
sudo dnf install -y python3.11 python3.11-pip python3.11-devel \
    python3.11-tkinter binutils
PYTHON=python3.11 bash packaging/build_linux.sh
```

PyInstaller **不能跨平台交叉编译**：Windows exe 在 Windows 上打，Linux ELF 在
Linux 上打。Linux 产物依赖构建机的 glibc，建议在目标发行版或更老的发行版上构建。

## 故障排查

### B 端显示“一帧都没解出来”

1. 看自动生成的 `qrfile-debug.png`：
   - 黑屏/纯色 → 息屏、锁屏、窗口被遮挡，或采集区域不对；
   - 有二维码但模糊 → 画面被缩放，A 用 `--safe`，B 用 `--robust`；
   - 只有终端/桌面 → 目标窗口不在采集区域，或被其他窗口盖住。
2. 运行 `qrfile.exe doctor`，确认「抓屏」后端是 **dxcam**；
3. 显式指定窗口：
   ```powershell
   .\qrfile.exe pull D:\out --window "实际窗口标题"
   ```
4. 目标窗口在副显示器时，用默认 `pull`（自动回退 mss），或 `--capture mss`。

### 播放器退不出

- 按 **Esc / q / Q / Ctrl+Q / Ctrl+C**；
- 默认 30 分钟后自动退出；
- 仍不行，从另一台机器强杀：
  ```bash
  ssh dhuang@hyena "pkill -9 -f qrfile"
  ```
- 或切到 Linux 虚拟终端（Ctrl+Alt+F2）后 `pkill -f qrfile`。

### 息屏 / 锁屏

`push` / `pull` / `record` 运行期间会自动阻止系统息屏。但它管不了：

- 手动锁屏（Win+L / Super+L）；
- 关闭/最小化 VNC/RDP 窗口；
- 切换用户、注销；
- 组策略强制断开空闲会话。

必要时手动关闭电源管理：

```bat
powercfg /change monitor-timeout-ac 0
powercfg /change standby-timeout-ac 0
```
```bash
gsettings set org.gnome.desktop.session idle-delay 0
gsettings set org.gnome.desktop.screensaver lock-enabled false
```

### 压缩不能解

- 默认 `auto`（zlib/lzma）跨平台安全；
- 如果 A 用了 `--compression max` 并选了 brotli/zstd/7z，B 必须安装同一个工具；
- 最省事：两端都用默认 `auto`。

## 版本历史

| 版本 | 主要内容 |
|---|---|
| 0.4.0 | 播放区间模式、会话存档 |
| 0.4.1 | 运行期间阻止息屏 |
| 0.4.2 | 顺序播放默认；0 帧自动保存诊断图 |
| 0.4.3 | Windows 增加 dxcam（DXGI）抓屏 |
| 0.4.9 | 全局键盘抓取；默认 30 分钟自动退出；精简参数 |
| 0.5.0 | 自动激活目标窗口；DPI 感知；采集区域打印 |
| 0.5.1 | 保留根目录；播放器退出打印区间播放命令模板 |
| **0.6.0** | 底部常驻「播放区间」输入框；默认一屏多码（fast） |

## 说明

- 本项目按 MIT 许可发布。
- 请只在你有权处置数据、并已获得必要授权的环境中使用。这条链路是公开的
  通用技术，但在受控环境里仍属于“数据外带通道”，请遵守所在组织的安全规定。

更详细的技术说明：

- 实时链路：[docs/LIVE.md](docs/LIVE.md)
- 静态二维码 / 打印 / 文本链路：[docs/TEXT-LINK.md](docs/TEXT-LINK.md)
