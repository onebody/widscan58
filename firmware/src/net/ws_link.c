/* GPL-3.0-or-later. */
#include <errno.h>
#include <inttypes.h>
#include <lwip/netdb.h>
#include <netinet/in.h>
#include <string.h>
#include <strings.h>
#include <sys/socket.h>
#include <unistd.h>

#include "esp_event.h"
#include "esp_netif.h"
#include "esp_system.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/ringbuf.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "net_prefs.h"
#include "ws_codec.h"
#include "ws_link.h"

#define RX_RING_BYTES 1024 /* the inbound side carries ASCII commands only */
#define RX_MAX_ITEM 128    /* one frame is one command line: commands.c's line
                            * buffer holds 128, and its read buffer is the same
                            * size, so nothing handed over is ever truncated */
#define BACKOFF_MIN_MS 500
#define BACKOFF_MAX_MS 30000
#define RCV_TIMEOUT_MS 200 /* how often the serve loop re-checks the down flag */
#define HANDSHAKE_BUDGET_MS 4000

/* The socket has two tasks on it: the dial task reads and tears down,
 * link.c's writer task sends. One mutex around every send, close and reopen
 * is what keeps a stale fd number out of the writer's hands - after a close,
 * lwIP hands the same small number to the next socket, and a frame would then
 * land inside somebody else's handshake. */
static int sock = -1;
static SemaphoreHandle_t sock_mu;
static RingbufHandle_t rx_ring;
static TaskHandle_t dial_task;
static net_prefs_t cfg;
static bool netif_built;
/* join_ap() brought the driver up (mode, start, power save). Re-arming - NET
 * ON again, or with new credentials - must not run that a second time:
 * esp_wifi_start and esp_wifi_connect both answer an error when the station is
 * already where we want it, and a relay that reported ERR net_start at that
 * moment would be lying about a working link. */
static bool driver_up;
static uint8_t out_frame[WS_MAX_FRAME + 16];
static volatile bool sta_up;
static volatile bool ws_up;
static volatile bool want_up;
static volatile uint32_t rx_drops;
static uint32_t backoff_ms = BACKOFF_MIN_MS;
static char why[40]; /* the last thing that happened, for NET STATUS */

static void set_why(const char *text) {
    strlcpy(why, text, sizeof(why));
}

static void on_wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data) {
    (void)arg;
    (void)data;
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        sta_up = false;
        /* Reconnection is the driver's own job: a second retry loop here would
         * keep interrupting it. want_up only gates it so NET OFF stays off. */
        if (want_up) esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        sta_up = true;
    }
}

static bool join_ap(void) {
    if (!netif_built) {
        /* lwIP's TCP/IP task comes up with esp_netif_init, not with the netif
         * itself. Without it the station associates, DHCP sends its first
         * discover, and tcpip.c:257 asserts "Invalid mbox" - a reboot loop a
         * saved NET ON walks straight back into. Idempotent, so an image that
         * already ran it is unaffected. */
        if (esp_netif_init() != ESP_OK) return false;
        if (!esp_netif_create_default_wifi_sta()) return false;
        /* The relay image never runs main.c's Wi-Fi bring-up, so the driver
         * init belongs here: esp_wifi_set_mode fails without it. RAM storage
         * keeps the driver from writing a second copy of our credentials. */
        const wifi_init_config_t mic = WIFI_INIT_CONFIG_DEFAULT();
        if (esp_wifi_init(&mic) != ESP_OK) return false;
        ESP_ERROR_CHECK(
            esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_wifi_event, NULL, NULL));
        ESP_ERROR_CHECK(
            esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_wifi_event, NULL, NULL));
        netif_built = true;
    }
    /* RAM, not FLASH: our copy of the credentials is namespace "net", and the
     * driver would otherwise write a second one behind our back. */
    if (esp_wifi_set_storage(WIFI_STORAGE_RAM) != ESP_OK) return false;
    wifi_config_t wc = {0};
    strlcpy((char *)wc.sta.ssid, cfg.ssid, sizeof(wc.sta.ssid));
    strlcpy((char *)wc.sta.password, cfg.pass, sizeof(wc.sta.password));
    wc.sta.threshold.authmode = cfg.pass[0] ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;
    if (driver_up) {
        /* A re-arm goes down before it goes up again: set_config refuses while
         * a connection attempt is live (ESP_ERR_WIFI_STATE) and connect refuses
         * once one is established. */
        esp_wifi_disconnect();
    }
    if (esp_wifi_set_config(WIFI_IF_STA, &wc) != ESP_OK) return false;
    if (driver_up) return esp_wifi_connect() == ESP_OK;
    if (esp_wifi_set_mode(WIFI_MODE_STA) != ESP_OK) return false;
    if (esp_wifi_start() != ESP_OK) return false;
    /* Power save would sleep the station between frames, and a dial's command
     * round-trip would then ride the beacon-interval wake-up latency. */
    if (esp_wifi_set_ps(WIFI_PS_NONE) != ESP_OK) return false;
    driver_up = true;
    return esp_wifi_connect() == ESP_OK;
}

