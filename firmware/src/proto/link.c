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
#if RX_WIFI_RELAY
#include "ws_link.h"
#endif

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
static volatile link_sink_t frame_sink = LINK_USB; /* where frames go (link_set_frame_sink) */

/* Largest frame we may have to build for the UART: SPECTRUM with 1024 bins.
 * RAWIQ never goes there (its chunks exceed the TX ring anyway). */
#define FRAME_UART_MAX (sizeof(frame_header_t) + sizeof(frame_spectrum_t) + 1024 + 4)
static uint8_t uart_frame_buf[FRAME_UART_MAX];

/* Only the queued sinks use link_task: USB because its driver drops bytes when
 * no host reads the FIFO, WS because the socket must not be written from the
 * scanner's core. The UART writes straight to the driver (link_raw_to) — an
 * item that would need a queue would also need heap the C5 does not have. */
static void link_task(void *arg) {
    const link_sink_t sink = (link_sink_t)(intptr_t)arg;
    const RingbufHandle_t q = queues[sink];
    for (;;) {
        size_t size;
        uint8_t *item = xRingbufferReceive(q, &size, portMAX_DELAY);
        if (!item) continue;
#if RX_WIFI_RELAY
        if (sink == LINK_WS) {
            /* A relay with no uplink keeps nothing: that is what "bounded
             * queue" means here. One item is one frame — the bridge only
             * delivers binary messages, and slicing a queue item across two
             * would split a frame's bytes. ws_link_write reports -1 when the
             * socket is gone, which is the same outcome as being down. */
            if (!ws_link_up() || ws_link_write(item, size) < 0) drops++;
            vRingbufferReturnItem(q, item);
            continue;
        }
#endif
        size_t done = 0;
        while (done < size) {
            size_t chunk = size - done > LINK_TX_CHUNK ? LINK_TX_CHUNK : size - done;
            int n = usb_serial_jtag_write_bytes(item + done, chunk, pdMS_TO_TICKS(LINK_WRITE_TIMEOUT_MS));
            if (n <= 0) break;
            done += (size_t)n;
        }
        if (done < size) drops++;
        vRingbufferReturnItem(q, item);
    }
}

void link_sink_start(size_t queue_bytes) {
    queues[LINK_USB] = xRingbufferCreate(queue_bytes, RINGBUF_TYPE_NOSPLIT);
    if (!queues[LINK_USB]) return;
    xTaskCreatePinnedToCore(link_task, "link", 4096, (void *)(intptr_t)LINK_USB, 5, NULL, portNUM_PROCESSORS - 1);
#if RX_WIFI_RELAY
    /* The relay's uplink queue: 32 KiB is about a minute of LEVELS frames, and
     * past that the writer drops the oldest rather than growing. */
    queues[LINK_WS] = xRingbufferCreate(32 * 1024, RINGBUF_TYPE_NOSPLIT);
    if (queues[LINK_WS])
        xTaskCreatePinnedToCore(link_task, "link_ws", 4096, (void *)(intptr_t)LINK_WS, 5, NULL, portNUM_PROCESSORS - 1);
#endif
}

bool link_ready(link_sink_t sink) {
    if (sink == LINK_UART) return uart_link_ready();
    return (unsigned)sink < LINK_COUNT && queues[sink];
}

void link_init(void) {
    usb_serial_jtag_driver_config_t cfg = {.tx_buffer_size = LINK_USB_TX_BYTES, .rx_buffer_size = 1024};
    ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&cfg));
    link_sink_start(LINK_QUEUE_BYTES);
    uart_link_init(); /* no sink where the target has no UART link */
}

size_t link_capacity(void) { return xRingbufferGetMaxItemSize(queues[LINK_USB]); }
uint32_t link_drops(void) { return drops; }
unsigned link_fill_permille_to(link_sink_t sink) {
    /* A no-split ring reports at most its largest possible item as free. */
    RingbufHandle_t q = (unsigned)sink < LINK_COUNT ? queues[sink] : NULL;
    if (!q) return 0;
    size_t free = xRingbufferGetCurFreeSize(q), max = xRingbufferGetMaxItemSize(q);
    return free >= max ? 0 : (unsigned)(1000 - free * 1000 / max);
}
unsigned link_fill_permille(void) { return link_fill_permille_to(LINK_USB); }

