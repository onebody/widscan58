# S3 作 Wi-Fi 中继宿主、C5 作扫频头 —— 设计

日期：2026-11-01 · 分支基线：`rf-track` @ `9de3319` · 状态：**待用户审阅**（本文件落盘时实现代码零行）

## 0. 目标与当前实况

目标（用户 2026-11-01 原话口径）：**C5 那边扫频的所有相关数据，都要经 S3 的 2.4G Wi-Fi 提交到 RF Track 平台，并通过平台显示。**

2026-11-01 22:1x 一手取证（`/tmp/w58-probe/identify2.py`，两端口当时无人占用）：

| 端口 | `INFO` 应答 | `PROTOCOL?` |
|---|---|---|
| `/dev/cu.usbmodem1201` | `widscan58\|esp32c5\|1\|Web-v0.10\|2026-10-29T00:07:24+08:00\|109,edb400d6` | `PROTOCOL RH` |
| `/dev/cu.usbmodem1301` | `widscan58\|esp32s3\|1\|rftrack-baseline-v0.1.0-28-g9de\|2026-10-31T21:38:24+08:00\|712,87e8c20e` | `PROTOCOL RH` |

设备端现状（同一轮 grep 取证）：

- `WIFI_MODE_STA` / `esp_wifi_connect` / SSID 处理：固件里 **0 处**。唯一的模式设置是 `firmware/src/main.c:95` 的 `esp_wifi_set_mode(WIFI_MODE_NULL)`——Wi-Fi 栈只为把 PHY 交给 SDR 嗅探路径（混杂模式），不做关联。
- `firmware/src/net/`：**目录不存在**；全仓库 `websocket` 引用 **0 处**。
- `firmware/src/proto/uart_link.c:61-76`：`#else` 分支（S3）四个函数全是 `return false/-1` 空桩，注释原文 "The S3 has no Pi link: keep the target building with no UART sink."

⇒ 结论按六档口径：**当前不支持**。传输层的抽象已经就位（`link.h` 的多 sink 设计），缺的是**设备端实现**，即文档里挂后的 F2。

## 1. 与《C5-S3 双芯片通讯方案-ChatGPT采集.md》的对账

仓库里已有一份用户收集的双芯片通信方案采集件（`docs/C5-S3-双芯片通讯方案-ChatGPT采集.md`，1190 行，永不入库）。它和本设计有两处实质分歧，必须显式记账而不是默默偏离。

### 分歧 A：C5 侧引脚 —— 采集件要独立第二 UART，本设计复用 GP11/GP12

采集件 §4.1 与 §九 的硬要求：「RH 使用 GP11 TX / GP12 RX，这一对引脚…应由现有 RH 模块继续独占，新链路不得调用该 UART 的 `uart_write_bytes()`」。

本设计**复用** GP11/GP12，理由：它的前提是「C5 同时还在为树莓派上的 RotorHazard 供数」。目标拓扑里 **S3 就是 GP11/12 的另一端**（它取代 Pi 的位置），没有第三方在同一总线上监听，所以"污染 RH"这条不发生。这条口已由 F1（`7bec675`）烧板实测过（UART 6.80 KiB/s、558 帧 CRC 全对）。

代价（写进界面与文档，不当实现细节藏起来）：C5 的 GP11/12 是**单主端口** ⇒ **S3 中继与 Pi 的 RotorHazard 互斥**，同一时刻只有一方拿得到 C5 的帧流。这与 2026-10-29 已裁定的 ②a「桥接时停/复 RH」是同一条互斥，只是互斥的另一方从"Pi 上的 RH"换成"S3"。

要「Pi RH 计圈」与「S3 Wi-Fi 上报」并存 ⇒ 走采集件的独立第二 UART 路线（本设计列为 **M3，后置**）。它的真实成本是 C5 新增固件 + 一次 GPIO 可用性审计：C5-Zero 边缘 9+9 里非 flash、非 USB 的可用脚只有 GP0–GP5（左列）与 GP6–GP10（右列内段），GP13/14 是 USB-Serial-JTAG，GP16–GP22 接 SPI flash，GP23/24/25/28 是背面内圈焊盘，GP26/27 分别是天线切换与 WS2812。

