/* Wi-Fi and uplink settings, NVS namespace "net" (the same store as
 * CONFIG SAVE's lap-timer blob, different keys). GPL-3.0-or-later. */
#pragma once
#include <stdbool.h>
#include "esp_err.h"

typedef struct {
    char ssid[33]; /* IEEE 802.11 caps it at 32 */
    char pass[65]; /* WPA2 passphrase caps at 63; 64 leaves a NUL and slack */
    char url[129]; /* ws://host:port/node?id=... */
    char id[33];   /* the name the bridge lists us under */
    bool on;
} net_prefs_t;

/* Reads the store; missing keys leave zeroed fields and on=false, so an
 * unconfigured relay behaves exactly like firmware without this feature. */
esp_err_t net_prefs_load(net_prefs_t *p);
esp_err_t net_prefs_store(const net_prefs_t *p);
/* Our five keys only - see the comment in net_prefs.c. */
esp_err_t net_prefs_erase(void);
/* The on/off switch alone, without the credentials: NET ON/OFF is an operator
 * intent that must survive a power cycle, while a typed passphrase stays in
 * RAM until NET SAVE. Storing the whole struct for a switch would write that
 * passphrase to flash behind SAVE's back. */
esp_err_t net_prefs_set_on(bool on);
