// GPL-3.0-or-later.
package com.onebody.widscan58.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class RhLapTrackerTest {
    private val rules = DEFAULT_RH_LAP_RULES

    @Test
    fun `the first pass of a run has no lap time`() {
        val t = RhLapTracker()
        val first = t.pass(10.0, rules)
        assertFalse(first.ignored)
        assertNull("nothing to measure against yet", first.lapS)
        assertEquals(1, t.passes)
        assertNull(t.lastLapS)
        assertEquals(10.0, t.pass(20.0, rules).lapS!!, 1e-9)
        assertEquals(2, t.passes)
        assertEquals(10.0, t.lastLapS!!, 1e-9)
    }

    @Test
    fun `a pass closer than the minimum lap to the last counted one is ignored`() {
        val t = RhLapTracker()
        t.pass(10.0, rules)
        val tooSoon = t.pass(15.0, rules)
        assertTrue(tooSoon.ignored)
        assertEquals(5.0, tooSoon.ignoredS!!, 1e-9)
        assertEquals("an ignored pass is not counted", 1, t.passes)
        // The next lap is still measured from the last counted pass, not from the ignored one.
        assertEquals(10.0, t.pass(20.0, rules).lapS!!, 1e-9)
    }

    @Test
    fun `with the minimum lap off even a half second counts as a lap`() {
        val loose = rules.copy(minLapOn = false)
        val t = RhLapTracker()
        t.pass(10.0, loose)
        assertEquals(0.5, t.pass(10.5, loose).lapS!!, 1e-9)
    }

    @Test
    fun `a fastest run is count consecutive laps and has to beat the record`() {
        val t = RhLapTracker()
        t.pass(0.0, rules)
        assertNull("one lap is not a run", t.pass(10.0, rules).fastest)
        assertNull("nor two", t.pass(20.0, rules).fastest)
        val record = t.pass(30.0, rules)
        assertEquals(30.0, record.fastest!!.totalS, 1e-9)
        assertEquals(listOf(10.0, 10.0, 10.0), record.fastest!!.laps)
        assertNull("nothing to beat yet", record.previous)
        assertNull("an equal run is not a record", t.pass(40.0, rules).fastest)
        val better = t.pass(49.0, rules)
        assertEquals(29.0, better.fastest!!.totalS, 1e-9)
        assertEquals(30.0, better.previous!!.totalS, 1e-9)
    }

    @Test
    fun `the run keeps only its last count laps`() {
        // The short laps here are the point of the test, so the minimum-lap rule is off.
        val loose = rules.copy(minLapOn = false, count = 3)
        val t = RhLapTracker()
        t.pass(0.0, loose)
        for (s in listOf(5.0, 15.0, 25.0)) t.pass(s, loose)
        assertEquals(listOf(5.0, 10.0, 10.0), t.fastest!!.laps)
        assertEquals(25.0, t.fastest!!.totalS, 1e-9)
        assertEquals(listOf(10.0, 10.0, 1.0), t.pass(26.0, loose).fastest!!.laps)
    }

    @Test
    fun `fastest tracking off leaves the lap times alone`() {
        val t = RhLapTracker()
        val noFastest = rules.copy(fastestOn = false)
        t.pass(0.0, noFastest)
        for (s in listOf(10.0, 20.0, 30.0)) {
            val r = t.pass(s, noFastest)
            assertTrue(r.lapS == null || r.lapS > 0)
            assertNull(r.fastest)
        }
        assertNull(t.fastest)
        assertEquals(4, t.passes)
    }

    @Test
    fun `a break ends the run and keeps what was counted`() {
        val t = RhLapTracker()
        t.pass(0.0, rules)
        for (s in listOf(10.0, 20.0, 30.0)) t.pass(s, rules)
        assertEquals(30.0, t.fastest!!.totalS, 1e-9)
        t.breakRun()
        assertEquals(4, t.passes)
        assertEquals(10.0, t.lastLapS!!, 1e-9)
        assertEquals(30.0, t.fastest!!.totalS, 1e-9)
        assertNull("a new pilot's first pass", t.pass(40.0, rules).lapS)
    }

    @Test
    fun `clear takes the whole run away`() {
        val t = RhLapTracker()
        t.pass(0.0, rules)
        for (s in listOf(10.0, 20.0, 30.0)) t.pass(s, rules)
        t.clear()
        assertEquals(0, t.passes)
        assertNull(t.lastLapS)
        assertNull(t.fastest)
        assertNull(t.pass(40.0, rules).lapS)
    }

    @Test
    fun `changed rules are applied to the passes kept`() {
        val t = RhLapTracker()
        for (s in listOf(10.0, 15.0, 20.0, 25.0)) t.pass(s, rules)
        assertEquals("only 10 and 20 were counted", 2, t.passes)
        t.recompute(rules.copy(minLapOn = false, count = 2))
        assertEquals(4, t.passes)
        assertEquals(5.0, t.lastLapS!!, 1e-9)
        assertEquals(listOf(5.0, 5.0), t.fastest!!.laps)
        assertEquals(10.0, t.fastest!!.totalS, 1e-9)
    }

    @Test
    fun `the dialog's limits hold whatever it sends`() {
        assertEquals(2, rules.of(count = 1).count)
        assertEquals(99, rules.of(count = 200).count)
        assertEquals(0.0, rules.of(minLapS = -5.0).minLapS, 1e-9)
        assertEquals(999.0, rules.of(minLapS = 5000.0).minLapS, 1e-9)
        assertEquals("a broken stored value keeps what worked", 8.0, rules.of(minLapS = Double.NaN).minLapS, 1e-9)
        assertEquals(3, rules.of(count = 3).count)
    }
}
