// The per-hop time breakdown, the recent-drop window and the link throughput rate
// the telemetry panel shows. All three are counters the device reports; the phone
// has to derive the rates. Mirrors web/src/views/debug.ts. GPL-3.0-or-later.
package com.onebody.widscan58.core

/** One hop's cost, in microseconds, as the metrics frame reports it. */
class Hop(val tuneUs: Long, val captureUs: Long, val dspUs: Long) {
    val totalUs: Long get() = tuneUs + captureUs + dspUs
}

/**
 * The device only counts dropped frames since boot; "what has it lost lately"
 * needs the counter sampled over a window.
 */
class DropWindow(private val seconds: Double = 10.0) {
    private val at = mutableListOf<Double>()
    private val counts = mutableListOf<Long>()

    fun add(now: Double, drops: Long) {
        at += now
        counts += drops
        while (at.isNotEmpty() && now - at.first() > seconds) {
            at.removeAt(0)
            counts.removeAt(0)
        }
    }

    /** Drops in the window. A counter that went backwards means the board rebooted. */
    fun recent(drops: Long): Long {
        val oldest = counts.firstOrNull() ?: return 0
        return (drops - oldest).coerceAtLeast(0)
    }
}

/**
 * KiB/s of wire bytes, from a total that only ever grows until the link closes.
 * Sampled at most once a second: faster would only measure the read chunking.
 */
class LinkRate(private val seconds: Double = 1.0) {
    private var at = Double.NaN
    private var bytes = 0L

    var perSecond = 0.0
        private set

    fun sample(now: Double, totalBytes: Long) {
        if (at.isNaN()) {
            at = now
            bytes = totalBytes
            return
        }
        if (now - at < seconds) return
        // A fresh link starts its total at zero again, which reads as a negative rate.
        perSecond = ((totalBytes - bytes) / (now - at)).coerceAtLeast(0.0)
        at = now
        bytes = totalBytes
    }
}
