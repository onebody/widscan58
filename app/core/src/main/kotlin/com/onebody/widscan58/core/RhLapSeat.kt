// What the device does with a seat's levels, ported from web/src/mock/lap.ts (itself a port of
// firmware/src/scan/lap.c): a 255 ms running median from a histogram of the level codes, a crossing
// that starts at EnterAt and ends below ExitAt, the pass at the middle of the peak plateau, and the
// peak/nadir history with hysteresis that the 0x0e reply pops from. The "EMA" filter is the firmware's
// other one: a median of three, then an exponential average with a fixed time constant whatever the
// update rate. GPL-3.0-or-later.
package com.onebody.widscan58.core

import kotlin.math.exp
import kotlin.math.roundToInt
import kotlin.math.roundToLong

private const val QUEUED = 8 // extremums kept per direction
private const val HYSTERESIS = 4 // level codes: 1 dB
private const val WINDOW_MS = 255L // median window
private const val MAX_SAMPLES = 255 // window cap: one byte per histogram bin
private const val TS_EVERY = 8 // updates per stored timestamp
private const val TS_SLOTS = (MAX_SAMPLES + 1) / TS_EVERY + 2
private const val EMA_TAU_US = 2720 // alpha 0.3 at 1030 updates/s

enum class RhLapFilter {
    MEDIAN,
    EMA,
}

/** A peak or nadir the node queued: its level, when it started, and how long it held. */
class RhNodeExtremum(val level: Int, val firstMs: Long, val durationMs: Int)

class RhPoppedExtremum(val peak: Boolean, val extremum: RhNodeExtremum)

class RhLapSeat(var enterAt: Int, var exitAt: Int) {
    var filter = RhLapFilter.MEDIAN
        private set
    var valid = false
        private set
    var level = 0
        private set
    var levelMs = 0L
        private set
    var nodePeak = 0
        private set
    var nodeNadir = 255
        private set
    var crossing = false
        private set
    var lap = 0
        private set
    var passMs = 0L
        private set
    var passPeak = 0
        private set
    var passNadir = 255
        private set
    var loopUs = 0
        private set

    private var ema = 0.0
    private val ring = IntArray(256)
    private val bins = IntArray(256)
    private val ts = LongArray(TS_SLOTS) // time of update k, for k % TS_EVERY == 0
    private var seq = 0
    private var first = 0
    private var lastMs = 0L
    private var med = 0
    private var below = 0
    private var filled = false
    private var curPeak = 0
    private var curFirst = 0L
    private var curLast = 0L
    private var dir = 1
    private var ext = RhNodeExtremum(0, 0, 0)
    private var extLast = 0L
    private val peaks = ArrayDeque<RhNodeExtremum>()
    private val nadirs = ArrayDeque<RhNodeExtremum>()

    /** After a frequency change: keeps thresholds, lap count and last pass. */
    fun reset() {
        bins.fill(0)
        seq = 0
        first = 0
        med = 0
        below = 0
        filled = false
        valid = false
        level = 0
        nodePeak = 0
        nodeNadir = 255
        crossing = false
        passNadir = 255
        dir = 1
        peaks.clear()
        nadirs.clear()
        loopUs = 0
    }

    fun setFilter(f: RhLapFilter) {
        if (f == filter) return
        filter = f
        reset()
    }

    fun feed(tMs: Long, level: Int, loopUs: Int) {
        this.loopUs = minOf(65535, loopUs)
        if (filter == RhLapFilter.EMA) return feedEma(tMs, level, loopUs)
        if (seq - first == MAX_SAMPLES) {
            dropOldest()
            filled = true
        }
        val k = seq++
        if (k % TS_EVERY == 0) ts[(k / TS_EVERY) % TS_SLOTS] = tMs
        lastMs = tMs
        ring[k and 255] = level
        bins[level]++
        if (level < med) below++
        while (seq - first > 1 && tMs - timeOf(first) >= WINDOW_MS) {
            dropOldest()
            filled = true
        }
        // Move the median to the lower middle level of the n updates in the window.
        val r = (seq - first - 1) / 2
        while (below > r) below -= bins[--med]
        while (below + bins[med] <= r) below += bins[med++]
        if (!filled) return
        filtered(timeOf(first + r), med)
    }