/* recv() with the socket's SO_RCVTIMEO: fills n bytes, or returns false if the
 * peer stalled past budget, closed or errored. Any progress resets the clock. */
static bool read_fill(int fd, uint8_t *buf, size_t n, uint32_t budget_ms) {
    size_t got = 0;
    uint32_t waited = 0;
    while (got < n) {
        ssize_t r = recv(fd, buf + got, n - got, 0);
        if (r > 0) {
            got += (size_t)r;
            waited = 0;
            continue;
        }
        if (r < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)) {
            waited += RCV_TIMEOUT_MS;
            if (waited > budget_ms) return false;
            continue;
        }
        return false;
    }
    return true;
}

static bool send_all(int fd, const uint8_t *buf, size_t n) {
    size_t sent = 0;
    while (sent < n) {
        ssize_t r = send(fd, buf + sent, n - sent, 0);
        if (r <= 0) {
            if (r < 0 && (errno == EINTR || errno == EAGAIN)) continue;
            return false;
        }
        sent += (size_t)r;
    }
    return true;
}

static bool dial_and_handshake(const char *host, uint16_t port, const char *path) {
    char port_text[8];
    snprintf(port_text, sizeof(port_text), "%u", port);
    struct addrinfo hints = {.ai_family = AF_INET, .ai_socktype = SOCK_STREAM}, *ai = NULL;
    if (getaddrinfo(host, port_text, &hints, &ai) != 0 || !ai) {
        set_why("dns");
        return false;
    }
    int fd = socket(ai->ai_family, ai->ai_socktype, ai->ai_protocol);
    if (fd < 0) {
        freeaddrinfo(ai);
        set_why("socket");
        return false;
    }
    struct timeval tv = {.tv_sec = RCV_TIMEOUT_MS / 1000, .tv_usec = (RCV_TIMEOUT_MS % 1000) * 1000};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    if (connect(fd, ai->ai_addr, ai->ai_addrlen) != 0) {
        close(fd);
        freeaddrinfo(ai);
        set_why("connect");
        return false;
    }
    freeaddrinfo(ai);

    uint8_t raw[16];
    char key[25], want[29];
    esp_fill_random(raw, sizeof(raw));
    ws_b64(raw, sizeof(raw), key, sizeof(key));
    ws_accept_key(key, want);
    char req[512];
    int n = snprintf(req, sizeof(req),
                     "GET %s HTTP/1.1\r\nHost: %s:%u\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n",
                     path, host, port, key);
    if (n <= 0 || (size_t)n >= sizeof(req)) {
        close(fd);
        set_why("url");
        return false;
    }
    xSemaphoreTake(sock_mu, portMAX_DELAY);
    sock = fd;
    bool ok = send_all(fd, (const uint8_t *)req, (size_t)n);
    xSemaphoreGive(sock_mu);
    if (!ok) {
        close(fd);
        sock = -1;
        set_why("send");
        return false;
    }

    /* The bridge answers the handshake before it decides whether it can serve
     * us (web/serve.mjs:368-390), so a 101 alone is not a link. Without these
     * checks a bad id or a replaced node would look "connected" and the relay
     * would sit in the node list producing zero frames - the false-alive the
     * spec forbids. */
    char resp[2048];
    size_t have = 0;
    uint32_t waited = 0;
    bool done = false;
    while (have + 1 < sizeof(resp) && waited <= HANDSHAKE_BUDGET_MS) {
        ssize_t r = recv(fd, resp + have, 1, 0);
        if (r == 1) {
            have++;
            resp[have] = 0; /* keep the string bounded for the header walk */
            waited = 0;
            if (have >= 4 && !memcmp(resp + have - 4, "\r\n\r\n", 4)) {
                done = true;
                break;
            }
            continue;
        }
        if (r < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)) {
            waited += RCV_TIMEOUT_MS;
            continue;
        }
        break;
    }
    if (!done) {
        close(fd);
        sock = -1;
        set_why(have ? "handshake_short" : "handshake");
        return false;
    }
    if (strncmp(resp, "HTTP/1.1 101", 12) != 0 && strncmp(resp, "HTTP/1.0 101", 12) != 0) {
        close(fd);
        sock = -1;
        set_why("not_101");
        return false;
    }
    ws_fold_response(resp, have);
    char value[64];
    if (!ws_header_value(resp, have, "Upgrade", value, sizeof(value)) || strcasecmp(value, "websocket") != 0) {
        close(fd);
        sock = -1;
        set_why("no_upgrade");
        return false;
    }
    if (!ws_header_value(resp, have, "Sec-WebSocket-Accept", value, sizeof(value)) || strcmp(value, want) != 0) {
        close(fd);
        sock = -1;
        set_why("bad_accept");
        return false;
    }
    return true;
}