### 分歧 B：协议分层 —— 采集件要新信封，本设计纯字节透传

采集件 §5 设计的是 10 B 头 + CRC-16/CCITT + 序号 + `HELLO/HEARTBEAT/SCAN_RESULT/LAP_EVENT/ACK/FLOW_CONTROL`。本设计的 S3 **不解析、不封包**，只在 UART 与 WS 之间搬字节。

三条理由：

1. widscan58 帧流自带 `0xA5` 同步头 + CRC + 重同步语义，平台侧 `rftrack/backend/app/services/frame_parser.py` 已按这套口径实现，并有 37 项对拍（期望值由 `web/src/serial/parser.ts` 权威导出）与真机 ① 路背书。再加一层信封＝同一件事做两遍，且两个 CRC 会互相掩盖。
2. 一旦 S3 解析帧，它就不再是 pipe——`web/serve.mjs:393` `attachNode()` / `:427` `bridgeRemote()` 今天做的正是零翻译字节转发，F2 既有约定也是「中继不做协议翻译」。翻案需要新证据，本设计没有。
3. 透传让 §6 的 L5「USB 路与 WS 路逐字段对拍」成为**可判定命题**：同一条 `frame_parser` 吃两份字节。封包后对拍点变成两套解析器，证明力直接下降。

采集件里有价值、本设计照收的两点：**丢帧策略按数据类别分开**（对应物＝扫描帧丢旧留新 + `link_drops()`/`link_fill_permille()` 计数；`LAP_EVENT` 类可靠事件不在 ③ 这条路上，明确不做）与**统一链路指标表**（落进 L2 的观测项）。

### 顺带修正我自己先前的推荐

上一轮我把「能力声明」岔口推荐成「(i) 保持 unsupported」。查到计划文档 §7 S3 行申报④：「`PUT /api/nodes/{id}` 仍未做…『显式声明能力』这条对客入口按阶段边界留给需要它的那一端（`fw_version`/`protocol_version`/`capabilities_json` 三列一律 NULL，读侧渲染 `unsupported`，不代填）」，且 `models.py:212-214` 那三列确实已在、`routers/nodes.py` 今天只有两个 GET。

⇒ 「人工显式声明」不是新开的红线，而是**原计划欠着的入口，现在正是需要它的那一端**。所以：M1/M2 期间如实显示 `unsupported`；把 `PUT /api/nodes/{id}` 作为 M2 收尾项补上（操作员写，绝不按到达的帧回填）。「按帧回填」仍然否决。

## 2. 拓扑与数据流

```
ESP32-C5-Zero（扫频头）                ESP32-S3（中继宿主）        接入机 / 平台
 scanner.c:343 FRAME_LEVELS            uart_link.c S3 分支         serve.mjs:393 attachNode
 频谱帧 / 遥测    ── GP11 TX ──921600──▶ UART RX ─┐                ingest.py:34-37 "remote"→"node-ws"
 RAWIQ（仍只走 USB，link.h:42）                    ├─ 有界发送环 ──▶ ws://:8011/node?id=… ──▶ /ws/bridge → capture
 命令 ASCII 行  ◀── GP12 RX ◀──921600─── UART TX ─┘   + 退避重连      backend: 落库/插件/告警/工作台
        （共地；S3 不解析任何帧）
```

每条边的归属：**C5→S3 是新链路**（波特率 + pace + S3 的 UART 实现）；**S3→桥是已实现且实测过的 `node-ws`**（③ 链路在 2026-10-31 用模拟节点全链跑通过）；**桥→平台不动**。

## 3. 带宽预算：为什么要 921600 + 拆限速

| 路 | 上限/实测 | 说明 |
|---|---|---|
| ① USB（S3 板） | 实测 **52.1 KB/s**；C5 板 **37.0 KB/s** | 2026-10-31 真机 |
| ②a UART @115200 | 线路上限 **11.25 KiB/s**；实测 **6.80 KiB/s** | F1 `7bec675` |
| 目标 UART @921600 | 线路上限 ≈ **90 KiB/s** | 对 37.0 有 2.4× 余量 |

