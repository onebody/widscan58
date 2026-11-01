/* Host tests for the relay's RFC6455 maths. pio test -e native.
 * GPL-3.0-or-later. */
#include <stdio.h>
#include <string.h>
#include <unity.h>
#include "../../src/net/ws_codec.c"
#include "../../src/proto/link.h"

void setUp(void) {}
void tearDown(void) {}

/* The Unity vendored here predates the SIZE family (2.6) and its _MESSAGE
 * variants; test_dsp only uses the classic macros. Sizes fit an int. */
#define TEST_ASSERT_EQUAL_SIZE(exp, act) TEST_ASSERT_EQUAL_INT((int)(exp), (int)(act))
#define ASSERT_EQ_MSG(exp, act, msg)                                        \
    do {                                                                    \
        if ((int)(exp) != (int)(act)) {                                      \
            printf("want %d got %d (%s)\n", (int)(exp), (int)(act), (msg));   \
            TEST_FAIL();                                                      \
        }                                                                     \
    } while (0)

static void hex_eq(const uint8_t *d, const char *want) {
    char got[41];
    for (int i = 0; i < 20; i++) sprintf(got + 2 * i, "%02x", d[i]);
    got[40] = 0;
    TEST_ASSERT_EQUAL_STRING(want, got);
}

/* RFC 3174 / FIPS 180-1 vectors, and the RFC 6455 1.3 handshake example,
 * which transitively validates sha1 + base64 + the GUID. */
static void test_sha1_vectors(void) {
    uint8_t d[20];
    ws_sha1((const uint8_t *)"", 0, d);
    hex_eq(d, "da39a3ee5e6b4b0d3255bfef95601890afd80709");
    ws_sha1((const uint8_t *)"abc", 3, d);
    hex_eq(d, "a9993e364706816aba3e25717850c26c9cd0d89d");
    ws_sha1((const uint8_t *)"abcdefghijklmnopqrstuvwxyzabcdef", 32, d);
    hex_eq(d, "25742491563d63a978ba9baf43d33f7572d86ef4");
    ws_sha1((const uint8_t *)"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq", 56, d);
    hex_eq(d, "84983e441c3bd26ebaae4aa1f95129e5e54670f1");
}

static void test_b64_padding(void) {
    char out[8];
    TEST_ASSERT_EQUAL(4, ws_b64((const uint8_t *)"f", 1, out, sizeof(out)));
    TEST_ASSERT_EQUAL_STRING("Zg==", out);
    TEST_ASSERT_EQUAL(4, ws_b64((const uint8_t *)"fo", 2, out, sizeof(out)));
    TEST_ASSERT_EQUAL_STRING("Zm8=", out);
    TEST_ASSERT_EQUAL(4, ws_b64((const uint8_t *)"foo", 3, out, sizeof(out)));
    TEST_ASSERT_EQUAL_STRING("Zm9v", out);
    TEST_ASSERT_EQUAL(0, ws_b64((const uint8_t *)"foo", 3, out, 4)); /* no room for NUL */
}

static void test_accept_key_rfc_example(void) {
    char out[29];
    ws_accept_key("dGhlIHNhbXBsZSBub25jZQ==", out);
    TEST_ASSERT_EQUAL_STRING("s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", out);
    ws_accept_key("", out); /* a peer that sends no key is not a handshake */
    TEST_ASSERT_EQUAL_STRING("", out);
}

/* A client->server frame, byte for byte, with a key we chose. Written out by
 * hand rather than round-tripped: the peer's frames carry no masking key, so
 * the two layouts differ by four bytes and cannot parse each other. */
static void test_write_frame_bytes(void) {
    uint8_t wire[224];
    const uint8_t payload[3] = {0x01, 0x02, 0x03};
    /* FIN|BIN, MASK|len 3, key 09 fa 11 32, then each byte XOR its key byte */
    const uint8_t want[9] = {0x82, 0x83, 0x09, 0xfa, 0x11, 0x32, 0x08, 0xf8, 0x12};
    TEST_ASSERT_EQUAL_SIZE(9, ws_write_frame(wire, sizeof(wire), WS_OP_BIN, payload, 3, 0x09fa1132));
    TEST_ASSERT_EQUAL_HEX8_ARRAY(want, wire, 9);
    /* A 125-byte payload still fits the one-byte length; 126 does not. */
    uint8_t big[200];
    memset(big, 0x5a, sizeof(big));
    size_t n = ws_write_frame(wire, sizeof(wire), WS_OP_TEXT, big, 1, 0x01020304);
    TEST_ASSERT_EQUAL_SIZE(2 + 4 + 1, n);
    TEST_ASSERT_EQUAL_HEX8(0x81, wire[1]);
    n = ws_write_frame(wire, sizeof(wire), WS_OP_TEXT, big, 126, 0x01020304);
    TEST_ASSERT_EQUAL_SIZE(4 + 4 + 126, n);
    TEST_ASSERT_EQUAL_HEX8(0xFE, wire[1]);
    TEST_ASSERT_EQUAL_HEX8(0x00, wire[2]);
    TEST_ASSERT_EQUAL_HEX8(0x7E, wire[3]);
}

