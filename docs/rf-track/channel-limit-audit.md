# 8 频道限制逐层审计（channel-limit-audit）

> RF Track 扩展前置审计 · 2026-10-27 · 基于 commit `01df9b6`
> 置信度标注：**[已确认]** / **[推断]** / **[实机]** / **[无法确认]**

结论先行：**8 的硬限制只在两处固件配置常量与 Web 的模型/UI 常量中；线路协议本身（帧格式）按 u8 计数，天然支持到 255 通道；`ENABLE` 位掩码是 u32，是协议层隐藏的 32 通道上限**。

## L1 固件扫描配置

| 位置 | 内容 | 置信度 |
|---|---|---|
| `firmware/src/scan/profile.h:8` | `#define PROFILE_MAX_CHANNELS 8` —— 唯一根常量 | [已确认] |
| `firmware/src/scan/profile.h:42` | `profile_t.ch[PROFILE_MAX_CHANNELS]`：默认计划数组 8 | [已确认] |
| `firmware/src/scan/scanner.h:13` | `scan_config_t.ch[PROFILE_MAX_CHANNELS]`：`CHANNELS` 命令写入的运行时计划 8 | [已确认] |
| `firmware/src/scan/planner.h:6,19` | `PLAN_MAX_PER_LO = PROFILE_MAX_CHANNELS`；`plan_t.hop[PROFILE_MAX_CHANNELS]`：LO 计划每跳最多 8 通道、每旋转最多 8 跳 | [已确认] |
| `firmware/src/proto/rh.h:18` | `RH_SEATS PROFILE_MAX_CHANNELS`：计时模式 8 座位 | [已确认] |
| `firmware/src/scan/scanner.c` | 逐跳度量按 `hop->nch` 遍历，`out` 缓冲按计划大小分配 | [已确认]（引用点 `scanner.c:343/479`） |
| 调谐范围 | C5 5345–5945 MHz（`profile.c` 的 `tune_lo/hi_khz`），6 个波段 ×8 = 48 个可命名频点 | [已确认] CLAUDE.md + `web/src/model/channelPlan.ts:11-18` |

**未发现** 固件 radio/PHY 层有独立的"8"限制；radio 层限制是调谐范围与 LO 数量（GROUP 模式 40 MHz 跨度合并 LO）[已确认] `planner.h:22-25`、CLAUDE.md。

## L2 设备输出协议（帧格式）

| 位置 | 内容 | 置信度 |
|---|---|---|
| `firmware/src/proto/frames.h:46/107` | `frame_metrics_t.nch` / `frame_levels_t.nch` 均为 **u8**，通道记录按 `nch` 重复 | [已确认] |
| `frames.h:52/113` | 通道 `id` 为 **u8**（profile 通道索引） | [已确认] |
| `web/src/proto/frames.ts:118/167` | Web 解析按 `nch` 循环读，**无 8 的检查** | [已确认] |
| `commands.c:407-410` | `ENABLE` 命令：`c.enabled = a & ((1u << c.nch) - 1)`，掩码 **u32**。nch≤8 时无感，但若 nch>32，`1u << c.nch` 未定义且掩码无法表达第 33+ 位 —— **协议层隐含 32 通道上限** | [已确认] |
| `web/src/views/channelGrid.ts:87-88` | 前端同样用 `1 << c.id` 计算 ENABLE 掩码（JS 32 位位运算） | [已确认] |

即：把 `PROFILE_MAX_CHANNELS` 改成 40 后，METRICS/LEVELS 帧**格式不变、自动携带 40 条记录**；但 `ENABLE` 位掩码需改协议（如 v2 改为按字节展开的掩码）或按节点分组各持 8 位。

## L3 Web 数据解析

`web/src/proto/frames.ts` 与 `web/src/model/store.ts`：**无任何 8 限制** [已确认]。

- `parseMetrics`/`parseLevels` 按 `nch` 读取（`frames.ts:118-137/167-187`）。
- `Store.channels` 数组长度由 `PROFILE?.channels.length` 决定（`store.ts:144`），帧内 `id` 越界则丢弃该通道记录（`store.ts:224-225` `if (!st) continue`）。

