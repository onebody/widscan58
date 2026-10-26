// The power meter's math: the calibration curve, the averaging that ignores clipped
// captures, and what one channel's card ends up showing. GPL-3.0-or-later.
package com.onebody.widscan58.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.pow
import kotlin.math.log10

class PowerTest {
    private var ms = 0L
    private val clock = { ms }

    private fun seriesOf(vararg values: Pair<Double, Float>): Series {
        val s = Series(16)
        values.forEach { (t, v) -> s.push(t, v) }
        return s
    }

    private fun powerStatus(
        gainMode: String = "MANUAL",
        gain: Int = Power.GAIN,
        rssiHz: Long = Power.WIN_HZ,
        mode: String = "SINGLE",
        specEvery: Int? = null,
    ) = statusOf(5_658_000).let { s ->
        Status(
            profile = s.profile, running = s.running, enabled = s.enabled, mode = mode,
            captureSamples = s.captureSamples, nfft = s.nfft, segments = s.segments,
            rateCode = s.rateCode, filter = s.filter, conjugate = s.conjugate, iqCorr = s.iqCorr,
            settleUs = s.settleUs, tskip = s.tskip, gainMode = gainMode, gain = gain,
            gainMax = s.gainMax, rawiqMax = s.rawiqMax, recal = s.recal, metrics = s.metrics,
            rssiHz = rssiHz, searchHz = s.searchHz, dcGuard = s.dcGuard, ema = s.ema,
            clearEma = s.clearEma, signalDb = s.signalDb, avgShift = s.avgShift,
            specEvery = specEvery ?: s.specEvery, specBins = s.specBins, specSpanHz = s.specSpanHz,
            specDetector = s.specDetector, plan = s.plan,
        )
    }

    @Test
    fun `the power scan is the one the calibration was measured with`() {
        assertEquals(
            listOf(
                "MODE SINGLE",
                "GAIN MANUAL 0",
                "WIN 15000000 15000000",
                "METRICS FULL",
                "SPEC 0 256 40000000 MAX",
            ),
            Power.commands(40_000_000),
        )
    }

    @Test
    fun `the monitor scan is put back per profile when no snapshot was taken`() {
        assertEquals(5_000_000L, Power.MONITOR_DEFAULTS.getValue("c5-race").rssiHz)
        assertEquals(
            "SPEC 4 256 11300000 MAX",
            Power.MONITOR_DEFAULTS.getValue("s3-race24").spec,
        )
        assertNull(Power.MONITOR_DEFAULTS["other-board"])
    }

    @Test
    fun `a scan is the meter's by its window and its spectrum hops`() {
        assertFalse(Power.isPowerScan(statusOf(5_658_000)))
        assertFalse(Power.isPowerScan(null))
        assertTrue(Power.isPowerScan(powerStatus(specEvery = 0)))
    }

    @Test
    fun `entering the meter snapshots the running scan, but never itself`() {
        val monitor = statusOf(5_658_000)
        val snapshot = Power.monitorScan(monitor)!!
        assertEquals(
            listOf("WIN ${monitor.rssiHz} ${monitor.searchHz}", "SPEC 4 256 40000000 MEAN"),
            snapshot.commands(),
        )
        assertNull(Power.monitorScan(powerStatus(specEvery = 0)))
        assertNull(Power.monitorScan(null))
    }

    @Test
    fun `the calibration interpolates between the measured channels`() {        assertEquals(27.4, Power.calOffset("c5-race", 5658)!!.db, 1e-9)
        assertEquals(25.2, Power.calOffset("c5-race", 5880)!!.db, 1e-9)
        // 5750 MHz lies between 5732/26.8 and 5769/26.1.
        assertEquals(26.8 + (26.1 - 26.8) * 18 / 37, Power.calOffset("c5-race", 5750)!!.db, 1e-9)
        assertTrue(Power.calOffset("c5-race", 5750)!!.calibrated)
    }

    @Test
    fun `past the measured points the offset holds for 10 MHz and then counts as uncalibrated`() {
        assertEquals(27.4, Power.calOffset("c5-race", 5500)!!.db, 1e-9)
        assertTrue(Power.calOffset("c5-race", 5648)!!.calibrated)
        assertFalse(Power.calOffset("c5-race", 5647)!!.calibrated)
        assertEquals(26.1, Power.calOffset("c5-race", 6100)!!.db, 1e-9)
        assertTrue(Power.calOffset("c5-race", 5927)!!.calibrated)
        assertFalse(Power.calOffset("c5-race", 5928)!!.calibrated)
    }