/* Where the payload actually lands, for each length-field width. */
static void test_write_frame_mask_offsets(void) {
    static uint8_t big[WS_MAX_FRAME];
    static uint8_t wire[WS_MAX_FRAME + 16];
    for (size_t j = 0; j < sizeof(big); j++) big[j] = (uint8_t)(j * 7 + 3);
    const size_t lens[] = {0, 1, 125, 126, 200, 65535, WS_MAX_FRAME};
    const size_t hdrs[] = {2, 2, 2, 4, 4, 4, 10};
    const uint8_t key[4] = {0x37, 0xfa, 0x21, 0x53};
    for (unsigned k = 0; k < sizeof(lens) / sizeof(lens[0]); k++) {
        size_t len = lens[k];
        char msg[32];
        snprintf(msg, sizeof(msg), "len=%zu", len);
        size_t n = ws_write_frame(wire, sizeof(wire), WS_OP_BIN, big, len, 0x37fa2153);
        ASSERT_EQ_MSG(hdrs[k] + 4 + len, n, msg);
        ASSERT_EQ_MSG(0x82, wire[0], msg);
        ASSERT_EQ_MSG(0x80 | (len <= 125 ? len : (len < 65536 ? 126 : 127)), wire[1], msg);
        for (int j = 0; j < 4; j++) ASSERT_EQ_MSG(key[j], wire[hdrs[k] + j], msg);
        for (size_t j = 0; j < len; j++) ASSERT_EQ_MSG(big[j] ^ key[j & 3], wire[hdrs[k] + 4 + j], msg);
    }
}

static void test_write_frame_rejects_small_cap(void) {
    uint8_t payload[4] = {1, 2, 3, 4}, wire[16];
    TEST_ASSERT_EQUAL_SIZE(0, ws_write_frame(wire, 8, WS_OP_BIN, payload, 4, 1)); /* 2+4+4 needed */
    TEST_ASSERT_EQUAL_SIZE(10, ws_write_frame(wire, 10, WS_OP_BIN, payload, 4, 1));
}

/* A peer (server) frame header: unmasked, and the three length widths. */
static void test_read_head_peer_frames(void) {
    uint8_t small[2] = {0x82, 0x03};
    ws_head_t h = ws_read_head(small, sizeof(small));
    TEST_ASSERT_EQUAL(WS_OK, h.err);
    TEST_ASSERT_TRUE(h.fin);
    TEST_ASSERT_EQUAL(WS_OP_BIN, h.opcode);
    TEST_ASSERT_EQUAL_SIZE(2, h.header);
    TEST_ASSERT_EQUAL_SIZE(3, h.len);

    uint8_t medium[4] = {0x81, 0x7E, 0x01, 0x2C}; /* 300 */
    h = ws_read_head(medium, sizeof(medium));
    TEST_ASSERT_EQUAL(WS_OK, h.err);
    TEST_ASSERT_EQUAL_SIZE(4, h.header);
    TEST_ASSERT_EQUAL_SIZE(300, h.len);

    uint8_t wide[10] = {0x82, 0x7F, 0, 0, 0, 0, 0, 1, 0, 0}; /* 65536: our limit, not over it */
    h = ws_read_head(wide, sizeof(wide));
    TEST_ASSERT_EQUAL(WS_OK, h.err);
    TEST_ASSERT_EQUAL_SIZE(10, h.header);
    TEST_ASSERT_EQUAL_SIZE(WS_MAX_FRAME, h.len);

    uint8_t over[10] = {0x82, 0x7F, 0, 0, 0, 0, 0, 1, 0, 1}; /* 65537 */
    TEST_ASSERT_EQUAL(WS_BAD, ws_read_head(over, sizeof(over)).err);

    uint8_t masked[2] = {0x82, 0x83}; /* peers must not mask */
    TEST_ASSERT_EQUAL(WS_BAD, ws_read_head(masked, sizeof(masked)).err);
}

static void test_head_truncation(void) {
    uint8_t two[2] = {0x82, 0x7E}; /* 126 follows but does not */
    TEST_ASSERT_EQUAL(WS_NEED_MORE, ws_read_head(two, 2).err);
    uint8_t four[4] = {0x82, 0x7E, 0x01, 0x2C};
    TEST_ASSERT_EQUAL(WS_NEED_MORE, ws_read_head(four, 3).err);
    TEST_ASSERT_EQUAL(WS_OK, ws_read_head(four, 4).err);
    uint8_t nine[9] = {0x82, 0x7F, 0, 0, 0, 0, 0, 1, 0}; /* 10 needed */
    TEST_ASSERT_EQUAL(WS_NEED_MORE, ws_read_head(nine, sizeof(nine)).err);
    uint8_t one[1] = {0x82};
    TEST_ASSERT_EQUAL(WS_NEED_MORE, ws_read_head(one, 1).err);
}

