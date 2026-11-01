/* The relay's Wi-Fi client: associate with the AP, dial the bridge's /node
 * endpoint, and move bytes. One writer task (link.c's WS sink) and one reader
 * task own the socket; commands arrive through the receive ring.
 * GPL-3.0-or-later. */
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "net_prefs.h"

/* The inbound ring, without arming anything: commands.c starts a reader on
 * LINK_WS at boot, and reading a port with no ring would return at once and
 * spin that task. app_main calls this before it, idempotent. */
esp_err_t ws_link_open(void);
bool ws_link_start(const net_prefs_t *p); /* association + dial loop; idempotent */
bool ws_link_stop(void);                  /* down to the same state as NET OFF */
bool ws_link_sta_up(void);                /* AP associated */
bool ws_link_up(void);                    /* WS handshake completed */
int ws_link_write(const void *data, size_t n);  /* one binary frame, -1 if down */
int ws_link_read(uint8_t *buf, size_t cap, uint32_t timeout_ms); /* one inbound frame */
uint32_t ws_link_drops(void);             /* inbound frames we had to discard */
void ws_link_status(char *out, size_t cap);  /* for NET STATUS */
