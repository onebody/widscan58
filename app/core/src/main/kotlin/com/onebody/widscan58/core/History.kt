// What the RSSI history chart needs from the store: which channels belong in it,
// the dBFS range its y-axis covers, and the crosshair's value pick. Keeping it here
// lets the padding and the window rules be tested without a canvas.
// Mirrors web/src/views/history.ts. GPL-3.0-or-later.
package com.onebody.widscan58.core

import kotlin.math.abs
import kotlin.math.ceil
import kotlin.math.floor

/** The web chart's fallback while the window holds no samples. */
val EMPTY_HISTORY_RANGE: Pair<Double, Double> = -70.0 to -30.0

/** A channel the device switched off has no history to draw. */
fun historyVisible(store: Store, id: Int): Boolean =
    store.status?.let { it.enabled and (1 shl id) != 0 } ?: true

/** The series for one channel: the EMA when the mode reports one, the raw RSSI otherwise. */
fun historySeries(store: Store, id: Int, smoothed: Boolean): Series? {
    val cs = store.channels.getOrNull(id) ?: return null
    return if (smoothed) smoothedSeries(cs) else cs.rssi
}

/**
 * The dBFS span covering the samples in view, padded and snapped to a 5 dB step so the
 * grid lines land on round numbers and the trace does not slide on every frame.
 */
fun historyRange(store: Store, now: Double, spanSeconds: Double, smoothed: Boolean): Pair<Double, Double> {
    var lo = Double.POSITIVE_INFINITY
    var hi = Double.NEGATIVE_INFINITY
    for (id in store.channels.indices) {
        if (!historyVisible(store, id)) continue
        historySeries(store, id, smoothed)?.forEachWithin(now, spanSeconds) { _, v ->
            if (v < lo) lo = v.toDouble()
            if (v > hi) hi = v.toDouble()
        }
    }
    if (!lo.isFinite()) return EMPTY_HISTORY_RANGE
    val bottom = floor((lo - RANGE_PADDING_DB) / RANGE_STEP_DB) * RANGE_STEP_DB
    val top = ceil((hi + RANGE_PADDING_DB) / RANGE_STEP_DB) * RANGE_STEP_DB
    return bottom to top
}

/** The sample nearest [time], when one lies within [within] seconds of the cursor. */
fun Series.valueAt(time: Double, within: Double): Float? {
    var best = Double.POSITIVE_INFINITY
    var found: Float? = null
    forEachWithin(time + within, within * 2.0) { t, v ->
        val d = abs(t - time)
        if (d < best) {
            best = d
            found = v
        }
    }
    return found?.takeIf { best <= within }
}

private const val RANGE_PADDING_DB = 2.0
private const val RANGE_STEP_DB = 5.0
