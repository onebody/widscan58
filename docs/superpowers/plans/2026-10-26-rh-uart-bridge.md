# Widscan58 UART / RotorHazard 双传输实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 C5 固件加一条 UART0（GP11 TX / GP12 RX，115200 8N1）传输通道，使树莓派上的 RotorHazard 可以不改服务端直接认作 8 座位节点，同时 USB 保留配置/监控能力。

**Architecture:** 把 `proto/link.c` 从单队列改成“每个 sink 一条 ringbuf + 一个写任务”；新增 `proto/uart_link.c` 作为 UART 后端；`commands.c` 为每个端口保存独立的行缓冲/协议状态，用 `cmd_lock` 串行化分发，回复原路返回；`rh.c` 把写命令状态与当前座位拆到每端口；扫描器的异步回复随 `scan_request_t.sink` 回给发起者，频谱帧仍只走 USB。

**Tech Stack:** ESP-IDF 5.5.5（pioarduino 55.03.312-1）、ESP32-C5/C6 PHY、FreeRTOS ringbuf、`esp_driver_uart`、RotorHazard node API level 36。设计依据：[2026-10-26-rh-uart-bridge-design.md](../specs/2026-10-26-rh-uart-bridge-design.md)。

**与设计文档的唯一偏差：** 设计 §4.1 写“`main.c` 在 `link_init()` 之后调用 UART 初始化”。本计划改为在 `link_init()` 末尾调用 `uart_link_init()`：传输层自己拥有两个 sink，且 `main.c` 当前带着一份与本特性无关的未提交改动（`wifi_boot_channel()`），避免把它带进这些提交。行为不变。

---

## 执行须知

- 仓库根目录是 `repo/`，所有 `git` 命令在 `repo/` 下执行。
- **已有未提交改动，绝不能 stage：** `firmware/src/main.c`、`tools/check_phy_abi.sh`。每个提交只 `git add` 任务里列出的路径，提交前用 `git diff --cached --stat` 核对。
- C5 的静态 SRAM 在 RF dump bank 以下只剩几百字节（`CLAUDE.md`）。本计划新增的状态全部放堆：UART 的 ringbuf/任务栈由驱动和 FreeRTOS 分配，`cmd_ports` 与 RH 的每端口状态用 `calloc`/`heap_caps_calloc`。不要新增静态数组或 IRAM 函数。
- 硬件相关的模块没有主机单测（`pio test -e native` 只覆盖 DSP/计圈）。每个任务的验收 = 两个目标都能编译通过；真实串口行为在 Task 7 用硬件验证。**不要**为了“有测试”给这些模块临时搭 mock。
- `pio` 不要并发跑（本机已知会死锁）；编译、烧写串行执行。
- 提交信息跟仓库现有风格：首行是一句陈述，正文说明“为什么/测了什么”。

## 文件结构

| 文件 | 动作 | 职责 |
|---|---|---|
| `firmware/src/proto/link.h` | 改 | 新增 `link_sink_t`、按 sink 的写/读/队列 API；旧 `link_*` 固定为 USB |
| `firmware/src/proto/link.c` | 改 | 每条 sink 一个 ringbuf + 写任务；USB 行为不变 |
| `firmware/src/proto/uart_link.h` | 新建 | UART 后端接口 |
| `firmware/src/proto/uart_link.c` | 新建 | UART0 驱动安装、RX/TX、GP12 上拉；非 C5 目标返回 false |
| `firmware/src/proto/rh.h` | 改 | `rh_idle/rh_rx` 带 sink；`rh_poll` 扫所有端口 |
| `firmware/src/proto/rh.c` | 改 | 每端口写状态 + 当前座位；回复走 `link_raw_to` |
| `firmware/src/proto/commands.c` | 改 | 每端口行缓冲/探针状态；回复路由；射频归属仲裁；两个读任务 |
| `firmware/src/scan/scanner.h` | 改 | `scan_request_t.sink` |
| `firmware/src/scan/scanner.c` | 改 | RAWIQ/FFTEVAL/RECAL 的文本回复发给请求方；帧仍走 USB |
| `firmware/src/gpio_out.c` | 改 | `gpio_out_allowed()` 排除 GP11/GP12（C5） |
| `firmware/src/CMakeLists.txt` | 改 | 加源文件与 `esp_driver_uart` |
| `docs/RotorHazard.md` | 改 | 树莓派接线与配置、2 s 射频归属规则 |
| `CLAUDE.md` | 改 | RotorHazard 模式说明、GPIO 命令的 `x` 含义 |

---

### Task 0: 提交设计稿与本计划

**Files:**
- Add: `docs/superpowers/specs/2026-10-26-rh-uart-bridge-design.md`
- Add: `docs/superpowers/plans/2026-10-26-rh-uart-bridge.md`

- [ ] **Step 1: 确认只有这两个文件在 docs 下未跟踪**

```bash
git status --short docs/
```

Expected:

```
?? docs/superpowers/
```

- [ ] **Step 2: 只添加这两个文件并提交**

```bash
git add docs/superpowers/specs/2026-10-26-rh-uart-bridge-design.md \
        docs/superpowers/plans/2026-10-26-rh-uart-bridge.md
git diff --cached --stat
git commit -m "$(cat <<'EOF'
docs: RotorHazard over UART design and implementation plan

The C5-Zero's UART0 (GP11/GP12) as a second host link at 115200 8N1, so
a Raspberry Pi can run RotorHazard with no server changes while USB stays
available for the web UI.
EOF
)"
```

Expected: 两个文件，`2 files changed`。`firmware/src/main.c` 与 `tools/check_phy_abi.sh` 不出现在暂存区。

---

### Task 1: 传输层改为双 sink

**Files:**
- Modify: `firmware/src/proto/link.h`
- Modify: `firmware/src/proto/link.c`

- [ ] **Step 1: 替换 `link.h` 全文**

