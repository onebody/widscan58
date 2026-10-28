# 阶段 4 实施计划（phase-4-implementation-plan）

> RF Track 扩展前置审计 · 2026-10-27 · 基于 commit `01df9b6`
> 前置阅读：`web-data-flow.md`（链路）、`web-extension-boundaries.md`（边界）、`channel-limit-audit.md`（8 频道限制）
> 置信度标注：**[已确认]** / **[推断]** / **[实机]** / **[无法确认]**

> 本计划为文档产出，**未经用户审核批准前不开始任何代码修改**。

## 里程碑总览

| 里程碑 | 内容 | 固件是否改动 | 出口判据 |
|---|---|---|---|
| M0 | 基线冻结与回归 | 否 | `npm test` + `e2e --sim` 全绿，基线截图/录影入库 |
| M1 | 统一数据模型（TrackModel）+ USB 适配器内联 | 否 | 单节点行为逐帧等价，新单测覆盖 |
| M2 | 网关适配器 + 多节点 | Pi 网关新增脚本；**固件不改** | 两节点样本隔离测试通过 |
| M3 | 40+ 频点展示 | 需固件新分支（`PROFILE_MAX_CHANNELS`、ENABLE 语义） | 频道数扩展测试通过 + 实机更新率达标 |
| M4 | 多节点信号活动 / 疑似干扰 / 候选轨迹 | 否 | 分析算法单测 + 实机场景验证 |
| M5 | ~~云端集中管理~~ → **多场地/多中继本地管理**（2026-10-28 用户定调改判：本地化部署为主，**不做云端**） | 否 | 见 `RF_Track_落地实施方案与计划.md` §3/§4 的 `site`/`relay`/`gateway` |

> **M2 已被取代（2026-10-28）**：本里程碑里"新建 `tools/rf_gateway.py`"的形态建议作废。`web/serve.mjs` 已在真板提供端点枚举、`busy/replaced` 独占语义、45 s 心跳、`/node?id=` 中继与 `/api/mode` 停启 RH，新后端改为它的**下游消费者**而不是重写一层网关——详见 [`web-extension-boundaries.md`](web-extension-boundaries.md) 中的取代说明与 [`../architecture/target-stack-landing.md`](../architecture/target-stack-landing.md) §4。M2 的**出口判据（两节点样本隔离）仍然有效**，只是实现载体从"新网关脚本"变成"经 `/ws/bridge/{ep}` 接进 serve.mjs"。

每个里程碑独立提交、独立可回滚（git tag `rftrack-m<N>`；仓库为 fork 拓扑，遵循"替代类特性单独分支"惯例，`rf-track` 从 `main` 拉出）。

## M0 基线冻结（0.5 天）

1. 在干净工作树跑通并记录：`cd web && npm test`（当前 56/56 通过，tsc 0 错误，2026-10-27 实测 [已确认]）、`node e2e/headless.mjs --sim`。
2. 固件 host 测试 `pio test -e native` 本机因 PlatformIO 环境问题（项目记忆：并发 pio 死锁、Python 版本限制）**本次审计未运行** [无法确认]；M0 由具备环境的人补跑一次并记录。
3. 用真机 + `serial_bridge.py` 录 60 s `.widscan58` 基线回放文件，作为后续每次改动的逐帧对照 [实机]。

## M1 统一数据模型（2–3 天，Web-only）

文件级顺序（每步后跑 `npm test` + `tsc --noEmit`）：

1. **新增** `web/src/model/nodes.ts`：`NodeHandle{id,label,kind,store,lastSeen}` + `TrackModel` 注册表。只新增，不改任何现有文件。
2. **改动** `web/src/main.ts`：`connect()` 成功后把 `(device, store)` 包装注册为 `nodeId="local"` 的 NodeHandle。渲染循环 `frame()` 改为遍历节点（`main.ts:602-617`，单节点时行为不变）。
3. **改动** `web/src/model/app.ts`：`device` 单例保留（兼容），新增只读 `track` 引用；`View` 接口不变。
4. **改动** `web/src/model/store.ts`：仅把 `channels` 容量/`planKey` 按节点实例化（已是实例字段，预计接近零改动 [已确认]）。
5. **新增** `web/src/model/nodes.test.ts`：双 Store 同 id 通道隔离、节点失联标记。

禁止：改 `parser.ts` / `frames.ts` / `device.ts` / `recorder.ts`。

## M2 网关适配器与多节点（3–5 天）