    @Test
    fun `a profile that was never calibrated has no offset`() {
        assertNull(Power.calOffset("s3-race24", 5800))
    }

    @Test
    fun `averaging is over linear power, not the dB numbers`() {
        val s = seriesOf(0.0 to -60f, 0.1 to -50f)
        // (1e-6 + 1e-5) / 2, back into dB.
        assertEquals(10 * log10((10.0.pow(-6.0) + 10.0.pow(-5.0)) / 2), Power.meanDb(s, 0.2, 1.0)!!, 1e-9)
    }

    @Test
    fun `no smoothing is the newest hop and an empty window falls back to it`() {
        val s = seriesOf(0.0 to -60f, 0.1 to -50f)
        assertEquals(-50.0, Power.meanDb(s, 0.2, 0.0)!!, 1e-9)
        assertEquals("nothing in the window", -50.0, Power.meanDb(s, 9.0, 1.0)!!, 1e-9)
        assertNull(Power.meanDb(Series(4), 0.0, 1.0))
    }

    @Test
    fun `the average leaves out the captures that clipped`() {
        val s = seriesOf(0.0 to -50f, 0.1 to -20f, 0.2 to -50f)
        val without = Power.meanDb(s, 0.3, 1.0, setOf(0.1))!!
        assertEquals(Power.meanDb(seriesOf(0.0 to -50f, 0.2 to -50f), 0.3, 1.0)!!, without, 1e-9)
        assertTrue("the clipped hop alone would sit 30 dB above it", without < -49.0)
    }

    @Test
    fun `clippedIn counts the window's captures, not the ring's`() {
        val s = seriesOf(0.0 to 1f, 0.1 to 0f, 0.2 to 1f, 5.0 to 1f)
        val clip = Power.clippedIn(s, 5.2, 1.0)
        assertEquals("only the last hop is in the window", setOf(5.0), clip.times)
        assertEquals(1.0, clip.share, 1e-9)
        val wide = Power.clippedIn(s, 5.2, 10.0)
        assertEquals(3, wide.times.size)
        assertEquals(0.75, wide.share, 1e-9)
        assertEquals(0.0, Power.clippedIn(Series(4), 1.0, 1.0).share, 1e-9)
    }

    @Test
    fun `a signal within 3 dB of its window's noise is not a signal`() {
        assertNull(Power.lessNoise(-59.0, -60.0))
        assertNull(Power.lessNoise(-61.0, -60.0))
        val expected = 10 * log10(10.0.pow(-5.0) - 10.0.pow(-6.0))
        assertEquals(expected, Power.lessNoise(-50.0, -60.0)!!, 1e-9)
        assertEquals("a board with no floor measured still reads", -50.0, Power.lessNoise(-50.0, null)!!, 1e-9)
    }

    @Test
    fun `the readout switches units where the web does`() {
        assertEquals("4.94 mW", Power.formatMw(6.94))
        assertEquals("39.8 mW", Power.formatMw(16.0))
        assertEquals("100 mW", Power.formatMw(20.0))
        assertEquals("501 mW", Power.formatMw(27.0))
        assertEquals("1.00 W", Power.formatMw(30.0))
        assertEquals("12.6 W", Power.formatMw(41.0))
        assertEquals("0.010 mW", Power.formatMw(-20.0))
    }

    @Test
    fun `readings only mean the calibrated scan`() {
        assertTrue(Power.scanIsReady(powerStatus()))
        assertFalse("auto gain changes the level", Power.scanIsReady(powerStatus(gainMode = "AUTO")))
        assertFalse(Power.scanIsReady(powerStatus(gain = 45)))
        assertFalse(Power.scanIsReady(powerStatus(rssiHz = 5_000_000)))
        assertFalse(Power.scanIsReady(powerStatus(mode = "GROUP")))
        assertFalse(Power.scanIsReady(null))
    }

    /** A store with one channel's hops at half-second intervals. */
    private fun channelWith(
        vararg hops: Triple<Double, Double, Int>,
        floor: Double? = null,
    ): ChannelState {
        val s = Store(clock, NoReplies)
        s.setProfile(profileOf(channel(0)))
        hops.forEach { (t, rssi, flags) ->
            ms = (t * 1000).toLong()
            s.ingest(
                frame(
                    FrameType.METRICS,
                    metricsOf(MetricChannel(0, rssi, flags, floor = floor)),
                ),
            )
        }
        return s.channels[0]
    }