static IRAM_ATTR bool frame_put(uint8_t type, const void *a, size_t alen, const void *b, size_t blen, size_t min_free,
                      TickType_t wait) {
    if (!streaming) return false; /* not a drop: nobody is listening for frames */
    size_t len = alen + blen;
    size_t total = sizeof(frame_header_t) + len + 4;
    if (len > 0xffff) {
        drops++;
        return false;
    }
    if (frame_sink == LINK_UART && type != FRAME_RAWIQ) {
        /* No queue here (the C5 heap cannot afford one): uart_link_write
         * blocks only as long as the line takes to drain, so the caller's
         * pacing (scanner uart_pace) is the backpressure. */
        if (total > sizeof(uart_frame_buf)) {
            drops++;
            return false;
        }
        uint8_t *p = uart_frame_buf;
        frame_header_t h = {FRAME_SYNC0, FRAME_SYNC1, type, PROTO_VERSION, (uint16_t)len};
        memcpy(p, &h, sizeof(h));
        if (alen) memcpy(p + sizeof(h), a, alen);
        if (blen) memcpy(p + sizeof(h) + alen, b, blen);
        uint32_t crc = esp_rom_crc32_le(0, p, sizeof(h) + len);
        memcpy(p + sizeof(h) + len, &crc, 4);
        return link_raw_to(LINK_UART, p, total);
    }
    RingbufHandle_t q = queues[link_frame_queue(type, frame_sink)];
    if (!q) {
        drops++;
        return false; /* a sink that was never started */
    }
    if (min_free && xRingbufferGetCurFreeSize(q) < total + min_free) {
        drops++;
        return false;
    }
    void *slot;
    if (xRingbufferSendAcquire(q, &slot, total, wait) != pdTRUE) {
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
    xRingbufferSendComplete(q, slot);
    return true;
}

IRAM_ATTR bool link_frame(uint8_t type, const void *a, size_t alen, const void *b, size_t blen, size_t min_free) {
    return frame_put(type, a, alen, b, blen, min_free, 0);
}

bool link_frame_wait(uint8_t type, const void *a, size_t alen, const void *b, size_t blen, uint32_t wait_ms) {
    return frame_put(type, a, alen, b, blen, 0, pdMS_TO_TICKS(wait_ms));
}

bool link_raw_to(link_sink_t sink, const void *data, size_t n) {
    if (sink == LINK_UART) {
        if (uart_link_write(data, n) != (int)n) {
            drops++;
            return false;
        }
        return true;
    }
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

void link_set_frame_sink(link_sink_t sink) {
    if ((unsigned)sink < LINK_COUNT) frame_sink = sink;
}
link_sink_t link_frame_sink(void) { return frame_sink; }

void link_flush(void) {
    /* Every queued sink: stale bytes must not reach the uplink after a
     * reconnect either. A sink that was never started has nothing to drain. */
    for (unsigned s = 0; s < LINK_COUNT; s++) {
        if (s == LINK_UART || !queues[s]) continue;
        size_t size;
        void *item;
        while ((item = xRingbufferReceive(queues[s], &size, 0)) != NULL) vRingbufferReturnItem(queues[s], item);
    }
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
#if RX_WIFI_RELAY
    if (sink == LINK_WS) return ws_link_read(buf, capacity, timeout_ms);
#endif
    return usb_serial_jtag_read_bytes(buf, capacity, timeout_ms == UINT32_MAX ? portMAX_DELAY : pdMS_TO_TICKS(timeout_ms));
}

int link_read(uint8_t *buf, size_t capacity, uint32_t timeout_ms) {
    return link_read_from(LINK_USB, buf, capacity, timeout_ms);
}
