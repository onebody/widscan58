// GPL-3.0-or-later.
package com.onebody.widscan58.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.log10

class StoreTest {
    private var ms = 0L
    private val clock = { ms }

    private fun store(codec: ReplyCodec = NoReplies): Store {
        val s = Store(clock, codec)
        s.setProfile(profileOf())
        return s
    }

    @Test
    fun `a channel plan change rebuilds the per-channel state`() {
        val s = store()
        s.ingest(frame(FrameType.LEVELS, levelsOf(5_658_000, 1, 0, levelOf(2, rssiDb = -55.0))))
        assertEquals(1, s.channels[2].rssi.size)

        s.setProfile(profileOf(channel(0), channel(1)))
        assertEquals(2, s.channels.size)
        assertEquals(0, s.channels[0].rssi.size)

        s.ingest(spectrumFrame(5_658_000, 200))
        assertEquals(1, s.segments.size)
        s.ingest(frame(FrameType.LEVELS, levelsOf(5_658_000, 1, 0, levelOf(1, rssiDb = -60.0))))
        val key = s.planKey
        s.setProfile(profileOf(channel(0), channel(1)))
        assertEquals("the same plan keeps the history", key, s.planKey)
        assertEquals(1, s.segments.size)
        assertEquals(1, s.channels[1].rssi.size)
    }

    @Test
    fun `metrics stamp the channel record and its series`() {
        val s = store()
        ms = 500
        s.ingest(frame(FrameType.LEVELS, levelsOf(5_658_000, 1, 0, levelOf(3, ChannelFlag.SIGNAL, rssiDb = -55.0))))
        val cs = s.channels[3]
        assertEquals(0.5, cs.updated, 1e-9)
        assertEquals(5_658_000L, cs.loKhz)
        assertEquals(-55.0, cs.latest!!.rssi, 1e-9)
        assertEquals(1, cs.rssi.size)
        assertTrue(cs.latest!!.has(ChannelFlag.SIGNAL))
    }

    @Test
    fun `channels outside the plan are ignored`() {
        val s = Store(clock)
        s.setProfile(profileOf(channel(0)))
        s.ingest(frame(FrameType.LEVELS, levelsOf(5_658_000, 1, 0, levelOf(5, rssiDb = -55.0))))
        assertEquals(1, s.channels.size)
        assertNull(s.channels[0].latest)
    }

    @Test
    fun `the smoothed series falls back to raw rssi without an ema`() {
        val cs = ChannelState(8)
        cs.rssi.push(0.0, -50f)
        cs.ema.push(0.0, -49f)
        assertSame(cs.ema, smoothedSeries(cs))
        cs.latest = metrics(-50.0, null)
        assertSame(cs.rssi, smoothedSeries(cs))
        cs.latest = metrics(-50.0, -49.0)
        assertSame(cs.ema, smoothedSeries(cs))
    }

    private fun metrics(rssi: Double, ema: Double?) = ChannelMetrics(
        id = 0,
        flags = 0,
        rssi = rssi,
        rssiEma = ema,
        floor = null,
        snr = null,
        clearFloor = null,
        peak = null,
        peakOffHz = null,
        centroidOffHz = null,
        obwLoHz = null,
        obwHiHz = null,
    )

    @Test
    fun `max hold never decays and the average follows the power domain`() {
        val s = store()
        s.ingest(spectrumFrame(5_658_000, specCode(-40.0), specCode(-40.0)))
        s.ingest(spectrumFrame(5_658_000, specCode(-80.0), specCode(-80.0)))
        val seg = s.segments[5_658_000L]!!
        assertEquals(-80.0, seg.latest.db[0], 1e-9)
        assertEquals(-40.0, seg.maxHold[0], 1e-9)
        // 1e-4 + 0.05 * (1e-8 - 1e-4) in linear power
        assertEquals(-40.2225, 10 * log10(seg.avg[0]), 0.001)

        s.resetHolds()
        assertEquals(-80.0, seg.maxHold[0], 1e-9)
        assertEquals(-80.0, 10 * log10(seg.avg[0]), 1e-6)
    }

    @Test
    fun `a segment with a different bin count restarts its holds`() {
        val s = store()
        s.ingest(spectrumFrame(5_658_000, specCode(-40.0), specCode(-40.0)))
        s.ingest(spectrumFrame(5_658_000, specCode(-80.0), specCode(-80.0), specCode(-80.0)))
        val seg = s.segments[5_658_000L]!!
        assertEquals(3, seg.maxHold.size)
        assertEquals(-80.0, seg.maxHold[0], 1e-9)
    }

    @Test
    fun `a segment whose lo left the plan is dropped after three seconds`() {
        val s = store()
        s.setStatus(statusOf(5_658_000))
        s.ingest(spectrumFrame(5_658_000, 200))
        ms = 1_000
        s.ingest(spectrumFrame(5_695_000, 200))
        assertEquals(2, s.segments.size)
        ms = 2_500 // still inside the window
        s.ingest(spectrumFrame(5_658_000, 200))
        assertTrue(s.segments.containsKey(5_695_000L))
        ms = 4_500
        s.ingest(spectrumFrame(5_658_000, 200))
        assertFalse(s.segments.containsKey(5_695_000L))
        assertTrue(s.segments.containsKey(5_658_000L))
    }

