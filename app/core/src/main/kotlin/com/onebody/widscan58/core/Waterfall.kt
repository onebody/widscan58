// Waterfall rows: one panorama composite per completed scan rotation, quantised
// to a colour ramp so history never recolours. Mirrors renderWaterfall() in
// web/src/views/panorama.ts. GPL-3.0-or-later.
package com.onebody.widscan58.core

import kotlin.math.floor
import kotlin.math.roundToInt

class Waterfall(val rowWidth: Int = 600) {
    /** Ramp indices (0..255), newest row first. */
    val rows = ArrayList<ByteArray>()
    val rowTimes = ArrayList<Double>()
    private var floorRef = Double.NaN
    private var lastVersion = 0L
    private var lastRange = ""

    /** Adds a row when at least one full rotation of spectrum frames has arrived since the last. */
    fun capture(store: Store, comp: Composite, fallbackFloor: Double, maxRows: Int): Boolean {
        val range = "${comp.f0}:${comp.f1}"
        if (range != lastRange) {
            rows.clear()
            rowTimes.clear()
            lastRange = range
        }
        val hops = (store.status?.plan?.size ?: 0).coerceAtLeast(1)
        if (store.waterfallVersion - lastVersion < hops) return false
        lastVersion = store.waterfallVersion
        val lo = (smoothedFloor(store) ?: fallbackFloor) - FLOOR_HEADROOM_DB
        val row = ByteArray(rowWidth) { j ->
            val v = comp.live[floor(j.toDouble() / rowWidth * comp.columns).toInt()]
            if (v.isNaN()) 0 else ((v - lo) / SPAN_DB * 255).roundToInt().coerceIn(0, 255).toByte()
        }
        rows.add(0, row)
        rowTimes.add(0, store.now())
        while (rows.size > maxRows.coerceAtLeast(1)) {
            rows.removeAt(rows.size - 1)
            rowTimes.removeAt(rowTimes.size - 1)
        }
        return true
    }

    /** Median of the per-LO floors, slowly smoothed, so one hop's outlier cannot shift the scale. */
    private fun smoothedFloor(store: Store): Double? {
        val floors = store.floorByLo.values.filter { !it.isNaN() }.sorted()
        if (floors.isEmpty()) return null
        val med = floors[floors.size / 2]
        floorRef = if (floorRef.isNaN()) med else floorRef + FLOOR_EMA * (med - floorRef)
        return floorRef
    }

    companion object {
        private const val FLOOR_HEADROOM_DB = 5.0
        private const val SPAN_DB = 45.0
        private const val FLOOR_EMA = 0.1
    }
}
