# Widscan58 树莓派 UART 直连 RotorHazard 设计

日期：2026-10-26
状态：已确认（UART 三线 / 仅 5.8 全频段 / USB 共存 / 单模块 8 座位）

## 1. 目标

让 Widscan58 通过 C5-Zero 排针上的 UART 直接连接树莓派，作为 RotorHazard 计时节点工作：

- 树莓派用 `/dev/ttyAMA0`、115200 8N1 与模块通信，RotorHazard 服务端零改动。
- 一根 UART 线提供 8 个座位（8 飞手），座位频率由 RH 服务端写入，覆盖 5345–5945 MHz 全 5.8 GHz 频段。
- USB 口同时保留：网页/APP 仍可读取状态、切换监控模式、查看频谱。
- 不做 2.4 GHz、不做同轮 2.4+5.8 混扫、不做单模块超过 8 个座位。

非目标：

- 不实现 I2C/SPI 从机路径。
- 不改变 RotorHazard 协议本身（node API level 36）。
- 不引入 Wi-Fi 网络、BLE 或第二个射频。

## 2. 决策摘要

| 决策 | 选择 | 理由 |
|---|---|---|
| 物理层 | UART0，GP11 TX / GP12 RX，三线 + GND | 丝印即 TX/RX；RH 官方参考硬件也走 Pi GPIO UART；服务端零改动 |
| 波特率 | 115200 8N1，无流控 | RH 非 S32 板型先试 115200；RH 轮询约 1–2 KB/s，线速余量约 10 倍 |
| 频段 | 5345–5945 MHz（5.8 全频段） | 现有 c5-race profile 与射频代码已验证，无需改 PHY |
| USB | 保留，与 UART 共存 | 现场配置/调试需要；射频归属用优先级规则解决 |
| 座位数 | 单模块 8 | `PROFILE_MAX_CHANNELS = 8`；>8 通过多模块多端口聚合，仅写文档不实现 |
| 回复路由 | RH/命令回复原路返回；帧只走 USB | 防止 RH 字节流混入频谱帧导致服务端解析错位 |

## 3. 硬件与树莓派配置

### 3.1 接线

| C5-Zero | 树莓派 40-pin | 说明 |
|---|---|---|
| GP11（U0TXD） | pin 10（GPIO15/RXD） | 丝印 TX |
| GP12（U0RXD） | pin 8（GPIO14/TXD） | 固件开内部上拉 |
| GND | pin 6 或其它 GND | 必须共地 |
| 5V | pin 2/4 或独立 USB | 二选一，避免反灌 |

- 两边均为 3.3V 电平，不加电平转换；C5-Zero 的 3V3 是输出，不得与 Pi 的 3.3V 相连。
- 线长 ≤ 30 cm，TX/RX 与 GND 成对；可选 100–330 Ω 串联电阻。
- GP12 内部上拉：Pi 掉电时其 TX 高阻，防止 RX 浮空产生随机字节。
- 备选：UART1 经 GPIO matrix 接 GP4/GP5，可避开 UART0 的 ROM 启动信息；本设计默认 UART0。

### 3.2 树莓派

1. `sudo raspi-config` → Interface Options → Serial Port：登录 shell 选 No，硬件串口选 Yes。
2. `/boot/firmware/config.txt` 增加 `enable_uart=1` 与 `dtoverlay=disable-bt`，使 `/dev/serial0` 指向 `ttyAMA0`（PL011）。
3. 确认 `/boot/firmware/cmdline.txt` 不含 `console=serial0,115200`。
4. RotorHazard `config.json`：`"GENERAL": { "SERIAL_PORTS": ["/dev/ttyAMA0"] }`。
5. 不要把板型选成 `S32_BPill`：它会先试 921600。默认非 S32 板型先试 115200。

## 4. 固件架构

### 4.1 传输层：单 sink 改为双 sink

`firmware/src/proto/link.h`、`link.c`：

```c
typedef enum { LINK_USB = 0, LINK_UART, LINK_COUNT } link_sink_t;

bool link_text_to(link_sink_t sink, const char *text);
bool link_raw_to(link_sink_t sink, const void *data, size_t n);
bool link_printf_to(link_sink_t sink, const char *fmt, ...);
int  link_read_from(link_sink_t sink, uint8_t *buf, size_t cap, uint32_t timeout_ms);
```