    @Test
    fun `the per-lo floor is only there when the device measured one`() {
        val s = store()
        s.ingest(frame(FrameType.LEVELS, levelsOf(5_658_000, 2, specCode(-90.0), levelOf(0, rssiDb = -50.0, snrDb = 20.0))))
        assertEquals(-90.0, s.floorByLo[5_658_000L]!!, 1e-9)
        val c = s.channels[0].latest!!
        assertEquals(20.0, c.snr!!, 1e-9)
        assertEquals(-70.0, c.floor!!, 1e-9)
        s.ingest(frame(FrameType.LEVELS, levelsOf(5_658_000, 1, 0, levelOf(0, rssiDb = -50.0))))
        assertFalse(s.floorByLo.containsKey(5_658_000L))
    }

    @Test
    fun `text replies decode info spurs and the profile`() {
        val codec = object : ReplyCodec {
            override fun profile(json: String): Profile? = if (json.contains("narrow")) profileOf(channel(0)) else null
            override fun status(json: String): Status? = if (json.contains("narrow")) statusOf(5_658_000) else null
            override fun gpio(json: String): List<GpioPin>? = if (json.contains("narrow")) parseGpio(1, "-", "L") else null
        }
        val s = store(codec)
        s.ingest(StreamItem.Text("INFO widscan58|c5-race|1|0.9.0|2026-10-19|1"))
        assertEquals("widscan58|c5-race|1|0.9.0|2026-10-19|1", s.info)
        s.ingest(StreamItem.Text("SPURS 2 5658000 5917000 extra"))
        assertEquals(listOf(5_658_000.0, 5_917_000.0), s.spursKhz)
        s.ingest(StreamItem.Text("PROFILE {\"name\":\"narrow\"}"))
        assertEquals(1, s.channels.size)
        s.ingest(StreamItem.Text("STATUS {\"name\":\"narrow\"}"))
        assertNotNull(s.status)
        s.ingest(StreamItem.Text("GPIO {\"name\":\"narrow\"}"))
        assertEquals(GpioSetting.LOW, s.gpio.single().saved)
        s.ingest(StreamItem.Text("PROFILE {\"name\":\"other\"}"))
        assertEquals("an undecodable reply leaves the state alone", 1, s.channels.size)
    }

    @Test
    fun `the console ring keeps what the link said`() {
        val s = store()
        s.addLog(LogDir.TX, "GAIN MANUAL 45")
        s.ingest(StreamItem.Text("OK"))
        s.ingest(StreamItem.Text("ERR rh_mode"))
        s.ingest(frame(FrameType.LOG, "REGDIFF t=1234 lo=5658000".toByteArray()))
        assertEquals(
            listOf("> GAIN MANUAL 45", "< OK", "< ERR rh_mode", "< REGDIFF t=1234 lo=5658000"),
            s.log.map { it.dir.arrow + " " + it.text },
        )
        assertEquals(listOf(LogDir.TX, LogDir.RX, LogDir.ERROR, LogDir.INFO), s.log.map { it.dir })
    }

    @Test
    fun `an undecodable reply is an error line as well as a dropped state`() {
        val s = store()
        s.ingest(StreamItem.Text("PROFILE {\"name\":\"x\"}"))
        assertEquals(
            listOf("bad PROFILE reply; ignored", "PROFILE {\"name\":\"x\"}"),
            s.log.map { it.text },
        )
        assertEquals(listOf(LogDir.ERROR, LogDir.RX), s.log.map { it.dir })
    }

    @Test
    fun `the console ring drops its oldest line and caps one line's text`() {
        val s = store()
        for (j in 0 until Store.LOG_MAX + 20) s.addLog(LogDir.RX, "line $j")
        assertEquals(Store.LOG_MAX, s.log.size)
        assertEquals("line 20", s.log.first().text)
        s.addLog(LogDir.RX, "x".repeat(320))
        assertEquals("a long reply is cut with the web's ellipsis", 302, s.log.last().text.length)
    }

    @Test
    fun `series keep the newest points and iterate oldest first`() {
        val s = Series(4)
        for (j in 0 until 6) s.push(j.toDouble(), j.toFloat())
        assertEquals(4, s.size)
        assertEquals(5f, s.newest()!!)
        val seen = ArrayList<Double>()
        s.forEach { t, _ -> seen += t }
        assertEquals(listOf(2.0, 3.0, 4.0, 5.0), seen)
        s.clear()
        assertEquals(0, s.size)
        assertNull(Series(2).newest())
    }

    @Test
    fun `a window walk skips what fell off the left edge`() {
        val s = Series(8)
        for (j in 0 until 8) s.push(j.toDouble(), j.toFloat())
        val seen = ArrayList<Double>()
        // now=7 and a 3 s span reaches back to 4.0 inclusive.
        s.forEachWithin(7.0, 3.0) { t, _ -> seen += t }
        assertEquals(listOf(4.0, 5.0, 6.0, 7.0), seen)

        val wrapped = ArrayList<Float>()
        for (j in 8 until 12) s.push(j.toDouble(), j.toFloat())
        s.forEachWithin(11.0, 2.5) { _, v -> wrapped += v }
        assertEquals(listOf(9f, 10f, 11f), wrapped)

        val boundary = ArrayList<Double>()
        s.forEachWithin(11.0, 0.0) { t, _ -> boundary += t }
        assertEquals("a zero span still keeps the sample taken now", listOf(11.0), boundary)

        val past = ArrayList<Double>()
        s.forEachWithin(12.0, 0.5) { t, _ -> past += t }
        assertEquals(emptyList<Double>(), past)
    }

    @Test
    fun `the history ring holds the widest span the UI offers`() {
        val s = Store(clock)
        s.setProfile(profileOf(channel(0)))
        // 250 samples per second, so a 120 s window needs 120 * 250 slots before it wraps.
        assertEquals(120 * 250, s.channels[0].rssi.capacity)
    }
}
