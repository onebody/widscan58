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
/* Create the USB sink's queue and writer task; uart_link.c owns the UART's
 * driver and writes straight to it. */
void link_sink_start(size_t queue_bytes);
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
