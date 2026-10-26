/* GPL-3.0-or-later. */
#include "driver/gpio.h"
#include "driver/uart.h"
#include "freertos/FreeRTOS.h"
#include "link.h"
#include "uart_link.h"

#define UART_LINK_PORT UART_NUM_0
#define UART_LINK_TX 11 /* the C5-Zero's U0TXD, silk "TX" */
#define UART_LINK_RX 12 /* U0RXD, silk "RX" */
#define UART_LINK_BAUD 115200
#define UART_LINK_RX_BYTES 4096
#define UART_LINK_TX_BYTES 4096
#define UART_LINK_QUEUE_BYTES 4096

#if RX_TARGET_C5
static bool installed;

bool uart_link_init(void) {
    const uart_config_t cfg = {
        .baud_rate = UART_LINK_BAUD,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    if (uart_param_config(UART_LINK_PORT, &cfg) != ESP_OK) return false;
    if (uart_set_pin(UART_LINK_PORT, UART_LINK_TX, UART_LINK_RX, -1, -1) != ESP_OK) return false;
    if (uart_driver_install(UART_LINK_PORT, UART_LINK_RX_BYTES, UART_LINK_TX_BYTES, 0, NULL, 0) != ESP_OK) return false;
    /* The Pi's TX floats while it is off; without the pull-up the RX line
     * fills the buffer with noise. */
    gpio_pullup_en(UART_LINK_RX);
    installed = true;
    link_sink_start(LINK_UART, UART_LINK_QUEUE_BYTES);
    return true;
}

int uart_link_write(const void *data, size_t n) {
    return installed ? uart_write_bytes(UART_LINK_PORT, data, n) : -1;
}

int uart_link_read(uint8_t *buf, size_t capacity, uint32_t timeout_ms) {
    if (!installed) return -1;
    return uart_read_bytes(UART_LINK_PORT, buf, capacity,
                           timeout_ms == UINT32_MAX ? portMAX_DELAY : pdMS_TO_TICKS(timeout_ms));
}
#else
/* The S3 has no Pi link: keep the target building with no UART sink. */
bool uart_link_init(void) { return false; }
int uart_link_write(const void *data, size_t n) {
    (void)data;
    (void)n;
    return -1;
}
int uart_link_read(uint8_t *buf, size_t capacity, uint32_t timeout_ms) {
    (void)buf;
    (void)capacity;
    (void)timeout_ms;
    return -1;
}
#endif
