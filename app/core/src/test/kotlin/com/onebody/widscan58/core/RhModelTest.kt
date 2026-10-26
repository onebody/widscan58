// GPL-3.0-or-later.
package com.onebody.widscan58.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** A link with canned answers, so the model's rules can be seen one at a time. */
private class FakeLink(startUptimeMs: Long = 1_000L) : RhLink {
    override var closed = false
    override var lastRequest = 0L
    override var lastResponse = 0L
    var latencyMs = 0L
    var uptime = startUptimeMs
    val writes = ArrayList<Pair<Int, IntArray>>()
    val lines = ArrayList<String>()
    val replies = HashMap<Int, ByteArray>()
    var textReply: (String) -> String = { "OK" }

    override fun read(cmd: Int, size: Int): ByteArray {
        lastResponse = uptime
        lastRequest = uptime - latencyMs
        if (cmd == Rh.READ_TIME_MILLIS) return (rhU16((uptime shr 16).toInt()) + rhU16((uptime and 0xffff).toInt())).toBytes()
        val p = replies[cmd] ?: throw RhLinkError("no reply for 0x${cmd.toString(16)}")
        if (p.size != size) throw RhLinkError("short reply for 0x${cmd.toString(16)}")
        return p
    }

    override fun write(cmd: Int, payload: IntArray) {
        writes += cmd to payload
    }

    override fun text(line: String): String {
        lines += line
        return textReply(line)
    }

    fun written(cmd: Int): List<IntArray> = writes.filter { it.first == cmd }.map { it.second }
}

private fun IntArray.toBytes(): ByteArray = ByteArray(size) { this[it].toByte() }

private fun passStats(
    lapId: Int = 0,
    msSinceLap: Int = 0,
    level: Int = 100,
    nodePeak: Int = 0,
    passPeak: Int = 0,
    loopUs: Int = 1000,
) = byteArrayOf(
    lapId.toByte(),
    (msSinceLap shr 8).toByte(), msSinceLap.toByte(),
    level.toByte(), nodePeak.toByte(), passPeak.toByte(),
    (loopUs shr 8).toByte(), loopUs.toByte(),
)

private fun extremums(
    crossing: Boolean = false,
    passNadir: Int = 255,
    nodeNadir: Int = 255,
    level: Int = 0,
    msAgo: Int = 0,
    durationMs: Int = 0,
    peak: Boolean = true,
) = byteArrayOf(
    ((if (crossing) FLAG_CROSSING else 0) or (if (level != 0 && peak) FLAG_PEAK else 0)).toByte(),
    passNadir.toByte(), nodeNadir.toByte(), level.toByte(),
    (msAgo shr 8).toByte(), msAgo.toByte(),
    (durationMs shr 8).toByte(), durationMs.toByte(),
)

private class Remember : RhSettings {
    var saved = RhSaved()
    var saves = 0
    override fun load(): RhSaved = saved
    override fun save(saved: RhSaved) {
        this.saved = saved
        saves++
    }
}

private class Rig(startMs: Long = 0L) {
    var nowMs = startMs
    val store = Store({ nowMs })
    val settings = Remember()
    val link = FakeLink()
    val model = RhModel(store, settings) { if (it.contains("SNR")) RhMetric.SNR else RhMetric.RSSI }

    fun start(channels: List<String> = Bands.names("R")) {
        link.replies[Rh.READ_REVISION_CODE] = byteArrayOf(RH_API_MARKER.toByte(), RH_API_LEVEL.toByte())
        link.replies[Rh.READ_MULTINODE_COUNT] = byteArrayOf(RH_SEATS.toByte())
        link.replies[Rh.READ_FREQUENCY] = byteArrayOf(0x16, 0x2A)
        link.replies[Rh.TEST_RX_REGISTER] = byteArrayOf(1)
        link.replies[Rh.READ_LAP_PASS_STATS] = passStats()
        link.replies[Rh.READ_LAP_EXTREMUMS] = extremums()
        link.textReply = { if (it == "CONFIG?") "CONFIG metrics=RSSI" else "OK" }
        model.start(link, channels)
    }

