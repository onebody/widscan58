// GPL-3.0-or-later.
package com.onebody.widscan58.core

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder

class SessionTest {
    private var ms = 0L
    private val clock = { ms }

    private fun written(recorder: SessionRecorder): ByteArray =
        ByteArrayOutputStream().also { recorder.writeTo(it) }.toByteArray()

    private fun file(vararg records: Pair<Long, ByteArray>): ByteArray {
        val out = ByteArrayOutputStream()
        records.forEach { (t, data) ->
            out.write(le8(t, data.size.toLong()))
            out.write(data)
        }
        return out.toByteArray()
    }

    private fun le8(a: Long, b: Long): ByteArray =
        ByteBuffer.allocate(8).order(ByteOrder.LITTLE_ENDIAN).putInt(a.toInt()).putInt(b.toInt()).array()

    @Test
    fun `a record is its time, its length, then the bytes`() {
        val r = SessionRecorder(clock)
        r.begin(emptyList())
        ms = 7
        r.add(byteArrayOf(1, 2))
        assertEquals(
            "u32 ms | u32 length | payload, little-endian",
            listOf(7, 0, 0, 0, 2, 0, 0, 0, 1, 2),
            written(r).map { it.toInt() and 0xff },
        )
    }

    @Test
    fun `the preamble lands before any byte from the link`() {
        val r = SessionRecorder(clock)
        r.begin(listOf("""PROFILE {"name":"c5-race"}"""))
        ms = 40
        r.add(byteArrayOf(1, 2, 3))

        val records = readSession(ByteArrayInputStream(written(r)))
        assertEquals(2, records.size)
        assertEquals(0L, records[0].tMs)
        assertEquals("PROFILE {\"name\":\"c5-race\"}\n", records[0].data.toString(Charsets.UTF_8))
        assertEquals(40L, records[1].tMs)
        assertEquals(listOf(1, 2, 3), records[1].data.map { it.toInt() })
        assertEquals("the byte count is what the file holds", records.sumOf { it.data.size }.toLong(), r.bytes)
    }

    @Test
    fun `a session reads back as it was written`() {
        val r = SessionRecorder(clock)
        r.begin(listOf("STATUS {}"))
        listOf(5, 11, 3).forEach { n ->
            ms += n.toLong()
            r.add(ByteArray(n) { (it + 1).toByte() })
        }
        r.stop()

        val back = readSession(ByteArrayInputStream(written(r)))
        assertEquals(listOf(0L, 5L, 16L, 19L), back.map { it.tMs })
        assertEquals(listOf(10, 5, 11, 3), back.map { it.data.size })
        assertEquals(listOf(1, 2, 3, 4, 5), back[2].data.take(5).map { it.toInt() })
    }

    @Test
    fun `a last record that never finished is dropped, not half played`() {
        val good = file(0L to "OK\n".toByteArray(), 20L to byteArrayOf(9, 9))
        val cut = good + le8(40, 8) + byteArrayOf(1, 2, 3)
        val records = readSession(ByteArrayInputStream(cut))
        assertEquals(2, records.size)
        assertEquals(20L, records.last().tMs)
    }

    @Test
    fun `an empty file and an impossible length are both nothing to play`() {
        assertEquals(0, readSession(ByteArrayInputStream(ByteArray(0))).size)
        assertEquals(0, readSession(ByteArrayInputStream(le8(0, 1L shl 30) + ByteArray(1024))).size)
    }

    @Test
    fun `a capture that runs out of room keeps what it has and stops taking more`() {
        val r = SessionRecorder(clock, maxBytes = 10)
        r.begin(emptyList())
        ms = 1
        r.add(ByteArray(7))
        assertTrue(r.active)
        r.add(ByteArray(6))

        assertFalse("the cap closes the capture", r.active)
        assertTrue(r.overflowed)
        r.add(ByteArray(1))
        assertEquals("a stopped recorder takes nothing further", 7L, r.bytes)
        assertEquals(1, readSession(ByteArrayInputStream(written(r))).size)
    }

    @Test
    fun `a file longer than the cap is read as far as the cap`() {
        val big = file(0L to ByteArray(6), 1L to ByteArray(6), 2L to ByteArray(6))
        assertEquals(2, readSession(ByteArrayInputStream(big), maxBytes = 12).size)
    }

    @Test
    fun `stopping keeps the capture for a save the user may still cancel`() {
        val r = SessionRecorder(clock)
        r.begin(listOf("OK"))
        r.add(byteArrayOf(1, 2))
        r.stop()
        assertEquals("the preamble counts towards the file", 5L, r.bytes)

        r.begin(emptyList())
        assertEquals("a new capture replaces the one it dropped", 0L, r.bytes)
    }

    @Test
    fun `a capture already written to a file can be thrown away`() {
        val r = SessionRecorder(clock)
        r.begin(emptyList())
        r.add(byteArrayOf(1, 2, 3))
        r.stop()
        val file = written(r)
        r.discard()
        assertEquals(0L, r.bytes)
        assertFalse(r.active)
        r.add(byteArrayOf(4))
        assertEquals("a discarded recorder takes nothing until it begins again", 0L, r.bytes)
        assertArrayEquals("the file it wrote is its own", byteArrayOf(0, 0, 0, 0, 3, 0, 0, 0, 1, 2, 3), file)
    }
}
