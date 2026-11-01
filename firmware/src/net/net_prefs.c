/* GPL-3.0-or-later. */
#include <string.h>
#include "net_prefs.h"
#include "nvs.h"

#define NS "net"

/* An empty field means "no value", so it is stored as a missing key: the
 * passphrase then never sits in flash as an empty string that Wi-Fi would
 * try to associate with. */
static esp_err_t put_str(nvs_handle_t h, const char *key, const char *v) {
    if (v[0]) return nvs_set_str(h, key, v);
    esp_err_t e = nvs_erase_key(h, key);
    return e == ESP_ERR_NVS_NOT_FOUND ? ESP_OK : e;
}

static void get_str(nvs_handle_t h, const char *key, char *out, size_t cap) {
    out[0] = 0;
    size_t n = cap;
    if (nvs_get_str(h, key, out, &n) != ESP_OK) out[0] = 0;
}

esp_err_t net_prefs_load(net_prefs_t *p) {
    memset(p, 0, sizeof(*p));
    nvs_handle_t h;
    esp_err_t e = nvs_open(NS, NVS_READONLY, &h);
    if (e == ESP_ERR_NVS_NOT_FOUND) return ESP_OK; /* never configured: off */
    if (e != ESP_OK) return e;
    get_str(h, "ssid", p->ssid, sizeof(p->ssid));
    get_str(h, "pass", p->pass, sizeof(p->pass));
    get_str(h, "url", p->url, sizeof(p->url));
    get_str(h, "id", p->id, sizeof(p->id));
    uint8_t on = 0;
    if (nvs_get_u8(h, "on", &on) != ESP_OK) on = 0;
    p->on = on != 0;
    nvs_close(h);
    return ESP_OK;
}

esp_err_t net_prefs_store(const net_prefs_t *p) {
    nvs_handle_t h;
    esp_err_t e = nvs_open(NS, NVS_READWRITE, &h);
    if (e != ESP_OK) return e;
    e = put_str(h, "ssid", p->ssid);
    if (e == ESP_OK) e = put_str(h, "pass", p->pass);
    if (e == ESP_OK) e = put_str(h, "url", p->url);
    if (e == ESP_OK) e = put_str(h, "id", p->id);
    if (e == ESP_OK) e = nvs_set_u8(h, "on", p->on ? 1 : 0);
    if (e == ESP_OK) e = nvs_commit(h);
    nvs_close(h);
    return e;
}

/* Per-key, not nvs_flash_erase_partition(): the latter would also take
 * CONFIG SAVE's lap-timer blob and the GPIO settings with it. NET RESET has
 * no business clearing the user's other settings. */
esp_err_t net_prefs_erase(void) {
    nvs_handle_t h;
    esp_err_t e = nvs_open(NS, NVS_READWRITE, &h);
    if (e == ESP_ERR_NVS_NOT_FOUND) return ESP_OK;
    if (e != ESP_OK) return e;
    static const char *keys[] = {"ssid", "pass", "url", "id", "on"};
    for (unsigned i = 0; i < sizeof(keys) / sizeof(keys[0]); i++) {
        e = nvs_erase_key(h, keys[i]);
        if (e == ESP_ERR_NVS_NOT_FOUND) e = ESP_OK;
        if (e != ESP_OK) break;
    }
    if (e == ESP_OK) e = nvs_commit(h);
    nvs_close(h);
    return e;
}

/* "off" erases rather than storing 0: net_prefs_load() already reads a missing
 * key as off, and put_str() keeps no empty values in the namespace either. */
esp_err_t net_prefs_set_on(bool on) {
    nvs_handle_t h;
    esp_err_t e = nvs_open(NS, NVS_READWRITE, &h);
    if (e != ESP_OK) return e;
    e = on ? nvs_set_u8(h, "on", 1) : nvs_erase_key(h, "on");
    if (e == ESP_ERR_NVS_NOT_FOUND) e = ESP_OK;
    if (e == ESP_OK) e = nvs_commit(h);
    nvs_close(h);
    return e;
}