    /** One poll, [seconds] of board time later. */
    fun poll(seconds: Double = 0.1, pass: ByteArray? = null, ext: ByteArray? = null) {
        nowMs += (seconds * 1000).toLong()
        link.uptime = nowMs
        pass?.let { link.replies[Rh.READ_LAP_PASS_STATS] = it }
        ext?.let { link.replies[Rh.READ_LAP_EXTREMUMS] = it }
        model.poll()
    }

    /** The newest level history point of a seat. */
    fun newest(seat: Int = 0): Double {
        var t = Double.NaN
        model.seats[seat].history.forEach { time, _ -> t = time }
        return t
    }
}

class RhModelTest {
    @Test
    fun `a start reads the node identity and puts the plan on the seats`() {
        val r = Rig()
        r.start()
        assertTrue("and it is running", r.model.running)
        assertNull(r.model.error)
        assertEquals(RhMetric.RSSI, r.model.metric)
        assertEquals("R1", r.model.seats[0].channel)
        assertEquals(5658, r.model.seats[0].mhz)
        assertEquals("R8", r.model.seats[7].channel)
        assertEquals("a frequency for every seat", 8, r.link.written(Rh.WRITE_FREQUENCY).size)
        assertEquals("the thresholds too", 8, r.link.written(Rh.WRITE_ENTER_AT_LEVEL).size)
    }

    @Test
    fun `a seat the plan does not reach is switched off`() {
        val r = Rig()
        r.start(Bands.names("R").take(3))
        assertEquals("R3", r.model.seats[2].channel)
        assertNull(r.model.seats[3].channel)
        assertEquals(0, r.model.seats[3].mhz)
        assertTrue(
            "the server's off frequency, not zero",
            r.link.written(Rh.WRITE_FREQUENCY).any { it.contentEquals(rhU16(Rh_FREQ_OFF)) }
        )
    }

    @Test
    fun `a board that is not a RotorHazard node says so and does not start`() {
        val r = Rig()
        r.link.replies[Rh.READ_REVISION_CODE] = byteArrayOf(0x12, 0x34)
        r.link.replies[Rh.READ_MULTINODE_COUNT] = byteArrayOf(RH_SEATS.toByte())
        r.model.start(r.link, Bands.names("R"))
        assertFalse(r.model.running)
        assertNotNull(r.model.error)
        assertTrue(r.store.log.any { it.dir == LogDir.ERROR })
    }

    @Test
    fun `a board with fewer seats than the plan says so too`() {
        val r = Rig()
        r.link.replies[Rh.READ_REVISION_CODE] = byteArrayOf(RH_API_MARKER.toByte(), RH_API_LEVEL.toByte())
        r.link.replies[Rh.READ_MULTINODE_COUNT] = byteArrayOf(4)
        r.model.start(r.link, Bands.names("R"))
        assertFalse(r.model.running)
        assertTrue("in its own words", r.model.error!!.contains("4 seats"))
    }

    @Test
    fun `a level the node has not measured is not a reading`() {
        val r = Rig()
        r.start()
        r.poll(pass = passStats(level = 1))
        assertNull("so the seat shows nothing", r.model.seats[0].level)
        r.poll(pass = passStats(level = 120, nodePeak = 133))
        assertEquals(120, r.model.seats[0].level)
        assertEquals(133, r.model.seats[0].nodePeak)
    }

    @Test
    fun `a level is kept at the middle of its round trip`() {
        val r = Rig()
        r.start()
        r.link.latencyMs = 100
        r.poll(pass = passStats(level = 120))
        assertEquals("50 ms before the poll", r.nowMs / 1000.0 - 0.05, r.newest(), 0.001)
    }