static void take_down(const char *reason) {
    xSemaphoreTake(sock_mu, portMAX_DELAY);
    ws_up = false;
    int fd = sock;
    sock = -1;
    xSemaphoreGive(sock_mu);
    if (reason) set_why(reason);
    if (fd >= 0) close(fd);
}

/* Discard a frame we are not handing out, so the stream stays framed. */
static bool skip_payload(int fd, size_t len) {
    uint8_t scratch[128];
    while (len) {
        size_t chunk = len < sizeof(scratch) ? len : sizeof(scratch);
        if (!read_fill(fd, scratch, chunk, 2000)) return false;
        len -= chunk;
    }
    return true;
}

static void serve_until_down(void) {
    int fd = sock;
    uint8_t hdr[10];
    for (;;) {
        if (!ws_up || !want_up) {
            /* This task owns the fd, and a stop must be the one to end the
             * session: the bridge only drops the endpoint when the TCP closes,
             * and returning without closing leaves a node that is "online" with
             * nobody behind it. */
            take_down("stopped");
            return;
        }
        size_t have = 0;
        ws_head_t h = {0};
        for (;;) {
            ssize_t r = recv(fd, hdr + have, 1, 0);
            if (r == 1) {
                have++;
                h = ws_read_head(hdr, have);
                if (h.err != WS_NEED_MORE) break;
                continue;
            }
            if (r < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)) {
                if (have) { /* half a header, then silence: the stream is gone */
                    take_down("peer_stall");
                    return;
                }
                h.err = WS_NEED_MORE;
                break; /* idle: the outer loop re-checks want_up */
            }
            take_down(r == 0 ? "peer_closed" : "recv");
            return;
        }
        if (h.err == WS_BAD) {
            take_down("peer_frame");
            return;
        }
        if (h.err == WS_NEED_MORE) continue;
        if (h.opcode == WS_OP_PING) {
            if (h.len > 125) {
                take_down("ping_oversize");
                return;
            }
            uint8_t payload[125], pong[10 + 125];
            if (!read_fill(fd, payload, h.len, 2000)) {
                take_down("peer_stall");
                return;
            }
            uint32_t mask;
            esp_fill_random(&mask, sizeof(mask));
            size_t total = ws_write_frame(pong, sizeof(pong), WS_OP_PONG, payload, h.len, mask);
            xSemaphoreTake(sock_mu, portMAX_DELAY);
            bool sent = ws_up && total && send_all(sock, pong, total);
            xSemaphoreGive(sock_mu);
            if (!sent) {
                take_down("pong_failed");
                return;
            }
            continue;
        }
        if (h.opcode == WS_OP_CLOSE) {
            /* serve.mjs puts the reason in the close payload behind a 2-byte
             * status code: 1008 "bad-id", "replaced", "busy". Keep the text. */
            uint8_t raw[40];
            size_t take = h.len < sizeof(raw) - 1 ? h.len : sizeof(raw) - 1;
            if (take && !read_fill(fd, raw, take, 2000)) {
                take_down("peer_stall");
                return;
            }
            raw[take] = 0;
            if (h.len > take && !skip_payload(fd, h.len - take)) {
                take_down("peer_stall");
                return;
            }
            take_down(take > 2 ? (const char *)raw + 2 : (const char *)raw);
            return;
        }
        if (!h.fin || h.len > RX_MAX_ITEM) {
            rx_drops++;
            set_why(h.len > RX_MAX_ITEM ? "inbound_oversize" : "inbound_fragmented");
            if (!skip_payload(fd, h.len)) {
                take_down("peer_stall");
                return;
            }
            continue;
        }
        uint8_t item[RX_MAX_ITEM];
        if (!read_fill(fd, item, h.len, 2000)) {
            take_down("peer_stall");
            return;
        }
        /* Never block the socket on a slow command reader: drop instead. */
        if (xRingbufferSend(rx_ring, item, h.len, 0) != pdTRUE) rx_drops++;
    }
}