static void test_parse_url(void) {
    char host[64], path[96];
    uint16_t port;
    const char *why;
    TEST_ASSERT_TRUE(ws_parse_url("ws://192.168.99.7:8011/node?id=s3-relay", host, sizeof(host), &port, path,
                                  sizeof(path), &why));
    TEST_ASSERT_EQUAL_STRING("192.168.99.7", host);
    TEST_ASSERT_EQUAL_UINT16(8011, port);
    TEST_ASSERT_EQUAL_STRING("/node?id=s3-relay", path);
    TEST_ASSERT_TRUE(ws_parse_url("ws://bridge.lan/local", host, sizeof(host), &port, path, sizeof(path), &why));
    TEST_ASSERT_EQUAL_UINT16(80, port);
    TEST_ASSERT_EQUAL_STRING("/local", path);
    /* every rejection below must say why, not just fail */
    TEST_ASSERT_FALSE(ws_parse_url("wss://h/node", host, sizeof(host), &port, path, sizeof(path), &why));
    TEST_ASSERT_EQUAL_STRING("wss_unsupported", why);
    TEST_ASSERT_FALSE(ws_parse_url("http://h/node", host, sizeof(host), &port, path, sizeof(path), &why));
    TEST_ASSERT_EQUAL_STRING("scheme", why);
    TEST_ASSERT_FALSE(ws_parse_url("ws:///node", host, sizeof(host), &port, path, sizeof(path), &why));
    TEST_ASSERT_EQUAL_STRING("empty_host", why);
    TEST_ASSERT_FALSE(ws_parse_url("ws://h:0/node", host, sizeof(host), &port, path, sizeof(path), &why));
    TEST_ASSERT_EQUAL_STRING("port", why);
    TEST_ASSERT_FALSE(ws_parse_url("ws://h:99999/node", host, sizeof(host), &port, path, sizeof(path), &why));
    TEST_ASSERT_EQUAL_STRING("port", why);
    TEST_ASSERT_FALSE(ws_parse_url("ws://h:8011x/node", host, sizeof(host), &port, path, sizeof(path), &why));
    TEST_ASSERT_EQUAL_STRING("port", why);
    TEST_ASSERT_FALSE(ws_parse_url("ws://h", host, sizeof(host), &port, path, sizeof(path), &why));
    TEST_ASSERT_EQUAL_STRING("path", why);
    TEST_ASSERT_FALSE(ws_parse_url("ws://averyveryveryveryveryveryveryveryveryveryveryverylonghost.example.com:8011/node",
                                   host, 8, &port, path, sizeof(path), &why));
    TEST_ASSERT_EQUAL_STRING("host_length", why);
    /* "/node" is five chars: cap 6 fits it exactly, cap 5 does not. */
    TEST_ASSERT_FALSE(ws_parse_url("ws://h/node", host, sizeof(host), &port, path, 5, &why));
    TEST_ASSERT_EQUAL_STRING("path_length", why);
    TEST_ASSERT_TRUE(ws_parse_url("ws://h/node", host, sizeof(host), &port, path, 6, &why));
    TEST_ASSERT_EQUAL_STRING("/node", path);
}

/* Which queue a frame of this type goes to, for a sink that may be WS or UART.
 * link.c calls this same inline, so the test cannot drift from the firmware. */
_Static_assert(LINK_WS == 2 && LINK_COUNT == 3, "link.h sink order changed; update ws_link and these tests");

static void test_frame_queue(void) {
    TEST_ASSERT_EQUAL(LINK_WS, link_frame_queue(FRAME_LEVELS, LINK_WS));
    TEST_ASSERT_EQUAL(LINK_USB, link_frame_queue(FRAME_RAWIQ, LINK_WS));
    TEST_ASSERT_EQUAL(LINK_USB, link_frame_queue(FRAME_RAWIQ, LINK_USB));
    /* The UART's is not a queue at all: frame_put writes it straight out. */
    TEST_ASSERT_EQUAL(LINK_UART, link_frame_queue(FRAME_SPECTRUM, LINK_UART));
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_sha1_vectors);
    RUN_TEST(test_b64_padding);
    RUN_TEST(test_accept_key_rfc_example);
    RUN_TEST(test_write_frame_bytes);
    RUN_TEST(test_write_frame_mask_offsets);
    RUN_TEST(test_write_frame_rejects_small_cap);
    RUN_TEST(test_read_head_peer_frames);
    RUN_TEST(test_head_truncation);
    RUN_TEST(test_parse_url);
    RUN_TEST(test_frame_queue);
    return UNITY_END();
}
