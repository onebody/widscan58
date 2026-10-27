# M0 基线冻结记录（m0-baseline）

> RF Track 阶段 4 · M0 · 2026-10-28 · 分支 `rf-track`（自 main@`01df9b6`）

## 测试基线（全部实测）

| 项 | 命令 | 结果 |
|---|---|---|
| Web 单测 | `cd web && npm test` | **56/56 通过**（397 ms，2026-10-28 03:13） |
| 类型检查 | `npx tsc --noEmit` | **0 错误** |
| Headless e2e（模拟器） | `node e2e/headless.mjs --sim` | **28/28 通过**（修复脚手架后，见下） |
| 固件 host 测试 | `cd firmware && pio test -e native` | **17/17 通过**（test_dsp：FFT/metrics/lap/plan/profile，2.2 s） |

e2e 关键读数（模拟器 c5-race）：telemetry rotationHz≈50、hopUsAvg≈4700、drops=0、crcErrors=0；METRICS SNR/RSSI 切换、REBOOT 恢复、RAWIQ 16380 上限、390px 无横向滚动均 PASS。

## 偏差与修复记录

1. **e2e 脚手架 macOS 修复（唯一代码改动）**：`web/e2e/headless.mjs` 的 "EnterAt edit reaches the seat" 检查在 macOS headless Chrome 下失败（27/28）。根因不是快捷键平台差异，而是 `type=number` 输入框对键盘快捷键选中和三击全选都不生效，追加输入变成 "125"+"201"→钳位 254。改用 `el.select()`（number 输入框支持，调试脚本实测 201 正确写入座位）后 28/28。未触碰任何业务代码。
2. **基线工件不入库**：计划原写"基线截图/录影入库"，但仓库惯例是 `web/e2e-out/` 与 `*.widscan58` 均在 `.gitignore`（首次 e2e 运行时确认）。遵循惯例：数字记录于本文档，工件留在本地 `web/e2e-out/`。
3. **真机 60 s 基线录制未完成**：需要插入 C5 实机。当前基线中的"真机"项由固件 host 测试 + 模拟器 e2e 覆盖；真机录制待硬件到位后补做（用 `python3 tools/serial_bridge.py` + Record 按钮，或 CLI 录制），补做前 **M1 只能以模拟器回放作逐帧对照**。

## M0 出口判定

除"真机录制"一项待硬件外，M0 出口判据满足：四套测试全绿，基线数字已在案。业务代码零改动（唯一 diff 是 e2e 脚手架的选区修复）。

## M1 开始前的注意事项

- 渲染循环、`Store`、`App` 的现状快照即本基线；M1 每步改动后须复跑上表前四项。
- 真机录制补齐前，若 M1 改动触及 `store.ts` 的数据路径，用模拟器 `?sim` 与 `mockDevice` 单测做逐帧对照，并在 M0 补录后追加一次真机回放 diff。