    @Test
    fun `a crossing the node reports becomes an enter and an exit`() {
        val r = Rig()
        r.start()
        r.poll(ext = extremums(crossing = false))
        assertTrue("the first read only learns the state", r.model.events.isEmpty())
        r.poll(ext = extremums(crossing = true))
        assertEquals(RhEventKind.ENTER, r.model.events.last().kind)
        r.poll(ext = extremums(crossing = false))
        assertEquals(RhEventKind.EXIT, r.model.events.last().kind)
    }

    @Test
    fun `a new lap id is a pass, timed by the stamp the node gave it`() {
        val r = Rig()
        r.start()
        r.poll(pass = passStats(lapId = 1, level = 150, passPeak = 170))
        assertTrue("the first read only learns the counter", r.model.events.isEmpty())
        r.poll(pass = passStats(lapId = 2, level = 150, passPeak = 170))
        assertNull("the first pass of a run has no lap time", r.model.seats[0].laps.lastLapS)
        r.poll(pass = passStats(lapId = 3, level = 150, passPeak = 170), seconds = 10.0)
        val e = r.model.events.last()
        assertEquals(RhEventKind.PASS, e.kind)
        assertEquals("ten seconds later", 10.0, e.lapS ?: 0.0, 0.001)
        assertEquals(170, e.peak)
        assertFalse(e.ignored)
    }

    @Test
    fun `a pass closer than the minimum lap is not counted but shown as ignored`() {
        val r = Rig()
        r.start()
        r.poll(pass = passStats(lapId = 1))
        r.poll(pass = passStats(lapId = 2))
        r.poll(pass = passStats(lapId = 3), seconds = 3.0)
        val e = r.model.events.last()
        assertEquals(RhEventKind.PASS, e.kind)
        assertTrue("and it says how soon", e.ignored)
        assertEquals(3.0, e.ignoredS ?: 0.0, 0.001)
        assertNull(e.lapS)
        assertEquals("it is not counted", 1, r.model.seats[0].laps.passes)
    }

    @Test
    fun `a fastest run is an event of its own with the record it beat`() {
        val r = Rig()
        r.start()
        r.model.setLapRules(r.model.lapRules.of(count = 2, minLapOn = false))
        r.poll(pass = passStats(lapId = 1))
        r.poll(pass = passStats(lapId = 2))
        r.poll(pass = passStats(lapId = 3), seconds = 10.0)
        r.poll(pass = passStats(lapId = 4), seconds = 10.0)
        r.poll(pass = passStats(lapId = 5), seconds = 7.0)
        val e = r.model.events.last()
        assertEquals(RhEventKind.FASTEST, e.kind)
        assertEquals(17.0, e.fastest!!.totalS, 0.001)
        assertEquals(20.0, e.previous!!.totalS, 0.001)
    }

    @Test
    fun `a counted pass with a lap time is the one the voice speaks`() {
        val heard = ArrayList<Pair<Int, Double>>()
        val r = Rig()
        r.model.onLap = { s, lapS -> heard += s.index to lapS }
        r.start()
        r.model.setLapRules(r.model.lapRules.of(minLapOn = false))
        r.poll(pass = passStats(lapId = 1))
        r.poll(pass = passStats(lapId = 2))
        assertTrue("the first pass of the run has no lap to speak", heard.isEmpty())
        r.poll(pass = passStats(lapId = 3), seconds = 5.0)
        assertEquals("one callout per seat", 8, heard.size)
        assertEquals(5.0, heard[0].second, 0.001)
    }

    @Test
    fun `extremums queued before the run started are not shown`() {
        val r = Rig()
        r.start()
        r.poll(seconds = 5.0, ext = extremums(level = 200, msAgo = 60_000))
        assertTrue("older than the run", r.model.seats[0].peaks.isEmpty())
        r.poll(ext = extremums(level = 200, msAgo = 500))
        assertEquals(1, r.model.seats[0].peaks.size)
        assertEquals(200, r.model.seats[0].peaks.last().level)
        assertTrue(r.model.seats[0].peaks.last().peak)
    }