```c
/* Non-blocking output queues to USB Serial/JTAG and, on the C5, the
 * RotorHazard UART. GPL-3.0-or-later. */
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Where replies go. Frames are USB-only: RotorHazard's byte stream has no
 * frame framing, and one stray 0xA5 would desynchronise the server. */
typedef enum { LINK_USB = 0, LINK_UART, LINK_COUNT } link_sink_t;

void link_init(void);
/* Create a sink's queue and writer task. link_init starts USB; uart_link.c
 * starts the UART once its driver is installed. */
void link_sink_start(link_sink_t sink, size_t queue_bytes);
/* Whether the sink was started (its transport is usable). */
bool link_ready(link_sink_t sink);

/* Queue a complete frame on USB; returns false (and counts a drop) if it
 * doesn't fit. min_free: refuse unless this much queue space would remain
 * (for low priority frames such as spectra). */
bool link_frame(uint8_t type, const void *a, size_t alen, const void *b, size_t blen, size_t min_free);
/* As link_frame, but wait up to wait_ms for the queue to drain enough: for
 * rare large frames (RAWIQ) that only fit a nearly empty queue. */
bool link_frame_wait(uint8_t type, const void *a, size_t alen, const void *b, size_t blen, uint32_t wait_ms);

/* Text (our ASCII replies) and raw bytes (RotorHazard replies). The plain
 * calls mean LINK_USB. */
bool link_text(const char *text);
bool link_raw(const void *data, size_t n);
bool link_printf(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
bool link_text_to(link_sink_t sink, const char *text);
bool link_raw_to(link_sink_t sink, const void *data, size_t n);
bool link_printf_to(link_sink_t sink, const char *fmt, ...) __attribute__((format(printf, 2, 3)));

/* Frames are only queued while streaming (monitor mode); text and raw bytes
 * always are. Both apply to the USB sink. */
void link_set_streaming(bool on);
bool link_streaming(void);
/* Drop everything queued and not yet handed to USB. */
void link_flush(void);

uint32_t link_drops(void);
unsigned link_fill_permille(void);
size_t link_capacity(void);

/* Read up to capacity bytes, waiting up to timeout_ms for the first
 * (UINT32_MAX: forever). Returns the count. */
int link_read(uint8_t *buf, size_t capacity, uint32_t timeout_ms);
int link_read_from(link_sink_t sink, uint8_t *buf, size_t capacity, uint32_t timeout_ms);
```

- [ ] **Step 2: 替换 `link.c` 全文（本任务先不接 UART 初始化）**

```c
/* GPL-3.0-or-later. */
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include "driver/usb_serial_jtag.h"
#include "esp_attr.h"
#include "esp_rom_crc.h"
#include "freertos/FreeRTOS.h"
#include "freertos/ringbuf.h"
#include "freertos/task.h"
#include "frames.h"
#include "link.h"

#if RX_TARGET_C5
/* The C5 heap is ~125 KB once the 128 KiB capture bank is reserved, and Wi-Fi
 * needs much of what's left. RAWIQ goes out in chunks, so the queue only has
 * to hold a reply (3 KB) and ride out short host stalls. */
#define LINK_QUEUE_BYTES (16 * 1024)
#define LINK_USB_TX_BYTES 4096
#else
#define LINK_QUEUE_BYTES (72 * 1024)
#define LINK_USB_TX_BYTES 16384
#endif
/* The USB driver cannot accept a write larger than its TX buffer; the UART's
 * TX ring is the same size. */
#define LINK_TX_CHUNK 4096
/* With no host attached the USB FIFO never drains; drop quickly. The UART
 * shifts bytes out at line rate whether or not anything listens. */
#define LINK_WRITE_TIMEOUT_MS 50
#define LINK_QUEUE_TIMEOUT_MS 100

static RingbufHandle_t queues[LINK_COUNT];
static volatile uint32_t drops;
static volatile bool streaming; /* frames allowed (link_set_streaming); off at boot */

static void link_task(void *arg) {
    link_sink_t sink = (link_sink_t)(intptr_t)arg;
    for (;;) {
        size_t size;
        uint8_t *item = xRingbufferReceive(queues[sink], &size, portMAX_DELAY);
        if (!item) continue;
        size_t done = 0;
        while (done < size) {
            size_t chunk = size - done > LINK_TX_CHUNK ? LINK_TX_CHUNK : size - done;
            int n = sink == LINK_UART
                        ? uart_link_write(item + done, chunk)
                        : usb_serial_jtag_write_bytes(item + done, chunk, pdMS_TO_TICKS(LINK_WRITE_TIMEOUT_MS));
            if (n <= 0) break;
            done += (size_t)n;
        }
        if (done < size) drops++;
        vRingbufferReturnItem(queues[sink], item);
    }
}

void link_sink_start(link_sink_t sink, size_t queue_bytes) {
    queues[sink] = xRingbufferCreate(queue_bytes, RINGBUF_TYPE_NOSPLIT);
    if (!queues[sink]) return;
    xTaskCreatePinnedToCore(link_task, sink == LINK_UART ? "uart" : "link", 4096, (void *)(intptr_t)sink, 5, NULL,
                            portNUM_PROCESSORS - 1);
}

bool link_ready(link_sink_t sink) { return (unsigned)sink < LINK_COUNT && queues[sink]; }

void link_init(void) {
    usb_serial_jtag_driver_config_t cfg = {.tx_buffer_size = LINK_USB_TX_BYTES, .rx_buffer_size = 1024};
    ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&cfg));
    link_sink_start(LINK_USB, LINK_QUEUE_BYTES);
}

size_t link_capacity(void) { return xRingbufferGetMaxItemSize(queues[LINK_USB]); }
uint32_t link_drops(void) { return drops; }
unsigned link_fill_permille(void) {
    /* A no-split ring reports at most its largest possible item as free. */
    size_t free = xRingbufferGetCurFreeSize(queues[LINK_USB]), max = xRingbufferGetMaxItemSize(queues[LINK_USB]);
    return free >= max ? 0 : (unsigned)(1000 - free * 1000 / max);
}

static IRAM_ATTR bool frame_put(uint8_t type, const void *a, size_t alen, const void *b, size_t blen, size_t min_free,
                      TickType_t wait) {
    if (!streaming) return false; /* not a drop: nobody is listening for frames */
    size_t len = alen + blen;
    size_t total = sizeof(frame_header_t) + len + 4;
    if (len > 0xffff || (min_free && xRingbufferGetCurFreeSize(queues[LINK_USB]) < total + min_free)) {
        drops++;
        return false;
    }
    void *slot;
    if (xRingbufferSendAcquire(queues[LINK_USB], &slot, total, wait) != pdTRUE) {
        drops++;
        return false;
    }
    uint8_t *p = slot;
    frame_header_t h = {FRAME_SYNC0, FRAME_SYNC1, type, PROTO_VERSION, (uint16_t)len};
    memcpy(p, &h, sizeof(h));
    if (alen) memcpy(p + sizeof(h), a, alen);
    if (blen) memcpy(p + sizeof(h) + alen, b, blen);
    uint32_t crc = esp_rom_crc32_le(0, p, sizeof(h) + len);
    memcpy(p + sizeof(h) + len, &crc, 4);
    xRingbufferSendComplete(queues[LINK_USB], slot);
    return true;
}

IRAM_ATTR bool link_frame(uint8_t type, const void *a, size_t alen, const void *b, size_t blen, size_t min_free) {
    return frame_put(type, a, alen, b, blen, min_free, 0);
}

bool link_frame_wait(uint8_t type, const void *a, size_t alen, const void *b, size_t blen, uint32_t wait_ms) {
    return frame_put(type, a, alen, b, blen, 0, pdMS_TO_TICKS(wait_ms));
}

bool link_raw_to(link_sink_t sink, const void *data, size_t n) {
    if (!link_ready(sink) || xRingbufferSend(queues[sink], data, n, pdMS_TO_TICKS(LINK_QUEUE_TIMEOUT_MS)) != pdTRUE) {
        drops++;
        return false;
    }
    return true;
}

bool link_raw(const void *data, size_t n) { return link_raw_to(LINK_USB, data, n); }
bool link_text_to(link_sink_t sink, const char *text) { return link_raw_to(sink, text, strlen(text)); }
bool link_text(const char *text) { return link_text_to(LINK_USB, text); }

void link_set_streaming(bool on) { streaming = on; }
IRAM_ATTR bool link_streaming(void) { return streaming; }

void link_flush(void) {
    size_t size;
    void *item;
    while ((item = xRingbufferReceive(queues[LINK_USB], &size, 0)) != NULL) vRingbufferReturnItem(queues[LINK_USB], item);
}

static bool printf_to(link_sink_t sink, const char *fmt, va_list ap) {
    char buffer[512];
    vsnprintf(buffer, sizeof(buffer), fmt, ap);
    return link_text_to(sink, buffer);
}

bool link_printf_to(link_sink_t sink, const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    bool ok = printf_to(sink, fmt, ap);
    va_end(ap);
    return ok;
}

bool link_printf(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    bool ok = printf_to(LINK_USB, fmt, ap);
    va_end(ap);
    return ok;
}

int link_read_from(link_sink_t sink, uint8_t *buf, size_t capacity, uint32_t timeout_ms) {
    if (sink == LINK_UART) return uart_link_read(buf, capacity, timeout_ms);
    return usb_serial_jtag_read_bytes(buf, capacity, timeout_ms == UINT32_MAX ? portMAX_DELAY : pdMS_TO_TICKS(timeout_ms));
}

int link_read(uint8_t *buf, size_t capacity, uint32_t timeout_ms) {
    return link_read_from(LINK_USB, buf, capacity, timeout_ms);
}
```