    @Test
    fun `a card reads the VTX through the attenuator`() {
        val cs = channelWith(Triple(0.4, -50.0, 0), floor = -60.0)
        val r = Power.readout(cs, "c5-race", 5658, attenDb = 30.0, smoothS = 0.5, now = 0.5)!!
        val sig = 10 * log10(10.0.pow(-5.0) - 10.0.pow(-6.0))
        assertEquals(sig + 27.4, r.boardDbm!!, 1e-9)
        assertEquals(sig + 27.4 + 30.0, r.vtxDbm!!, 1e-9)
        assertEquals(-50.0, r.levelDbfs, 1e-9)
        assertEquals(-60.0, r.noiseDbfs!!, 1e-9)
        assertFalse(r.clipping)
        assertFalse(r.overRange)
        assertTrue(r.calibrated)
        assertFalse(r.stale)
    }

    @Test
    fun `most of the window clipped means the level means nothing`() {
        val cs = channelWith(
            Triple(0.1, -20.0, ChannelFlag.SATURATED),
            Triple(0.2, -19.0, ChannelFlag.SATURATED),
            Triple(0.3, -21.0, ChannelFlag.SATURATED),
            Triple(0.4, -50.0, 0),
            floor = -60.0,
        )
        val r = Power.readout(cs, "c5-race", 5658, attenDb = 30.0, smoothS = 1.0, now = 0.5)!!
        assertTrue(r.clipping)
        assertNull(r.boardDbm)
        assertNull(r.vtxDbm)
        // Smoothing off, the latest capture shows as it is: three clipped hops behind it
        // are not this one's business.
        val latest = Power.readout(cs, "c5-race", 5658, attenDb = 30.0, smoothS = 0.0, now = 0.5)!!
        assertFalse(latest.clipping)
        assertEquals(-50.0, latest.levelDbfs, 1e-9)
    }

    @Test
    fun `a weak signal is reported as no signal`() {
        val cs = channelWith(Triple(0.4, -59.0, 0), floor = -60.0)
        val r = Power.readout(cs, "c5-race", 5658, attenDb = 30.0, smoothS = 0.5, now = 0.5)!!
        assertNull(r.signalDbfs)
        assertNull(r.boardDbm)
        assertFalse(r.clipping)
        assertEquals(-59.0, r.levelDbfs, 1e-9)
    }

    @Test
    fun `input above the measured range is flagged while the number still shows`() {
        val cs = channelWith(Triple(0.4, -25.0, 0), floor = -60.0)
        val r = Power.readout(cs, "c5-race", 5658, attenDb = 30.0, smoothS = 0.5, now = 0.5)!!
        assertTrue("−25 dBFS + 27.4 is above −5 dBm", r.boardDbm!! > Power.INPUT_MAX_DBM)
        assertTrue(r.overRange)
        assertNotNull(r.vtxDbm)
    }

    @Test
    fun `a channel that stopped reporting is stale`() {
        val cs = channelWith(Triple(0.4, -50.0, 0), floor = -60.0)
        assertTrue(Power.readout(cs, "c5-race", 5658, 30.0, 0.5, now = 1.5)!!.stale)
        assertFalse(Power.readout(cs, "c5-race", 5658, 30.0, 0.5, now = 1.0)!!.stale)
    }

    @Test
    fun `an uncalibrated board has nothing to show`() {
        val cs = channelWith(Triple(0.4, -50.0, 0), floor = -60.0)
        assertNull(Power.readout(cs, "s3-race24", 5658, 30.0, 0.5, now = 0.5))
        assertNull(Power.readout(ChannelState(4), "c5-race", 5658, 30.0, 0.5, now = 0.5))
    }

    @Test
    fun `every hop leaves a clip mark so the series stay the same length`() {
        val cs = channelWith(
            Triple(0.1, -50.0, 0),
            Triple(0.2, -20.0, ChannelFlag.SATURATED),
        )
        assertEquals(2, cs.rssi.size)
        assertEquals("the clip series is indexed by the same hops", cs.rssi.size, cs.clipped.size)
        val marks = mutableListOf<Float>()
        cs.clipped.forEach { _, v -> marks += v }
        assertEquals(listOf(0f, 1f), marks)
    }
}
