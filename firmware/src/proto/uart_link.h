/* RotorHazard over UART0 on the ESP32-C5-Zero header (GP11 TX, GP12 RX).
 * GPL-3.0-or-later. */
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Install UART0 at 115200 8N1. False where the target has no such link. */
bool uart_link_init(void);
/* True once uart_link_init installed the driver: the UART carries replies
 * directly (no queue), so this is all link_ready needs to know. */
bool uart_link_ready(void);
/* Blocks until the bytes reach the TX ring; the UART shifts them out at line
 * rate whether or not the Pi reads them, so this cannot hang. */
int uart_link_write(const void *data, size_t n);
int uart_link_read(uint8_t *buf, size_t capacity, uint32_t timeout_ms);