说明：这一步 `link.c` 引用了 `uart_link_write` / `uart_link_read` / `uart_link_init` 中前两个，所以**不能**单独编译成功，必须和 Task 2 的 `uart_link.h/.c` 一起落地。为了保持本任务可提交，**把 Step 2 与 Task 2 合并执行，最后按 Task 2 的提交步骤一次性提交**；本任务不单独 commit。若执行器坚持逐任务提交，则先创建 `uart_link.c` 的桩（返回 -1），再在 Task 2 替换，但仓库规范不喜欢中间桩，推荐合并。

- [ ] **Step 3: 静态检查（不构建）**

```bash
cd firmware
grep -n "queues\[LINK_USB\]" src/proto/link.c | wc -l   # 期望 8
grep -n "link_text_to\|link_read_from\|link_sink_start" src/proto/link.h
```

Expected: 8，以及三个新声明各一行。

---

### Task 2: UART 后端、构建与 GPIO 排除

**Files:**
- Create: `firmware/src/proto/uart_link.h`
- Create: `firmware/src/proto/uart_link.c`
- Modify: `firmware/src/proto/link.c`（Task 1 的最终两行）
- Modify: `firmware/src/CMakeLists.txt`
- Modify: `firmware/src/gpio_out.c`

- [ ] **Step 1: 新建 `firmware/src/proto/uart_link.h`**

```c
/* RotorHazard over UART0 on the ESP32-C5-Zero header (GP11 TX, GP12 RX).
 * GPL-3.0-or-later. */
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Install UART0 at 115200 8N1 and start LINK_UART. False (and no sink) where
 * the target has no such link. */
bool uart_link_init(void);
/* For link.c's writer and reader tasks. */
int uart_link_write(const void *data, size_t n);
int uart_link_read(uint8_t *buf, size_t capacity, uint32_t timeout_ms);
```

- [ ] **Step 2: 新建 `firmware/src/proto/uart_link.c`**

```c
/* GPL-3.0-or-later. */
#include "driver/gpio.h"
#include "driver/uart.h"
#include "freertos/FreeRTOS.h"
#include "link.h"
#include "uart_link.h"

#define UART_LINK_PORT UART_NUM_0
#define UART_LINK_TX 11 /* the C5-Zero's U0TXD, silk "TX" */
#define UART_LINK_RX 12 /* U0RXD, silk "RX" */
#define UART_LINK_BAUD 115200
#define UART_LINK_RX_BYTES 4096
#define UART_LINK_TX_BYTES 4096
#define UART_LINK_QUEUE_BYTES 4096

#if RX_TARGET_C5
static bool installed;

bool uart_link_init(void) {
    const uart_config_t cfg = {
        .baud_rate = UART_LINK_BAUD,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    if (uart_param_config(UART_LINK_PORT, &cfg) != ESP_OK) return false;
    if (uart_set_pin(UART_LINK_PORT, UART_LINK_TX, UART_LINK_RX, -1, -1) != ESP_OK) return false;
    if (uart_driver_install(UART_LINK_PORT, UART_LINK_RX_BYTES, UART_LINK_TX_BYTES, 0, NULL, 0) != ESP_OK) return false;
    /* The Pi's TX floats while it is off; without the pull-up the RX line
     * fills the buffer with noise. */
    gpio_pullup_en(UART_LINK_RX);
    installed = true;
    link_sink_start(LINK_UART, UART_LINK_QUEUE_BYTES);
    return true;
}

int uart_link_write(const void *data, size_t n) {
    return installed ? uart_write_bytes(UART_LINK_PORT, data, n) : -1;
}

int uart_link_read(uint8_t *buf, size_t capacity, uint32_t timeout_ms) {
    if (!installed) return -1;
    return uart_read_bytes(UART_LINK_PORT, buf, capacity,
                           timeout_ms == UINT32_MAX ? portMAX_DELAY : pdMS_TO_TICKS(timeout_ms));
}
#else
/* The S3 has no Pi link: keep the target building with no UART sink. */
bool uart_link_init(void) { return false; }
int uart_link_write(const void *data, size_t n) {
    (void)data;
    (void)n;
    return -1;
}
int uart_link_read(uint8_t *buf, size_t capacity, uint32_t timeout_ms) {
    (void)buf;
    (void)capacity;
    (void)timeout_ms;
    return -1;
}
#endif
```

- [ ] **Step 3: 在 `link.c` 接上 UART**

在 `#include "link.h"` 之后加：

```c
#include "uart_link.h"
```

把 `link_init()` 的结尾改成：

```c
void link_init(void) {
    usb_serial_jtag_driver_config_t cfg = {.tx_buffer_size = LINK_USB_TX_BYTES, .rx_buffer_size = 1024};
    ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&cfg));
    link_sink_start(LINK_USB, LINK_QUEUE_BYTES);
    uart_link_init(); /* no sink where the target has no UART link */
}
```

- [ ] **Step 4: 改 `firmware/src/CMakeLists.txt`**

`set(sources` 中加入一行（放在 `"proto/rh.c"` 之后）：

```cmake
    "proto/uart_link.c"
```

`REQUIRES` 加 `esp_driver_uart`：

```cmake
    REQUIRES esp_app_format esp_driver_gpio esp_driver_uart esp_driver_usb_serial_jtag esp_wifi esp_phy esp_timer
             esp_hw_support nvs_flash esp_rom espressif__esp-dsp)
```