static void dial_task_fn(void *arg) {
    (void)arg;
    char host[64], path[128];
    uint16_t port = 80;
    const char *bad = NULL;
    for (;;) {
        while (!want_up) vTaskDelay(pdMS_TO_TICKS(50));
        if (!sta_up) {
            vTaskDelay(pdMS_TO_TICKS(200));
            continue;
        }
        if (!ws_parse_url(cfg.url, host, sizeof(host), &port, path, sizeof(path), &bad)) {
            /* A bad URL will not fix itself: stand down until NET SET. */
            set_why(bad ? bad : "url");
            want_up = false;
            continue;
        }
        if (!dial_and_handshake(host, port, path)) {
            vTaskDelay(pdMS_TO_TICKS(backoff_ms));
            backoff_ms = backoff_ms * 2 > BACKOFF_MAX_MS ? BACKOFF_MAX_MS : backoff_ms * 2;
            continue;
        }
        backoff_ms = BACKOFF_MIN_MS;
        ws_up = true;
        set_why("up");
        serve_until_down();
    }
}

esp_err_t ws_link_open(void) {
    if (!sock_mu) sock_mu = xSemaphoreCreateMutex();
    if (!rx_ring) rx_ring = xRingbufferCreate(RX_RING_BYTES, RINGBUF_TYPE_NOSPLIT);
    return sock_mu && rx_ring ? ESP_OK : ESP_ERR_NO_MEM;
}

bool ws_link_start(const net_prefs_t *p) {
    if (!p || !p->ssid[0] || !p->url[0]) return false;
    if (want_up && !memcmp(&cfg, p, sizeof(cfg))) return true; /* armed with these already */
    if (ws_link_open() != ESP_OK) return false;
    cfg = *p;
    if (!dial_task && xTaskCreate(dial_task_fn, "ws_dial", 6144, NULL, 5, &dial_task) != pdPASS) return false;
    if (!join_ap()) {
        set_why("wifi_start");
        return false;
    }
    want_up = true;
    return true;
}

bool ws_link_stop(void) {
    /* Order matters: the dial task has to close the TCP while the STA is still
     * up, or the FIN goes out over a dead interface and the bridge keeps the
     * endpoint registered forever. */
    want_up = false;
    for (int i = 0; i < 20 && ws_up; i++) vTaskDelay(pdMS_TO_TICKS(50));
    sta_up = false;
    esp_wifi_disconnect();
    return !ws_up;
}

bool ws_link_sta_up(void) {
    return sta_up;
}

bool ws_link_up(void) {
    return ws_up;
}

int ws_link_write(const void *data, size_t n) {
    if (!ws_up || !data) return -1;
    uint32_t mask;
    esp_fill_random(&mask, sizeof(mask));
    size_t total = ws_write_frame(out_frame, sizeof(out_frame), WS_OP_BIN, data, n, mask);
    if (!total) return -1;
    int rc = -1;
    xSemaphoreTake(sock_mu, portMAX_DELAY);
    if (ws_up && sock >= 0 && send_all(sock, out_frame, total)) rc = (int)n;
    else ws_up = false; /* broken session; the dial task re-dials */
    xSemaphoreGive(sock_mu);
    return rc;
}

int ws_link_read(uint8_t *buf, size_t cap, uint32_t timeout_ms) {
    if (!rx_ring || !buf || !cap) return 0;
    size_t size = 0;
    TickType_t wait = timeout_ms == UINT32_MAX ? portMAX_DELAY : pdMS_TO_TICKS(timeout_ms);
    uint8_t *item = xRingbufferReceive(rx_ring, &size, wait);
    if (!item) return 0;
    /* One item is one whole frame, so a cap below its size would drop the
     * tail - the caller's buffer is RX_MAX_ITEM for exactly that reason. The
     * clamp stays as the guard it is, counted rather than silent. */
    size_t copy = size < cap ? size : cap;
    memcpy(buf, item, copy);
    vRingbufferReturnItem(rx_ring, item);
    if (copy < size) rx_drops++;
    return (int)copy;
}

uint32_t ws_link_drops(void) {
    return rx_drops;
}

void ws_link_status(char *out, size_t cap) {
    wifi_ap_record_t ap;
    int rssi = esp_wifi_sta_get_ap_info(&ap) == ESP_OK ? ap.rssi : 0;
    /* A JSON fragment, not a kv line: NET STATUS hands this straight to the
     * platform, and every field there is a field. why is our own literal. */
    snprintf(out, cap, "\"sta\":%s,\"ws\":%s,\"rssi\":%d,\"backoff_ms\":%" PRIu32
                    ",\"in_drops\":%" PRIu32 ",\"why\":\"%s\"",
             sta_up ? "true" : "false", ws_up ? "true" : "false", rssi, backoff_ms, rx_drops, why);
}
