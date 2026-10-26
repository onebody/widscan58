// GPL-3.0-or-later.
package com.onebody.widscan58.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Feeds `n` updates one millisecond apart, returning the time of the first one after the block. */
private fun RhLapSeat.pump(t0: Long, n: Int, level: Int): Long {
    for (k in 0 until n) feed(t0 + k, level, 1000)
    return t0 + n
}

class RhLapSeatTest {
    @Test
    fun `the median filter needs its whole window before it reports a level`() {
        val s = RhLapSeat(120, 100)
        var t = s.pump(0, 200, 150)
        assertFalse("nothing is reported while the window fills", s.valid)
        assertEquals(0, s.level)
        t = s.pump(t, 60, 150)
        assertTrue("the 255 ms window is full", s.valid)
        assertEquals(150, s.level)
        assertTrue("and it passes EnterAt straight away", s.crossing)
    }

    @Test
    fun `a spike shorter than the window does not make a lap`() {
        val s = RhLapSeat(120, 100)
        var t = s.pump(0, 300, 60)
        t = s.pump(t, 1, 240) // one sample of the quad flashing past
        t = s.pump(t, 100, 60)
        assertEquals(60, s.level)
        assertFalse(s.crossing)
        assertEquals(0, s.lap)
    }

    @Test
    fun `a level held above EnterAt crosses, and dropping below ExitAt ends the lap`() {
        val s = RhLapSeat(120, 100)
        var t = s.pump(0, 300, 60)
        assertFalse(s.crossing)
        t = s.pump(t, 200, 200)
        assertTrue("the median is now above EnterAt", s.crossing)
        assertEquals(200, s.nodePeak)
        assertEquals(0, s.lap)
        t = s.pump(t, 200, 60)
        assertFalse(s.crossing)
        assertEquals(1, s.lap)
        assertEquals(200, s.passPeak)
    }

    @Test
    fun `the pass is stamped at the middle of the peak plateau`() {
        // The median's own timestamps, not the updates' (they lag the filter by half a window).
        val s = RhLapSeat(120, 100)
        var t = s.pump(0, 300, 60)
        var first = -1L
        var last = -1L
        for (k in 0 until 200) { // a long, flat pass
            s.feed(t + k, 200, 1000)
            if (s.level == 200) {
                if (first < 0) first = s.levelMs
                last = s.levelMs
            }
        }
        t += 200
        for (k in 0 until 200) { // and the pilot flies away
            s.feed(t + k, 60, 1000)
            if (s.level == 200) last = s.levelMs
        }
        assertTrue(s.lap > 0)
        assertEquals(first + (last - first) / 2, s.passMs)
        assertEquals(200, s.passPeak)
    }

    @Test
    fun `forcing the end of a crossing counts the lap where it stands`() {
        val s = RhLapSeat(120, 100)
        var t = s.pump(0, 300, 60)
        t = s.pump(t, 200, 200)
        assertTrue(s.crossing)
        s.endCrossing()
        assertFalse(s.crossing)
        assertEquals(1, s.lap)
        s.endCrossing()
        assertEquals("a second force changes nothing", 1, s.lap)
    }

    @Test
    fun `the nadir keeps falling while no crossing is running`() {
        val s = RhLapSeat(120, 100)
        val t = s.pump(0, 300, 60)
        assertEquals(60, s.nodeNadir)
        s.pump(t, 200, 40)
        assertEquals(40, s.nodeNadir)
        assertEquals("and so does the pass nadir", 40, s.passNadir)
    }

    @Test
    fun `peaks and nadirs queue oldest first, and each queue keeps eight`() {
        val s = RhLapSeat(120, 100)
        var t = s.pump(0, 300, 60)
        for (j in 0 until 10) {
            t = s.pump(t, 200, 220)
            t = s.pump(t, 200, 60)
        }
        val popped = mutableListOf<RhPoppedExtremum>()
        while (true) popped += s.popExtremum() ?: break
        assertEquals("one queue per direction, eight deep", 16, popped.size)
        assertEquals(8, popped.count { it.peak })
        assertEquals("the older ones fell off", listOf(220), popped.filter { it.peak }.map { it.extremum.level }.distinct())
        assertEquals(listOf(60), popped.filter { !it.peak }.map { it.extremum.level }.distinct())
        assertEquals("oldest first", popped.map { it.extremum.firstMs }.sorted(), popped.map { it.extremum.firstMs })
        assertTrue("every entry held its plateau long enough to time", popped.all { it.extremum.durationMs > 0 })
        assertNull(s.popExtremum())
    }

    @Test
    fun `a change of filter starts the window again`() {
        val s = RhLapSeat(120, 100)
        s.pump(0, 300, 200)
        assertTrue(s.valid)
        s.setFilter(RhLapFilter.EMA)
        assertFalse("the median window was dropped", s.valid)
        assertEquals(0, s.loopUs)
    }

    @Test
    fun `the EMA filter reports from the first update and averages towards the level`() {
        val s = RhLapSeat(120, 100)
        s.setFilter(RhLapFilter.EMA)
        s.feed(0, 60, 1000)
        assertTrue("no window to fill", s.valid)
        assertEquals(60, s.level)
        s.feed(1, 200, 1000)
        assertEquals("the average starts at what it first sees", 200, s.level)
        s.feed(2, 240, 1000)
        assertEquals("the median of three drops a lone spike", 200, s.level)
        assertTrue("and the level is above EnterAt", s.crossing)
        s.pump(3, 40, 60)
        assertEquals("then it settles on the new level", 60, s.level)
    }

    @Test
    fun `a frequency change keeps the thresholds, the lap count and the last pass`() {
        val s = RhLapSeat(120, 100)
        var t = s.pump(0, 300, 60)
        t = s.pump(t, 200, 200)
        s.endCrossing()
        val lap = s.lap
        val passPeak = s.passPeak
        s.reset()
        assertEquals("the same pilot may cross again", lap, s.lap)
        assertEquals(passPeak, s.passPeak)
        assertEquals(120, s.enterAt)
        assertEquals(100, s.exitAt)
        assertEquals("but the filter starts empty", 0, s.level)
        assertFalse(s.crossing)
        assertEquals(0, s.nodePeak)
        assertEquals(255, s.nodeNadir)
    }
}