关键事实：②a 只跑到 6.80 KiB/s **不是线的错**，是固件自己钳的——`commands.c:407-416` 在「MONITOR 且 sink==UART」时置 `c.uart_pace = true` 并把 `spec_bins` 砍到 ≤512。用户已拍板「921600 + 放开限速」。

改法（不新增抽象）：钳位值由线路速率决定，而不是写死 512。`uart_link.c` 导出一个 `uart_link_line_rate()`，`commands.c:414` 那行按它换算 `spec_bins` 上限与 pace 步进；**@115200 时换算结果与今天的 512 一致**，这样旧行为是参数化而非改动，回归不破。

Wi-Fi 侧不是瓶颈：局域网单条 WS，本地压测过 1134 帧/s（S9-4）。

## 4. 改动清单

### 固件（新分支 `fw-wifi-relay`，新 env `[env:esp32s3-relay]`）

| 项 | 文件 | 性质 |
|---|---|---|
| WS 客户端 | **新** `firmware/src/net/ws_link.c/h` | `esp_websocket_client` + 静态发送环（16 KiB）+ drop-oldest 计数 + 指数退避（上限 30 s） |
| S3 的 UART | `uart_link.c:61-76` 空桩→真实现 | RX ring 4096 / TX ring 2048；`gpio_pullup_en` 沿用 C5 分支那条防浮空理由 |
| 第三个 sink | `link.h:12` enum 加 `LINK_WS`；`link.c:36` frame_sink 三态 | 纯加项 |
| 命令面 | `commands.c` 新增 `NET SSID/PASS/URL/ON/OFF/STATUS/SAVE/RESET` | 沿用 `CONFIG SAVE/RESET` 的 NVS 约定（`commands.c:371`/`:380`）；**必须同时进 `exact[]` 白名单**（`:246-249`），否则命令根本不被识别 |
| 第三个读任务 | `commands.c:756-758` 的同形写法：`if (link_ready(LINK_WS)) xTaskCreate…` | `ports` 是 `calloc(LINK_COUNT,…)`（`:741`，注释说明 C5 的 SRAM 很紧）⇒ `LINK_WS` 让 C5 也多一个槽位，用 `#if RX_TARGET_S3` 把它挡在扫描 env 外 |
| C5 侧 | 波特率 115200→921600 + §3 的 pace 换算 | 其余零改动；RAWIQ 只走 USB 这条已有**执行点**（`link.c:100` 的 `if (frame_sink == LINK_UART && type != FRAME_RAWIQ)`），不只是 `link.h:42` 的注释 |
| 构建 | `[env:esp32s3-relay]` = esp32s3 超集 + `-DRX_WIFI_RELAY=1`；`net/**` 与 Wi-Fi 组件只进它；每 env 的 sdkconfig 覆盖放 `sdkconfig.defaults.esp32s3-relay`（`sdkconfig.*` 本身被 .gitignore 忽略） | 「原功能不受损」的**可判定据不能写成逐字节相同**：`CMakeLists.txt:12-24` 把 `git describe` 与 HEAD 提交时间灌进镜像（每次提交都变），而 Task 5 改的正是 `link.c` 里 USB/UART 共用的那段。改用下面 L6 的三条 |

红线：未配置凭据（`NET` 关）时，中继 env 的 S3 除"不连网"外不应有任何行为差异；扫描 env 完全不编译 `net/**`。

### 平台（`rf-track` 侧，与固件分支分开验收）

- `/api/health` 增加**按端点**的 `{frames, crc, resync, last_frame_mono, skipped}` 快照。`capture.py:581-608` 已经在 `(gateway_id, ep)` 维度各自累积，只是只汇总出来。这同时补上 2026-11-01 那笔 99,465 crc / 13.28 MB resync 归因不清的观测缺口——没有按端点分解，L2 的「crc=0」证明不了是哪条流干净。
- `PUT /api/nodes/{id}`：操作员显式写 `capabilities_json`/`fw_version`/`protocol_version`（列已在 `models.py:212-214`）。M2 收尾项。

