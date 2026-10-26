// The board's boot-time output pins: how GPIO? reports them and which ones differ
// from their saved settings. Mirrors web/src/model/gpio.ts. GPL-3.0-or-later.
package com.onebody.widscan58.core

/** What a pin does: nothing, drives low, drives high, or belongs to the flash or USB. */
enum class GpioSetting(val char: Char) {
    NONE('-'),
    LOW('L'),
    HIGH('H'),
    RESERVED('x'),
    ;

    companion object {
        /** Unknown characters are a broken reply, not a setting. */
        fun of(char: Char): GpioSetting? = entries.firstOrNull { it.char == char }
    }
}

/** One pin: what it drives now, and what it will drive after a restart. */
class GpioPin(val n: Int, val active: GpioSetting, val saved: GpioSetting)

/**
 * The firmware sends one character per pin, so a reply whose strings disagree with
 * its own count is truncated or corrupt; the web throws on it, we drop it whole.
 */
fun parseGpio(count: Int, active: String, saved: String): List<GpioPin>? {
    if (count < 0 || active.length != count || saved.length != count) return null
    return (0 until count).map { n ->
        val a = GpioSetting.of(active[n]) ?: return null
        val s = GpioSetting.of(saved[n]) ?: return null
        GpioPin(n, a, s)
    }
}

/** The pins whose change has not been saved, so a restart would lose them. */
fun gpioUnsaved(pins: List<GpioPin>): List<GpioPin> = pins.filter { it.active != it.saved }