- [ ] **Step 5: 改 `firmware/src/gpio_out.c`**

把第 22–29 行的平台块改成：

```c
/* USB Serial/JTAG is the host link and GP11/GP12 are the RotorHazard UART
 * (uart_link.c); the flash pins (and PSRAM's, when it is on) are reserved by
 * IDF at startup (esp_mspi_pin_reserve). */
#if CONFIG_IDF_TARGET_ESP32C5
#define USB_PINS (BIT64(13) | BIT64(14))
#define UART_PINS (BIT64(11) | BIT64(12))
#elif CONFIG_IDF_TARGET_ESP32S3
#include "soc/usb_pins.h"
#define USB_PINS (BIT64(USBPHY_DM_NUM) | BIT64(USBPHY_DP_NUM))
#define UART_PINS 0
#endif
```

把 `gpio_out_allowed()` 改成：

```c
bool gpio_out_allowed(unsigned n) {
    return n < SOC_GPIO_PIN_COUNT && GPIO_IS_VALID_OUTPUT_GPIO(n) && !(USB_PINS & BIT64(n)) &&
           !(UART_PINS & BIT64(n)) && !esp_gpio_is_reserved(BIT64(n));
}
```

- [ ] **Step 6: 编译两个目标**

```bash
cd ~/workspace/FPV_space/widscan58-clone/repo/firmware
pio run -e esp32c5
pio run -e esp32s3
```

Expected: 两次 `SUCCESS`。若链接报 `RF dump bank overlaps BSS`，说明新增静态数据超了：检查 `link.c` 只多了 `queues[2]`（`+4` 字节）与 `uart_link.c` 的 `installed`（1 字节），其余状态应已在堆上；不要用调小 `sdkconfig` 的方式绕过。

- [ ] **Step 7: 提交**

```bash
cd ~/workspace/FPV_space/widscan58-clone/repo
git add firmware/src/proto/link.c firmware/src/proto/link.h \
        firmware/src/proto/uart_link.c firmware/src/proto/uart_link.h \
        firmware/src/CMakeLists.txt firmware/src/gpio_out.c
git diff --cached --stat
git commit -m "$(cat <<'EOF'
link/uart: a second sink on UART0 (GP11 TX, GP12 RX)

USB keeps its queue, writer task and frame path. link.c now runs one
queue and writer per sink, and uart_link.c installs UART0 at 115200 8N1
with 4 KB RX/TX rings and a pull-up on RX (a powered-off Pi's TX floats).
link_init() starts the UART sink after the USB driver; the S3 build gets
a stub. The GPIO commands refuse GP11/GP12 so a saved pin cannot take
the link. Frames stay USB-only.
EOF
)"
```

Expected: 暂存区 6 个文件；`firmware/src/main.c` 与 `tools/check_phy_abi.sh` 仍为未暂存的 `M`。

---

### Task 3: RH 状态按端口拆分

**Files:**
- Modify: `firmware/src/proto/rh.h`
- Modify: `firmware/src/proto/rh.c`
- Modify: `firmware/src/proto/commands.c`（只改 3 个调用点，暂不重构任务）

- [ ] **Step 1: 改 `rh.h` 的接口**

在 `#include "lap.h"` 之前加：

```c
#include "link.h"
```

把这一段：

```c
/* Command bytes from the host. rh_idle: no write payload pending (a byte
 * may then start one of our text lines instead, see commands.c). */
bool rh_idle(void);
void rh_rx(uint8_t b, int64_t now_us);
/* Abandon a write whose payload stopped arriving. */
void rh_poll(int64_t now_us);
```

改成：

```c
/* Command bytes from one port. rh_idle: that port has no write payload
 * pending (a byte may then start one of our text lines instead, see
 * commands.c). */
bool rh_idle(link_sink_t sink);
void rh_rx(link_sink_t sink, uint8_t b, int64_t now_us);
/* Abandon writes whose payload stopped arriving, on every port. */
void rh_poll(int64_t now_us);
```

同时把文件头的 `WRITE_CURNODE_INDEX selects the seat the other commands
 * address.` 改成 `... address, per port.`。

- [ ] **Step 2: 改 `rh.c` 的状态定义**

把它：

```c
static unsigned cur;
/* Write in progress. */
static uint8_t wcmd, wbuf[4], wneed, wgot;
static int64_t wstart;
```

改成：

```c
/* Per port (USB host, Pi): the seat WRITE_CURNODE_INDEX selected and the
 * write in progress. Seats and levels are shared below: there is one
 * receiver. On the heap, like the seats: the C5's static RAM below the RF
 * dump bank is nearly full. */
typedef struct {
    uint8_t wcmd, wbuf[4], wneed, wgot;
    int64_t wstart;
    unsigned cur;
} rh_port_t;
static rh_port_t *port;
```

把 `rh_init()` 改成：

```c
esp_err_t rh_init(void) {
    seat = heap_caps_calloc(RH_SEATS, sizeof(*seat), MALLOC_CAP_INTERNAL);
    port = heap_caps_calloc(LINK_COUNT, sizeof(*port), MALLOC_CAP_INTERNAL);
    if (!seat || !port) return ESP_ERR_NO_MEM;
    const profile_t *p = profile_default(radio_caps()->target);
    for (unsigned j = 0; j < RH_SEATS; j++) {
        /* The server reads the frequency first and rejects 0: report the
         * profile's channels until it writes its own. */
        seat[j].freq_mhz = (uint16_t)(profile_equiv_khz(p, p->ch[j < p->nch ? j : 0].khz) / 1000 + 0.5);
        lap_init(&seat[j].lap, DEFAULT_ENTER, DEFAULT_EXIT);
    }
    return ESP_OK;
}
```

- [ ] **Step 3: 改 `read_cmd`**

函数签名改成 `static void read_cmd(link_sink_t sink, uint8_t cmd) {`，在 `outn = 0;` 之后加：

```c
    unsigned cur = port[sink].cur;
```

其余 `seat[cur]` 不变。最后一行：

```c
    link_raw(out, outn);
```

改成：

```c
    link_raw_to(sink, out, outn);
```

- [ ] **Step 4: 改 `set_frequency` 与 `write_cmd`**

`set_frequency`：

```c
static void set_frequency(link_sink_t sink, uint16_t mhz) {
    if (mhz < MIN_FREQ || mhz > MAX_FREQ) return;
    scan_config_t c;
    scanner_get_config(&c);
    uint32_t khz;
    bool mapped = profile_from_equiv_khz(c.profile, mhz * 1000u, &khz); /* 1111: the server's "off" */
    unsigned cur = port[sink].cur;
    portENTER_CRITICAL(&mux);
    ...
```

（函数其余部分不变。）

`write_cmd`：

```c
static void write_cmd(link_sink_t sink, uint8_t cmd, const uint8_t *d) {
    unsigned cur = port[sink].cur;
    switch (cmd) {
    ...
    case WRITE_CURNODE_INDEX:
        if (d[0] < RH_SEATS) port[sink].cur = d[0];
        break;
    ...
    }
}
```

