// GPL-3.0-or-later.
package com.onebody.widscan58.core

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** A byte of a reply, as the unsigned level code the node means it to be. */
private fun ByteArray.u8(o: Int = 0): Int = this[o].toInt() and 0xff

/** A board in RotorHazard mode, driven the way the wire drives it. */
private class Board(
    var nowMs: Long = 0L,
    inBand: (Int) -> Boolean = { it in 5600..6000 },
) {
    val sent = ArrayList<ByteArray>()
    val device = RhDevice({ nowMs }, { sent += it }, inBand) { "cfg" }

    /** What the server does first: give a seat a frequency it can tune, so it starts measuring. */
    fun live() = write(Rh.WRITE_FREQUENCY, 0x16, 0x2A) // 5674

    /** Bytes onto the wire, one at a time. */
    fun rx(bytes: ByteArray) = bytes.forEach { device.input(it.toInt() and 0xff) }

    /** What a read put on the wire, checksum included; null when the node stayed silent. */
    fun raw(cmd: Int): ByteArray? {
        sent.clear()
        device.input(cmd)
        return sent.singleOrNull()
    }

    /** The same reply's payload, and only while its checksum adds up. */
    fun read(cmd: Int): ByteArray? = raw(cmd)?.let { rhCheckReply(it, it.size - 1) }

    fun write(cmd: Int, vararg payload: Int) = rx(rhEncodeWrite(cmd, intArrayOf(*payload)))

    fun text(line: String): String? = device.text(line)

    /** Levels for the seat the server selected, one millisecond apart. */
    fun feed(from: Int, to: Int, level: Int) {
        for (k in from until to) {
            nowMs = k.toLong()
            device.feed(0, k.toLong(), level)
        }
    }
}

class RhDeviceTest {
    @Test
    fun `a read is answered at once with its payload and a sum`() {
        val b = Board()
        assertArrayEquals(byteArrayOf(8, 8), b.raw(Rh.READ_MULTINODE_COUNT))
        assertArrayEquals(byteArrayOf(0, 0), b.raw(Rh.READ_ADDRESS))
        assertArrayEquals(
            "the takeover probe: marker 0x25, API 36", byteArrayOf(0x25, 36, (0x25 + 36).toByte()),
            b.raw(Rh.READ_REVISION_CODE)
        )
        assertArrayEquals(byteArrayOf(0, 0, 0), b.raw(Rh.READ_RHFEAT_FLAGS))
        b.nowMs = 1_000_000
        assertArrayEquals(
            byteArrayOf(0, 0x0F, 0x42, 0x40, (0x0F + 0x42 + 0x40).toByte()),
            b.raw(Rh.READ_TIME_MILLIS)
        )
    }

    @Test
    fun `a text read answers with its padded block`() {
        val b = Board()
        val fw = b.read(Rh.READ_FW_VERSION)!!
        assertEquals(16, fw.size)
        assertEquals("0.3.0-sim", String(fw, Charsets.US_ASCII).trimEnd('\u0000'))
        assertEquals("Widscan58 c5", String(b.read(Rh.READ_FW_PROCTYPE)!!, Charsets.US_ASCII).trimEnd('\u0000'))
    }

    @Test
    fun `a read the node does not know gets no reply at all`() {
        val b = Board()
        assertNull(b.read(0x01))
        assertNull(b.read(0x3b))
        assertTrue("and nothing was queued", b.sent.isEmpty())
    }

    @Test
    fun `the seat index selects the seat for every command after it`() {
        val b = Board()
        b.write(Rh.WRITE_CURNODE_INDEX, 2)
        assertEquals(2, b.read(Rh.READ_CURNODE_INDEX)!!.u8())
        b.write(Rh.WRITE_FREQUENCY, 0x16, 0x2A) // 5674
        b.write(Rh.WRITE_CURNODE_INDEX, 0)
        assertEquals(5658, rhBe16(b.read(Rh.READ_FREQUENCY)!!)) // the profile's channel, untouched
        b.write(Rh.WRITE_CURNODE_INDEX, 2)
        assertEquals(5674, rhBe16(b.read(Rh.READ_FREQUENCY)!!))
    }