1. **新增** `tools/rf_gateway.py`：Pi 侧，N×(串口/S3 桥) → WebSocket 广播；消息 = `{node, kind:"bytes"|"event", payload}`。复用 `serial_bridge.py` 的串口处理代码模式（复制而非 import，保持 tools 独立可部署 [推断]）。
2. **新增** `web/src/gateway/gatewayDevice.ts`：实现 `Transport` 接口（`device.ts:7-12`），把 `{node,bytes}` 分发给对应节点的 `StreamParser`。入口 `?gateway=ws://…`（仿 `?bridge=`，`main.ts:571-573`）。
3. **改动** `web/src/main.ts`：网关连接 → 一次创建 N 个 NodeHandle；节点上下线事件 → Console 日志。
4. **新增** `web/src/views/nodesBar.ts`（可选）：节点在线状态条。
5. 测试：`gatewayDevice.test.ts`（乱序/重复/断线重连/节点混流）；手工用两个 MockTransport 级联模拟双节点。

固件零改动：节点固件即今天的固件；S3 桥接器侧协议属另一硬件分支，不在本阶段 [实机：S3 桥接链路未审计，当前无法确认其转发格式]。

## M3 40+ 频点（固件分支 + Web，5–8 天）

顺序（依赖 channel-limit-audit.md 的逐层清单）：

1. **实机前置实验**（不改代码）：临时把 `profile.h:8` 改 16 烧板，测更新率、hop 时长、丢帧率——决定 40 通道用 GROUP 还是分节点。**此实验在独立分支，用完即弃** [实机]。
2. 固件分支 `rf-track-ch40`：`PROFILE_MAX_CHANNELS 8→40` 连锁修改（`profile.h:42`、`scanner.h:13`、`planner.h:6,19`）；`ENABLE` 掩码改为按节点≤32 或新增 `ENABLEV2` 命令（`commands.c:407-410`）；`plan_build` 大计划验证（`planner.c`）。静态 RAM 预算红线：CLAUDE.md 记载 dump bank 下仅剩几百字节，`scan_config_t` 扩到 40 通道会 +~600 B → 必须实机链接验证 [实机]。
3. Web：`channelPlan.ts` `MAX_CHANNELS` 可配置（从 `PROFILE?` 读，不硬编码）、`canvas.ts` 程序化调色板、`channelPlanDialog.ts` 满额提示改造、`channelGrid.ts` 分组/虚拟化、`store.ts` 序列容量复核（40 通道 × 30 K 点 ≈ 38 MB [推断]，必要时降采样）。
4. `frames.ts`/`frames.h` 帧格式**不动**（nch u8 足够）[已确认]。
5. 测试：`channelPlan.test.ts` 扩展用例；e2e 用模拟器 profile 注入 40 通道；实机带宽测试。

## M4 分析层（时间待 M1–M3 经验校准）

- **新增** `web/src/model/track/`：`activity.ts`（多节点同频点活动时间线对齐）、`interference.ts`（疑似干扰：多节点同时强信号、宽带特征、nodemutex）、`candidates.ts`（轨迹候选：节点间信号移交序列）。全部纯函数 + 单测，输入 = 各 NodeHandle 的 `Series`/`segments`。
- UI：`views/trackView.ts` 新页（仿 `powerMain` 的页面挂载方式，`main.ts:153-159`）。
- 时轴对齐以网关接收时间为准（见 boundaries §3）[推断，需实测对齐误差]。

## 明确不做（本阶段）

- 不搬迁 `web/src` 既有文件到任何新目录结构；不引入框架/状态库/图表库。
- 不改 Android `app/`（其 8 通道布局是否跟进留待 Web 方案验证后 [无法确认]）。
- 不实现云端功能，M5 仅在网关 API 预留鉴权与上行钩子。

## 回滚方案（软件/固件/数据格式）

| 层 | 机制 |
|---|---|
| Web | 每里程碑 git tag；`rf-track` 分支独立，`main` 永远可用。线上 Pages 站点回滚 = Pages workflow 重跑旧 ref（改 workflow 只能走网页编辑器，项目记忆）[已确认流程] |
| 固件 | M3 固件只存在于 `rf-track-ch40` 分支与 `firmware/versions.json` 新条目；主站固件清单不加它。回滚 = 网页刷写器选回 `official`/`rh-uart` 版本（该机制已存在 [已确认] CLAUDE.md "Firmware versions"） |
| 协议 | 新帧类型/新命令采用"纯新增"，`PROTO_VERSION` 不变即不影响旧 UI；若必须改帧布局则升 `PROTO_VERSION=2`，旧 UI 按 v1 解析器拒绝并提示刷机 [已确认：version 字节存在于帧头 `frames.h:14`] |
| 数据格式 | 网关 WS 消息带 `v` 字段；`.widscan58` 录制格式不动，多节点录制用新扩展名 `.rftrack`，旧文件永远可回放 [推断] |
