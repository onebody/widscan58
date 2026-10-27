# Widscan58 Web 数据链路审计（web-data-flow）

> RF Track 扩展前置审计 · 2026-10-27 · 基于 commit `01df9b6`（main，本地领先 origin/main 54 个提交）
>
> 结论置信度标注：**[已确认]** = 已由源码逐行核实；**[推断]** = 根据源码推断，未运行验证；**[实机]** = 需要实机验证；**[无法确认]** = 当前无法确认。

## 1. 实际技术栈

| 层 | 事实 | 置信度 |
|---|---|---|
| 前端框架 | **无框架**。纯 TypeScript + 手写 DOM（`web/src/util/canvas.ts` 的 `el()` 辅助），无 React/Vue/任何 UI 库 | [已确认] `web/package.json` 仅依赖 `esptool-js`、`spark-md5` |
| 构建 | Vite 8 + `tsc --noEmit`，`npm run dev`（localhost:5173）/ `npm run build` / `npm test`（Vitest） | [已确认] `web/package.json` scripts |
| 图表 | 全部为手写 `<canvas>` 2D 绘制，无图表库 | [已确认] `web/src/views/*.ts` |
| 服务端 | **不存在**。Web UI 是纯静态站点（GitHub Pages），无后端 API | [已确认] 仓库无任何 server 目录；`web/serve.mjs` 只是静态文件服务 |
| 与设备通信 | 浏览器内 Web Serial（Chrome/Edge/Firefox/Android Chrome），Android 走 WebUSB CDC 回退 | [已确认] `web/src/serial/device.ts:23-30` |
| 替代传输 | 模拟器 `MockTransport`、回放 `ReplayTransport`、桥接 `WebSocketTransport`（`tools/serial_bridge.py`） | [已确认] `web/src/serial/device.ts:139-175` |
| PWA | Service Worker 离线缓存（`web/src/sw.js`、`web/src/util/pwa.ts`） | [已确认] |
| 桌面壳 | `web-desktop/`：Electron 壳（app:// 协议），复用同一 web 构建 | [已确认] 目录存在，细节见 CLAUDE.md |
| Android app | `app/`：独立 Android 工程（Kotlin），同样直连 USB，不在本次 Web 审计范围内 | [已确认] 目录存在 |

**对扩展的含义**：没有服务端、没有全局状态库、没有虚拟 DOM。数据流是单向字节流 → 可变单例 Store → 按版本号触发的手绘渲染。扩展多节点/40 频点时，新增一个本地网关服务（或复用 `serial_bridge.py` 模式）是唯一自然的服务端引入点，而不是改造某个既有后端。

## 2. 设备侧：数据从射频到 USB

1. **扫描循环**（固件，`firmware/src/scan/scanner.c`）：按 LO 计划逐跳调谐 → 采样 → FFT → 逐通道度量。每跳结束后发送帧：
   - `FRAME_METRICS`（METRICS FULL）：[已确认] `scanner.c:479`，`link_frame(FRAME_METRICS, &h, sizeof(h), out, hop->nch * sizeof(frame_channel_t), 0)`
   - `FRAME_LEVELS`（METRICS RSSI|SNR）：[已确认] `scanner.c:343`
   - `FRAME_SPECTRUM`：[已确认] `scanner.c:255`
   - `FRAME_TELEMETRY`（约 4 Hz）：[已确认] `scanner.c:501`（`emit_telemetry`）
2. **帧封装**（`firmware/src/proto/link.c`）：帧头 `0xA5 'R' type version | u16 len | payload | crc32`（[已确认] `frames.h:8`、`link.c:85-108`）。`streaming` 标志控制帧是否允许输出（`link_set_streaming`，PROTOCOL 切换时翻转，[已确认] `commands.c:227/233`）。
3. **USB 输出是 USB Serial/JTAG 外设，不是 GPIO UART**：
   - `link_init()` 安装 `usb_serial_jtag_driver`（[已确认] `link.c:70-75`）。
   - 帧先进 16 KB（C5）no-split 环形缓冲，由 `link_task` 写 `usb_serial_jtag_write_bytes`（[已确认] `link.c:19/41-57`）；无主机时 50 ms 超时丢弃并计 `drops`。
   - GPIO UART（UART0，GP11/12）只在 RotorHazard 模式启用（`uart_link.c`），帧和遥测**永不**走 UART（CLAUDE.md 与 `link.c:37-40` 注释一致）[已确认]。

## 3. 主机侧：字节流到渲染的完整链路