- [ ] **Step 5: 改 `rh_idle` / `rh_rx` / `rh_poll`**

```c
bool rh_idle(link_sink_t sink) { return !port[sink].wneed; }

void rh_rx(link_sink_t sink, uint8_t b, int64_t now_us) {
    rh_port_t *p = &port[sink];
    if (p->wneed) {
        p->wbuf[p->wgot++] = b;
        if (p->wgot < p->wneed) return;
        uint8_t sum = 0;
        for (unsigned j = 0; j + 1 < p->wneed; j++) sum += p->wbuf[j];
        bool ok = sum == p->wbuf[p->wneed - 1];
        p->wneed = 0;
        if (ok) write_cmd(sink, p->wcmd, p->wbuf);
        return;
    }
    if (b <= 0x50) {
        read_cmd(sink, b);
        return;
    }
    unsigned n = write_size(b);
    if (!n) return; /* unknown write: ignored, as RotorHazard's own nodes */
    p->wcmd = b;
    p->wneed = (uint8_t)(n + 1);
    p->wgot = 0;
    p->wstart = now_us;
}

void rh_poll(int64_t now_us) {
    for (unsigned j = 0; j < LINK_COUNT; j++)
        if (port[j].wneed && now_us - port[j].wstart > WRITE_TIMEOUT_US) port[j].wneed = 0;
}
```

- [ ] **Step 6: 让 `commands.c` 的旧单任务继续编译**

只改这三处调用（本任务不重构它）：

- `rh_rx(RH_REVISION_CODE, now);` → `rh_rx(LINK_USB, RH_REVISION_CODE, now);`
- `rh_idle()` → `rh_idle(LINK_USB)`
- `rh_rx(b, now);` → `rh_rx(LINK_USB, b, now);`

`rh_poll(now);` 不变。

- [ ] **Step 7: 编译**

```bash
cd ~/workspace/FPV_space/widscan58-clone/repo/firmware
pio run -e esp32c5
```

Expected: `SUCCESS`。

- [ ] **Step 8: 提交**

```bash
cd ~/workspace/FPV_space/widscan58-clone/repo
git add firmware/src/proto/rh.c firmware/src/proto/rh.h firmware/src/proto/commands.c
git diff --cached --stat
git commit -m "$(cat <<'EOF'
rh: per-port write state and seat selection

The USB host and the Pi can each be half-way through a write and on a
different seat; seats and levels stay shared. Replies go back to the port
that asked. The per-port state is on the heap: static RAM below the C5's
RF dump bank has almost no room left.
EOF
)"
```

Expected: 3 个文件。

---

### Task 4: 扫描器回复发给请求方

**Files:**
- Modify: `firmware/src/scan/scanner.h`
- Modify: `firmware/src/scan/scanner.c`

- [ ] **Step 1: `scanner.h` 加 sink 字段**

在 `#include "radio.h"` 之后加：

```c
#include "link.h"
```

`scan_request_t` 改成：

```c
typedef struct {
    scan_req_type_t type;
    link_sink_t sink;        /* where the reply text goes; frames stay on USB */
    uint32_t lo_khz;
    uint16_t n;
    uint8_t rate_code;
    int32_t p1, p2, p3;      /* FFTEVAL parameters */
} scan_request_t;
```

- [ ] **Step 2: `scanner.c` 改文本回复**

`do_rawiq()`：

```c
    if (radio_capture(r->n, r->rate_code, c->settle_us, &words, NULL) != ESP_OK) {
        link_text_to(r->sink, "ERR capture\n");
        return;
    }
```

```c
        if (!link_frame_wait(FRAME_RAWIQ, &h, sizeof(h), iq + 2 * off, 2u * len, 500)) {
            link_text_to(r->sink, "ERR link_full\n");
            return;
        }
```

`do_fft_eval()`：

```c
    if (n > 8192) {
        link_text_to(r->sink, "ERR ffteval_capn\n"); /* the evaluation uses the aperture above 8192 words */
        return;
    }
```

```c
            if (radio_capture(n, c->rate_code, c->settle_us, &words, NULL) != ESP_OK) {
                link_text_to(r->sink, "ERR capture\n");
                return;
            }
```

```c
            link_printf_to(r->sink, "FFTEVAL %s %s ERR %s\n", r->type == SCAN_REQ_FFTEVAL_SYNTH ? "SYNTH" : "LIVE",
                           psd_fft_name(acc[t].type), esp_err_to_name(acc[t].err));
```

```c
        link_printf_to(r->sink,
                       "FFTEVAL %s %s tone %.2f floor %.2f spur %.2f sfdr %.2f rel %.3f dmean %.3f drms %.3f dmax %.2f fft_us %u psd_us %u\n",
                       r->type == SCAN_REQ_FFTEVAL_SYNTH ? "SYNTH" : "LIVE", psd_fft_name(acc[t].type),
                       (double)acc[t].tone_db, (double)acc[t].floor_db, (double)acc[t].spur_db,
                       (double)(acc[t].tone_db - acc[t].spur_db), (double)acc[t].floor_rel_db,
                       (double)acc[t].diff_mean_db, (double)acc[t].diff_rms_db, (double)acc[t].diff_max_db,
                       (unsigned)acc[t].fft_us, (unsigned)acc[t].psd_us);
```

```c
    link_text_to(r->sink, "FFTEVAL END\n");
```

`scanner_task` 的 RECAL 分支：

```c
            else if (req.type == SCAN_REQ_RECAL) {
                radio_force_recal();
                memset(sp->iq, 0, sizeof(sp->iq));
                link_text_to(req.sink, "OK RECAL\n");
            }
```

- [ ] **Step 3: 确认帧仍在 USB**

```bash
cd ~/workspace/FPV_space/widscan58-clone/repo/firmware
grep -n "link_frame\|link_frame_wait" src/scan/scanner.c
```

Expected: 4 处（SPECTRUM、LEVELS、METRICS、TELEMETRY、RAWIQ），都不带 `_to`，即仍走 USB。

- [ ] **Step 4: 编译并提交**

```bash
pio run -e esp32c5
cd ~/workspace/FPV_space/widscan58-clone/repo
git add firmware/src/scan/scanner.c firmware/src/scan/scanner.h
git diff --cached --stat
git commit -m "$(cat <<'EOF'
scanner: reply to the port that asked

RAWIQ, FFTEVAL and RECAL text goes back to scan_request_t.sink. Frames
still go to USB only: they are a binary stream with no place on the Pi's
RotorHazard link.
EOF
)"
```

Expected: 2 个文件，编译 `SUCCESS`。

---

### Task 5: 每端口命令分发、回复路由与射频归属

**Files:**
- Modify: `firmware/src/proto/commands.c`

- [ ] **Step 1: 加头文件与“当前回复端口”**

在 `#include "freertos/task.h"` 之后加：

```c
#include "freertos/semphr.h"
```

在 `static bool reply_overflow;` 之后插入：

