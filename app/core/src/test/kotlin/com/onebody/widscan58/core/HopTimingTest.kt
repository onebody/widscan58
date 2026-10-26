// GPL-3.0-or-later.
package com.onebody.widscan58.core

import org.junit.Assert.assertEquals
import org.junit.Test

class HopTimingTest {
    private var ms = 0L
    private val clock = { ms }

    private fun store(): Store {
        val s = Store(clock)
        s.setProfile(profileOf())
        return s
    }

    @Test
    fun `both frame types push a hop with the timings off the wire`() {
        val s = store()
        s.ingest(frame(FrameType.METRICS, metricsOf(MetricChannel(0, -50.0), tuneUs = 120, captureUs = 340, dspUs = 75)))
        s.ingest(frame(FrameType.LEVELS, levelsOf(5_658_000, 1, 0, levelOf(0, rssiDb = -50.0), tuneUs = 90, captureUs = 300, dspUs = 60)))
        assertEquals(2, s.hopTimes.size)
        assertEquals(120L, s.hopTimes[0].tuneUs)
        assertEquals(535L, s.hopTimes[0].totalUs)
        assertEquals(90L, s.hopTimes[1].tuneUs)
        assertEquals(450L, s.hopTimes[1].totalUs)
    }

    @Test
    fun `the hop ring keeps the newest hops and drops the oldest`() {
        val s = store()
        for (j in 0 until Store.HOP_HISTORY + 25)
            s.ingest(frame(FrameType.METRICS, metricsOf(MetricChannel(0, -50.0), tuneUs = j.toLong())))
        assertEquals(Store.HOP_HISTORY, s.hopTimes.size)
        assertEquals("the oldest surviving hop is the 25th", 25L, s.hopTimes.first().tuneUs)
        assertEquals("the newest is at the end", (Store.HOP_HISTORY + 24).toLong(), s.hopTimes.last().tuneUs)
    }

    @Test
    fun `the drop window counts only what happened inside it`() {
        val w = DropWindow()
        w.add(0.0, 5)
        w.add(9.0, 8)
        assertEquals(3L, w.recent(8))
        // the first sample is now out of the window, so only the 9 s reading is the baseline.
        w.add(10.5, 9)
        assertEquals(1L, w.recent(9))
    }

    @Test
    fun `a rebooted drop counter reads as no recent drops`() {
        val w = DropWindow()
        w.add(0.0, 40)
        assertEquals("nothing sampled yet", 0L, DropWindow().recent(7))
        assertEquals(0L, w.recent(2))
    }

    @Test
    fun `the link rate only updates once its second has passed`() {
        val r = LinkRate()
        r.sample(0.0, 1_000)
        r.sample(0.5, 2_000)
        assertEquals("the first half second is still the baseline", 0.0, r.perSecond, 1e-9)
        r.sample(2.0, 6_000)
        assertEquals(2_500.0, r.perSecond, 1e-9)
        // A new link restarts its byte total.
        r.sample(3.0, 300)
        assertEquals(0.0, r.perSecond, 1e-9)
    }
}
