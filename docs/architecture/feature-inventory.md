# Widscan58 功能清单（RF Track 阶段 0）

> 审计日期：2026-10-28，基线 `rf-track` @ `015018b`。标注：**[已由源码确认]** / **[根据源码推断]** / **[需要实机验证]** / **[当前无法确认]**。

## 1. Web 前端功能（web/src/views/，逐文件核实）

| 视图文件 | 功能 | RF Track 关联 |
|---|---|---|
| `channelGrid.ts` | 频道矩阵：多频道 RSSI 卡片、活动条 | 与规划中 `channel-matrix` 直接对应，可演进复用 **[已由源码确认]** |
| `zoom.ts` | 单频道详情（大图 + 阈值） | 对应 `channel-detail` **[已由源码确认]** |
| `history.ts` | RSSI 历史趋势（EMA/maxHold/average，时间窗） | 对应 `rssi-trend` **[已由源码确认]** |
| `panorama.ts` | 全景扫描视图（hops 扫描） | 依赖离散频道跳频数据，**不是**连续频谱 **[已由源码确认]** |
| `power.ts` | 功率计模式（衰减/平滑参数） | 原功能，保护对象 **[已由源码确认]** |
| `rawiq.ts` | RAW IQ 快照视图（16380 采样点） | 唯一接近"真实频谱"的数据源（瞬时 IQ→FFT 推导） **[已由源码确认]** |
| `timer.ts` | 圈速计时（座位卡、EnterAt/ExitAt 阈值、"now" 捕捉） | 原功能，保护对象 **[已由源码确认]** |
| `rhServerDialog.ts` | RotorHazard 服务器连接（轮询 RH 4.5 API 叠加飞手名） | 原功能 **[已由源码确认]** |
| `channelPlanDialog.ts` | 频道方案编辑（上限 8、满额黄色提示） | 原功能 **[已由源码确认]** |
| `gpioDialog.ts` | GPIO 输出控制 | 原功能 **[已由源码确认]** |
| `flashDialog.ts` | 网页刷写（esptool-js，固件版本选择） | 原功能 **[已由源码确认]** |
| `debug.ts` | 调试视图 | 原功能 **[已由源码确认]** |
| `controls.ts`、`timerEventsDialog.ts` | 控制/计时事件对话框 | 原功能 **[已由源码确认]** |

模拟数据：`web/src/mock/`（mockDevice、lap 模拟、dsp 模拟），e2e 与演示用。RF Track 扩展时模拟数据必须显式标 `MOCK/DEMO`。**[已由源码确认]**

## 2. 固件功能（firmware/src/）

| 模块 | 功能 | 数据真实性 |
|---|---|---|
| `scan/scanner.c` | 频道扫描循环（≤8 频道，hop 计划驱动） | 真实测量 **[已由源码确认]** |
| `scan/metrics.c` | RSSI→dB、EMA、峰值 | 真实测量+推导 **[已由源码确认]** |
| `scan/lap.c` | 过圈检测（enter/exit 阈值+历史窗） | 真实测量+规则推导 **[已由源码确认]** |
| `scan/planner.c` | L/R 频段混跳计划（单 LO 组） | 真实 **[已由源码确认]** |
| `dsp/fft_eval.c` + `psd.c` | FFT（R4 sc16）、PSD、dB 度量 | 由 RAW IQ 推导 **[已由源码确认]** |
| `radio/c5/radio_c5.c` | C5 收发、原始 I/Q（SRAM 银行 0x40830000，16380 字） | 真实；采集机制为逆向所得 **[已由源码确认]** |
| `proto/` | 文本命令 + 二进制帧（0xA5 'R'，CRC32） | — **[已由源码确认]** |
| RH 模式 | RotorHazard 兼容（UART，板卡默认静默） | **[需要实机验证]** |

固件宿主测试：`test/test_dsp/` 17 用例（FFT/度量/lap/计划），本机 `pio test -e native` 17/17 通过。**[已由源码确认，本机实测]**

## 3. 数据真实性分级（RF Track 必须延续的口径）

1. **真实测量**：离散频道 RSSI/SNR、RAW IQ 快照、SPECTRUM 帧（真实频率轴帧，仅 USB 链路承载）。
2. **推导**：dB 换算、EMA/峰值、FFT/PSD（来自 IQ）、lap 判定。
3. **模拟**：web/src/mock/*（目前仅测试用途）。
4. **不存在**：连续频谱瀑布（SPECTRUM 帧是真实频率轴帧但跳频式采样，非任意频率驻留）、测向/定位/轨迹、GPS。这些在无对应数据源时必须显示 unsupported，不得伪造。**[已由源码确认]**

## 4. 原功能保护清单（回归基线）

- 单元测试 56/56（Vitest）、e2e 28/28（无头 Chrome，含真实链路检查 `reads>100 && errors==0`）、固件宿主测试 17/17。全部在 M0 基线实测通过（tag `rftrack-m0`）。**[已通过软件测试；未做真机回归]**
- 真机验证状态：C5 硬件功能（RH 模式、真 LEVELS 帧、S3 通路）标注**需要实机验证**；详见 `docs/rf-track/m0-baseline.md` 与 ChatGPT 采集的真机录制方案。**[未执行]**