```c
/* The port whose command is being handled; cmd_lock serializes the two reader
 * tasks, so reply_sink, the reply buffer and the protocol state have one
 * owner at a time. */
static SemaphoreHandle_t cmd_lock;
static link_sink_t reply_sink;
/* The UART's last byte: while it is recent the radio belongs to RotorHazard
 * and USB's PROTOCOL MONITOR is refused. */
#define UART_HOLD_US 2000000
static int64_t last_uart_rx = -UART_HOLD_US;

static bool reply_text(const char *text) { return link_text_to(reply_sink, text); }
static bool reply_printf(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static bool reply_printf(const char *fmt, ...) {
    char buffer[512];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buffer, sizeof(buffer), fmt, ap);
    va_end(ap);
    return reply_text(buffer);
}
```

- [ ] **Step 2: 把本文件里的回复改成 `reply_text` / `reply_printf`**

```bash
cd ~/workspace/FPV_space/widscan58-clone/repo/firmware
sed -i '' 's/\blink_text(/reply_text(/g; s/\blink_printf(/reply_printf(/g' src/proto/commands.c
grep -n "link_text(\|link_printf(" src/proto/commands.c
```

Expected: `grep` 无输出（`link_text_to` / `link_printf_to` 不在 `commands.c` 里使用）。

- [ ] **Step 3: `handle()` 带 sink，并加射频仲裁**

签名与开头：

```c
static void handle(char *line, link_sink_t sink) {
    reply_sink = sink;
    if (proto == PROTO_RH && !rh_allowed(line)) {
        reply_text("ERR rh_mode\n");
        return;
    }
```

把协议切换分支：

```c
    } else if (!strcmp(line, "PROTOCOL RH") || !strcmp(line, "PROTOCOL MONITOR")) {
        bool monitor = line[9] == 'M';
        if (monitor && sink == LINK_UART) { reply_text("ERR uart_port\n"); return; }
        /* RotorHazard owns the radio while its link is live. */
        if (monitor && esp_timer_get_time() - last_uart_rx < UART_HOLD_US) {
            reply_text("ERR uart_rh\n");
            return;
        }
        set_proto(monitor ? PROTO_MONITOR : PROTO_RH);
        reply_text("OK\n");
    }
```

替换原来的：

```c
    } else if (!strcmp(line, "PROTOCOL RH") || !strcmp(line, "PROTOCOL MONITOR")) {
        set_proto(line[9] == 'R' ? PROTO_RH : PROTO_MONITOR);
        reply_text("OK\n");
    }
```

- [ ] **Step 4: 异步请求带上 sink**

四处：

```c
        scan_request_t r = {.type = SCAN_REQ_RECAL, .sink = sink};
```

```c
        scan_request_t r = {.type = SCAN_REQ_RAWIQ, .sink = sink, .lo_khz = a, .n = (uint16_t)b, .rate_code = (uint8_t)d};
```

```c
        scan_request_t r = {.type = SCAN_REQ_FFTEVAL_SYNTH, .sink = sink, .p1 = sa, .p2 = sb, .p3 = sd};
```

```c
        scan_request_t r = {.type = SCAN_REQ_FFTEVAL_LIVE, .sink = sink, .lo_khz = a, .p3 = sd};
```

- [ ] **Step 5: 把行缓冲换成每端口**

把从 `/* Line assembly for our commands. */` 到 `commands_task` 结束（`}` 在 `void commands_task_start` 之前）整段替换成：

```c
/* Line assembly and protocol state per port: the USB host and the Pi write
 * different protocols, and one must not reset the other's half-sent line. */
typedef struct {
    char line[128];
    size_t used;
    bool overflow;
    bool probe;      /* monitor mode: a lone RotorHazard probe, waiting to see nothing follows */
    bool text;       /* RotorHazard mode: one of our command lines in progress */
    int64_t text_start;
    int64_t last_rx;
} cmd_port_t;
static cmd_port_t *ports; /* heap: static SRAM below the C5's RF dump bank is nearly full */

static void line_byte(cmd_port_t *p, uint8_t b, link_sink_t sink) {
    if (b == '\r') return;
    if (b == '\n') {
        if (p->overflow) link_text_to(sink, "ERR command_length\n");
        else if (p->used) {
            p->line[p->used] = 0;
            handle(p->line, sink);
        }
        p->used = 0;
        p->overflow = false;
        return;
    }
    if (p->used + 1 < sizeof(p->line)) p->line[p->used++] = (char)b;
    else p->overflow = true;
}

#define PROBE_IDLE_US 100000 /* RotorHazard waits 2 s and flushes before its probe */
#define PROBE_QUIET_MS 5     /* and then waits for the reply */
#define TEXT_TIMEOUT_US 200000

static void commands_task(void *arg) {
    link_sink_t sink = (link_sink_t)(intptr_t)arg;
    cmd_port_t *p = &ports[sink];
    uint8_t buf[64];
    for (;;) {
        int n = link_read_from(sink, buf, sizeof(buf), p->probe ? PROBE_QUIET_MS : 20);
        int64_t now = esp_timer_get_time();
        xSemaphoreTake(cmd_lock, portMAX_DELAY);
        if (n <= 0) {
            if (p->probe) {
                p->probe = false;
                set_proto(PROTO_RH);
                rh_rx(sink, RH_REVISION_CODE, now);
            }
            rh_poll(now);
            if (p->text && now - p->text_start > TEXT_TIMEOUT_US) {
                p->text = false;
                p->used = 0;
                p->overflow = false;
            }
            xSemaphoreGive(cmd_lock);
            continue;
        }
        for (int i = 0; i < n; i++) {
            uint8_t b = buf[i];
            /* The Pi is a RotorHazard server: its first byte takes the radio
             * back from a USB monitor session (and applies the saved RH
             * settings, as PROTOCOL RH does). */
            if (sink == LINK_UART && proto != PROTO_RH) set_proto(PROTO_RH);
            if (proto == PROTO_MONITOR) {
                if (p->probe) { /* more followed: it was just a byte of a line */
                    p->probe = false;
                    line_byte(p, RH_REVISION_CODE, sink);
                }
                if (n == 1 && b == RH_REVISION_CODE && !p->used && now - p->last_rx >= PROBE_IDLE_US) p->probe = true;
                else line_byte(p, b, sink);
            } else if (p->text || (rh_idle(sink) && rh_text_start(b))) {
                if (!p->text) {
                    p->text = true;
                    p->text_start = now;
                    p->used = 0;
                    p->overflow = false;
                }
                if (b == '\n') p->text = false;
                line_byte(p, b, sink); /* may switch to monitor mode */
            } else {
                rh_rx(sink, b, now);
            }
            p->last_rx = now;
            if (sink == LINK_UART) last_uart_rx = now;
        }
        xSemaphoreGive(cmd_lock);
    }
}
```

- [ ] **Step 6: 启动两个读任务**

`commands_task_start()` 改成：

