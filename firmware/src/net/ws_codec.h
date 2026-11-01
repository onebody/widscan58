/* RFC6455 framing for the relay's ws:// uplink: pure byte maths, no sockets,
 * no IDF, so pio test -e native can drive it. GPL-3.0-or-later. */
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define WS_OP_CONT 0x0
#define WS_OP_TEXT 0x1
#define WS_OP_BIN 0x2
#define WS_OP_CLOSE 0x8
#define WS_OP_PING 0x9
#define WS_OP_PONG 0xA
/* serve.mjs sends spectra-sized frames; 64 KiB is far above our largest
 * frame, and anything bigger is a broken peer we must not trust. */
#define WS_MAX_FRAME 65536

#define WS_OK 0
#define WS_NEED_MORE 1
#define WS_BAD (-1)

void ws_sha1(const uint8_t *msg, size_t n, uint8_t out[20]);
/* Standard base64. Writes NUL. Returns chars written, 0 if cap too small. */
size_t ws_b64(const uint8_t *in, size_t n, char *out, size_t cap);
/* Sec-WebSocket-Accept for a client key: 28 chars + NUL. Empty on overflow. */
void ws_accept_key(const char *client_key, char out[29]);

/* One client->server frame, always masked (RFC 6455 5.3). Returns total
 * bytes, 0 if cap too small. */
size_t ws_write_frame(uint8_t *out, size_t cap, uint8_t opcode, const void *payload, size_t len, uint32_t mask);

typedef struct {
    int err;       /* WS_OK / WS_NEED_MORE / WS_BAD */
    uint8_t opcode;
    bool fin;
    size_t header; /* bytes before the payload */
    size_t len;    /* payload bytes */
} ws_head_t;
/* A server->client frame header: must be unmasked. */
ws_head_t ws_read_head(const uint8_t *buf, size_t have);

/* ws://host:port/path only. wss:// is refused on purpose: the relay is a
 * LAN device (spec section 7). Returns false and sets *why if it can't. */
bool ws_parse_url(const char *url, char *host, size_t host_cap, uint16_t *port, char *path, size_t path_cap,
                  const char **why);

/* A handshake response the relay has read whole. Folding every CR and LF to
 * NUL turns it into NUL-separated pieces: the status line, then one per
 * header, then an empty piece for the blank line that ends it. */
void ws_fold_response(char *resp, size_t len);
/* The value of a header, name matched case-insensitively, first match wins.
 * Returns false if no piece is that header. The name and the colon are read
 * only within the piece, so "Connection: Upgrade" never answers "Upgrade". */
bool ws_header_value(const char *resp, size_t len, const char *name, char *out, size_t out_cap);