```
ESP32-C5 USB Serial/JTAG
  │  (0xA5 'R' 帧 + ASCII 行，115200 波特率仅形式参数)
  ▼
WebSerialTransport.readLoop            web/src/serial/device.ts:75-107
  │  port.readable.getReader()，1 MB bufferSize
  ▼
Device.onBytes                         web/src/serial/device.ts:283-307
  ├─ rawListeners → Recorder（录制原始流）
  ├─ (RH 协议时) RhLink.push → 轮询事务
  ▼
StreamParser.push                      web/src/serial/parser.ts:36-103
  │  字节流 → StreamItem{text|frame}；CRC32 校验，错帧逐字节重同步
  ▼
listeners（main.ts connect() 注册）      web/src/main.ts:422-425
  ▼
Store.ingest                           web/src/model/store.ts:172-257
  ├─ 文本行：PROFILE/STATUS JSON、INFO、SPURS → store.profile/status/info/spursKhz
  ├─ FRAME_METRICS/LEVELS → parseMetrics/parseLevels（proto/frames.ts:101/148）→ onMetrics:225
  │    逐通道写入 ChannelState{latest, rssi/ema/floor/clipped Series}
  ├─ FRAME_SPECTRUM → onSpectrum:236 → segments Map（每 LO 一段，max-hold + 指数平均）
  ├─ FRAME_TELEMETRY → store.telemetry
  └─ FRAME_RAWIQ → RawIqAssembler 组包（frames.ts:260-284，丢块即弃整帧快照）
  ▼
Store.touch() → version++ → 通知订阅者
  ▼
渲染循环 frame()                        web/src/main.ts:602-617
  │  每个 View 记录已绘 version，变化且到达自身 minMs 节流才重绘
  ├─ ChannelGrid（频道卡片）  views/channelGrid.ts   minMs=100
  ├─ Panorama（全景频谱+瀑布）views/panorama.ts      minMs=33
  ├─ Zoom / History / Telemetry / Controls / RawIqView / Console
  └─ timer / power 页各视图（model/timer.ts、model/power.ts）
```

**命令下行链路** [已确认]：`App.send`（`model/app.ts:91-104`）→ `Device.command`（`device.ts:310-337`，串行化队列 + `replyMatcher` 匹配应答 + 3 s 超时）→ `transport.write` → USB。连接握手时 `startMode`（`main.ts:185-222`）发 `PROTOCOL?` 判断旧固件，monitor 模式下 `syncDevice`（`main.ts:375-385`）依次发 `INFO / PROFILE? / STATUS? / SPURS?`。

## 4. 协议要点（扩展时必须保持的事实）

- 帧格式 v1 在两侧硬镜像：`firmware/src/proto/frames.h` 与 `web/src/proto/frames.ts`，C 侧用 `_Static_assert` 锁死 34/26/32/4/59 字节布局（[已确认] `frames.h:118-122`）。**任何帧格式改动必须两侧同步并升 `PROTO_VERSION`。**
- 文本行解析按"大写命令词开头"过滤垃圾（[已确认] `parser.ts:96`）；帧中断行会在 sync 字节处截断（`parser.ts:83`）。
- 回放/录制复用同一原始字节流（`serial/recorder.ts`），文件头写 `PROFILE/STATUS/SPURS` 快照（[已确认] `main.ts:546-554`）；CLAUDE.md 明示录制文件**不跨固件版本兼容**。
- `STATUS.plan`（`frames.ts:307-311`）驱动全景图的 LO 覆盖范围：`bandRange`（`views/panorama.ts:21-25`）取被扫 LO 段的并集，无 spectrum 帧时回落到 `profile.band_khz`。

## 5. 关键结论（供扩展方案引用）

1. **单设备单例假设贯穿全栈**：`Store.channels` 按"计划索引"（=帧内 channel id）寻址（[已确认] `store.ts:223-226`，`this.channels[c.id]`），`App` 只持有一个 `Device`（`app.ts:34`）。多节点扩展必须在此处引入节点维度，否则两个节点同 id 通道会互相覆盖。
2. **无队列积压路径**：浏览器端解析是同步 push 模型，无背压；USB 侧丢弃发生在固件环形缓冲（telemetry `drops` 可观测）。多节点数据量增大时，丢弃点会从固件转移到浏览器主线程 [推断]。
3. **渲染节流已按视图分离**，40 频点带来的 DOM/canvas 增量主要落在 ChannelGrid 与 History，Panorama 走 spectrum 帧与通道数无关 [已确认]。
4. 模拟器 `web/src/mock/mockDevice.ts` 用与真机相同的协议对话（含 `planBuild` 复现），是扩展数据模型的首个测试双胞胎 [已确认]。
