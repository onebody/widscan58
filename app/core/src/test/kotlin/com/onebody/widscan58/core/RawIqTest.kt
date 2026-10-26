// GPL-3.0-or-later.
package com.onebody.widscan58.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.log10
import kotlin.math.roundToInt
import kotlin.math.sin
import kotlin.math.sqrt

private const val HDR = 14

/** One wire chunk: the 14-byte header the firmware writes, then interleaved I/Q pairs. */
private fun rawiqPayload(loKhz: Long, rateHz: Long, n: Int, offset: Int, i: ByteArray, q: ByteArray, shift: Int) =
    ByteBuffer.allocate(HDR + 2 * i.size).order(ByteOrder.LITTLE_ENDIAN).apply {
        putInt(loKhz.toInt())
        putInt(rateHz.toInt())
        putShort(n.toShort())
        putShort(offset.toShort())
        put(0.toByte())
        put(shift.toByte())
        for (j in i.indices) {
            put(i[j])
            put(q[j])
        }
    }.array()

private fun rawiqOf(i: ByteArray, q: ByteArray, shift: Int = 0) =
    RawIq(5_733_000, 80_000_000, i.size, false, shift, i, q)

class RawIqTest {
    @Test
    fun `a snapshot reports its DC, its loudest count and its AC power`() {
        val s = RawIqAnalysis.stats(rawiqOf(byteArrayOf(2, -2, 4, 0), byteArrayOf(1, 1, -1, -1)))
        assertEquals(1.0, s.meanI, 1e-9)
        assertEquals(0.0, s.meanQ, 1e-9)
        assertEquals(4, s.peak)
        assertEquals(sqrt(6.0), s.rms, 1e-9)
        // The wire's counts are a 10-bit sample shifted down, so one of them is 4 LSB wide.
        assertEquals(4.0, s.scale(2), 1e-9)
    }

    @Test
    fun `a full-scale tone reads zero dBFS in the bin it belongs to`() {
        val nfft = RawIqAnalysis.FFT_POINTS
        val bin = 100
        val n = 2 * nfft
        val i = ByteArray(n)
        val q = ByteArray(n)
        // shift 2 puts the 10-bit full scale, 512, at 128 transferred counts; the wire is signed
        // bytes, so 127 is as loud as a tone can be.
        for (j in 0 until n) {
            val ph = 2 * PI * bin * j / nfft
            i[j] = (127 * cos(ph)).roundToInt().toByte()
            q[j] = (127 * sin(ph)).roundToInt().toByte()
        }
        val r = rawiqOf(i, q, shift = 2)
        val dbs = RawIqAnalysis.spectrum(r, RawIqAnalysis.stats(r))

        val peak = dbs.indices.maxBy { dbs[it] }
        assertEquals("bin 100 of a shifted spectrum sits above the centre", nfft / 2 + bin, peak)
        assertEquals(20 * log10(127.0 / 128), dbs[peak], 0.2)
        assertTrue("the rest of the span stays down", dbs[nfft / 2] < -80)
    }

    @Test
    fun `a capture becomes a snapshot only once every chunk has arrived`() {
        val s = Store(clock = { 0L }).also { it.setProfile(profileOf(channel(0))) }
        val n = 2 * RawIqAnalysis.FFT_POINTS
        val first = ByteArray(n / 2) { (it % 7).toByte() }
        val second = ByteArray(n / 2) { (it % 5).toByte() }

        s.ingest(frame(FrameType.RAWIQ, rawiqPayload(5_658_000, 20_000_000, n, 0, first, second, shift = 2)))
        assertNull("the head of a capture is not a capture", s.rawiq)

        s.ingest(
            frame(FrameType.RAWIQ, rawiqPayload(5_658_000, 20_000_000, n, n / 2, second, first, shift = 2)),
        )
        val r = s.rawiq!!
        assertEquals(5_658_000L, r.loKhz)
        assertEquals(20_000_000L, r.rateHz)
        assertEquals(n, r.n)
        assertEquals(2, r.shift)
        assertEquals(first[3], r.i[n / 2 + 3])
        assertEquals(second[3], r.q[n / 2 + 3])
    }

    @Test
    fun `the spectrum's axis rounds out to the decades the grid is labelled with`() {
        assertEquals(-40.0 to 25.0, RawIqAnalysis.bounds(doubleArrayOf(-37.0, 12.0, -3.5)))
    }
}
