// Band panorama: stitch the per-LO spectrum segments onto one pixel grid.
// Mirrors composite() and bandRange() in web/src/views/panorama.ts. GPL-3.0-or-later.
package com.onebody.widscan58.core

import kotlin.math.abs
import kotlin.math.log10
import kotlin.math.roundToInt

/** A frequency span in kHz. */
class Span(val lo: Double, val hi: Double)

/** One point per display column; NaN where no segment covers it. */
class Composite(
    val f0: Double,
    val f1: Double,
    val live: DoubleArray,
    val max: DoubleArray,
    val avg: DoubleArray,
    val floor: DoubleArray,
) {
    val columns: Int get() = live.size
}

/** The span of the LO segments scanned (stale ones drop out in [Store]), or the
 *  profile's band before the first spectrum. */
fun bandRange(store: Store): Span {
    val profile = store.profile ?: return Span(0.0, 1.0)
    if (store.segments.isEmpty()) return Span(profile.bandKhz[0].toDouble(), profile.bandKhz[1].toDouble())
    var lo = Double.POSITIVE_INFINITY
    var hi = Double.NEGATIVE_INFINITY
    for (seg in store.segments.values) {
        val start = seg.latest.startKhz()
        lo = minOf(lo, start)
        hi = maxOf(hi, start + seg.latest.bins * seg.latest.binHz / 1000.0)
    }
    return Span(lo, hi)
}

/** Each point comes from the segment whose LO is nearest: the analog filter
 *  rolls off at segment edges, so the middle of a segment is the honest sample. */
fun composite(store: Store, f0: Double, f1: Double, n: Int): Composite {
    fun nan() = DoubleArray(n) { Double.NaN }
    val live = nan()
    val max = nan()
    val avg = nan()
    val floor = nan()
    val best = DoubleArray(n) { Double.POSITIVE_INFINITY }
    for (seg in store.segments.values) {
        val sp = seg.latest
        val start = sp.startKhz() // centre of bin 0
        val bin = sp.binHz / 1000.0
        val fl = store.floorByLo[sp.loKhz]
        for (x in 0 until n) {
            val f = f0 + ((x + 0.5) / n) * (f1 - f0)
            val k = ((f - start) / bin).roundToInt()
            if (k < 0 || k >= sp.bins) continue
            val d = abs(f - sp.loKhz)
            if (d >= best[x]) continue
            best[x] = d
            live[x] = sp.db[k]
            max[x] = seg.maxHold[k]
            avg[x] = 10 * log10(seg.avg[k] + 1e-30)
            if (fl != null) floor[x] = fl
        }
    }
    return Composite(f0, f1, live, max, avg, floor)
}