- 每个 sink 一条 ringbuf 和一个写任务。USB 队列维持现有大小（C5 16 KB，TX 4 KB）；UART 队列 4 KB。
- 现有 `link_frame`、`link_text`、`link_raw`、`link_printf` 保持原签名，语义固定为 `LINK_USB`，避免扫描器/遥测大量调用点改动。
- `link_read` 保持为 `LINK_USB`；新增 `link_read_from`。
- UART 写任务固定 CPU1、优先级 5；写失败与超时计入现有 `drops`。

新增 `firmware/src/proto/uart_link.c`：

```c
uart_config_t c = {
    .baud_rate = 115200,
    .data_bits = UART_DATA_8_BITS,
    .parity = UART_PARITY_DISABLE,
    .stop_bits = UART_STOP_BITS_1,
    .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
    .source_clk = UART_SCLK_DEFAULT,
};
uart_driver_install(UART_NUM_0, 4096, 0, 0, NULL, 0);
uart_param_config(UART_NUM_0, &c);
uart_set_pin(UART_NUM_0, 11, 12, -1, -1);
gpio_pullup_en(12);
```

`main.c` 在 `link_init()` 之后调用该初始化。控制台仍为 `CONFIG_ESP_CONSOLE_NONE=y`；UART0 时钟已由现有 C5 复位规避代码解锁。

### 4.2 协议分发：每端口独立状态，回复原路返回

`firmware/src/proto/commands.c`：

- 每端口一份：`line[128]`、`used`、`overflow`、`probe`、`text`、`text_start`、`last_rx`。
- `handle(char *line, link_sink_t sink)`；`reply_send(sink)`；所有单行回复改为 `link_text_to(sink, …)` / `link_printf_to(sink, …)`。
- 两个读任务（USB、UART）用 `cmd_lock` 串行进入分发，保证全局回复缓冲无竞争。
- UART 端口不参与 monitor↔RH 探针切换：收到第一个 RH 字节时，若 `proto != PROTO_RH` 则调用 `set_proto(PROTO_RH)`；UART 不允许切到 MONITOR。
- 扫描器异步回复（`OK RECAL`、RAWIQ 错误）在 `scan_request_t` 中携带 `sink`，由扫描任务用 `link_text_to` 回复。
- 无人订阅的错误（如 `ERR psd`）继续只写 USB，避免污染 RH 流。

### 4.3 RH 状态按端口拆分

`firmware/src/proto/rh.c`：

```c
typedef struct {
    uint8_t wcmd, wbuf[4], wneed, wgot;
    int64_t wstart;
    unsigned cur;
    link_sink_t sink;
} rh_port_t;
```

- 接口改为 `rh_rx(link_sink_t, uint8_t, int64_t)`、`rh_poll(int64_t)`（遍历两端口）、`rh_idle(link_sink_t)`。
- `read_cmd` 的回复改走 `link_raw_to(port->sink, …)`。
- 座位频率、`enabled`、级别状态仍全局共享：物理上只有一台接收机，两个主机写同一座位时“后写生效”。

### 4.4  GPIO 与构建

- `gpio_out_allowed()` 增加排除 UART TX/RX 引脚（与现有 USB 引脚排除同法），防止 `GPIO 11 HIGH` 抢走串口。
- `firmware/src/CMakeLists.txt`：`sources` 增加 `proto/uart_link.c`；`REQUIRES` 增加 `esp_driver_uart`。
- `sdkconfig.defaults` 不变。

## 5. 数据流与射频归属

```
网页/APP ──USB CDC──┐
                    ├─► LINK_USB  sink ──► 帧/遥测/文本/RH 回复
树莓派 RH ──UART0──┘
                    └─► LINK_UART sink ──► 仅 RH 回复与命令回复
```

规则：

1. RH 优先：UART 首个 RH 字节到来即切换全局 `proto` 为 RH，停止 USB 帧流，应用已保存的 RH 配置（增益、度量、滤波、座位计划）。
2. USB 仍可读：RH 白名单命令（`STATUS?`、`INFO`、`CONFIG?` 等）照常可用。
3. UART 活跃期间（最近 2 s 有流量），USB 的 `PROTOCOL MONITOR` 返回 `ERR uart_rh`。
4. UART 空闲超过 2 s 后，USB 可切回 MONITOR 恢复频谱。
5. 开机默认仍为 RH；树莓派未接时 USB 行为与现状一致。

