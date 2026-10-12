// ============================================================================
//  RC003 → ESP32-C6 BLE 桥 · 阶段 1：诊断固件 + 机器可读协议输出
// ----------------------------------------------------------------------------
//  目标（只做一件事）：作为 BLE central 连上小米 RC003，把所有 HID 报告原样
//  打印出来，直接回答一个决定性的问题：
//
//      「返回 / 音量± 这三个 Windows 会丢掉的 usage，
//        到底在不在 BLE 报文的原始字节里？」
//
//  如果答案是「在」——整条免驱路线就被证实了：
//    遥控器 → ESP32-C6 → USB CDC → Windows 小助手 → 你的 App
//  没有 kbdhid、没有 LL 钩子、没有 Frida、没有管理员、没有 Secure Boot。
//
//  本固件同时输出两套东西，**串口上人读输出与机读协议行是交织的**：
//
//    · 人读诊断：GATT 表、每条报告的十六进制裸 dump、逐槽位解码、死键计分板
//    · 机读协议：主机端 ovb_rc003/esp32_serial_input.py 解析的那些行 ——
//
//        RDY <协议版本> <固件版本> <板名>
//        DEV <遥控器广播名>
//        BAT <电量百分比>
//        KEY <usage 十六进制> <D|U> <序号> <开机毫秒>
//        ERR <说明>
//
//      主机端解析器**只认这些前缀**，其它任何行一律忽略，所以下面那些中文
//      诊断可以随便穿插，不会污染数据流。
//
//  ⚠ BLE HID 键盘报告是**全量快照**（每个报告列出「此刻按住了哪几个 usage」），
//    里面**没有**按下/松开事件。所以 KEY 行里 D/U 两个边沿是本固件拿相邻两次
//    报告做差集合成的 —— 见 emitKeyboardEdges()。合成逻辑一旦出错，就会表现
//    为「按键粘住」或「重复触发」，这是本文件最容易出错的地方。
//
//  硬件：ESP32-C6FH8（无 USB-OTG，USB 口只能做 CDC 串口）
//  串口：USB Serial/JTAG = COM3，全程保留，不会被固件占用掉
// ============================================================================

#include <Arduino.h>
#include <NimBLEDevice.h>
#include <vector>

// ---------------------------------------------------------------------------
// 配置
// ---------------------------------------------------------------------------
static const char* BRIDGE_NAME = "RC003-Bridge";

// 机读协议版本 —— 必须与 esp32_serial_input.PROTOCOL_VERSION 一致。
// 主机端在 RDY 里读到不认识的版本只会记一条日志，不会因此拒连。
static const int         PROTOCOL_VERSION = 1;
static const char*       FW_VERSION       = "0.2.0";
static const char*       BOARD_NAME       = "esp32-c6";

// 小米 RC003 的广播名候选（不同固件/区域不完全一致）
static const char* NAME_HINTS[] = { "MI RC", "mi rc", "RC003", "Remote", "遥控" };
static const int    NAME_HINT_COUNT = sizeof(NAME_HINTS) / sizeof(NAME_HINTS[0]);

static const uint16_t HID_SERVICE_UUID = 0x1812;
static const uint16_t REPORT_CHAR_UUID = 0x2A4D;
static const uint16_t REPORT_REF_UUID  = 0x2908;
static const uint16_t BATTERY_LVL_UUID = 0x2A19;

static const uint32_t SCAN_SECONDS = 10;

// 一份键盘报告里最多几个 usage 槽。实测遥控器只填 3 个（报告 8 或 9 字节，
// 见 keyboardUsageBase()），这里留一倍余量，按实际长度取用。
static const int MAX_USAGE_SLOTS = 8;

// ---------------------------------------------------------------------------
// RC003 usage 表（来自本项目三份独立实测）
// ---------------------------------------------------------------------------
struct UsageName { uint16_t usage; const char* name; };

