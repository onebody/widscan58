# Web 回归测试与回滚方案（web-regression-test-plan）

> RF Track 扩展前置审计 · 2026-10-27 · 基于 commit `01df9b6`
> 置信度标注：**[已确认]** / **[推断]** / **[实机]** / **[无法确认]**

## 1. 现有测试资产（基线）

| 资产 | 命令 | 覆盖 | 本次审计实测 |
|---|---|---|---|
| Web 单测（Vitest） | `cd web && npm test` | 解析器 10 用例、协议/模拟器/计时/功率/GPIO/频道计划/i18n 等 10 文件 56 用例 | **56/56 通过，427 ms**（2026-10-27）[已确认] |
| 类型检查 | `npx tsc --noEmit`（含在 build 里） | 全量类型 | **0 错误** [已确认] |
| Headless e2e | `node e2e/headless.mjs --sim`（模拟器）/ 配 `serial_bridge.py` 打真机 | 双模式切换、REBOOT 恢复、截图+results.json | 未在本次审计运行 [无法确认] |
| 固件 host 测试 | `pio test -e native` | FFT vs DFT、metrics、lap 检测 | 未运行（本机 PIO 环境问题，项目记忆）[无法确认] |
| RH 兼容 | `tools/rh_check.py` | 发现/座位/轮询/通过/误码 | 未运行 [无法确认] |
| 真机回放 | `?bridge=` + 录制的 `.widscan58` | 任意场景复放 | 机制 [已确认] |

## 2. 测试矩阵（新增 + 保留）

### 2.1 原 USB 单节点回归（每里程碑必跑，门槛=全绿）

1. `npm test` 全量 + `tsc --noEmit` —— 解析器/CRC/重同步用例一个不改 [已确认现状]。
2. `e2e/headless.mjs --sim` —— 模拟器走完整协议栈 [已确认现状]。
3. **真机冒烟（M1 起）**：真 C5 + `serial_bridge.py` + `e2e/headless.mjs`（无 `--sim`），对照 M0 基线 results.json 与截图 [实机]。
4. **逐帧等价性**：M0 基线 `.widscan58` 在改造前后各回放一次，diff `Store` 导出（`window.widscan58` 调试钩子已存在，`main.ts:447` [已确认]）——元数据帧逐字节一致、metrics 序列数值一致。
5. 命令面回归：Connect → 自动 `INFO/PROFILE?/STATUS?/SPURS?`（`main.ts:378` [已确认]）；Channels Edit → `CHANNELS …`；断开 → `PROTOCOL RH` 静默（`main.ts:462` [已确认]）。
6. 回放/录制：改造后仍能录制并回放自己的文件（ReplayTransport 未动的证据）。
7. PWA：`npx vite preview` 验证 SW 注册与离线加载（CLAUDE.md 明示 dev server 不注册 SW [已确认]）。

### 2.2 频道数量扩展测试

- 单测：`normalizePlan` 超额截断、40 频点计划 → `CHANNELS` 命令串、编辑器满额提示。
- 协议级（模拟器注入 40 通道 profile）：METRICS 帧 40 记录解析、`ENABLE` 掩码越界行为（>32 位时必须走新命令，见 channel-limit-audit §L2）。
- **实机**：40 通道计划烧板 → `TIMING?`/遥测读 hop 时长与 `drops`；每通道更新率 ≥ 目标值（目标值由 M3 前置实验定）[实机]。

### 2.3 多节点样本隔离

- 单测：两个 NodeHandle 收到同 `channel id` 流互不串写；节点 A 断线不清空节点 B [新增，M1/M2]。
- 网关注入测试：WS 流乱序（node 交错）、重复节点连接、单节点断流 3 s（对齐 `main.ts:590-599` 的静默判路语义）后标记离线而不崩溃。
- **实机**：两块 C5（或一真一模拟）经网关，各配不同频点，验证 UI 标签/颜色/历史图归属正确 [实机]。

### 2.4 数据完整性（乱序/重复/断线/补传）

- 解析层已有：CRC 错帧、半帧重同步（`parser.test.ts`）[已确认]。
- 新增单测：RAWIQ 丢块弃整帧（`RawIqAssembler` 已有逻辑 [已确认]，补多节点下用例）；网关消息乱序重排窗口；**补传语义 = 无**（固件丢弃不重发，`link.c` drops 计数 [已确认]）→ 测试断言 UI 显示丢帧计数（telemetry `drops`）而非静默补齐。
- 断线重连：板子 re-enumerate 自动重连（`main.ts:556-578`）回归；网关 WS 断开 → 全节点标记离线 → 重连后 `syncDevice` 重放。

### 2.5 Web 构建与性能

- `npm run build`（含 tsc）通过；bundle 尺寸对比 M0（阈值 +10% 告警）[推断阈值]。
- 性能探针：e2e 增加 40 通道模拟下 ChannelGrid 单帧渲染耗时与主线程长任务统计；水线：帧渲染 < 16 ms（现有 minMs 节流 100 ms 起 [已确认]）[推断阈值，按实测校准]。
- 内存：40 通道 × 2 节点的 Series 容量复核（≈ 2×38 MB 上限 [推断]），超限则降采样并在 CHANGELOG 记录。

### 2.6 需要实机验证的清单（汇总）

1. M0 基线录制与真机 e2e。
2. 40 通道 hop 时长 / 更新率 / 丢帧率（决定 M3 走向）。
3. `PROFILE_MAX_CHANNELS` 扩容后的静态 RAM 链接（dump bank 红线，CLAUDE.md）[实机]。
4. 双节点时轴对齐误差（网关接收时间 vs 设备 t_ms）。
5. S3 桥接器 → Pi 的实际数据格式（本次审计范围外 [无法确认]）。
6. 固件 host 测试补跑（有环境后）。

## 3. 回滚方案

| 变更层 | 触发条件 | 回滚动作 |
|---|---|---|
| Web 代码 | 任一回归门槛不达标 | milestone git tag 恢复；`rf-track` 分支 rebase 丢弃坏提交；Pages 站点重跑旧 ref 的 workflow |
| 固件 | 实机异常（更新率/RAM/丢帧） | 网页刷写器选回 `official`（upstream/main）或 `rh-uart`（origin/main）——该机制已内建 [已确认]；`rf-track-ch40` 固件版本条目从 `versions.json` 摘除即可让主站用户不可见 |
| 协议 | v2 帧不被旧 UI 识别 | 帧头 `version` 字节（`frames.h:14` [已确认]）保证旧解析器拒识并提示，不产生脏数据；新命令全部"纯新增"，旧固件对未知命令回 `ERR`（`commands.c` 既有行为 [已确认]） |
| 数据格式 | 网关消息格式变更 | WS 消息 `v` 字段协商；旧客户端收到不认识的 `v` 断开并提示升级，不静默错解 |
| 网关 | Pi 侧故障 | 节点 C5 本身行为不变（固件不依赖网关）；用户直插 USB 即回到今天的工作方式——这是"固件零依赖网关"设计动机 [已确认链路独立性] |

## 4. 执行纪律

- 每个里程碑合并前：§2.1 全绿 + 该里程碑专属用例全绿 + 基线回放逐帧 diff 通过。
- 真机测试前确认无人占用串口（网页端/bridge/CLI 互斥 [已确认] CLAUDE.md）。
- 测试不达标**不得**用 `--no-verify` 或跳过钩子绕过；按仓库惯例修复后新提交。
