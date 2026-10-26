// GPL-3.0-or-later.
package com.onebody.widscan58.core

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class RhProtocolTest {
    @Test
    fun `a write carries its payload's checksum, not the command's`() {
        // 5658 MHz is 0x161A; 0x16 + 0x1A = 0x30.
        assertArrayEquals(byteArrayOf(0x51, 0x16, 0x1A, 0x30), rhEncodeWrite(Rh.WRITE_FREQUENCY, rhU16(5658)))
        // A seat index alone: the payload sums to itself.
        assertArrayEquals(byteArrayOf(0x7a, 3, 3), rhEncodeWrite(Rh.WRITE_CURNODE_INDEX, intArrayOf(3)))
        // Wraps at 256 like the firmware's byte sum.
        assertArrayEquals(
            byteArrayOf(0x71, 0xFF.toByte(), 0xFF.toByte(), 0xFE.toByte()),
            rhEncodeWrite(Rh.WRITE_ENTER_AT_LEVEL, intArrayOf(255, 255))
        )
    }

    @Test
    fun `a read reply is its payload plus a sum, and nothing else is accepted`() {
        assertArrayEquals(byteArrayOf(125), rhCheckReply(byteArrayOf(125, 125), 1))
        assertNull("the command byte is not in the checksum", rhCheckReply(byteArrayOf(0x0d.toByte(), 125, 125), 1))
        assertNull("a wrong sum", rhCheckReply(byteArrayOf(125, 124), 1))
        assertNull("a reply one byte short", rhCheckReply(byteArrayOf(125), 1))
        assertNull("a reply with a stray byte after the sum", rhCheckReply(byteArrayOf(125, 125, 0), 1))
        assertNull("an empty payload still needs its sum", rhCheckReply(byteArrayOf(), 0))
        assertArrayEquals(byteArrayOf(), rhCheckReply(byteArrayOf(0), 0))
    }

    @Test
    fun `pass stats are one byte id, a big endian lap age, levels and a big endian loop`() {
        val p = byteArrayOf(7, 0x27, 0x10, -76, -56, -66, 0x03, -24) // 10000 ms, 180/200/190 levels, 1000 us
        val ps = rhParsePassStats(p)
        assertEquals(7, ps.lapId)
        assertEquals(10_000, ps.msSinceLap)
        assertEquals(180, ps.level)
        assertEquals(200, ps.nodePeak)
        assertEquals(190, ps.passPeak)
        assertEquals(1000, ps.loopUs)
    }

    @Test
    fun `extremums report the crossing state and one queued peak or nadir`() {
        val crossing = rhParseExtremums(byteArrayOf(0x01, 100, 95, 0, 0, 0, 0, 0))
        assertTrue(crossing.crossing)
        assertEquals(100, crossing.passNadir)
        assertEquals(95, crossing.nodeNadir)
        assertNull("no level in byte 3 means the queue is empty", crossing.extremum)

        val queued = rhParseExtremums(byteArrayOf(0x03, 100, 95, -46, 0x01, -12, 0x00, 0x64))
        assertTrue("bit 0 is the crossing, bit 1 the direction", queued.crossing)
        val e = queued.extremum!!
        assertTrue(e.peak)
        assertEquals(210, e.level)
        assertEquals(500, e.msAgo)
        assertEquals(100, e.durationMs)

        assertFalse(rhParseExtremums(byteArrayOf(0x02, 0, 0, 1, 0, 0, 0, 0)).crossing)
        assertFalse(rhParseExtremums(byteArrayOf(0x00, 0, 0, 1, 0, 0, 0, 0)).extremum!!.peak)
    }

    @Test
    fun `a level code is valid unless it is none or the rail`() {
        assertFalse(rhValidLevel(0))
        assertFalse(rhValidLevel(255))
        assertTrue(rhValidLevel(1))
        assertTrue(rhValidLevel(254))
    }

    @Test
    fun `the two metrics read the same code differently`() {
        // The firmware's defaults, -32.5 and -37.5 dBFS, are codes 125 and 105.
        assertEquals(-32.5, rhLevelDb(125, RhMetric.RSSI), 1e-9)
        assertEquals(-37.5, rhLevelDb(105, RhMetric.RSSI), 1e-9)
        assertEquals(31.25, rhLevelDb(125, RhMetric.SNR), 1e-9)
    }

    @Test
    fun `frequency and uptime are big endian on this wire`() {
        assertEquals(5658, rhBe16(byteArrayOf(0x16, 0x1A.toByte())))
        // What the seat was set to is what its 0x03 reply says back — the read the web
        // uses to verify every frequency write.
        val write = rhEncodeWrite(Rh.WRITE_FREQUENCY, rhU16(Rh_FREQ_OFF))
        assertEquals(1111, rhBe16(rhCheckReply(write.copyOfRange(1, write.size), 2)!!))
        assertEquals(1_000_000L, rhBe32(byteArrayOf(0, 0x0F, 0x42, 0x40)))
        assertEquals(4_294_967_295L, rhBe32(byteArrayOf(-1, -1, -1, -1)))
    }

    @Test
    fun `the handshake constants are the ones the firmware answers`() {
        assertEquals(0x25, RH_API_MARKER)
        assertEquals(36, RH_API_LEVEL)
        assertEquals(1111, Rh_FREQ_OFF)
        assertEquals(8, RH_SEATS)
        assertEquals(0x22, Rh.READ_REVISION_CODE)
        assertEquals(0x51, Rh.WRITE_FREQUENCY)
        assertTrue("a read is a single byte", Rh.READ_REVISION_CODE < Rh.WRITE_FREQUENCY)
    }
}
