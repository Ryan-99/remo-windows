# RC003 → ESP32-C6 BLE 桥 · 阶段 1（诊断固件 + 机器可读协议）

> ## ✅ 阶段 1 结论（已实测）
>
> **三个「死键」确实存在于 BLE 报文里，免驱路线成立。**
>
> 实测计分板：
>
> ```
> 返回    0x00F1: 4  ✅
> 音量+   0x0080: 4  ✅
> 音量-   0x0081: 4  ✅
> >>> 死键在 BLE 报文里【可见】—— 免驱路线成立！
> ```
>
> 所以：这三个键是在 **Windows 侧**被 `kbdhid` / `mshidumdf` 丢掉的，不是遥控器没发。
> **不需要 QL-4 驱动**，也不需要测试签名 / 关 Secure Boot。往阶段 2 走。
>
> 下面整篇是复现和排障过程，仍然有效 —— 尤其是
> [键盘报告有两种布局](#键盘报告有两种布局这是踩过的坑) 和
> [构建环境](#构建环境的坑) 两节，都踩过。

## 这块板子到底是什么

插上后实测：

```
Chip type:   ESP32-C6FH8 (QFN32)  revision v0.2
Features:    Wi-Fi 6, BT 5 (LE), IEEE802.15.4, Single Core + LP Core, 160MHz
Flash:       8MB (embedded)
USB mode:    USB-Serial/JTAG
MAC:         98:88:e0:6c:01:c4
枚举:        VID_303A&PID_1001  →  COM3 (MI_00 CDC) + JTAG (MI_02)
```

⚠️ **不是 ESP32-S3。** 这个区别是决定性的：

| | USB-OTG（能当 USB 键盘） | 只有 USB-Serial/JTAG |
|---|---|---|
| ESP32-S2 / S3 / P4 | ✅ | |
| **ESP32-C6 / C3 / C5 / H2** | | ✅ ← 你这块 |

C6 **没有 USB-OTG 外设**，USB 口在硬件上就只能做串口。所以它**不可能**被伪装成一个 USB 键盘（那正是 QL-4 驱动做的事，需要在主机端装内核驱动）。

但这不是坏消息 —— 反而让方案更安全：**COM3 全程可用**，固件随时可刷，不存在刷坏 USB 变砖的风险。

---

## 那这条路还成立吗

成立，但形态变了：

```
                    ┌──────────────┐   USB CDC    ┌──────────────────┐
   RC003 ──BLE──▶  │  ESP32-C6    │ ──────────▶  │  Windows 小助手   │
   (HID over GATT)  │  BLE central │   (COM3)     │  ~100 行 Python  │
                    └──────────────┘  按键行文本   └──────────────────┘
                                                          │
                                                          ▼
                                                     你的 App

   对比：遥控器 ──BLE──▶ Windows 蓝牙栈 ──▶ kbdhid ──▶ ✗ 三个键在这里被吞
                                              └─▶ 这就是为什么需要 QL-4 驱动
```

**关键差别：** 这条路上 Windows 的蓝牙栈、kbdhid.sys、低级键盘钩子**全部不参与**。按键是 ESP32 从 BLE 报文里直接读出来的原始 usage，不经过任何会丢数据的内核组件。所以「返回 / 音量± 是不是死键」这个问题在这里根本不存在 —— 前提是它们确实在 BLE 报文里。

**这就是阶段 1 唯一要验证的事。**

代价要说清楚：

- ❌ 遥控器被 ESP32 占用了，Windows 就**收不到**它 —— 包括**语音键**（ATVV 走的是同一条 BLE 链路）。语音链路要在阶段 2 单独处理（ESP32 同时挂 HID + ATVV，把音频转 USB 音频类……而 C6 没有 USB-OTG，做不到；需要用 I2S/PCM 或者换 S3）。
- ❌ 需要一个小助手程序常驻（不是零依赖了）。
- ✅ 但是：**不需要管理员权限、不需要测试签名、不需要关 Secure Boot、不需要 Frida、不需要装驱动。**

---

## 硬件准备

1. 板子插 USB（已经插好了，COM3）
2. **关闭 Windows 蓝牙**（设置 → 蓝牙和其他设备 → 蓝牙开关 → 关）
   - 遥控器是 BLE HID，通常只接受**一个**主机连接。不关的话 ESP32 抢不到。
   - 完全可逆，测完打开即可。
3. 遥控器进入**配对模式**：同时长按 **【主页】+【菜单】**，直到指示灯快速闪烁
   - （具体组合以你实际遥控器为准；配对模式下它会重新开始广播）

---

## 编译与烧录

固件就在本仓库的 `firmware/esp32-c6-bridge/`，所以在仓库根目录下直接：

```powershell
$BR = "firmware\esp32-c6-bridge"

python -m platformio run -d $BR                 # 编译
python -m platformio run -d $BR -t upload       # 烧录
python -m platformio device monitor -d $BR      # 看串口（115200）
```

**端口没有写死。** `platformio.ini` 里故意不设 `upload_port` / `monitor_port`，
PlatformIO 会自己枚举：只有一个串口就直接用，有多个才让你挑。换机器不用改任何东西
（每台机器给 USB-Serial/JTAG 分的号都不一样）。要临时钉死某一个口就加命令行参数：

```powershell
python -m platformio run -d $BR -t upload --upload-port COM7
python -m platformio device monitor -d $BR --port COM7
```

Windows 端 App 那边也一样是自适应识别的，见下面
[Windows 端怎么用](#windows-端怎么用remo--ovb-rc003)。

### ⚠️ 为什么 platform 必须是 pioarduino，不能是官方的 `espressif32`

这是最容易卡住的一步，已经踩过：

官方 `platformio/espressif32` 最新只到 **7.1.2**，它封装的 Arduino 内核是
**arduino-esp32 2.0.17**（`framework-arduinoespressif32` 的版本号最高就是
`4.20017.*`）。而 **ESP32-C6 是 arduino-esp32 3.0 才加入的芯片**。所以官方平台上
`esp32-c6-devkitc-1` 这块板的定义里写的是：

```json
"frameworks": ["espidf"]
```

只有 IDF，**没有 arduino**。于是 `board = esp32-c6-devkitc-1` + `framework = arduino`
在官方平台上是个非法组合，报错是：

```
Error: This board does not support arduino framework!
```

**这不是网络问题，也不是板子问题，是官方平台根本不支持。** 解法是换成社区维护的
**pioarduino** 分支（跟进 arduino-esp32 3.x，C6/C3/H2/P4 的 arduino 框架都在里面），
`platformio.ini` 里已经写好了：

```ini
platform = https://github.com/pioarduino/platform-espressif32/releases/download/55.03.311/platform-espressif32.zip
```

（`55.03.311` = Arduino v3.3.11 + ESP-IDF v5.5.5。钉死版本号是为了避免上游更新导致
构建漂移。）

### 还要一个显式依赖：NimBLE-Arduino

**arduino-esp32 3.x 不再内置 NimBLE。** 它的 `libraries/` 里只剩 Bluedroid 那套
（`BLE/`、`SimpleBLE/`），没有 `NimBLEDevice.h`。所以 `platformio.ini` 里必须写：

```ini
lib_deps =
    h2zero/NimBLE-Arduino@^2.1.0
```

不写就是编译期 `fatal error: NimBLEDevice.h: No such file or directory`。
**这一行要联网**（从 PlatformIO registry 拉），第一次编译前先把下面
[构建环境的坑](#构建环境的坑) 里的镜像设好。

### 构建环境的坑

这几条都是实测卡住过的，按顺序设好能省掉大部分折腾。都是**本机环境问题**，
不是板子或代码问题：

**1. 本机代理会让下载挂在半路。** 这台机器上有个 Clash 在 `127.0.0.1:7890`，
PlatformIO / pip 走它会一直挂着直到超时。绕过：

```powershell
$env:no_proxy = "*"          # 大小写两种都设，不同的库认的写法不一样
$env:NO_PROXY = "*"
```

**2. 控制台是 GBK，进度条会直接把 Python 打崩。** PlatformIO 的进度条里有 `░`
这个字符，GBK 编不出来，报 `UnicodeEncodeError` 然后整个进程挂掉：

```powershell
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8       = "1"
```

**3. GitHub 直连约 2 KB/s，走镜像约 15–23 MB/s。** `platform` 那一行是 GitHub
release 直链，实测速度差三个数量级。把 `platformio.ini` 里的 URL 临时换成：

```
https://gh-proxy.com/https://github.com/pioarduino/.../platform-espressif32.zip
```

**4. ESP-IDF 的工具链包建议预置。** ESP-IDF 的 `idf_tools.py` 从
`dl.espressif.com` 拉归档，同样容易断。可以先把归档下好放进 `~/.platformio/dist`，
让 idf_tools 直接命中缓存。**注意校验 sha256**，文件名和哈希都要和
`tools/idf_tools.py` 里的清单对上，放错了它会当成缓存未命中继续去下。

**5. pip 依赖走国内镜像。** `uv` / `pip` 装平台包时：

```powershell
$env:UV_DEFAULT_INDEX = "https://mirrors.aliyun.com/pypi/simple/"
$env:UV_INDEX_URL     = "https://mirrors.aliyun.com/pypi/simple/"
$env:PIP_INDEX_URL    = "https://mirrors.aliyun.com/pypi/simple/"
```

### 如果依赖下载反复中断

PlatformIO 的下载器有个要命的性质：**连接一断就从 0 重下**，所以在大文件上会永远
卡在「下载 → 断 → 重新开始」的循环里（实测每次只拿到 ~5MB）。它的日志长这样：

```
Tool Manager: Warning! Package Mirror: ('Connection broken: IncompleteRead(5597820 bytes read, 253231201 more expected)', ...)
Tool Manager: Looking for another mirror...
```

而服务器**是支持断点续传的**（`Range` 请求返回 `206 Partial Content`）。所以手动用
curl 续传即可绕过：

```bash
curl -L -C - --retry 3 --retry-all-errors -o pkg.tar.gz "<包的 download_url>"
```

下载 URL 可以从 PlatformIO 注册表 API 拿：

```bash
curl -s "https://api.registry.platformio.org/v3/packages/<owner>/tool/<pkg>" | python -m json.tool
```

下完之后解压进 `~/.platformio/packages/<包名>/`，并**最后**才写 `.piopm`（PlatformIO
靠这个文件判断包已安装；中途失败时不要写，否则它会以为装好了）：

```json
{
    "type": "tool",
    "name": "<包名>",
    "version": "<版本>",
    "spec": { "owner": "<owner>", "id": <id>, "name": "<包名>", "requirements": null, "uri": null }
}
```

`id` 来自上面那个 API 响应的 `id` 字段。格式可以照抄 `~/.platformio/platforms/*/.piopm`。

## 你会看到什么

**扫描阶段** —— 所有 BLE 设备列出来，遥控器那行会带 `[HID 0x1812]`：

```
  (无名)                          6c:xx:xx:xx:xx:xx  rssi=-52  [HID 0x1812]
  Mi Remote                       1c:xx:xx:xx:xx:xx  rssi=-61  [HID 0x1812]
```

**订阅阶段** —— GATT 全表，关注 `0x2A4D` 那几条特征和它们的 `ReportRef: id=1`。
**带 `★ 输入报告（按键走这条）` 的那条才是键盘报告**，按键只有从它上面来：

```
▸ 服务 00001812-0000-1000-8000-00805f9b34fb
    ├─ 特征 00002a4d-...   handle=0x0023  -N-
    │     └─ ReportRef: id=1 type=1  ★ 输入报告（按键走这条）
    │     └─ ✅ 已订阅 notify
    ├─ 特征 00002a4e-...   handle=0x0026  -W-
    │     └─ ReportRef: id=2 type=2        ← 输出报告，不是按键
```

这块板子上这条路还挂着 **8 条厂商私有特征在推数据**，字节布局各不相同。固件只认
`ReportRef` 描述符里 `type == 1`（Input）的那些，**不是靠猜首字节** —— 猜错了会把
厂商私有数据喂进边沿合成器，凭空冒出按下或者粘键。见 `src/main.cpp` 的 `onNotify()`。

**监听阶段** —— 每按一个键打一行，键盘报告还会解码：

```
[   41230 ms] handle=0x0023 reportId=1 len=8  00 00 4A 00 00 00 00 00
      slot0  usage=0x004A  主页
```

## 判定标准（这是全部意义所在）

按 **【返回】**：

```
[   52100 ms] handle=0x0023 reportId=1 len=8  00 00 F1 00 00 00 00 00
      slot0  usage=0x00F1  返回      ★★★ 死键！Windows 会丢弃它 ★★★
```

- ✅ **看得到 `0x00F1`** → 免驱路线成立，整条架构可以往下走。按 `s` 打计分板确认。
- ❌ **完全没有任何报告**（按键时串口静默）→ 坏消息：遥控器固件在**发出去之前**就把这三个键吞了。那说明问题在遥控器侧，BLE 路线也救不了，得回到 QL-4 驱动那条路（或者接受这三个键无法用）。

这一个观察就能省掉后面几周的瞎猜。

### 键盘报告有两种布局（这是踩过的坑）

上面的 `len=8` 是**关键**。实测见过两种布局，差别只在开头有没有那个「报告 ID」字节：

```
8 字节   00 | 00 | F1 00 | 00 00 | 00 00        → usage 槽位在 2, 4, 6
         mod  rsvd   u1      u2      u3

9 字节   01 | 00 | 00 | F1 00 | 00 00 | 00 00   → usage 槽位在 3, 5, 7
         RID  mod  rsvd   u1      u2      u3
```

**这块小米蓝牙语音遥控器发的是 8 字节那种。** 早先的固件只认 9 字节（`len >= 9 &&
data[0] == 0x01`），于是整个解码链路被**静默跳过**：原始 hex dump 照打不误，
但 `slot0 usage=` 一行不出、`KEY` 一行不出、计分板恒为 `0/0/0`。

这个 bug 伪装得很好 —— 串口上有输出、看起来一切正常，**只有计分板是 0**。排查时
先看计分板，别被 dump 骗了。现在两种布局都认，见 `src/main.cpp` 的
`keyboardUsageBase()`。

---

## 协议行（Windows 端 App 读的就是这个）

串口上**人读诊断与机读协议是交织的**：App 端 `ovb_rc003/esp32_serial_input.py`
只认下面这五个前缀，其它任何行（GATT 表、十六进制 dump、中文注解、计分板）
一律忽略。所以你可以随便看诊断输出，不会干扰数据。

| 行 | 含义 |
|---|---|
| `RDY <协议版本> <固件版本> <板名>` | 固件开机 |
| `DEV <广播名>` | 连上了哪个遥控器（**只有名字，没有蓝牙地址**） |
| `BAT <电量>` | 电量，连接时读一次、之后随 notify 更新 |
| `KEY <usage 十六进制> <D\|U> <序号> <毫秒>` | 按键边沿，`D`=按下 `U`=松开 |
| `ERR <说明>` | 出错 / 掉线 |

一条真实的样子：

```
RDY 1 0.2.0 esp32-c6
DEV MI RC
BAT 46
KEY 0x00F1 D 1 52310
KEY 0x00F1 U 2 52480
```

⚠️ **`KEY` 行的 D/U 是固件自己合成的，不是遥控器给的。** BLE HID 键盘报告是
**全量快照**——每份报告只说「此刻按住了哪几个 usage」，里面没有边沿事件。固件
拿相邻两份报告做差集，新出现的算按下、消失的算松开（见 `src/main.cpp` 的
`emitKeyboardEdges()`）。这也是唯一可能有 bug 的地方：合成错了会表现为粘键或
重复触发。掉线时固件会把还按着的键补发一轮 `U`，避免主机端粘住。

⚠️ **`RDY` 和 `DEV` 只在开机时发一次**，不是握手。App 打开串口时会丢掉之前
缓冲的字节，所以如果 App 是在板子开机**之后**才启动的，这两行就收不到——
`remote_name` 和 `protocol_version` 会保持未知，**这不影响按键**。

Windows 端的自适应识别**正是靠这个特性来确认端口的**：串口候选多于一个时，它会
打开候选口听一会儿协议行（`RDY`/`DEV`/`BAT`/`KEY`/`ERR` 之一即可确认），没听到就
发一个 `r` 再听。因为**打开串口本身就会让 ESP32-C6 重启**（USB-Serial/JTAG 外设在
主机接入时的行为），开机徽标通常一两秒内就自己来了，`r` 只是对「早就开机跑着的
板子」的兜底。只有一个候选时探测器**不会打开任何端口**——那种情况下确认与否结果
都一样，白开一次只会多掉一次 BLE 连接。

手动想让 App 拿到 `DEV`：按板子的 RST 键，或在串口发 `r`。`BAT` 不受影响，
电量会周期性上报。

## 串口命令

| 按 | 作用 |
|---|---|
| `s` | 打印死键计分板（三个死键各收到多少次） |
| `r` | 重启，重新扫描（板子复位也行；顺带重发 `RDY`/`DEV`） |

每 30 秒自动打一次计分板。

---

## Windows 端怎么用（Remo / OVB RC003）

读这个串口的是 `apps/windows/rc003` 里的 `ovb_rc003/esp32_serial_input.py`。它和
Windows 蓝牙通路是**互斥**的（BLE 外设同时只接受一个中心），所以是个模式开关，
不是叠加：

| 配置项 | 默认 | 说明 |
|---|---|---|
| `input_source` | `windows_hid` | 改成 `esp32_bridge` 才走桥接板 |
| `esp32_serial_port` | `""` | 留空 = 自适应识别；填了就钉死这个口 |
| `esp32_serial_baud` | `115200` | |
| `esp32_serial_probe` | `true` | 关掉 = 只按 USB ID 排序，不确认协议 |
| `esp32_serial_probe_timeout` | `2.5` | 每个候选口的探测上限（秒） |
| `esp32_serial_scan_all_ports` | `false` | 连没认出来的 USB ID 也探测 |
| `esp32_start_timeout` | `5.0` | 等 reader 起来的时限 |
| `esp32_ping_interval` | `10.0` | 主机侧 `PING` 周期，0 = 关 |

识别规则：先按 USB ID 给串口**排序**（`0x303A` 原生 USB-Serial/JTAG 第一档，
CH340/CP2102/FTDI/PL2303 这些转串口芯片第二档，其余不参与），再用协议行
**确认**。只有排序不够（同一台机器上两块 ESP32 分不开），只有探测也不够
（那就得把机器上每个串口都打开一遍）。

设置页里那个「检测」按钮是**只读**的：它只排序，不打开任何口，所以桥接板正在
跑的时候按也安全。

## 常见问题

**扫不到遥控器** → 90% 是 Windows 蓝牙没关，遥控器还连在 Windows 上。
**连上了但订阅失败** → 报告特征要求加密链路，看 `[ble] 配对完成` 那行；Just Works 免配对码应该能过。
**扫到了但没认出哪个是** → 看 `[HID 0x1812]` 标记；实在不行把地址填到 `main.cpp` 里硬指定。
**按键时串口完全静默** → 见上面的「判定标准」第二条，这是有价值的负面结果，不是 bug。
**原始 dump 在打、但 `slot0 usage=` 和 `KEY` 一行不出、计分板恒为 `0/0/0`**
→ 就是[报告布局](#键盘报告有两种布局这是踩过的坑)那个坑：解码器认错了字节偏移。
先看计分板，别被 dump 骗了 —— 有输出不等于解码成功。

**Windows 端认不出板子** → 只有**一个**候选串口时探测器不打开任何端口，直接用它；
有多个候选时才会去听协议行确认。如果板子是接在 CH340 / CP2102 这类转串口芯片上，
先装好驱动；实在不行在设置页把端口名填死（留空 = 自动检测）。