static const UsageName USAGE_TABLE[] = {
    { 0x0028, "确定"     }, { 0x0035, "直播"   }, { 0x003E, "麦克风" },
    { 0x004A, "主页"     }, { 0x004F, "右"     }, { 0x0050, "左"     },
    { 0x0051, "下"       }, { 0x0052, "上"     }, { 0x0065, "菜单"   },
    { 0x0066, "电源"     }, { 0x007F, "静音"   }, { 0x0080, "音量+"  },
    { 0x0081, "音量-"    },
};
static const int USAGE_TABLE_COUNT = sizeof(USAGE_TABLE) / sizeof(USAGE_TABLE[0]);

// ★ 这三个正是 Windows kbdhid.sys 会静默丢弃的
static bool isDeadUsage(uint16_t u) {
    return u == 0x00F1 || u == 0x0080 || u == 0x0081;
}

static const char* usageName(uint16_t u) {
    if (u == 0x00F1) return "返回";
    for (int i = 0; i < USAGE_TABLE_COUNT; i++)
        if (USAGE_TABLE[i].usage == u) return USAGE_TABLE[i].name;
    return nullptr;
}

// ---------------------------------------------------------------------------
// 状态
// ---------------------------------------------------------------------------
struct ReportSub {
    NimBLERemoteCharacteristic* chr   = nullptr;
    uint8_t  reportId   = 0;
    uint8_t  reportType = 0;   // 1 = Input
    uint16_t handle     = 0;
    uint32_t count      = 0;
    bool     isBattery  = false;   // 电池不是按键，走 BAT 行而不是 HEX dump
};
static std::vector<ReportSub> g_subs;

static NimBLEClient* g_client = nullptr;
static volatile bool g_connected = false;

// 统计：三个死键各收到多少次（这是本次实验的核心指标）
static uint32_t g_deadHits[3] = { 0, 0, 0 };   // 返回 / 音量+ / 音量-
static uint32_t g_totalReports = 0;

// ---------------------------------------------------------------------------
// 按键边沿合成状态
// ---------------------------------------------------------------------------
// 遥控器每发一份报告 = 一份「当前按住了哪些 usage」的快照。要还原成主机能用的
// 按下/松开事件，必须记住上一份快照并取差集。
static uint16_t g_held[MAX_USAGE_SLOTS] = { 0 };   // 上一份报告里的 usage 集合
static int      g_heldCount = 0;                   // 该集合的元素个数

// KEY 行的序号，从 1 开始。主机端只用它来丢弃重复投递，不解释含义。
static uint32_t g_seq = 0;

// ---------------------------------------------------------------------------
// 工具
// ---------------------------------------------------------------------------
static void hexDump(const uint8_t* d, size_t n, char* out, size_t outSize) {
    size_t p = 0;
    for (size_t i = 0; i < n && p + 3 < outSize; i++)
        p += snprintf(out + p, outSize - p, "%02X ", d[i]);
    if (p > 0) out[p - 1] = '\0'; else out[0] = '\0';
}

// ===========================================================================
//  机读协议输出（主机端 esp32_serial_input.py 消费的就是这些行）
// ===========================================================================

// 键盘报告里 usage 槽位从第几个字节开始。
//
// 两种布局都实测见过，差别只在开头有没有那个「报告 ID」字节：
//   8 字节   00 | 00 | F1 00 | 00 00 | 00 00        → 槽位在 2, 4, 6
//            mod  rsvd   u1      u2      u3
//   9 字节   01 | 00 | 00 | F1 00 | 00 00 | 00 00   → 槽位在 3, 5, 7
//            RID  mod  rsvd   u1      u2      u3
//
// 现在这块小米蓝牙语音遥控器发的是 8 字节那种。早先只认 9 字节，整个解码
// 链路就被静默跳过了 —— 原始 hex dump 照打，但 KEY 行一条不出、计分板恒为 0。
static size_t keyboardUsageBase(size_t n) {
    return (n >= 9) ? 3 : 2;
}

