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
#include "uart_link.h"

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
    uart_link_init(); /* no sink where the target has no UART link */
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