    @Test
    fun `an impossible frequency is discarded and off is only a seat that maps to nothing`() {
        val b = Board()
        b.write(Rh.WRITE_FREQUENCY, 0x00, 0x32) // 50 MHz
        assertEquals("kept what it had", 5658, rhBe16(b.read(Rh.READ_FREQUENCY)!!))
        b.write(Rh.WRITE_FREQUENCY, 0x04, 0x57) // 1111: RotorHazard's "off"
        assertEquals(Rh_FREQ_OFF, rhBe16(b.read(Rh.READ_FREQUENCY)!!))
        assertEquals("a frequency the profile cannot tune", 0, b.read(Rh.TEST_RX_REGISTER)!!.u8())
        b.write(Rh.WRITE_FREQUENCY, 0x16, 0x2A)
        assertEquals(1, b.read(Rh.TEST_RX_REGISTER)!!.u8())
    }

    @Test
    fun `a write with a wrong sum is thrown away without a word`() {
        val b = Board()
        b.rx(byteArrayOf(Rh.WRITE_CURNODE_INDEX.toByte(), 5, 0)) // the sum should be 5
        assertTrue("and never answered", b.sent.isEmpty())
        assertEquals("so the seat index never moved", 0, b.read(Rh.READ_CURNODE_INDEX)!!.u8())
    }

    @Test
    fun `a write whose payload stopped arriving is abandoned`() {
        val b = Board()
        b.rx(byteArrayOf(Rh.WRITE_FREQUENCY.toByte(), 0x16)) // the second byte never comes
        b.nowMs += 101
        assertArrayEquals(
            "the next byte starts a command again",
            byteArrayOf(8, 8),
            b.raw(Rh.READ_MULTINODE_COUNT)
        )
    }

    @Test
    fun `a level the node never measured reads one, not none`() {
        val b = Board(inBand = { false })
        val ps = rhParsePassStats(b.read(Rh.READ_LAP_PASS_STATS)!!)
        assertEquals("so the app must not show it as a measurement", 1, ps.level)
        assertEquals(0, ps.nodePeak)
        assertEquals(0, ps.lapId)
        val ex = rhParseExtremums(b.read(Rh.READ_LAP_EXTREMUMS)!!)
        assertFalse("and no crossing is running", ex.crossing)
        assertNull("with nothing queued", ex.extremum)
    }

    @Test
    fun `a seat that maps to nothing stays dead however strong the signal`() {
        val b = Board(inBand = { false })
        b.live()
        b.feed(0, 400, 240)
        assertEquals(1, rhParsePassStats(b.read(Rh.READ_LAP_PASS_STATS)!!).level)
    }

    @Test
    fun `a pass counts on the wire with its age the level and the loop`() {
        val b = Board()
        b.live()
        b.text("LEVELFILTER EMA")
        b.write(Rh.WRITE_ENTER_AT_LEVEL, 120)
        b.write(Rh.WRITE_EXIT_AT_LEVEL, 100)
        b.feed(0, 30, 60)
        b.feed(30, 60, 200)
        b.nowMs = 60
        assertTrue("the crossing flag is on the wire", rhParseExtremums(b.read(Rh.READ_LAP_EXTREMUMS)!!).crossing)
        b.feed(60, 90, 60)
        b.nowMs = 100
        val ps = rhParsePassStats(b.read(Rh.READ_LAP_PASS_STATS)!!)
        assertEquals(1, ps.lapId)
        assertEquals(200, ps.passPeak)
        assertTrue("and the pass is a few tens of milliseconds old", ps.msSinceLap in 10..100)
        assertEquals(1000, ps.loopUs)
        assertFalse(rhParseExtremums(b.read(Rh.READ_LAP_EXTREMUMS)!!).crossing)
    }

    @Test
    fun `the extremum queue is popped one read at a time, oldest first`() {
        val b = Board()
        b.live()
        b.text("LEVELFILTER EMA")
        b.feed(0, 30, 60)
        b.feed(30, 60, 200)
        b.feed(60, 90, 60)
        b.feed(90, 120, 200) // only a reversal closes the nadir
        b.nowMs = 200
        val first = rhParseExtremums(b.read(Rh.READ_LAP_EXTREMUMS)!!).extremum!!
        assertTrue("the peak closed first", first.peak)
        assertEquals(200, first.level)
        assertTrue(first.msAgo in 1..200)
        val second = rhParseExtremums(b.read(Rh.READ_LAP_EXTREMUMS)!!).extremum!!
        assertFalse(second.peak)
        assertEquals(60, second.level)
        assertNull("and the rise still running is not queued yet", b.read(Rh.READ_LAP_EXTREMUMS)?.let { rhParseExtremums(it).extremum })
    }