### 桥（`web/serve.mjs`）

**0 改动**。`/node?id=` 端点、`node:<id>` 枚举（`listEndpoints()` 的 `kind:"remote"`）、`busy/replaced/node offline` 三条关闭理由都已在且 ③ 路实测过。banner 在 `:534`。

## 5. 里程碑

- **M1（不需要飞线，也不需要 C5 供数）**：中继 env 的 S3 **不扫频**。原因是一块芯片只有一只 PHY：`main.c:92-99` 那条 esp-sdr 路径把 2.4 GHz 的射频直接拿去接收，而 STA 关联必须停在 AP 的信道上收 beacon/发包——同一只射频两个主人，同时做等于两件都做坏（S3 的扫描带 2,410–2,473 MHz 与 2.4G Wi-Fi 完全重叠）。M1 只证**上行管道本身**：关联 AP → 拨通 `ws://<接入机>:8011/node?id=<name>` → 页面经平台发 `INFO`，S3 的 ASCII 应答沿同一条 WS 回来，平台侧该节点显示 active。**不造任何假扫描帧**——伪造数据是红线，且 `NET STATUS`/`INFO` 这类文本行本身就够证明字节是双向的。
- **M2（用户要的那句话）**：S3 作中继宿主 + C5 作扫频头 + 921600 + 拆 pace。C5 的全部扫频帧经 S3 的 Wi-Fi 进平台并上屏。
- **M3（可选，后置）**：C5 独立第二 UART ⇒ S3 上报与 Pi RH 计圈并存（采集件 §九 的原始形态）。

M1 是 M2 的严格子集，且能在 C5 缺席时独立取得真机证据——这是切它的唯一理由，不是为了多做功能。

## 6. 测试与验收（六档口径，数字都要真跑）

| 层 | 怎么做 | 门槛 | 能标到哪档 |
|---|---|---|---|
| L0 回归 | 后端 pytest · 工作台 `node --test` · 浏览器冒烟 · `cd web && npm test`（动桥时）· `pio test -e native` | 587 passed/1 skipped · 214 · 65 · 56 · native 全绿；**不许用 skip 换绿** | 已通过软件测试 |
| L1 假中继 | Node 脚本按 `node-ws` 接 `/node?id=`，把真板录制的 C5 字节段按 37 KiB/s 分块喂进去 | 帧数>0、`crc_errors=0`、`resync_bytes=0`、`skipped_frames` 无新增类型 | **仅完成模拟测试**（写明：代码路径证据，不是无线电证据） |
| M1 上行回环 | 中继 S3 连 AP 并拨通 `/node?id=`；页面经 `/ws/bridge/node:<id>` 发 `INFO` 与 `NET STATUS` | ≤500 ms 收到 ASCII 应答、应答走**同一条** WS；平台侧该节点 `active`；期间无假扫描帧入库 | 已通过真实硬件测试 |
| L2 飞线静置 | C5→S3 接好，`PROTOCOL MONITOR` 跑满 30 min，期间不碰串口、不发命令 | `crc_errors=0` **且** `resync_bytes=0`；固件 `link_drops()=0`；实测 ≥30 KiB/s（对照 ① 的 37.0 基线） | 已通过真实硬件测试 |
| L3 断网重连 | 路由器断电 60 s 再回 | ≤30 s 内自动重连；平台侧 `offline`→`active` 回弹**一次且只一次**；重连期不出现假活（在列表但 0 帧） | 已通过真实硬件测试 |
| L4 命令回环 | 页面经 WS 发 `PROTOCOL MONITOR` / `GAIN` / `SCAN START` | 每条 ≤500 ms 收到 ASCII 应答；应答落回**同一条** WS；不出现 `ERR uart_rh` | 已通过真实硬件测试 |
| L5 帧对拍 | 同一块 C5 分别走 ①USB 与 ③Wi-Fi 各 60 s，两份字节过同一个 `frame_parser.decode` | 帧类型分布、档位序号、`rssi/snr` 取值集合、`channels` 数逐字段相同；只允许时间戳不同（都以接入机接收时刻打）；RAWIQ 只应出现在 USB 那一份 | 已通过真实硬件测试 |
| L6 回退 | 重烧 `[env:esp32s3]`（扫描 env）产物，按 ① USB 的已记录口径跑 protocol MONITOR 60 s ×3；`esp32c5` 至少编译通过 + 体积对照 | **原来写的「与该 env 今天的二进制逐字节相同」在设计上不成立**，两条独立理由：①`firmware/CMakeLists.txt:12-24` 把 `git describe --dirty` 与 HEAD 提交时间灌进镜像，任何新提交都会变；②Task 5 参数化的正是 `link.c` 里 USB/UART 共用路径。改成三条真判据：(a) `esp32s3`/`esp32c5` 都 SUCCESS；(b) `firmware.bin` 体积差逐项可解释（参数化 + 一个端口槽 + enum 值），不设"必须为 0"；(c) 真机回到已记录基线：`crc_errors`/`resync_bytes`/`dropped_chunks`/`stalls` 恒 0，S3 吞吐 52.1 KB/s ±10%，且 `CONFIG RESET` 后无 Wi-Fi 残留态。(c) 没跑就只能标「已通过软件测试」并写明真机回归**未执行** | 已通过真实硬件测试 |