```c
void commands_task_start(void) {
    reply = malloc(REPLY_BYTES);
    if (reply) reply[0] = 0;
    ports = calloc(LINK_COUNT, sizeof(*ports));
    if (!ports) return; /* without buffers there is no host protocol */
    cmd_lock = xSemaphoreCreateMutex();
    /* Boot in RotorHazard mode (rh_init has run); monitor mode starts from the defaults. */
    scan_config_t c;
    scanner_get_config(&c);
    other_cfg = c;
    rh_defaults(&c);
    rh_saved_from(&rh_builtin, &c);
    rh_saved = rh_builtin;
    rh_saved_load();
    rh_saved_apply(&c, &rh_saved);
    scanner_set_config(&c);
    link_set_streaming(false);
    xTaskCreatePinnedToCore(commands_task, "cmd", 6144, (void *)(intptr_t)LINK_USB, 6, NULL, portNUM_PROCESSORS - 1);
    if (link_ready(LINK_UART))
        xTaskCreatePinnedToCore(commands_task, "cmd_uart", 6144, (void *)(intptr_t)LINK_UART, 6, NULL,
                                portNUM_PROCESSORS - 1);
}
```

- [ ] **Step 7: 编译**

```bash
cd ~/workspace/FPV_space/widscan58-clone/repo/firmware
pio run -e esp32c5
pio run -e esp32s3
```

Expected: 两次 `SUCCESS`。

- [ ] **Step 8: 自查回复路由覆盖**

```bash
grep -n "reply_text\|reply_printf" src/proto/commands.c | wc -l   # 应 > 60
grep -n "link_text(\|link_printf(" src/proto/commands.c           # 无输出
grep -n "rh_rx(\|rh_idle(" src/proto/commands.c                    # 4 处都带 sink
```

Expected: 第一行 > 60；第二行无输出；第三行是 `rh_rx(sink, RH_REVISION_CODE, …)`、`rh_idle(sink)`、`rh_rx(sink, b, now)`（以及 `rh_rx(LINK_USB, …)` 不应再出现）。

- [ ] **Step 9: 提交**

```bash
cd ~/workspace/FPV_space/widscan58-clone/repo
git add firmware/src/proto/commands.c
git diff --cached --stat
git commit -m "$(cat <<'EOF'
commands: two host ports, replies to the sender

Each port keeps its own line buffer, probe and text state; cmd_lock
serializes dispatch so the shared reply buffer and protocol state have
one owner. The Pi's first byte switches the radio to RotorHazard (with
the saved RH settings). While the UART has had traffic in the last 2 s,
USB's PROTOCOL MONITOR answers ERR uart_rh; the UART itself cannot
switch to monitor. Async scanner replies carry the requesting sink.
EOF
)"
```

Expected: 1 个文件。

---

### Task 6: 文档

**Files:**
- Modify: `docs/RotorHazard.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: `docs/RotorHazard.md` 开头与新增章节**

把开头：

```markdown
timer node: one USB port with 8 seats, like an S32_BPill board with 8 RX5808
receivers. The RotorHazard server needs no plugins or changes, only the serial
port in its config.
```

改成：

```markdown
timer node: one USB port or the C5-Zero's UART header, with 8 seats, like an
S32_BPill board with 8 RX5808 receivers. The RotorHazard server needs no
plugins or changes, only the serial port in its config.
```

在 `## Configuring RotorHazard` 之后插入：

```markdown
## Raspberry Pi over UART

The C5-Zero's header carries the same node protocol at 115200 8N1. This is
the wiring for a Raspberry Pi (both sides are 3.3 V; no level shifter):

| C5-Zero | Raspberry Pi 40-pin | |
|---|---|---|
| GP11 (TX) | pin 10 (GPIO15 / RXD) | |
| GP12 (RX) | pin 8 (GPIO14 / TXD) | the board enables an internal pull-up |
| GND | pin 6 | common ground |
| 5V | pin 2 or 4, or the board's USB | one source only |

Do not connect the C5-Zero's 3V3 output to the Pi's 3.3 V, and keep the wires
under ~30 cm with GND routed alongside.

On the Pi:

1. `sudo raspi-config` → Interface Options → Serial Port: login shell **No**,
   hardware serial **Yes**.
2. In `/boot/firmware/config.txt` add `enable_uart=1` and
   `dtoverlay=disable-bt`, so `/dev/serial0` is the PL011 `/dev/ttyAMA0`.
3. Remove any `console=serial0,115200` from `/boot/firmware/cmdline.txt`.
4. Put the port in RotorHazard's `config.json`:

   ```json
   { "GENERAL": { "SERIAL_PORTS": ["/dev/ttyAMA0"] } }
   ```

5. Do not select the `S32_BPill` board type: it tries 921600 first. The default
   non-S32 type tries 115200 first, which is what this link uses.

USB stays usable while the Pi is connected. The web UI can read the state and
use RotorHazard mode's commands, but `PROTOCOL MONITOR` is refused with
`ERR uart_rh` while the Pi has sent anything in the last 2 s. After 2 s of
silence it switches to monitor mode; the Pi's next byte takes the radio back.
The reverse also holds: a monitor session started before the Pi connects ends
at the Pi's first byte.

The ROM prints its boot banner on GP11 at power-up. RotorHazard rejects those
bytes on the checksum and retries its probe; do not reset the board during a
race. Only the 5.8 GHz band is supported, as over USB. A second module on a
second serial port is how to run more than 8 pilots.
```

- [ ] **Step 2: `CLAUDE.md` 的三处**

RotorHazard 模式的引言：

```markdown
The board answers RotorHazard's serial node protocol (node API level 36) as one
multi-node processor with 8 seats, like an S32_BPill board with 8 receivers.
RotorHazard's server needs no changes: add the port to `GENERAL` →
`SERIAL_PORTS` in its config (e.g. `"SERIAL_PORTS": ["/dev/ttyACM0"]`).
```

改成：

```markdown
The board answers RotorHazard's serial node protocol (node API level 36) as one
multi-node processor with 8 seats, like an S32_BPill board with 8 receivers.
RotorHazard's server needs no changes: add the port to `GENERAL` →
`SERIAL_PORTS` in its config. The port can be USB (`/dev/ttyACM0`) or the
C5-Zero's UART0 on GP11 (TX) / GP12 (RX), 115200 8N1 (`src/proto/uart_link.c`;
wiring and Pi setup in docs/RotorHazard.md).
```

RotorHazard 模式的列表里，在 “**Firmware:**” 那条之后加一条：

```markdown
- **Two ports:** USB and the UART have separate line and write state and each
  port's replies go back to it; seats and levels are shared. The Pi's first
  byte switches the radio to RotorHazard with the saved RH settings, ending a
  USB monitor session. While the UART has had traffic in the last 2 s, USB's
  `PROTOCOL MONITOR` answers `ERR uart_rh` (the UART itself answers
  `ERR uart_port`); after 2 s of silence the web UI can take the radio back.
  Frames and telemetry never go to the UART.
```

GPIO 命令表格行里的：