    @Test
    fun `a board that restarted sets its seats up again`() {
        val r = Rig()
        r.start()
        r.poll(pass = passStats(lapId = 7))
        val before = r.link.writes.size
        r.link.replies[Rh.READ_LAP_PASS_STATS] = passStats(lapId = 0) // its counter starts over
        r.nowMs += 100
        r.link.uptime = 10 // the uptime went backwards
        r.model.poll()
        assertEquals(
            "the seat frequencies again", 8,
            r.link.writes.subList(before, r.link.writes.size).count { it.first == Rh.WRITE_FREQUENCY }
        )
        assertTrue("and the console says why", r.store.log.any { it.text.contains("restarted") })
        assertEquals(0, r.model.seats[0].lapId)
        assertTrue("so no pass is credited across the gap", r.model.events.none { it.kind == RhEventKind.PASS })
    }

    @Test
    fun `a read that does not come back is the cycle's error, not a crash`() {
        val r = Rig()
        r.start()
        r.link.replies.remove(Rh.READ_LAP_PASS_STATS)
        r.poll()
        assertNotNull(r.model.error)
        assertTrue("and the loop is still running", r.model.running)
        r.link.replies[Rh.READ_LAP_PASS_STATS] = passStats()
        r.poll()
        assertNull("a good cycle clears it", r.model.error)
    }

    @Test
    fun `a threshold the sheet sets goes to the seat and is remembered`() {
        val r = Rig()
        r.start()
        r.model.setThreshold(2, RhThreshold.ENTER, 140.6)
        assertEquals("rounded", 141, r.model.seats[2].enter)
        assertEquals(141, r.settings.saved.enter[2])
        assertEquals("one save per change", 1, r.settings.saves)
        r.link.writes.clear()
        r.model.setThreshold(2, RhThreshold.EXIT, 0.0)
        assertEquals(
            "the seat first, then its threshold",
            listOf(Rh.WRITE_CURNODE_INDEX, Rh.WRITE_EXIT_AT_LEVEL),
            r.link.writes.map { it.first },
        )
        assertEquals("and never below the node's own floor", 1, r.model.seats[2].exit)
        r.model.setThreshold(2, RhThreshold.ENTER, 900.0)
        assertEquals("nor above its ceiling", 254, r.model.seats[2].enter)
    }

    @Test
    fun `the thresholds stored before a start are the ones written`() {
        val r = Rig()
        r.settings.saved = RhSaved(enter = IntArray(RH_SEATS) { 130 }, exit = IntArray(RH_SEATS) { 110 })
        r.start()
        assertEquals(130, r.model.seats[0].enter)
        assertEquals("and on the wire", 130, r.link.written(Rh.WRITE_ENTER_AT_LEVEL).first()[0])
    }

    @Test
    fun `the level of the last second is the average of what was polled`() {
        val r = Rig()
        r.start()
        r.poll(pass = passStats(level = 100))
        r.poll(pass = passStats(level = 140), seconds = 0.1)
        assertEquals(120.0, r.model.recentLevel(0, 1.0) ?: 0.0, 0.001)
        assertEquals("nothing older than the window", 140.0, r.model.recentLevel(0, 0.05) ?: 0.0, 0.001)
        r.poll(pass = passStats(level = 1), seconds = 0.1)
        assertEquals("an unmeasured level is not a reading", 120.0, r.model.recentLevel(0, 1.0) ?: 0.0, 0.001)
    }

    @Test
    fun `a seat outside the board's band is reported as one`() {
        val r = Rig()
        r.start()
        r.link.replies[Rh.TEST_RX_REGISTER] = byteArrayOf(0)
        r.model.writeSeats(listOf("R1"))
        assertTrue(r.store.log.any { it.dir == LogDir.ERROR && it.text.contains("outside") })
    }

