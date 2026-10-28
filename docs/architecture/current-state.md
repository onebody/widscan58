# Widscan58 当前状态审计（RF Track 阶段 0）

> 审计日期：2026-10-28。基线：`rf-track` 分支 @ `015018b`（tag `rftrack-m0`）。
> 结论标注约定：**[已由源码确认]** / **[根据源码推断]** / **[需要实机验证]** / **[当前无法确认]**。
> 本文档只读记录现状；深度专题见 `docs/rf-track/` 各文档（channel-limit-audit、web-data-flow、web-extension-boundaries、phase-4-implementation-plan、web-regression-test-plan、m0-baseline）。

## 1. 仓库概况

- 项目：Widscan58 —— 5.8GHz FPV 频道扫描器 / RSSI 电平表 / 圈速计时器。**[已由源码确认]**
- 上游：`origin` = https://github.com/CapnBry/widscan58.git；`fork` = https://github.com/onebody/widscan58.git（中文定制线）。**[已由源码确认，git remote]**
- 许可证：GPL-3.0-or-later（仓库根 `LICENSE`；web `package.json` 声明一致）。**[已由源码确认]**
- 顶层目录：
  - `web/` — 浏览器前端（本审计与 RF Track 的主战场）
  - `firmware/` — ESP-IDF 固件（ESP32-C5 / S3 双 target）
  - `app/` — Android 客户端（Kotlin/Gradle，独立于 web）
  - `web-desktop/` — Electron 桌面壳（复用 web 构建，app:// 协议）
  - `tools/` — 辅助脚本（含 `serial_bridge.py` WebSocket 桥）
  - `docs/`、`images/`、`ref/` — 文档与参考资料

## 2. Web 技术栈

| 项 | 事实 | 依据 |
|---|---|---|
| 框架 | **无框架**，纯手写 TypeScript + DOM（`el()` helper），无 React/Vue | web/src 全目录 **[已由源码确认]** |
| 构建 | Vite 8 + tsc --noEmit（`npm run build` = `tsc --noEmit && vite build`） | web/package.json **[已由源码确认]** |
| 测试 | Vitest（56 个单元测试，10 个文件）；e2e 用 puppeteer-core 无头 Chrome（28 项检查） | web/package.json、web/e2e/headless.mjs **[已由源码确认，M0 实测 56/56 与 28/28 通过]** |
| 运行时依赖 | 仅 3 个：`esptool-js@0.7.0`（Apache-2.0，网页刷写）、`spark-md5@3.0.2`（WTFPL/MIT，固件校验）、`serialport@12.0.0`（MIT，仅 serve.mjs 本地服务用） | package.json + node_modules 元数据 **[已由源码确认]** |
| 状态管理 | 无状态库。`App` 单例（web/src/model/app.ts）持有 `Store`/`TimerModel`/`RhServer`；视图各自订阅、节流渲染 | **[已由源码确认]** |
| 图表 | 手绘 canvas（web/src/util/canvas.ts），无图表库 | **[已由源码确认]** |
| PWA | 有 service worker（sw.js） | **[已由源码确认]** |
| i18n | 自研字典（web/src/i18n.ts），中/英 | **[已由源码确认]** |

## 3. 固件技术栈

- ESP-IDF，PlatformIO 包装（`pio test -e native` 可跑宿主测试，17/17 通过；esp32c5/s3 构建环境在本机有已知 pio 坑）。**[已由源码确认 / 本机实测]**
- 第三方组件仅 `espressif/esp-dsp ~1.6.0`（firmware/src/idf_component.yml；FFT/PSD 用）。**[已由源码确认]**
- 原始 I/Q 采集不依赖公开组件：直接读固定 SRAM 银行 `IQ_BUFFER = 0x40830000`，16380 字（firmware/src/radio/c5/radio_c5.c:28-37），机制未见于乐鑫公开文档，属逆向所得。**[已由源码确认]**
- RF 功能：频道 RSSI 扫描、L/R 混跳计划（planner）、lap 检测（lap.c）、metrics（dB/EMA，dsp/psd.c）、RAW IQ 快照、频谱帧（SPECTRUM，真实频率轴帧，仅 USB 链路承载）。**[已由源码确认]**
- RotorHazard 兼容模式（PROTOCOL MONITOR/RH 切换；RH 走 UART，板卡默认静默）。**[已由源码确认，细节待实机复核]**