## 6. 多频段与多飞手

- RH 座位频率经 `profile_from_equiv_khz()` 映射；c5-race 的 scale = 1.0、调谐范围 5345–5945 MHz，因此 L/R/E/F/A/B 及自定义 5.8 频率当前即可写入。
- 超出该范围的座位被标记为不可映射并关闭（RH 显示 off）。
- 8 座位按频率排序后，40 MHz 窗口内合并，Raceband 相邻两频道共用一次 LO，约 4 跳/轮。
- 单跳约 1–1.5 ms，一轮约 4–6 ms；RH 每 100 ms 轮询一次，余量充足。
- 超过 8 飞手：RH 支持多个串口，每口一台模块；单模块扩容不在本设计范围。
- 2.4 GHz 不在本设计范围。后续若要做，需独立完成：按会话选择 profile（40/20 MHz 带宽）、`adc_path_set` 的 2.4 分支、2.4 校准验证、PHY 频段切换验证。

## 7. 错误处理

| 场景 | 处理 |
|---|---|
| 芯片复位时 UART0 输出 ROM 启动信息 | RH 校验和与重试；比赛期间不主动复位；彻底规避可改 UART1 |
| Pi 掉电导致 RX 浮空 | GP12 内部上拉 |
| Pi 重启或发送 break，半条写命令 | 现有 100 ms 写超时丢弃未完成写 |
| 波特率不匹配（误选 S32 板型） | 文档禁止；校验和拒绝绝大多数；频率范围检查兜底 |
| GPIO 命令抢 UART 引脚 | `gpio_out_allowed` 排除 |
| UART 混入频谱帧 | 帧固定走 USB；UART sink 只接受 RH/命令回复 |
| USB 与 Pi 争夺扫描计划 | 2 s UART 空闲后才释放射频；UART 期间拒绝 MONITOR |

## 8. 内存预算

- UART ringbuf 4 KB + 驱动 RX 4 KB + 写任务栈 4 KB ≈ 12 KB，全部在堆上。
- C5 可用堆约 125 KB；扫描器静态 SRAM 不受影响。
- RH 回复为几十字节量级，UART 队列不会成为瓶颈。

## 9. 验证计划

阶段 0（不改代码）：

1. USB monitor 下 `RAWIQ 5362000 4096` 与 `RAWIQ 5917000 4096` 验证全频段。
2. 现有 USB RH + `tools/rh_check.py -p /dev/ttyACM0 --seats 8` 验证 8 座位基线。

阶段 1（UART）：

3. Pi 上 `miniterm /dev/ttyAMA0 115200`，发 `INFO`、`PROTOCOL?` 验证双向。
4. Pi 上运行 `rh_check.py -p /dev/ttyAMA0 --seats 8`，确认发现 8 节点、无通信错误。
5. 真机 RH：日志出现 `Serial multi-node found ... count=8 ... baudrate=115200`，VTX 过门可记圈。

阶段 2（共存与异常）：

6. RH 运行中连接 USB 网页：可读状态，`PROTOCOL MONITOR` 返回 `ERR uart_rh`；停 RH 2 s 后可切回。
7. 拔 UART、重启 Pi、复位 C5：模块不崩，RH 自动恢复。
8. 检查 `TIMING?`、`STATUS?` 的 heap 与 drops，确认新增队列无压力。

## 10. 实施顺序与工作量

| 阶段 | 内容 | 估算 |
|---|---|---|
| 0 | 全频段与 RH 基线复测 | 0.5 天 |
| 1 | 双 sink 传输层、UART 驱动、回复路由 | 2 天 |
| 2 | RH 每端口状态、射频仲裁 | 1 天 |
| 3 | GPIO 排除、STATUS/文档 | 0.5 天 |
| 4 | 树莓派联调、真机计时 | 1 天 |

## 11. 兼容性

- USB 协议与帧格式不变，网页/APP 无需改动。
- RH 协议与 node API level 不变，服务端无需插件。
- 现有 GPIO 保存/恢复逻辑不变；仅新增 UART 引脚排除。
- 已保存的 RH 配置（NVS）格式不变。
