// GPL-3.0-or-later.
package com.onebody.widscan58.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.util.concurrent.atomic.AtomicInteger

/** A reply as the node puts it on the wire: the payload and its checksum. */
private fun rhFrame(payload: ByteArray): ByteArray = payload + byteArrayOf(payload.sum().toByte())

private fun be16(v: Int): ByteArray = byteArrayOf((v shr 8).toByte(), v.toByte())

/** A seat-less board (every channel off), so a poll cycle is exactly one read. */
private class StubLink : RhLink {
    override var closed = false
    override var lastRequest = 0L
    override var lastResponse = 0L
    val cycles = AtomicInteger()

    /** The port goes away after this many cycles; zero never closes it. */
    var closeAfterCycles = 0

    override fun read(cmd: Int, size: Int): ByteArray =
        when (cmd) {
            Rh.READ_REVISION_CODE -> rhFrame(byteArrayOf(RH_API_MARKER.toByte(), RH_API_LEVEL.toByte()))
            Rh.READ_MULTINODE_COUNT -> rhFrame(byteArrayOf(RH_SEATS.toByte()))
            Rh.READ_TIME_MILLIS -> rhFrame(byteArrayOf(0, 0, 1, 0)).also {
                if (cycles.incrementAndGet() == closeAfterCycles) closed = true
            }
            Rh.READ_FREQUENCY -> rhFrame(be16(Rh_FREQ_OFF))
            else -> throw RhLinkError("unexpected read 0x${cmd.toString(16)}")
        }

    override fun write(cmd: Int, payload: IntArray) {}

    override fun text(line: String): String =
        if (line == "CONFIG?") "CONFIG {\"metrics\":\"RSSI\"}" else "OK"
}

private class StoredSettings : RhSettings {
    override fun load(): RhSaved = RhSaved()
    override fun save(saved: RhSaved) {}
}

private class PollerRig {
    var nowMs = 0L
    val store = Store({ nowMs })
    val link = StubLink()
    val model = RhModel(store, StoredSettings()) { if (it.contains("SNR")) RhMetric.SNR else RhMetric.RSSI }
    val poller = RhPoller({ nowMs }, model, intervalMs = 5L)
}

private fun awaitWithin(what: String, done: () -> Boolean) {
    val deadline = System.currentTimeMillis() + 5_000
    while (!done()) {
        if (System.currentTimeMillis() > deadline) fail("timed out waiting for $what")
        Thread.sleep(2)
    }
}

class RhPollerTest {
    @Test
    fun `the poller runs a cycle at a time until it is stopped`() {
        val rig = PollerRig()
        rig.poller.start(rig.link, List(RH_SEATS) { "" })
        awaitWithin("four cycles") { rig.link.cycles.get() >= 4 }
        assertTrue(rig.model.running)
        rig.poller.stop()
        assertFalse(rig.poller.running)
        assertFalse(rig.model.running)
        val after = rig.link.cycles.get()
        Thread.sleep(30L)
        assertEquals("stop ends the loop", after, rig.link.cycles.get())
    }

    @Test
    fun `the poller ends when the link does`() {
        val rig = PollerRig()
        rig.link.closeAfterCycles = 2
        rig.poller.start(rig.link, List(RH_SEATS) { "" })
        awaitWithin("the loop to end") { !rig.poller.running }
        assertTrue(rig.link.cycles.get() >= 2)
        assertFalse(rig.model.running)
    }
}