// 从键盘报告里取出「当前按住的 usage」集合，返回去重后的槽位数。
static int extractUsages(const uint8_t* d, size_t n, uint16_t* out, int maxOut) {
    int count = 0;
    for (size_t off = keyboardUsageBase(n); off + 1 < n && count < maxOut; off += 2) {
        uint16_t u = (uint16_t)d[off] | ((uint16_t)d[off + 1] << 8);
        // 0x0000 = 空槽；0x0001 = HID 的 ErrorRollOver，都不是真实按键。
        if (u == 0x0000 || u == 0x0001) continue;
        bool dup = false;
        for (int i = 0; i < count; i++) if (out[i] == u) { dup = true; break; }
        if (!dup) out[count++] = u;
    }
    return count;
}

static void emitKey(uint16_t usage, bool pressed, uint32_t ms) {
    // 必须与 parse_protocol_line() 的 KEY 分支逐字对齐：4 个字段、大写十六进制。
    // uint16_t 会整型提升成 int，而 %04X 要的是 unsigned int —— 显式转一下。
    Serial.printf("KEY 0x%04X %c %lu %lu\n",
                  (unsigned)usage, pressed ? 'D' : 'U',
                  (unsigned long)++g_seq, (unsigned long)ms);
}

// 拿新旧两份快照做差集，合成按下 / 松开边沿。
//
// 顺序上先发 D 再发 U：先按下再松开，主机端看到的时序才单调。同一份报告里
// 同时含有一个新按下和一个已松开的 usage 是正常情况（连击两个键时）。
static void emitKeyboardEdges(const uint16_t* now, int nowCount, uint32_t ms) {
    // 新出现的 → 按下
    for (int i = 0; i < nowCount; i++) {
        bool was = false;
        for (int j = 0; j < g_heldCount; j++)
            if (g_held[j] == now[i]) { was = true; break; }
        if (!was) emitKey(now[i], true, ms);
    }
    // 上一份里还在、这一份没了的 → 松开
    for (int i = 0; i < g_heldCount; i++) {
        bool still = false;
        for (int j = 0; j < nowCount; j++)
            if (now[j] == g_held[i]) { still = true; break; }
        if (!still) emitKey(g_held[i], false, ms);
    }
    // 快照滚动
    g_heldCount = nowCount;
    for (int i = 0; i < nowCount; i++) g_held[i] = now[i];
}

// 断开时把还按着的键全部补一条松开边沿，再清空快照。
//
// 这两件事缺一不可：不补松开，主机端会一直以为那个键还按着（重连要几秒钟，
// 期间会看到粘键）；不清空，重连后如果用户正按着同一个键，那份报告和残留快照
// 一致，按下边沿就被吞掉了。
static void releaseAllHeld(uint32_t ms) {
    for (int i = 0; i < g_heldCount; i++) emitKey(g_held[i], false, ms);
    g_heldCount = 0;
}

// 逐槽位解码键盘报告（两种布局见 keyboardUsageBase()）。
// 这一份是**人读**版本，打印槽位序号并标出死键，方便肉眼对照原始 dump。
// 机读版本走 extractUsages() + emitKeyboardEdges()，两者刻意分开：人读要看到
// 槽位序号和全 0 的松开报告，机读只要边沿。
static void decodeKeyboardReport(const uint8_t* d, size_t n) {
    uint16_t usages[3] = { 0, 0, 0 };
    int found = 0;
    for (int slot = 0; slot < 3; slot++) {
        size_t off = keyboardUsageBase(n) + (size_t)slot * 2;
        if (off + 1 >= n) break;
        uint16_t u = (uint16_t)d[off] | ((uint16_t)d[off + 1] << 8);
        usages[slot] = u;
        if (u == 0) continue;
        found++;

        const char* nm = usageName(u);
        bool dead = isDeadUsage(u);

        Serial.printf("      slot%d  usage=0x%04X  %-8s %s\n",
                      slot, (unsigned)u, nm ? nm : "(未知)",
                      dead ? "★★★ 死键！Windows 会丢弃它 ★★★" : "");

        if (u == 0x00F1) g_deadHits[0]++;
        else if (u == 0x0080) g_deadHits[1]++;
        else if (u == 0x0081) g_deadHits[2]++;
    }
    if (found == 0) Serial.println("      (全 0 = 松开事件)");
}