## 4. Git 与工作区状态（审计时点）

- 分支：`rf-track`（当前，@015018b，含 M0 基线与审计文档）、`main`（@01df9b6，vs origin/main 领先 54 / 落后 4）、`rh-uart-bridge`。**[已由源码确认]**
- 工作区**不干净**：5 个已修改文件（web/package.json、package-lock.json、serve.mjs、src/i18n.ts、src/main.ts）与 4 个未跟踪文档（docs/ 下 3 份 ChatGPT 采集文档 + 本实施计划）。属并行会话工作，本阶段未触碰、未提交。**[已由源码确认，git status]**
- 风险：RF Track 后续阶段开工前必须先与用户确认这批未提交修改的归属与处置（提交/暂存/忽略），避免混入产品线提交。**[根据源码推断]**

## 5. CI

- 仅 1 个 workflow：`.github/workflows/pages.yml` —— 构建 FW_ENVS=esp32c5 固件 + web UI 并发布 GitHub Pages；push main / PR / 手动触发。无单元测试/固件测试 CI。**[已由源码确认]**

## 6. 设置系统现状（§13.5 审计）

设置**全部**持久化在浏览器 localStorage，无服务端配置。共 7 个键（键名与读写点均已核实）：**[已由源码确认]**

| 键 | 内容 | 读写点 |
|---|---|---|
| `widscan58.settings` | `UiSettings`：选中频道、maxHold/average、历史指标与时间窗、dB 量程、uiMode（monitor/timer）、语音、功率计参数等 | app.ts:67（载入，含 "rh"→"timer" 迁移）、app.ts:78（每次 changed() 全量写回） |
| `widscan58.device` | 频道方案（≤8）、hop 模式、gain | channelPlan.ts:57/62/77 |
| `widscan58.timer` | 计时阈值 enter/exit、lap 规则 | timer.ts:57/80/97 |
| `widscan58.rhserver` | RH 服务器 URL 与开关 | rhServer.ts:10/145/160 |
| `widscan58.theme` | 主题名 | main.ts:50/58 |
| `widscan58.lang` | 界面语言 | i18n.ts:8/11/35 |
| `widscan58.fwUrl` | 刷写固件源覆盖 | flasher.ts:50 |

要点：
- 已有"默认值 + JSON 合并 + try/catch 容错"的朴素机制（app.ts:66-75），`Object.assign` 合并意味着旧配置多出的键保留、新键取默认。**[已由源码确认]**
- 已有一次配置迁移先例（"rh"→"timer" 重命名兼容）。**[已由源码确认]**
- **缺失**：配置版本号、Schema 校验、优先级体系、布局持久化、按组件实例隔离——RF Track 设置系统须在此之上增量建设，而不是另起炉灶。**[根据源码推断]**
- 阈值/增益等设备侧设置另有固件侧 profile 持久化（设备 flash），与 localStorage 分属两层。**[根据源码推断，固件持久化细节待复核]**

## 7. 与 RF Track 相关的关键结论

1. 单板物理上限 8 频道：`PROFILE_MAX_CHANNELS 8`（firmware/src/scan/profile.h:8），渗入 scanner.h:13、planner.h:6/19、rh.h:18；命令解析拒收第 9 频道（commands.c:315/365）。**[已由源码确认]**
2. 隐藏上限 32 频道：ENABLE 掩码为 u32（commands.c:408 `c.enabled = a & ((1u << c.nch) - 1)`；web 侧 `1 << id`），第 33 频道起静默失效——扩容改造时必须换位集/位图。**[已由源码确认]**
3. 帧协议本身不是限制者：帧头 nch/id 均为 u8。**[已由源码确认]**
4. 多节点必须经网关聚合：web 端 `App.device` 单例、Store 按频道 id 单键存储，无节点维度。**[已由源码确认]**
5. Transport 接口（web/src/serial/device.ts:7）是真实数据/网关/模拟源接入的自然适配点；`?bridge=ws://` 模式已存在（tools/serial_bridge.py）。**[已由源码确认]**
