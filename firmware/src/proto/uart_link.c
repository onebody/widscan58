/* GPL-3.0-or-later. */
#include "driver/gpio.h"
#include "driver/uart.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "link.h"
#include "uart_link.h"

#define UART_LINK_PORT UART_NUM_0
#define UART_LINK_TX 11 /* the C5-Zero's U0TXD, silk "TX" */
#define UART_LINK_RX 12 /* U0RXD, silk "RX" */
#define UART_LINK_BAUD 115200
/* The reader polls every 20 ms, so 1 KB rides out 80 ms of delay at line rate.
 * The C5's heap cannot afford more: see commands_task_start in commands.c. */
#define UART_LINK_RX_BYTES 1024
#define UART_LINK_TX_BYTES 1024

#if RX_TARGET_C5
static bool installed;
/* The UART carries replies straight from the caller (the C5 has no heap for a
 * queue plus a writer task). Command and scanner replies can overlap there, so
 * serialize whole writes. */
static SemaphoreHandle_t write_lock;

bool uart_link_init(void) {
    const uart_config_t cfg = {
        .baud_rate = UART_LINK_BAUD,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    write_lock = xSemaphoreCreateMutex();
    if (!write_lock) return false;
    if (uart_param_config(UART_LINK_PORT, &cfg) != ESP_OK) return false;
    if (uart_set_pin(UART_LINK_PORT, UART_LINK_TX, UART_LINK_RX, -1, -1) != ESP_OK) return false;
    if (uart_driver_install(UART_LINK_PORT, UART_LINK_RX_BYTES, UART_LINK_TX_BYTES, 0, NULL, 0) != ESP_OK) return false;
    /* The Pi's TX floats while it is off; without the pull-up the RX line
     * fills the buffer with noise. */
    gpio_pullup_en(UART_LINK_RX);
    installed = true;
    return true;
}

bool uart_link_ready(void) { return installed; }

int uart_link_write(const void *data, size_t n) {
    if (!installed) return -1;
    xSemaphoreTake(write_lock, portMAX_DELAY);
    int written = uart_write_bytes(UART_LINK_PORT, data, n);
    xSemaphoreGive(write_lock);
    return written;
}

int uart_link_read(uint8_t *buf, size_t capacity, uint32_t timeout_ms) {
    if (!installed) return -1;
    return uart_read_bytes(UART_LINK_PORT, buf, capacity,
                           timeout_ms == UINT32_MAX ? portMAX_DELAY : pdMS_TO_TICKS(timeout_ms));
}
#else
/* The S3 has no Pi link: keep the target building with no UART sink. */
bool uart_link_init(void) { return false; }
bool uart_link_ready(void) { return false; }
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