// ---------------------------------------------------------------------------
// 通知回调
// ---------------------------------------------------------------------------
static void onNotify(NimBLERemoteCharacteristic* chr,
                     uint8_t* data, size_t len, bool isNotify) {
    // 找到这条特征对应的登记项
    ReportSub* sub = nullptr;
    for (auto& s : g_subs)
        if (s.chr == chr) { sub = &s; break; }

    g_totalReports++;
    if (sub) sub->count++;

    // 时间戳（毫秒，自启动）
    uint32_t ms = millis();

    // 电池：走 BAT 行，不做十六进制 dump，也不参与按键边沿合成。
    if (sub && sub->isBattery) {
        if (len >= 1) Serial.printf("BAT %u\n", (unsigned)data[0]);
        return;
    }

    char hex[128];
    hexDump(data, len, hex, sizeof(hex));

    Serial.printf("[%8lu ms] ", (unsigned long)ms);
    if (sub) Serial.printf("handle=0x%04X reportId=%u ", sub->handle, sub->reportId);
    else     Serial.print("handle=?? ");

    Serial.printf("len=%u  %s\n", (unsigned)len, hex);

    // 只处理 HID 输入报告。判定依据是 ReportRef 描述符里的 type（1 = Input），
    // 比猜首字节可靠得多 —— 这条链路上还挂着 8 条厂商私有特征在推数据，字节
    // 布局各不相同，误喂进边沿合成器会凭空冒出按下或粘键。
    if (sub && sub->reportType == 1 && len >= 8) {
        decodeKeyboardReport(data, len);          // 人读：逐槽位

        uint16_t now[MAX_USAGE_SLOTS];
        int nowCount = extractUsages(data, len, now, MAX_USAGE_SLOTS);
        emitKeyboardEdges(now, nowCount, ms);     // 机读：合成边沿
    }
}

// ---------------------------------------------------------------------------
// 客户端回调
// ---------------------------------------------------------------------------
class ClientCallbacks : public NimBLEClientCallbacks {
    void onConnect(NimBLEClient* c) override {
        Serial.println("[ble] 已连接");
        g_connected = true;
    }
    void onDisconnect(NimBLEClient* c, int reason) override {
        Serial.printf("[ble] 断开，reason=%d (0x%02X)\n", reason, reason);
        g_connected = false;
        releaseAllHeld(millis());
        Serial.printf("ERR disconnected reason=0x%02X\n", reason);
    }
    void onPassKeyEntry(NimBLEConnInfo& info) override {
        Serial.println("[ble] 对端要求配对码 —— 遥控器没有键盘，理论上不该走到这里");
    }
    void onAuthenticationComplete(NimBLEConnInfo& info) override {
        Serial.printf("[ble] 配对完成 encrypted=%d bonded=%d authenticated=%d\n",
                      info.isEncrypted(), info.isBonded(), info.isAuthenticated());
    }
};

// ---------------------------------------------------------------------------
// 扫描：把看到的所有设备打出来（名字过滤失败时靠这个定位遥控器）
// ---------------------------------------------------------------------------
class ScanCallbacks : public NimBLEScanCallbacks {
    void onResult(const NimBLEAdvertisedDevice* dev) override {
        bool hasHid = dev->isAdvertisingService(NimBLEUUID(HID_SERVICE_UUID));
        String name = dev->haveName() ? String(dev->getName().c_str()) : String("(无名)");

        Serial.printf("  %-32s %s  rssi=%-4d %s\n",
                      name.c_str(),
                      dev->getAddress().toString().c_str(),
                      dev->getRSSI(),
                      hasHid ? "[HID 0x1812]" : "");
    }
};

