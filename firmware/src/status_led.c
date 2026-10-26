/* The C5-Zero's WS2812 on GP27: dark until the scanner hears a signal on any
 * channel, then steady for the first second, blinking while it stays, and
 * dark again two seconds after the last one — a board with no host attached
 * still shows whether it is receiving anything. The XIAO puts a plain
 * active-low LED on GP27; leaving the line high-Z while dark (instead of the
 * RMT idle low) makes that LED follow the same pattern. GPL-3.0-or-later.
 *
 * A frame is 24 RMT symbols, one bit each (high 0.4 or 0.8 us, low for the
 * rest of 1.2 us at 10 MHz), so the peripheral keeps the timing: bit-banging
 * would hold interrupts off for 30 us in the middle of the scan loop. The
 * symbols are on the heap, as the C5's static RAM below the RF dump bank is
 * nearly full (sram_guard.ld). */
#include "status_led.h"
#include "sdkconfig.h"

#if CONFIG_IDF_TARGET_ESP32C5
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include "driver/gpio.h"
#include "driver/rmt_tx.h"
#include "esp_attr.h"
#include "esp_heap_caps.h"
#include "esp_rom_sys.h"
#include "esp_timer.h"
#include "hal/gpio_ll.h"
#include "soc/gpio_struct.h"
#include "soc/soc_caps.h"

#define LED_GPIO 27
#define LED_RES_HZ 10000000 /* 0.1 us ticks */
#define LED_TICKS 12        /* 1.2 us per bit */
#define LED_T0H 4           /* 0.4 us, a 0 bit */
#define LED_T1H 8           /* 0.8 us, a 1 bit */
#define LED_BITS 24
#define LED_LEVEL 128       /* green, of 255: readable in daylight */
#define POLL_MS 50
#define SOLID_MS 1000       /* the first second of a signal stays lit */
#define HOLD_MS 2000        /* no signal this long: dark */
#define BLINK_ON_MS 100
#define BLINK_PERIOD_MS 400

static rmt_channel_handle_t chan;
static rmt_encoder_handle_t enc;
static rmt_symbol_word_t *frame;
static esp_timer_handle_t ticker;
/* Written by the scanner task, read by the timer task: one aligned 32-bit
 * store is atomic. Milliseconds since boot, so a difference wraps in 49 days. */
static volatile uint32_t heard_ms;
static bool active, lit;
static uint32_t start_ms;

/* Drive the line only while lit: high-Z with a pull-down otherwise. That keeps
 * the WS2812's black frame latched (and a plain LED on the same pin off)
 * without depending on the RMT idle level. The LL output-enable writes only
 * leave the RMT route in the GPIO matrix alone, unlike gpio_set_direction(),
 * which would switch the pin back to plain GPIO. */
static void output_enable(bool on) {
    if (on) {
        gpio_set_pull_mode(LED_GPIO, GPIO_FLOATING);
        gpio_ll_output_enable(&GPIO, LED_GPIO);
    } else {
        gpio_ll_output_disable(&GPIO, LED_GPIO);
        gpio_set_pull_mode(LED_GPIO, GPIO_PULLDOWN_ONLY);
    }
}

static void draw(bool on) {
    if (on) output_enable(true);
    const uint32_t grb = on ? (uint32_t)LED_LEVEL << 16 : 0; /* one LED, G then R then B */
    for (unsigned b = 0; b < LED_BITS; b++) {
        const bool one = (grb >> (LED_BITS - 1 - b)) & 1;
        frame[b].level0 = 1;
        frame[b].duration0 = one ? LED_T1H : LED_T0H;
        frame[b].level1 = 0;
        frame[b].duration1 = LED_TICKS - (one ? LED_T1H : LED_T0H);
    }
    rmt_transmit(chan, enc, frame, LED_BITS * sizeof(frame[0]), &(rmt_transmit_config_t){.loop_count = 0});
    if (!on) {
        esp_rom_delay_us(100); /* the frame plus the WS2812's 50 us latch gap */
        output_enable(false);
    }
}

static void poll(void *arg) {
    (void)arg;
    const uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);
    bool want = false;
    if (now - heard_ms <= HOLD_MS) {
        if (!active) { /* the first signal after silence: start over, steady */
            active = true;
            start_ms = now;
        }
        const uint32_t since = now - start_ms;
        want = since < SOLID_MS || (since - SOLID_MS) % BLINK_PERIOD_MS < BLINK_ON_MS;
    } else {
        active = false;
    }
    if (want != lit) {
        lit = want;
        draw(want);
    }
}

esp_err_t status_led_init(void) {
    rmt_tx_channel_config_t cc = {
        .gpio_num = LED_GPIO,
        .clk_src = RMT_CLK_SRC_DEFAULT,
        .resolution_hz = LED_RES_HZ,
        .mem_block_symbols = SOC_RMT_MEM_WORDS_PER_CHANNEL, /* a frame needs 24 */
        .trans_queue_depth = 2,
    };
    esp_err_t err = rmt_new_tx_channel(&cc, &chan);
    if (err != ESP_OK) return err;
    rmt_copy_encoder_config_t ec = {0};
    if ((err = rmt_new_copy_encoder(&ec, &enc)) != ESP_OK || (err = rmt_enable(chan)) != ESP_OK) goto fail;
    if (!(frame = heap_caps_malloc(LED_BITS * sizeof(*frame), MALLOC_CAP_INTERNAL))) {
        err = ESP_ERR_NO_MEM;
        goto fail;
    }
    const esp_timer_create_args_t ta = {.callback = poll, .name = "status_led"};
    if ((err = esp_timer_create(&ta, &ticker)) != ESP_OK) goto fail;
    heard_ms = (uint32_t)(esp_timer_get_time() / 1000) - HOLD_MS - 1; /* idle at boot */
    draw(false);
    if ((err = esp_timer_start_periodic(ticker, POLL_MS * 1000)) != ESP_OK) goto fail;
    return ESP_OK;

fail:
    if (ticker) {
        esp_timer_delete(ticker);
        ticker = NULL;
    }
    if (frame) {
        free(frame);
        frame = NULL;
    }
    if (enc) {
        rmt_del_encoder(enc);
        enc = NULL;
    }
    rmt_disable(chan);
    rmt_del_channel(chan);
    chan = NULL;
    output_enable(false); /* the driver left the pin as an output */
    return err;
}

/* Called from the scan loop (IRAM code): no flash access on a hop. */
IRAM_ATTR void status_led_signal(void) { heard_ms = (uint32_t)(esp_timer_get_time() / 1000); }

#else
/* No status LED is wired on the other targets (the S3 board is a mock-up of
 * the band with its own display). */
esp_err_t status_led_init(void) { return ESP_ERR_NOT_SUPPORTED; }
void status_led_signal(void) {}
#endif
