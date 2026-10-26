// One wire line per scanner control, with the ranges the firmware accepts.
// Mirrors the client-side limits in web/src/views/controls.ts. GPL-3.0-or-later.
package com.onebody.widscan58.core

import kotlin.math.abs
import kotlin.math.roundToInt

enum class RecalMode {
    CACHE,
    ONCE,
    EVERY,
    ;

    companion object {
        /** STATUS reports this in its own casing; an unknown word leaves nothing selected. */
        fun of(word: String?): RecalMode? =
            entries.firstOrNull { it.name.equals(word?.trim(), ignoreCase = true) }
    }
}

object DeviceCommands {
    /** The PHY filter-table index; -1 hands the choice to the PHY itself. */
    const val FILTER_AUTO = -1
    const val FILTER_MAX = 63

    /** The manual-gain ceiling when the board reports none: the PHY table is 82 deep. */
    const val GAIN_FALLBACK_MAX = 82

    val FFT_SIZES = intArrayOf(64, 128, 256, 512, 1024, 2048)
    val CAPTURE_SIZES = intArrayOf(256, 512, 1024, 2048, 4096, 8192, 16380)
    val SPECTRUM_BINS = intArrayOf(64, 128, 256, 512, 1024)

    const val RECAL = "RECAL"
    const val REBOOT = "REBOOT"
    const val BOOTLOADER = "BOOTLOADER"

    fun mode(plan: LoPlan) = "MODE ${plan.name}"

    fun gain(mode: GainMode, value: Int, max: Int?) = when (mode) {
        GainMode.HARDWARE -> "GAIN HARDWARE"
        GainMode.MANUAL -> "GAIN MANUAL ${value.coerceIn(0, (max ?: GAIN_FALLBACK_MAX).coerceAtLeast(0))}"
    }

    fun filter(code: Int) =
        if (code <= FILTER_AUTO) "FILTER AUTO" else "FILTER ${code.coerceIn(0, FILTER_MAX)}"

    fun fft(bins: Int) = "FFT ${nearest(FFT_SIZES, bins)}"

    fun capture(samples: Int) = "CAPN ${nearest(CAPTURE_SIZES, samples)}"

    fun segments(n: Int) = "SEGMENTS ${n.coerceIn(1, 64)}"

    fun metrics(mode: MetricsMode) = "METRICS ${mode.name}"

    /** Drives a pin now; `GPIO SAVE` is what makes it the boot setting. */
    fun gpioPin(pin: Int, setting: GpioSetting) = "GPIO $pin ${setting.name}"

    /** Both windows are the device's own ± Hz; the web UI's inputs are kHz. */
    fun windows(rssiHz: Long, searchHz: Long) =
        "WIN ${windowHz(rssiHz)} ${windowHz(searchHz)}"

    /** Smoothing factors are sent as thousandths, matching how the firmware stores them. */
    fun ema(alpha: Double, clearAlpha: Double) = "EMA ${milli(alpha)} ${milli(clearAlpha)}"

    fun spectrum(every: Int, bins: Int, spanHz: Long, detectorMax: Boolean) =
        "SPEC ${every.coerceIn(0, 255)} ${nearest(SPECTRUM_BINS, bins)} " +
            "${spanHz.coerceIn(100_000, 40_000_000)} ${if (detectorMax) "MAX" else "MEAN"}"

    fun settle(microseconds: Int) = "SETTLE ${microseconds.coerceIn(0, 20_000)}"

    fun recalMode(mode: RecalMode) = "RECALMODE ${mode.name}"

    /** The bits are toggles: the mask the device reports, with one bit flipped. */
    fun tuneSkip(mask: Int, bit: Int) = "TUNESKIP ${(mask xor bit) and 7}"

    fun conjugate(on: Boolean) = "CONJ ${if (on) 1 else 0}"

    fun iqCorrection(on: Boolean) = "IQCORR ${if (on) 1 else 0}"

    /** Which of the eight planned channels the device measures. */
    fun enable(mask: Int) = "ENABLE ${mask and 0xff}"

    fun channelPlan(names: List<String>) = Bands.channelsCommand(names)

    private fun windowHz(hz: Long) = hz.coerceIn(10_000, 20_000_000)

    private fun milli(alpha: Double) =
        (alpha.coerceIn(0.001, 1.0) * 1000).roundToInt().coerceAtLeast(1)

    private fun nearest(options: IntArray, value: Int): Int =
        options.minByOrNull { abs(it - value) } ?: options.first()
}