```markdown
`GPIO?` answers `GPIO {"count":N,"active":"…","saved":"…"}`, one character per GPIO: `-` unused, `L`, `H`, `x` not available (USB, and the flash pins IDF reserves).
```

改成：

```markdown
`GPIO?` answers `GPIO {"count":N,"active":"…","saved":"…"}`, one character per GPIO: `-` unused, `L`, `H`, `x` not available (USB, the C5's UART pins 11/12, and the flash pins IDF reserves).
```

- [ ] **Step 3: 提交**

```bash
cd ~/workspace/FPV_space/widscan58-clone/repo
git add docs/RotorHazard.md CLAUDE.md
git diff --cached --stat
git commit -m "$(cat <<'EOF'
docs: wire the Pi to the board over UART

Wiring (GP11/GP12, 3.3 V), raspi-config/disable-bt, /dev/ttyAMA0 in
SERIAL_PORTS, and how USB and the Pi share the radio: the UART holds it
for 2 s after its last byte.
EOF
)"
```

Expected: 2 个文件。

---

### Task 7: 验证（硬件部分需要板子和 Pi）

**Files:** 无（除非发现缺陷）

- [ ] **Step 1: 两个目标编译 + 主机测试**

```bash
cd ~/workspace/FPV_space/widscan58-clone/repo/firmware
pio run -e esp32c5
pio run -e esp32s3
pio test -e native
```

Expected: `SUCCESS` / `SUCCESS` / 所有测试通过。

- [ ] **Step 2: USB 基线（先确认没回归）**

关闭网页端、Chrome、其它占用串口的进程（本机已知 Chrome 会占住 `/dev/ttyACM0`），然后：

```bash
cd ~/workspace/FPV_space/widscan58-clone/repo/firmware
pio run -e esp32c5 -t upload
cd ..
python3 tools/rh_check.py -p /dev/ttyACM0 --seats 8
```

Expected：`discovered 8 nodes in …`，每个 node 的 `api 36`；结尾的 error report 里 communication errors 为 0。

- [ ] **Step 3: 没有 Pi 时用 USB-UART 适配器**

3.3 V 适配器：TX→GP12，RX→GP11，GND↔GND（不要接适配器的 5 V）。然后：

```bash
python3 tools/rh_check.py -p /dev/ttyUSB0 --seats 8
```

（macOS 上是 `/dev/cu.usbserial-*`。）

Expected：同上，8 个节点、api 36、无通信错误。若连不上：确认适配器是 3.3 V 电平、TX/RX 交叉、`INFO` 能在 `miniterm` 里得到回复。

- [ ] **Step 4: 树莓派手工连通**

Pi 上按文档配置好后：

```bash
python3 -m serial.tools.miniterm /dev/ttyAMA0 115200
```

依次输入 `INFO`、`PROTOCOL?`、`CONFIG?`（每行回车）。

Expected：

- `INFO widscan58|esp32c5|1|…`；
- `PROTOCOL RH`；
- 一行 `CONFIG {...}`（可能很长）。

`PROTOCOL MONITOR` 应回 `ERR uart_port`。

- [ ] **Step 5: Pi 上跑 RotorHazard 自己的接口代码**

Pi 上准备 RotorHazard 源码（`tools/rh_check.py` 不复制它，只调用它）：

```bash
git clone https://github.com/RotorHazard/RotorHazard ref/RotorHazard
python3 -m venv /tmp/rhvenv
/tmp/rhvenv/bin/pip install gevent pyserial
/tmp/rhvenv/bin/python tools/rh_check.py -p /dev/ttyAMA0 --seats 8
```

Expected：`discovered 8 nodes`、`api 36`、`set node 1..8: ok`、结尾无通信错误。

- [ ] **Step 6: 真机 RotorHazard**

`config.json` 的 `SERIAL_PORTS` 指向 `/dev/ttyAMA0`，重启服务端。

Expected：服务端日志出现 `Serial multi-node found ... count=8 ... baudrate=115200`；在 RotorHazard 里给 8 个座位设 R1–R8，VTX 过门能记圈。

- [ ] **Step 7: 共存与异常**

1. Pi 正在轮询时，USB 接网页端：能读 `CONFIG?`；`PROTOCOL MONITOR` 回 `ERR uart_rh`。
2. 停掉 RotorHazard，等待 > 2 s，网页端再切 MONITOR：`OK`，频谱恢复；Pi 下一次探针（≤ 2 s）把射频拿回去，网页端帧流停止。
3. 运行中拔掉 UART、重启 Pi、复位 C5：模块都不崩，Pi 重启后重新发现 8 节点。
4. 收发过程中在 Pi 上 `Ctrl-C` 一个半截写命令：100 ms 后该端口的写状态自动丢弃，后续命令正常。
5. `CONFIG?` 的 `heap_free` 与遥测里的 `drops`：空闲时应与改动前同量级（UART 新增约 22 KB 堆：RX/TX 各 4 KB、输出队列 4 KB、写任务栈 4 KB、读任务栈 6 KB）。

- [ ] **Step 8: 记录结果**

把实测的发现/速率/异常写入 `docs/RotorHazard.md`（如果与现有表格不同）并单独提交；没有差异则不提交。

---

## 自检

**规格覆盖**

| 规格条目 | 任务 |
|---|---|
| 双 sink 传输层（§4.1） | Task 1 |
| UART0 驱动、115200、GP12 上拉（§3.1、§4.1） | Task 2 |
| `esp_driver_uart`、源文件、GPIO 排除（§4.4） | Task 2 |
| 每端口命令状态、回复原路、异步 sink（§4.2） | Task 4、Task 5 |
| RH 每端口写状态/座位（§4.3） | Task 3 |
| 射频归属、2 s 规则、`ERR uart_rh`（§5） | Task 5 |
| 5.8 全频段与 8 座位（§6） | 无需改射频；Step 2/5 回归 |
| 错误处理（§7） | Task 2（上拉、GPIO）、Task 3（写超时）、Task 5（仲裁） |
| 内存预算（§8） | 全部新状态在堆；Task 2 Step 6 的链接检查 |
| 验证计划（§9） | Task 7 |
| 文档（`docs/RotorHazard.md`、`CLAUDE.md`） | Task 6 |

**占位符扫描：** 无 TBD/TODO/“类似 Task N”；每个改代码的步骤都有最终代码。

**类型一致性：** `link_sink_t`（`LINK_USB`/`LINK_UART`/`LINK_COUNT`）、`link_text_to`、`link_printf_to`、`link_raw_to`、`link_read_from`、`link_sink_start`、`link_ready`、`rh_idle(link_sink_t)`、`rh_rx(link_sink_t, uint8_t, int64_t)`、`rh_poll(int64_t)`、`scan_request_t.sink` 在各任务中同名同型。

**已知边界：** UART 上能用的命令与 USB 的 RotorHazard 模式完全相同（`rh_allowed` + `rh_text_start`）；`STATUS?` 首字母 `S` 不在 `A`–`P`/`T` 内，因此在两种端口上原本都不可用，本计划不改变这一点。