## 7. 分支、红线与脱敏

- 新分支 `fw-wifi-relay`（从 `rf-track` 切）。`main`/`rf-track` 不动；`rf-track` 永不合入 `main`，只许 `main`→`rf-track` 单向。
- 编译期隔离优先于运行时开关（理由见 §4 表末与「替代硬件类特性必须单开分支」这条既有约束）。
- 依赖零新增：**不**引入 `esp_websocket_client`（它把 mbedTLS 拖进来，且会动 esp-sdr 钉死的依赖集）。`ws://` 的握手与成帧自带在 `src/net/ws_codec.c`（SHA-1 + base64 + 帧头），代价是 ~120 行自己维护的密码学胶水，好处是成帧层能在 `pio test -e native` 上跑真向量、且不用在干净环境重验一套新钉版依赖。
- 既有红线原样继承：队列有界 + drop-oldest 计数；插件崩了不拖垮 capture；离散信道 RSSI 不伪装成连续频谱；模拟数据标 MOCK/DEMO；未声明即 `unsupported`。
- 推送前逐对象脱敏扫描：`git rev-list --objects fork/<branch>..HEAD` 逐 blob，正则**须含本机真实 Wi-Fi 网段**（那个地址只存在于本机与项目记忆里，本文档与计划里都不写出其字面值，扫描规则本身也不许把它当例子写进来）。本仓库口径：`rftrack/**` 一律 `192.168.99.*`、`docs/**` 一律 `203.0.113.x`；接入机的真实地址只许留在 /tmp 与本地库里。NVS 默认值与测试夹具不得出现真实 SSID/密码。`origin`（CapnBry/widscan58）永不推；只推 `fork`。
- 烧 S3 前确认串口空闲、且用户没在网页端占着板子；S3 原固件（QiYun-LapTimer-Node）4 MB 整片备份仍在 `/tmp/w58-probe/s3-full-4MB-backup.bin`；S3 bootloader 偏移 0x0、C5 是 0x2000，别混。

## 8. 开放项（实现前要有答案）

1. **S3 是哪块板 / 排针可用情况**：决定我把它接到 C5 的哪对 GPIO，也决定接线表怎么写。目前一手证据只有 esptool 报的 `ESP32-S3 (QFN56) revision v0.2` + 4 MB flash。
2. **AP 凭据给法**：建议在板子自己的串口上敲 `NET SSID=` / `NET PASS=`，经 NVS 保存，不进聊天、不进仓库、不进测试夹具。
3. 能力声明：确认按 §1 末的口径（M1/M2 如实 `unsupported`，M2 收尾补 `PUT /api/nodes/{id}`）。
4. C5 是否要重新烧一版含 921600 的固件：现在 C5 报的是 `Web-v0.10` 版本串，需确认它跑的到底是不是带 F1 UART 链路的那份（`edb400d6` 那条），别默认。