    /** Force the end of a running crossing (the 0x78 command). */
    fun endCrossing() {
        if (crossing) end()
    }

    /** The oldest queued peak or nadir, or null once both queues are empty. */
    fun popExtremum(): RhPoppedExtremum? {
        val p = peaks.firstOrNull()
        val n = nadirs.firstOrNull()
        if (p == null && n == null) return null
        val peak = p != null && (n == null || p.firstMs < n.firstMs)
        return RhPoppedExtremum(peak, (if (peak) peaks else nadirs).removeFirst())
    }

    /** Time of update k in the window: stored every TS_EVERY updates, interpolated between. */
    private fun timeOf(k: Int): Long {
        val k0 = k - (k % TS_EVERY)
        val t0 = ts[(k0 / TS_EVERY) % TS_SLOTS]
        if (k == k0) return t0
        var k1 = k0 + TS_EVERY
        var t1: Long
        if (k1 < seq) {
            t1 = ts[(k1 / TS_EVERY) % TS_SLOTS]
        } else {
            k1 = seq - 1
            t1 = lastMs
        }
        return t0 + (t1 - t0) * (k - k0) / (k1 - k0)
    }

    private fun dropOldest() {
        val v = ring[first and 255]
        bins[v]--
        if (v < med) below--
        first++
    }

    // The firmware works in integers (Q8 average, a Padé weight); this is the exact form.
    private fun feedEma(tMs: Long, level: Int, loopUs: Int) {
        val k = seq++
        val prevMs = lastMs
        ring[k and 255] = level
        lastMs = tMs
        if (k < 2) {
            ema = level.toDouble()
            return filtered(tMs, level)
        }
        val three = listOf(level, ring[(k - 1) and 255], ring[(k - 2) and 255]).sorted()
        ema += (1 - exp(-loopUs.toDouble() / EMA_TAU_US)) * (three[1] - ema)
        filtered(prevMs - (EMA_TAU_US / 1000.0).roundToLong(), ema.roundToInt())
    }

    private fun end() {
        crossing = false
        lap = (lap + 1) and 0xff
        passMs = curFirst + (curLast - curFirst) / 2
        passPeak = curPeak
        passNadir = level
    }

    private fun close(q: ArrayDeque<RhNodeExtremum>) {
        q.addLast(RhNodeExtremum(ext.level, ext.firstMs, minOf(65535, (extLast - ext.firstMs).toInt())))
        if (q.size > QUEUED) q.removeFirst()
    }

    private fun filtered(t: Long, v: Int) {
        level = v
        levelMs = t
        nodePeak = maxOf(nodePeak, v)
        nodeNadir = minOf(nodeNadir, v)
        val rising = dir > 0
        if (!valid || (if (rising) v > ext.level else v < ext.level)) {
            ext = RhNodeExtremum(v, t, 0)
            extLast = t
        } else if (v == ext.level) {
            extLast = t
        } else if (if (rising) v + HYSTERESIS <= ext.level else v >= ext.level + HYSTERESIS) {
            close(if (rising) peaks else nadirs)
            dir = -dir
            ext = RhNodeExtremum(v, t, 0)
            extLast = t
        }
        valid = true
        if (!crossing && v >= enterAt) {
            crossing = true
            curPeak = v
            curFirst = t
            curLast = t
        } else if (crossing) {
            if (v > curPeak) {
                curPeak = v
                curFirst = t
                curLast = t
            } else if (v == curPeak) curLast = t
            if (v < exitAt) end()
        }
        if (!crossing && v < passNadir) passNadir = v
    }
}
