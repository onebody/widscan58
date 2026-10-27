# RF Track 扩展边界与兼容方案（web-extension-boundaries）

> RF Track 扩展前置审计 · 2026-10-27 · 基于 commit `01df9b6`
> 置信度标注：**[已确认]** / **[推断]** / **[实机]** / **[无法确认]**

## 1. 设计前提（来自现有代码的硬约束）

1. Web UI 无框架、无服务端；所有设备交互经由浏览器（Web Serial / WebUSB / WebSocket 桥）[已确认]。
2. 浏览器同一时间只能持有一个串口端口；`App.device` 是单例（`web/src/model/app.ts:34`）[已确认]。**这意味着"多个 C5 节点"不可能靠浏览器多开 Web Serial 实现，必须引入聚合层。**
3. `Store` 以帧内 `channel id`（u8，profile 计划索引）为通道主键（`store.ts:223-226`）[已确认]；`nodeId` 维度不存在。
4. 仓库已有多传输抽象 `Transport`（`device.ts:7-12`：WebSerial / WebSocket bridge / Mock / Replay），且 `?bridge=ws://…` 已是可用入口（`main.ts:571-573`）[已确认]。**这是天然的网关数据接入点。**
5. `tools/serial_bridge.py`（Serial↔WebSocket）已存在并已被 UI 消费 [已确认] —— 树莓派网关可沿用同一 WebSocket 形态，无需新发明协议。

## 2. 分层边界：哪些保持不变、哪些扩展、哪些新增

### 2.1 禁止修改（原 USB 功能保护面）

| 文件/模块 | 理由 |
|---|---|
| `web/src/serial/parser.ts` | 帧同步/CRC/重同步是链路正确性核心，有 10 个单测覆盖 |
| `web/src/proto/frames.ts` | 与固件 `frames.h` 逐字节镜像（`_Static_assert` 锁布局），任何改动=协议版本升级 |
| `web/src/serial/device.ts` 的 `Device`/`WebSerialTransport` | 单节点 USB 链路本体；`replyMatcher` 命令队列语义脆弱 |
| `web/src/serial/recorder.ts`、`serial/flasher.ts`、`serial/webusb.ts` | 录制/刷机是独立子系统，扩展不触碰 |
| `web/src/mock/*` | 模拟器 = 回归测试基线，只允许随协议版本同步更新 |
| `firmware/src/proto/frames.h`、`link.c`、`scanner.c` 的帧发射点 | 原 USB 数据面保持逐字节不变 |
| 固件 radio/DSP 全部（`radio/`、`dsp/`） | 任务明令禁止 |

### 2.2 允许最小扩展的现有文件

| 文件 | 扩展内容 |
|---|---|
| `web/src/model/store.ts` | 从"单 Store"演进为"每节点一个 Store 实例 + 节点注册表"；`ingest()` 逻辑不动，外面包一层 source 归属 |
| `web/src/model/channelPlan.ts` | `MAX_CHANNELS` 常量来源改为可配置（8 → 40），`BANDS` 保持 |
| `web/src/views/channelGrid.ts` | 卡片按节点分组/虚拟化；`1 << id` 掩码逻辑改为节点内掩码 |
| `web/src/util/canvas.ts:34` | 调色板扩到 ≥40（程序化生成 HSL 而非 8 个 CSS 变量） |
| `web/src/main.ts` | 连接入口增加"网关"transport 选择；渲染循环遍历各节点 store |
| `web/src/views/panorama.ts` / `history.ts` | 多节点叠加显示（每节点一线/一层） |

### 2.3 新增（不搬迁旧代码）

```
web/src/model/nodes.ts          # 节点注册表：nodeId → { name, transport, store, lastSeen }
web/src/model/track/            # 多节点分析：活动关联、疑似干扰、候选轨迹（纯函数 + 单测）
web/src/gateway/gatewayDevice.ts# GatewayTransport：WebSocket JSON 事件流 → 复用 StreamItem 语义
tools/rf_gateway.py             # 树莓派网关：S3 桥接器串口 + 若干 USB 节点 → WS 广播
```

- 网关协议建议：**每条消息携带 `node` 字段，载荷复用现有帧/文本格式**（即 WS 帧内 = `nodeId` + 原始字节流分片或已解析 JSON）。这样 `StreamParser`、`frames.ts` 全部复用，网关不做 DSP [推断，待网关设计定稿]。
- `GatewayTransport` 实现 `Transport` 接口后，`Device`、`Store.ingest`、全部视图零改动即可吃多节点流 [已确认接口形状]。

## 3. 统一内部数据模型

现状：`Store` 既是 USB 解析产物也是唯一状态。建议**不重写**，而是：

```
TrackModel（新增，唯一新状态根）
├─ nodes: Map<nodeId, NodeHandle>
│         NodeHandle { id, label, kind: "usb"|"gateway", store: Store, status }
├─ unifiedPlan: 统一频点表（40+ 频点，频点 → 各节点订阅关系）
└─ analysis: 多节点活动/干扰/轨迹（只读消费各 NodeHandle.store 的 Series）
```

- `Store` 保持每节点一个实例：`channel id` 冲突自然消除，`HISTORY_SECONDS` 容量按节点分摊 [已确认可行性，见 store.ts 结构]。
- USB 直连节点 = `nodeId: "local"`，走现有 `connect()` 路径，一行不改 [已确认]。
- 时钟对齐：固件帧的 `t_ms` 是设备 uptime（`frames.ts:5`），跨节点不可比；`Store.now()` 用 `performance.now()`（`store.ts:130-132`）。统一模型必须以**网关/浏览器接收时刻**为多节点公共时间轴，`t_ms` 仅作节点内时序 [已确认源码语义；对齐精度需实机验证]。

## 4. 是否需要新增后端/本地 API —— 需要

理由（全部源于已确认事实）：

1. 浏览器串口互斥：两个 USB 节点无法同时接进一个页面 [已确认]。
2. S3 无线桥接器 → 树莓派的链路必须由 Pi 上的常驻进程终结 [已确认拓扑，任务书给定]。
3. 云端集中管理是后续目标，Pi 网关天然是"本地 API + 可选云上行"的分层点；现仓库无任何服务端可复用 [已确认]。

形态建议：Pi 上 `tools/rf_gateway.py`（扩展 `serial_bridge.py` 的模式）提供 WebSocket（节点流）+ REST（节点管理/计划下发）。Web UI 通过 `?gateway=ws://pi/` 接入；无网关时一切照旧。**不在浏览器直连云端**（现有 https 页连局域网 http 设备已被浏览器策略阻断，见项目记忆/web README），云端只与网关对话 [推断]。

## 5. 兼容性红线（验收标准）

1. 拔掉网关、只插一个 C5：UI 行为与今天逐帧一致（同 view、同命令、同回放文件）。
2. `npm test` 56 个既有用例零改动通过；`e2e/headless.mjs --sim` 通过。
3. 固件二进制不变（阶段 4 的 Web-only 里程碑），或固件仅做**纯新增**改动（新增命令/帧类型，不改既有帧布局——沿用"替代硬件类特性单独分支、原代码只加不改"的项目惯例）。
4. 回放 `.widscan58` 文件仍可在新 UI 打开（ReplayTransport 路径未动）[已确认可达]。