// ---------------------------------------------------------------------------
// GATT 全表打印 + 订阅
// ---------------------------------------------------------------------------
static void subscribeAll(NimBLEClient* client) {
    Serial.println();
    Serial.println("════════ GATT 表 ════════");

    const std::vector<NimBLERemoteService*>& svcs = client->getServices(true);
    Serial.printf("发现 %u 个服务\n\n", (unsigned)svcs.size());

    for (auto* svc : svcs) {
        Serial.printf("▸ 服务 %s\n", svc->getUUID().toString().c_str());

        const std::vector<NimBLERemoteCharacteristic*>& chars =
            svc->getCharacteristics(true);

        for (auto* chr : chars) {
            Serial.printf("    ├─ 特征 %s   handle=0x%04X  %s%s%s\n",
                          chr->getUUID().toString().c_str(),
                          chr->getHandle(),
                          chr->canRead()      ? "R" : "-",
                          chr->canNotify()    ? "N" : "-",
                          chr->canIndicate()  ? "I" : "-");

            if (!chr->canNotify() && !chr->canIndicate()) continue;

            ReportSub sub;
            sub.chr    = chr;
            sub.handle = chr->getHandle();

            // 电池特征走 BAT 行：先读一次当前值，再照常订阅，
            // 这样后续的电量变化也自动变成 BAT 行。
            if (chr->getUUID() == NimBLEUUID(BATTERY_LVL_UUID)) {
                sub.isBattery = true;
                if (chr->canRead()) {
                    try {
                        NimBLEAttValue v = chr->readValue();
                        if (v.size() >= 1) Serial.printf("BAT %u\n", (unsigned)v[0]);
                    } catch (...) {
                        Serial.println("    │     └─ 电量读取失败（可能需要先加密）");
                    }
                }
            }

            // Report Reference 描述符 (0x2908) = [reportId, reportType]
            NimBLERemoteDescriptor* ref = chr->getDescriptor(NimBLEUUID(REPORT_REF_UUID));
            if (ref) {
                try {
                    NimBLEAttValue v = ref->readValue();
                    if (v.size() >= 2) {
                        sub.reportId   = v[0];
                        sub.reportType = v[1];
                        Serial.printf("    │     └─ ReportRef: id=%u type=%u%s\n",
                                      sub.reportId, sub.reportType,
                                      sub.reportType == 1
                                          ? "  ★ 输入报告（按键走这条）" : "");
                    }
                } catch (...) {
                    Serial.println("    │     └─ ReportRef 读取失败（可能需要加密）");
                }
            }

            // 只订阅输入报告（type 1）与未知类型的；输出报告订阅无意义
            if (sub.reportType != 0 && sub.reportType != 1) {
                Serial.println("    │     └─ 跳过（非输入报告）");
                continue;
            }

            if (chr->subscribe(true, onNotify, true)) {
                g_subs.push_back(sub);
                Serial.printf("    │     └─ ✅ 已订阅 notify\n");
            } else {
                // getLastError() 挂在 NimBLEClient 上；NimBLERemoteCharacteristic
                // 没有这个成员（2.x），写成 chr->getLastError() 编不过。
                Serial.printf("    │     └─ ❌ 订阅失败 (err=%d)\n", client->getLastError());
            }
        }
        Serial.println();
    }

    Serial.printf("共订阅 %u 条特征\n", (unsigned)g_subs.size());
    Serial.println("════════════════════════");
}

// ---------------------------------------------------------------------------
static void printBanner() {
    Serial.println();
    Serial.println("╔══════════════════════════════════════════════════════════╗");
    Serial.println("║  RC003 → ESP32-C6 BLE 桥 · 阶段 1 诊断固件                ║");
    Serial.println("║  目的：验证三个「死键」是否存在于 BLE 原始报文中          ║");
    Serial.println("╚══════════════════════════════════════════════════════════╝");
    Serial.println();
}