    @Test
    fun `the metric CONFIG reports is the metric the model shows`() {
        val r = Rig()
        r.start()
        r.poll(pass = passStats(level = 120))
        r.link.textReply = { if (it == "CONFIG?") "CONFIG metrics=SNR" else "OK" }
        r.model.setMetric(RhMetric.SNR)
        assertEquals(RhMetric.SNR, r.model.metric)
        assertNull("and the levels it read in the other metric are gone", r.model.seats[0].level)
        assertTrue("the command the board was given", r.link.lines.contains("METRICS SNR"))
    }

    @Test
    fun `clearing the events clears the laps but not the thresholds`() {
        val r = Rig()
        r.start()
        r.poll(pass = passStats(lapId = 1))
        r.model.clearEvents()
        assertTrue(r.model.events.isEmpty())
        assertEquals(0, r.model.seats[0].laps.passes)
        assertEquals(125, r.model.seats[0].enter)
    }

    @Test
    fun `changing the lap rules recomputes them without new events`() {
        val r = Rig()
        r.start()
        r.poll(pass = passStats(lapId = 1))
        r.poll(pass = passStats(lapId = 2))
        r.poll(pass = passStats(lapId = 3), seconds = 3.0) // ignored: closer than the 8 s minimum
        val before = r.model.events.size
        r.model.setLapRules(r.model.lapRules.of(minLapOn = false))
        assertEquals("no new events", before, r.model.events.size)
        assertEquals("but the seat counted it", 2, r.model.seats[0].laps.passes)
        assertEquals(3.0, r.model.seats[0].laps.lastLapS ?: 0.0, 0.001)
    }

    @Test
    fun `a channel change ends the run without losing the laps`() {
        val r = Rig()
        r.start()
        r.poll(pass = passStats(lapId = 1))
        r.poll(pass = passStats(lapId = 2))
        r.poll(pass = passStats(lapId = 3), seconds = 10.0)
        val passes = r.model.seats[0].laps.passes
        r.model.writeSeats(listOf("B1", "B2"))
        assertEquals("the other pilot's laps stay", passes, r.model.seats[0].laps.passes)
        r.poll(pass = passStats(lapId = 4), seconds = 10.0)
        assertNull("and the run is over: its first pass has no lap time", r.model.seats[0].laps.lastLapS)
    }
}

class RhSimLinkTest {
    @Test
    fun `the simulated board laps the way the web one does`() {
        var nowMs = 0L
        val store = Store({ nowMs })
        val link = RhSimLink({ nowMs }) { "metrics=RSSI" }
        val model = RhModel(store, Remember()) { RhMetric.RSSI }
        model.start(link, Bands.names("R"))
        assertTrue("a simulated node answers", model.running)
        // A minute of a board measuring, polled the way the server polls it.
        repeat(600) {
            nowMs += 100
            model.poll()
        }
        val passes = model.events.filter { it.kind == RhEventKind.PASS && !it.ignored }
        assertTrue("the quads went around, saw ${passes.size} passes", passes.isNotEmpty())
        assertTrue("and a lap is a lap", passes.any { (it.lapS ?: 0.0) > 1.0 })
        assertNotNull("the seats have levels", model.seats[0].level)
        assertTrue("with a peak each", model.seats.all { it.nodePeak != null })
    }

    @Test
    fun `the simulated board counts its transactions the way a real link does`() {
        var nowMs = 0L
        val link = RhSimLink({ nowMs }) { "metrics=RSSI" }
        val model = RhModel(Store({ nowMs }), Remember()) { RhMetric.RSSI }

        model.start(link, listOf("R1"))
        nowMs = 100
        model.poll()

        assertTrue("the reads it ran", link.reads > 0)
        assertTrue("the writes it ran", link.writes > 0)
        assertEquals("and nothing that failed", 0, link.errors)
    }
}
