// GPL-3.0-or-later.
package com.onebody.widscan58.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.ByteBuffer
import java.nio.ByteOrder

private fun buffer(n: Int) = ByteBuffer.allocate(n).order(ByteOrder.LITTLE_ENDIAN)

/** u8 slot, so the byte writes read like the wire format they build. */
private fun ByteBuffer.b(i: Int, v: Int): ByteBuffer = put(i, v.toByte())

class FrameParserTest {
    @Test
    fun `metrics decodes the header and one channel record`() {
        val p = buffer(34 + 26)
        p.putInt(0, 1234).putInt(4, 5000).putInt(8, 5_658_000)
        p.putInt(12, 230).putInt(16, 170).putInt(20, 3650)
        p.putShort(24, 0.toShort()).putShort(26, (-5234).toShort()).putShort(28, (-6000).toShort())
        p.b(30, 45).b(31, 2).b(32, 4).b(33, 1)
        val o = 34
        p.b(o, 1).b(o + 1, ChannelFlag.SIGNAL or ChannelFlag.SPUR)
        p.putShort(o + 2, (-5234).toShort()).putShort(o + 4, (-5200).toShort())
        p.putShort(o + 6, (-9000).toShort()).putShort(o + 8, 4000.toShort())
        p.putShort(o + 10, Short.MIN_VALUE).putShort(o + 12, 1200.toShort())
        p.putInt(o + 14, 150_000).putInt(o + 18, -20_000)
        p.putShort(o + 22, 5000.toShort()).putShort(o + 24, 10_000.toShort())

        val m = parseMetrics(p.array())
        assertEquals(1234L, m.seq)
        assertEquals(5_658_000L, m.loKhz)
        assertEquals(MetricsMode.FULL, m.mode)
        assertEquals(-52.34, m.floorBin!!, 1e-9)
        assertEquals(-60.0, m.dc!!, 1e-9)
        assertEquals(45, m.gain)
        assertEquals(2, m.hopIndex)
        assertEquals(4, m.hops)

        val c = m.channels.single()
        assertEquals(1, c.id)
        assertEquals(-52.34, c.rssi, 1e-9)
        assertEquals(-52.0, c.rssiEma!!, 1e-9)
        assertEquals(-90.0, c.floor!!, 1e-9)
        assertEquals(40.0, c.snr!!, 1e-9)
        assertNull(c.clearFloor)
        assertEquals(12.0, c.peak!!, 1e-9)
        assertEquals(150_000L, c.peakOffHz)
        assertEquals(-20_000L, c.centroidOffHz)
        assertEquals(5_000_000L, c.obwLoHz)
        assertEquals(10_000_000L, c.obwHiHz)
        assertTrue(c.has(ChannelFlag.SIGNAL) && c.has(ChannelFlag.SPUR))
    }

    @Test
    fun `levels uses the quarter-db codes`() {
        val m = parseLevels(levels(mode = 1, snrCode = 0).array())
        assertEquals(MetricsMode.RSSI, m.mode)
        assertNull(m.floorBin)
        assertEquals(-13.75, m.channels[0].rssi, 1e-9) // code 200
        assertEquals(-18.75, m.channels[1].rssi, 1e-9) // code 180
        assertNull(m.channels[0].floor)
        assertNull(m.channels[0].rssiEma)
    }

    @Test
    fun `levels in snr mode reports both the signal and its floor`() {
        val p = levels(mode = 2, snrCode = 160) // 40 dB
        p.b(30, 200) // per-bin floor: 200 / 2 - 127.5
        val m = parseLevels(p.array())
        assertEquals(MetricsMode.SNR, m.mode)
        assertEquals(-27.5, m.floorBin!!, 1e-9)
        val c = m.channels[0]
        assertEquals(40.0, c.snr!!, 1e-9)
        assertEquals(-53.75, c.floor!!, 1e-9) // rssi - snr
    }

    private fun levels(mode: Int, snrCode: Int): ByteBuffer {
        val p = buffer(32 + 4 * 2)
        p.putInt(0, 7).putInt(4, 100).putInt(8, 5_732_000)
        p.putInt(12, 230).putInt(16, 170).putInt(20, 40)
        p.putShort(24, 0.toShort())
        p.b(26, 50).b(27, 1).b(28, 4).b(29, mode).b(30, 0).b(31, 2)
        p.b(32, 1).b(33, 0).b(34, 200).b(35, snrCode)
        p.b(36, 2).b(37, ChannelFlag.SIGNAL).b(38, 180).b(39, 0)
        return p
    }

    @Test
    fun `spectrum maps codes to dbFS and its own frequency span`() {
        val p = buffer(20 + 3)
        p.putInt(0, 99).putInt(4, 5_658_000).putInt(8, -20_000_000).putInt(12, 156_250)
        p.putShort(16, 3.toShort()).b(18, 1)
        p.b(20, 200).b(21, 255).b(22, 0)

        val s = parseSpectrum(p.array())
        assertEquals(3, s.bins)
        assertEquals(-27.5, s.db[0], 1e-9)
        assertEquals(0.0, s.db[1], 1e-9)
        assertEquals(-127.5, s.db[2], 1e-9)
        assertEquals(5_638_000.0, s.startKhz(), 1e-9)
        assertEquals(5_638_468.75, s.endKhz(), 1e-9)
    }

    @Test
    fun `telemetry decodes rotation rate and link fill`() {
        val p = buffer(59)
        p.putInt(0, 4000).putInt(4, 12_000).putInt(8, 96_000).putInt(12, 60_500)
        p.putShort(44, 250.toShort())
        p.b(58, 1)
        val t = parseTelemetry(p.array())
        assertEquals(4000L, t.tMs)
        assertEquals(60.5, t.rotationHz, 1e-9)
        assertEquals(0.25, t.linkFill, 1e-9)
        assertTrue(t.running)
    }

    @Test
    fun `raw iq chunks assemble into one snapshot and a lost chunk drops it`() {
        val a = RawIqAssembler()
        assertNull(a.add(rawIq(offset = 0, pairs = byteArrayOf(1, 2, 3, 4))))
        val r = a.add(rawIq(offset = 2, pairs = byteArrayOf(5, 6, 7, 8)))!!
        assertEquals(4, r.n)
        assertTrue(r.i.contentEquals(byteArrayOf(1, 3, 5, 7)))
        assertTrue(r.q.contentEquals(byteArrayOf(2, 4, 6, 8)))

        val b = RawIqAssembler()
        b.add(rawIq(offset = 0, pairs = byteArrayOf(1, 2)))
        assertNull(b.add(rawIq(offset = 2, pairs = byteArrayOf(1, 2, 9, 9)))) // a chunk went missing
    }

    private fun rawIq(offset: Int, pairs: ByteArray): ByteArray {
        val p = ByteArray(14 + pairs.size)
        val b = ByteBuffer.wrap(p).order(ByteOrder.LITTLE_ENDIAN)
        b.putInt(0, 5_658_000).putInt(4, 80_000_000)
        b.putShort(8, 4.toShort()).putShort(10, offset.toShort()).b(12, 0).b(13, 2)
        pairs.copyInto(p, 14)
        return p
    }
}