## L4 前端数据模型

| 位置 | 内容 | 置信度 |
|---|---|---|
| `web/src/model/channelPlan.ts:21` | `export const MAX_CHANNELS = 8`（注释明言"固件最多扫这么多"） | [已确认] |
| `channelPlan.ts:34` | `normalizePlan`：`slice(0, MAX_CHANNELS)` 截断到 8 | [已确认] |
| `channelPlan.ts:51` | 默认计划 R1–R8 | [已确认] |
| `web/src/model/store.ts:143` | 历史序列容量 `HISTORY_SECONDS * 250` 按每通道分配（120 s × 250/s ≈ 30 K 点/通道）—— 40 通道 ≈ 4.8 M 浮点，约 38 MB，需要复核 | [已确认]（容量公式）；内存影响 [推断] |

## L5 UI 布局与固定数组

| 位置 | 内容 | 置信度 |
|---|---|---|
| `web/src/views/channelPlanDialog.ts:107/188/207` | 频道编辑器：`selected.size < MAX_CHANNELS` 才允许追加；满额提示 "The plan is full" | [已确认] |
| `web/src/util/canvas.ts:34` | 通道颜色 `series: [1..8].map(v('--s'+j))` —— **固定 8 色**，第 9+ 通道回落 `muted` 灰 | [已确认] `canvas.ts:48-51` |
| `web/src/styles.css` | `--s1..--s8` 八个 CSS 变量 | [已确认]（grep 命中） |
| `web/src/views/channelGrid.ts:49` | "All" 按钮掩码 fallback `(1 << (profile?.channels.length ?? 8)) - 1` —— 8 只是缺省值，非硬限 | [已确认] |
| `web/src/views/channelGrid.ts:76-122` | 卡片网格由 `profile.channels` 动态生成，**无 8 硬编码**；但卡片 DOM 会随通道数线性增长（每卡一个 canvas sparkline） | [已确认]；性能影响 [推断/实机] |
| `web/src/mock/mockDevice.ts:43-46/75-80` | 模拟器两个 profile 的默认计划均 8 通道；`rhSeats` 8 座位 | [已确认] |

## L6 服务端 / 其他模块

- **无服务端** [已确认]。`tools/widscan58cli.py`、`tools/serial_bridge.py` 为透传客户端/桥，不带通道数假设 [已确认]（CLAUDE.md 描述，未逐行审计 → [推断]）。
- `tools/rh_check.py` 按 RH 协议 8 座位测试 [推断]。
- Android `app/` 是否同样硬编码 8：本次未审计 [无法确认]（项目记忆显示其卡片布局按 8 通道设计）。

## 需要实机验证的事项

1. **单 radio 时分复用下 40 频点的更新率**：8 通道 GROUP（4 LO）约 60 更新/s·通道（CLAUDE.md 实测表）。40 通道即使 GROUP 到 ~10 LO，每通道更新率将跌到 ~10–15/s（4 ms/hop 线性摊薄）[推断]，是否满足"疑似干扰分析"的时延要求必须实测。
2. 固件侧 40 通道计划下 `plan_build` 的 LO 合并效果与 hop 时长（`hop_max_span_khz=40`，40 个频点横跨 600 MHz → ≥15 LO）[推断]。
3. `METRICS FULL` 帧在 40 通道 × ~60 hops/s 下的 USB 带宽与 C5 16 KB 环形缓冲丢帧率 [实机]。

## 修改清单小结（详见 phase-4 文档）

- 必改常量/数据结构：`profile.h:8`（或新固件模式）、`channelPlan.ts:21`、`canvas.ts:34` 调色板、`channelPlanDialog.ts`。
- 必改协议（若要 >32 使能位）：`ENABLE` 掩码语义（`commands.c:407-410` + `frames.ts` 无关，仅命令行）。
- 无需改动即可承受 40 通道：`frames.ts` 解析、`store.ts` 结构（仅需扩容）、`frames.h` 帧格式。
