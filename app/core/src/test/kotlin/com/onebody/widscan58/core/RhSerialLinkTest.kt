// GPL-3.0-or-later.
package com.onebody.widscan58.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

/** A reply as the node puts it on the wire: the payload and its checksum. */
private fun rhFrame(payload: ByteArray): ByteArray = payload + byteArrayOf(payload.sum().toByte())

/** A link on a byte pipe that answers as fast as a board can, so a transaction never really waits. */
private class Echo(startMs: Long = 1_000L) {
    var nowMs = startMs
    val sent = ArrayList<ByteArray>()

    /** What the board puts on the wire for the bytes the link just sent. */
    var answer: (ByteArray) -> ByteArray? = { null }
    lateinit var link: RhSerialLink

    init {
        link = RhSerialLink({ nowMs }) { bytes ->
            sent += bytes
            answer(bytes)?.let { reply ->
                nowMs += ROUND_TRIP_MS
                link.push(reply)
            }
        }
    }

    companion object {
        const val ROUND_TRIP_MS = 12L
    }
}

class RhSerialLinkTest {
    @Test
    fun `a read gets its payload and the round trip times`() {
        val e = Echo()
        e.answer = { b -> if (b[0] == Rh.READ_REVISION_CODE.toByte()) rhFrame(byteArrayOf(RH_API_MARKER.toByte(), 36)) else null }
        val payload = e.link.read(Rh.READ_REVISION_CODE, 2)
        assertEquals(listOf(RH_API_MARKER, 36), payload.map { it.toInt() and 0xff })
        assertEquals(1, e.link.reads)
        assertEquals(0, e.link.errors)
        assertEquals(Echo.ROUND_TRIP_MS, e.link.lastResponse - e.link.lastRequest)
    }

    @Test
    fun `a read starts each transaction with an empty buffer`() {
        val e = Echo()
        // A reply that arrived after its own timeout must not answer the next request.
        e.link.push(byteArrayOf(0x00, 0x11, 0x22))
        e.answer = { rhFrame(byteArrayOf(RH_API_MARKER.toByte(), 36)) }
        val payload = e.link.read(Rh.READ_REVISION_CODE, 2)
        assertEquals(listOf(RH_API_MARKER, 36), payload.map { it.toInt() and 0xff })
    }

    @Test
    fun `a bad checksum is asked for again`() {
        val e = Echo()
        var tries = 0
        e.answer = {
            tries++
            if (tries == 1) byteArrayOf(RH_API_MARKER.toByte(), 36, 0) else rhFrame(byteArrayOf(RH_API_MARKER.toByte(), 36))
        }
        val payload = e.link.read(Rh.READ_REVISION_CODE, 2)
        assertEquals(listOf(RH_API_MARKER, 36), payload.map { it.toInt() and 0xff })
        assertEquals(2, e.link.reads)
        assertEquals(1, e.link.errors)
        assertEquals(2, e.sent.size)
    }

    @Test
    fun `a read gives up when the link closes`() {
        val e = Echo()
        e.answer = {
            e.link.closed = true
            null
        }
        val error = assertThrows(RhLinkError::class.java) { e.link.read(Rh.READ_TIME_MILLIS, 4) }
        assertTrue(error.message.orEmpty().contains("0x33"))
        assertEquals(1, e.link.reads)
        assertEquals(1, e.link.errors)
    }

    @Test
    fun `a write goes out with its checksum and waits for nothing`() {
        val e = Echo()
        e.link.write(Rh.WRITE_FREQUENCY, rhU16(5658))
        assertEquals(listOf(rhEncodeWrite(Rh.WRITE_FREQUENCY, rhU16(5658)).toList()), e.sent.map { it.toList() })
        assertEquals(1, e.link.writes)
        assertEquals(0, e.link.reads)
    }

    @Test
    fun `a text command is answered by one line`() {
        val e = Echo()
        e.answer = { "CONFIG {\"gain\":45}\nleft over\n".toByteArray() }
        assertEquals("CONFIG {\"gain\":45}", e.link.text("CONFIG?"))
        assertEquals("CONFIG?\n", e.sent[0].toString(Charsets.UTF_8))
    }

    @Test
    fun `a text command that is not answered throws`() {
        val e = Echo()
        e.answer = {
            e.link.closed = true
            null
        }
        val error = assertThrows(RhLinkError::class.java) { e.link.text("GAIN?") }
        assertTrue(error.message.orEmpty().contains("GAIN?"))
    }

    @Test
    fun `closing a link ends the transaction that is waiting`() {
        val e = Echo()
        var failure: Throwable? = null
        val reader = Thread { failure = runCatching { e.link.read(Rh.READ_TIME_MILLIS, 4) }.exceptionOrNull() }
        reader.start()
        Thread.sleep(20L)
        e.link.close()
        reader.join(2_000L)
        assertTrue("expected the wait to be woken, got $failure", failure is RhLinkError)
    }
}
