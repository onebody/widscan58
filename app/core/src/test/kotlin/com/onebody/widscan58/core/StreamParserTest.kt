// GPL-3.0-or-later.
package com.onebody.widscan58.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class StreamParserTest {
    private fun bytesOf(vararg v: Int) = ByteArray(v.size) { v[it].toByte() }

    @Test
    fun `crc32 matches the zlib check value`() {
        assertEquals(0xCBF43926L, Crc32.of("123456789".toByteArray()))
    }

    @Test
    fun `a frame round-trips through the envelope`() {
        val payload = bytesOf(1, 2, 3, 4)
        val items = StreamParser().push(encodeFrame(FrameType.SPECTRUM, payload))
        val frame = items.single() as StreamItem.Frame
        assertEquals(FrameType.SPECTRUM, frame.type)
        assertTrue(payload.contentEquals(frame.payload))
    }

    @Test
    fun `a frame split across reads is reassembled`() {
        val wire = encodeFrame(FrameType.METRICS, bytesOf(9, 8, 7))
        val parser = StreamParser()
        assertTrue(parser.push(wire.copyOfRange(0, 8)).isEmpty())
        val items = parser.push(wire.copyOfRange(8, wire.size))
        assertEquals(1, items.size)
        assertEquals(0L, parser.resyncBytes)
    }

    @Test
    fun `a corrupt frame is skipped and the next one still parses`() {
        val parser = StreamParser()
        val good = encodeFrame(FrameType.SPECTRUM, bytesOf(5, 6))
        val broken = good.clone().also { it[6] = (it[6] + 1).toByte() }
        val items = parser.push(broken + bytesOf(0xff) + good)
        assertEquals(1, items.size)
        assertEquals(1L, parser.crcErrors)
        assertTrue(parser.resyncBytes > 0)
    }

    @Test
    fun `text lines end at the newline and stop at a sync byte`() {
        val parser = StreamParser()
        val wire = "OK scan started\n".toByteArray() + encodeFrame(FrameType.LEVELS, bytesOf(1)) +
            "INFO widscan58|esp32c5".toByteArray()
        val items = parser.push(wire)
        assertEquals(listOf("OK scan started"), items.filterIsInstance<StreamItem.Text>().map { it.line })
        assertEquals(1, items.count { it is StreamItem.Frame })
    }

    @Test
    fun `only uppercase reply words become text`() {
        // The tail of a frame we joined part-way through must not reach the console.
        val parser = StreamParser()
        val items = parser.push("junk\n".toByteArray() + "STATUS {\"a\":1}\n".toByteArray())
        assertEquals(listOf("STATUS {\"a\":1}"), items.filterIsInstance<StreamItem.Text>().map { it.line })
    }

    @Test
    fun `a frame that has not arrived yet waits instead of desyncing`() {
        val parser = StreamParser()
        val header = bytesOf(0xa5, 0x52, 2, 1, 0xff, 0xff) // claims a full 64 KiB payload
        assertTrue(parser.push(header + ByteArray(16)).isEmpty())
        assertEquals(0L, parser.crcErrors)
        assertEquals(0L, parser.resyncBytes)
    }
}
