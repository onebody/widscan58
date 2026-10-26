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