    @Test
    fun `forcing the end of a crossing counts the lap`() {
        val b = Board()
        b.live()
        b.text("LEVELFILTER EMA")
        b.feed(0, 40, 200)
        assertTrue(rhParseExtremums(b.read(Rh.READ_LAP_EXTREMUMS)!!).crossing)
        b.write(Rh.FORCE_END_CROSSING, 0)
        assertEquals(1, rhParsePassStats(b.read(Rh.READ_LAP_PASS_STATS)!!).lapId)
    }

    @Test
    fun `the thresholds the server writes are the ones the seat answers`() {
        val b = Board()
        assertEquals("the firmware's own defaults", 125, b.read(Rh.READ_ENTER_AT_LEVEL)!!.u8())
        assertEquals(105, b.read(Rh.READ_EXIT_AT_LEVEL)!!.u8())
        b.write(Rh.WRITE_ENTER_AT_LEVEL, 150)
        b.write(Rh.WRITE_EXIT_AT_LEVEL, 130)
        assertEquals(150, b.read(Rh.READ_ENTER_AT_LEVEL)!!.u8())
        assertEquals(130, b.read(Rh.READ_EXIT_AT_LEVEL)!!.u8())
        b.write(Rh.WRITE_CURNODE_INDEX, 4)
        assertEquals("and they belong to one seat only", 125, b.read(Rh.READ_ENTER_AT_LEVEL)!!.u8())
    }

    @Test
    fun `a line this mode does not allow is refused and a stray scan byte is not a line at all`() {
        val b = Board()
        assertEquals("PROTOCOL RH", b.text("PROTOCOL?"))
        assertTrue(b.text("CONFIG?")!!.startsWith("CONFIG "))
        assertNotNull(b.text("INFO"))
        assertEquals("ERR rh_mode", b.text("CHANNELS R1=5658"))
        assertEquals("ERR rh_mode", b.text("RECAL ALL"))
        // Our commands all start A..P or T, so a scan line arrives as a dropped byte and a tail.
        b.sent.clear()
        b.rx("SCAN START\n".toByteArray(Charsets.US_ASCII))
        assertEquals(1, b.sent.size)
        assertEquals("ERR rh_mode", String(b.sent.single(), Charsets.US_ASCII).trim())
    }

    @Test
    fun `the metric and the filter the receiver card sets are the ones CONFIG says`() {
        val b = Board()
        b.text("METRICS SNR")
        assertEquals(RhMetric.SNR, b.device.config.metric)
        b.text("LEVELFILTER MEDIAN")
        assertEquals(RhLapFilter.MEDIAN, b.device.config.levelFilter)
        b.text("GAIN MANUAL 60")
        assertEquals(60, b.device.config.gain)
        b.text("FFT 512")
        assertEquals(512, b.device.config.nfft)
        b.text("MODE SINGLE")
        assertEquals(LoPlan.SINGLE, b.device.config.mode)
        b.text("FFT 100")
        assertEquals("a size the board cannot do is refused", 512, b.device.config.nfft)
        b.text("GAIN MANUAL 200")
        assertEquals("and so is a gain above the board's ceiling", 60, b.device.config.gain)
        b.text("METRICS NOISE")
        assertEquals("and so is a metric it does not have", RhMetric.SNR, b.device.config.metric)
        b.text("CONFIG RESET")
        assertEquals("back to the built-in settings", RhMetric.RSSI, b.device.config.metric)
        assertEquals(GainMode.MANUAL, b.device.config.gainMode)
        assertEquals(45, b.device.config.gain)
        assertEquals(256, b.device.config.nfft)
        assertEquals(LoPlan.GROUP, b.device.config.mode)
        assertEquals(RhLapFilter.MEDIAN, b.device.config.levelFilter)
    }

    @Test
    fun `a metric change forgets every seat's levels`() {
        val b = Board()
        b.live()
        b.text("LEVELFILTER EMA")
        b.feed(0, 40, 200)
        assertEquals(200, rhParsePassStats(b.read(Rh.READ_LAP_PASS_STATS)!!).level)
        b.text("METRICS SNR")
        assertEquals(1, rhParsePassStats(b.read(Rh.READ_LAP_PASS_STATS)!!).level)
    }

    @Test
    fun `leaving the mode puts the board back to the monitor protocol`() {
        val b = Board()
        assertEquals("OK", b.text("PROTOCOL MONITOR"))
        assertEquals("MONITOR", b.device.protocol)
        assertEquals("OK", b.text("PROTOCOL RH"))
        assertEquals("RH", b.device.protocol)
    }
}
