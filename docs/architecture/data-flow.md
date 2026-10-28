# Widscan58 数据流审计（RF Track 阶段 0）

> 审计日期：2026-10-28。深度版见 `docs/rf-track/web-data-flow.md`；本文为其结构化摘要，行号均已复核。
> 标注：**[已由源码确认]** / **[根据源码推断]** / **[需要实机验证]**。

## 1. 主链路：设备 → 浏览器

```text
ESP32-C5 固件
  ├─ 扫描/计时循环 (scan/) → metrics、lap、profile
  ├─ RAW IQ：SRAM 银行 0x40830000，16380 字 (radio/c5/radio_c5.c:28-37)
  └─ proto/link.c：USB CDC (usb_serial_jtag 外设，非 GPIO UART)
       16KB no-split 环形缓冲 → link_task → usb_serial_jtag_write_bytes
       二进制帧: 0xA5 'R' type(u8) ver(u8) len(u16) payload crc32
       文本行与帧交错复用同一流
         ↓ USB
浏览器 (web/src/)
  ├─ serial/webusb.ts  WebSerialTransport.readLoop
  ├─ serial/parser.ts  StreamParser：CRC32 校验 + 失步重同步
  ├─ serial/device.ts  Device.onBytes 分发 (Transport 接口在 device.ts:7)
  ├─ proto/frames.ts   帧解码（与 firmware/src/proto/frames.h 镜像，_Static_assert 34/26/32/4/59）
  └─ model/store.ts    Store.ingest → 版本号++ → 各视图节流渲染循环 (main.ts)
```

帧类型：`FRAME_METRICS=1, SPECTRUM=2, TELEMETRY=3, RAWIQ=4, LOG=5, LEVELS=6`。**[已由源码确认]**

## 2. 渲染路径

`Store.ingest`（store.ts）按键写入并 `version++`；`main.ts` 用 `requestAnimationFrame` 帧循环（701/703 行附近）+ `setInterval`（677 行附近）按视图节流重绘，而非每帧推送订阅。行号基于 `rf-track` @`d53651b`（那批并行改动已随 `7bec675` 入库，工作树此后未再改动 web 源码，所以行号不再会漂移；若后续 S 阶段改动 `main.ts` 需重新核）。**[已由源码确认]**

含义（对 RF Track）：
- 新组件接入点 = 订阅 Store 版本号或自行注册到渲染循环；无需框架。
- Store 以频道 id（u8，profile 下标）为**唯一**键——无节点维度，多节点必须经网关改名/加前缀后进入。**[根据源码推断]**

## 3. 次级数据流

| 流 | 路径 | 备注 |
|---|---|---|
| RH 服务器 | web `RhServer` → HTTP 轮询 RH 4.5 API → 飞手名叠加 | 独立于 USB 链路 **[已由源码确认]** |
| RH UART 模式 | 固件 PROTOCOL 切换 → rh.h (RH_SEATS=8) → web `serial/rhLink.ts` 100ms 轮询 | **[需要实机验证]** |
| 模拟 | `web/src/mock/mockDevice.ts` 实现 Transport 接口 | 已是适配层形态，可作 MOCK 数据源蓝本 **[已由源码确认]** |
| WebSocket 桥 | `tools/serial_bridge.py`（`?bridge=ws://`） | 多节点网关的自然起点 **[已由源码确认]** |
| 刷写 | `serial/flasher.ts` esptool-js，固件清单来自 GitHub Pages | **[已由源码确认]** |

## 4. 频率/单位约定（现状）

- 频率在 web 侧以频道名（R1–R8/L1–L8）↔ MHz 映射（channelPlan.ts）；固件侧命令用 kHz（`RAWIQ <lo_khz>`）。帧内无统一频率单位字段——RF Track 统一数据模型需显式约定（建议 kHz 整数）并写进文档。**[根据源码推断]**
- RSSI 单位：固件输出 dB 换算值（metrics.c），帧内为整型编码；SNR 可选。**[已由源码确认，编码细节见 web-data-flow.md]**

## 5. 对多节点扩展的结论

1. 适配层位置已存在：`Transport`（serial/device.ts:7）+ `mockDevice` 先例 + `?bridge=ws://` 先例。新增"网关 Transport"不触碰协议与原视图。**[已由源码确认]**
2. 频道上限 8（物理）与 ENABLE u32 掩码（隐藏 32）两处限制必须先于多频道扩展解决，详见 `docs/rf-track/channel-limit-audit.md`。**[已由源码确认]**
3. RAW IQ / SPECTRUM 帧只经 USB 承载（commands.c:505 注释），网关方案需评估其带宽占用。**[已由源码确认]**