static void printDeadKeyScoreboard() {
    Serial.println();
    Serial.println("┌──────────── 死键计分板 ────────────┐");
    Serial.printf("│  总报告数      : %-6lu          │\n", (unsigned long)g_totalReports);
    Serial.printf("│  返回  0x00F1  : %-6lu %s   │\n",
                  (unsigned long)g_deadHits[0], g_deadHits[0] ? "✅" : "  ");
    Serial.printf("│  音量+ 0x0080  : %-6lu %s   │\n",
                  (unsigned long)g_deadHits[1], g_deadHits[1] ? "✅" : "  ");
    Serial.printf("│  音量- 0x0081  : %-6lu %s   │\n",
                  (unsigned long)g_deadHits[2], g_deadHits[2] ? "✅" : "  ");
    Serial.println("└────────────────────────────────────┘");
    if (g_deadHits[0] || g_deadHits[1] || g_deadHits[2]) {
        Serial.println(">>> 死键在 BLE 报文里【可见】—— 免驱路线成立！");
    }
    Serial.println();
}

// ---------------------------------------------------------------------------
void setup() {
    Serial.begin(115200);
    delay(1500);   // 等 USB CDC 枚举

    // 开机横幅 + RDY/DEV 这类元数据行。
    //
    // ⚠ RDY 只在**开机**发一次，不是握手。主机端打开串口时会 reset_input_buffer()
    //   丢掉之前的所有字节，而本固件又刻意不拉 DTR/RTS（见 esp32_serial_input.py
    //   的 _open_port），所以主机如果在板子开机之后才启动，就收不到 RDY 和 DEV ——
    //   remote_name / protocol_version 会保持未知，这是预期行为，不影响按键。
    //   想让主机拿到它们：按板子的 RST，或在串口发 'r' 让固件重启。
    //   （BAT 不受影响：电量特征会周期性 notify，主机迟早收到。）
    //
    //   主机端的就绪判定不看 RDY —— 它是「读线程起来了」而不是「板子回话了」，
    //   所以这里的先后顺序不影响能否连上。
    Serial.printf("RDY %d %s %s\n", PROTOCOL_VERSION, FW_VERSION, BOARD_NAME);

    printBanner();

    NimBLEDevice::init(BRIDGE_NAME);
    NimBLEDevice::setMTU(247);

    // bonding + Just Works（遥控器无输入/输出能力） + LE Secure Connections
    NimBLEDevice::setSecurityAuth(true, false, true);
    NimBLEDevice::setSecurityIOCap(BLE_HS_IO_NO_INPUT_OUTPUT);

    Serial.println("[1/4] 扫描中… 请把遥控器置于配对模式（同时长按【主页】+【菜单】直到指示灯快闪）");
    Serial.println();

    NimBLEScan* scan = NimBLEDevice::getScan();
    scan->setScanCallbacks(new ScanCallbacks(), false);
    scan->setActiveScan(true);
    scan->setInterval(100);
    scan->setWindow(80);

    NimBLEScanResults results = scan->getResults(SCAN_SECONDS * 1000);

    if (results.getCount() == 0) {
        Serial.println("ERR scan found no devices");
        Serial.println();
        Serial.println("❌ 没扫到任何设备。检查：");
        Serial.println("   · Windows 蓝牙是否已【关闭】？（遥控器同时只能连一个主机）");
        Serial.println("   · 遥控器是否已进入配对模式（主页+菜单 长按，指示灯快闪）？");
        Serial.println("   · 遥控器是否有电？（按任意键看指示灯）");
        Serial.println();
        Serial.println("   重置后重试。");
        return;
    }

    // 选目标：优先 HID 服务，其次名字匹配
    // NimBLEScanResults::getDevice() 在 2.x 里返回的是 const 指针，所以这几
    // 个局部变量都得跟着 const。connect() 接受 const NimBLEAdvertisedDevice*，
    // 所以不影响后面连接。
    const NimBLEAdvertisedDevice* hidDev = nullptr;
    const NimBLEAdvertisedDevice* nameDev = nullptr;

    for (int i = 0; i < results.getCount(); i++) {
        const NimBLEAdvertisedDevice* d = results.getDevice(i);
        if (d->isAdvertisingService(NimBLEUUID(HID_SERVICE_UUID)) && !hidDev) hidDev = d;

        if (d->haveName() && !nameDev) {
            String nm = String(d->getName().c_str());
            for (int h = 0; h < NAME_HINT_COUNT; h++)
                if (nm.indexOf(NAME_HINTS[h]) >= 0) { nameDev = d; break; }
        }
    }

    const NimBLEAdvertisedDevice* target = nameDev ? nameDev : hidDev;
    if (!target) {
        Serial.println("ERR scan found no RC003 (no name hint, no HID advertiser)");
        Serial.println();
        Serial.println("⚠️  扫到了设备，但没有名字匹配、也没发现 HID 服务的。");
        Serial.println("    若上面列表里有疑似遥控器的项，改 main.cpp 里的地址硬指定后重刷。");
        return;
    }

    Serial.println();
    Serial.printf("[2/4] 目标：%s  %s  rssi=%d\n",
                  target->haveName() ? target->getName().c_str() : "(无名)",
                  target->getAddress().toString().c_str(),
                  target->getRSSI());

    // DEV 行只带广播名，**不带蓝牙地址** —— 主机端会把它存进 listener 并可能
    // 显示出来，而项目的隐私契约（tests/test_privacy_contract.py）禁止把
    // MAC / 设备路径带进包内。地址只出现在上面这行人读诊断里。
    Serial.printf("DEV %s\n",
                  target->haveName() ? target->getName().c_str() : "unknown");

    Serial.println("[3/4] 连接 + 配对…");
    g_client = NimBLEDevice::createClient();
    g_client->setClientCallbacks(new ClientCallbacks(), false);
    g_client->setConnectionParams(12, 12, 0, 400);

    if (!g_client->connect(target)) {
        Serial.println("ERR connect failed");
        Serial.println("❌ 连接失败");
        return;
    }

    // HID 报告特征通常要求加密链路 —— 触发 Just Works 配对。
    // 加密状态在 2.x 里挂在连接信息上：NimBLEClient 本身没有 isEncrypted()，
    // 那个成员属于 NimBLEConnInfo（NimBLEDevice.h 在 central 角色下会带上它）。
    if (!g_client->getConnInfo().isEncrypted()) {
        Serial.println("[ble] 链路未加密，发起配对…");
        g_client->secureConnection();
        uint32_t t0 = millis();
        while (!g_client->getConnInfo().isEncrypted() && millis() - t0 < 8000) delay(100);
        Serial.printf("[ble] 加密状态：%s\n",
                      g_client->getConnInfo().isEncrypted() ? "已加密 ✅" : "仍未加密 ⚠️");
    }

    Serial.println();
    Serial.println("[4/4] 枚举 GATT 并订阅全部输入报告…");
    subscribeAll(g_client);

    Serial.println();
    Serial.println("════════════ 开始监听 ════════════");
    Serial.println("现在按遥控器上的键（每个键按一下、松开）。");
    Serial.println("重点按：【返回】【音量+】【音量-】——这三个就是 Windows 丢掉的。");
    Serial.println();
    Serial.println("命令：按串口发送  s = 打印死键计分板 | r = 重新扫描");
    Serial.println();
    Serial.println("本行以下同时输出机读协议行（PC 端 App 就吃这些）：");
    Serial.println("  KEY <usage> <D|U> <序号> <毫秒>   —— D=按下 U=松开");
    Serial.println("  BAT <电量>                        —— 电量变化时");
    Serial.println("  ERR <说明>                        —— 出错 / 掉线");
    Serial.println("═══════════════════════════════════");
    Serial.println();
}

// ---------------------------------------------------------------------------
void loop() {
    if (Serial.available()) {
        int c = Serial.read();
        if (c == 's' || c == 'S') printDeadKeyScoreboard();
        if (c == 'r' || c == 'R') { Serial.println("重启中…"); delay(200); ESP.restart(); }
    }

    // 掉线提示
    static bool warned = false;
    if (!g_connected && g_subs.size() > 0 && !warned) {
        warned = true;
        Serial.println("⚠️  连接已断开。按 'r' 重新扫描，或复位板子。");
    }
    if (g_connected) warned = false;

    // 每 30 秒自动打一次计分板
    static uint32_t last = 0;
    if (millis() - last > 30000) { last = millis(); printDeadKeyScoreboard(); }

    delay(50);
}
