/* The board's status LED: lit while the scanner hears a signal on a channel.
 * GPL-3.0-or-later. */
#pragma once
#include "esp_err.h"

/* Start the LED dark, with its own timer driving the pattern. A target without
 * the LED answers ESP_ERR_NOT_SUPPORTED; a failure is not fatal. */
esp_err_t status_led_init(void);
/* A signal was heard on a channel now. Cheap: one store, any call rate. */
void status_led_signal(void);
