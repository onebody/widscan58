// GPL-3.0-or-later.
package com.onebody.widscan58.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class HistoryTest {
    private var ms = 0L
    private val clock = { ms }

    private fun store(enabled: Int = 0b11): Store = Store(clock).apply {
        setProfile(profileOf(channel(0), channel(1)))
        setStatus(statusOf(5_658_000, enabled = enabled))
    }

    private fun levels(store: Store, atMs: Long, vararg idDb: Pair<Int, Double>) {
        ms = atMs
        val levels = idDb.map { (id, db) -> levelOf(id, rssiDb = db) }.toTypedArray()
        store.ingest(frame(FrameType.LEVELS, levelsOf(5_658_000, 1, 0, *levels)))
    }

    @Test
    fun `an empty window shows the web chart's default range`() {
        val s = store()
        assertEquals(-70.0 to -30.0, historyRange(s, s.now(), 30.0, smoothed = false))
    }

    @Test
    fun `the range hugs the points in view and snaps to a five db step`() {
        val s = store()
        levels(s, 10_000, 0 to -58.2, 1 to -43.7)
        // floor((-58.2-2)/5)*5 .. ceil((-43.7+2)/5)*5
        assertEquals(-65.0 to -40.0, historyRange(s, s.now(), 30.0, smoothed = false))
    }

    @Test
    fun `a point older than the span cannot stretch the chart`() {
        val s = store()
        levels(s, 1_000, 0 to -90.0)
        levels(s, 40_000, 0 to -50.0)
        // Without the window the -90 would drag the bottom of the chart to -95.
        assertEquals(-55.0 to -45.0, historyRange(s, s.now(), 30.0, smoothed = false))
    }

    @Test
    fun `a channel the device switched off stays out of the chart`() {
        val s = store(enabled = 0b01)
        levels(s, 10_000, 0 to -50.0, 1 to -20.0)
        // Channel 1's -20 would otherwise lift the top of the chart to -15.
        assertEquals(-55.0 to -45.0, historyRange(s, s.now(), 30.0, smoothed = false))
    }

    @Test
    fun `the smoothed metric reads the ema series when the device sends one`() {
        val s = store()
        ms = 10_000
        s.ingest(
            frame(
                FrameType.METRICS,
                metricsOf(MetricChannel(id = 0, rssi = -50.0, rssiEma = -80.0)),
            )
        )
        assertEquals(-85.0 to -75.0, historyRange(s, s.now(), 30.0, smoothed = true))
        assertEquals(-55.0 to -45.0, historyRange(s, s.now(), 30.0, smoothed = false))
    }

    @Test
    fun `the crosshair picks the nearest sample and gives up past half a second`() {
        val series = Series(8)
        // A hole where the device did not report, so "nearest" is not the same as "here".
        for (j in intArrayOf(0, 1, 2, 3, 6, 7, 8, 9)) series.push(j.toDouble(), (-40 - j).toFloat())
        assertEquals(-41f, series.valueAt(1.4, within = 0.5))
        assertNull(series.valueAt(5.0, within = 0.5))
        assertNull(series.valueAt(-0.6, within = 0.5))
    }
}
