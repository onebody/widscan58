// The sheets' writes and the poll cycle share one seat-select pointer (0x7a) and the seats' own
// state, so a change waits for the cycle in flight instead of landing in the middle of it.
// GPL-3.0-or-later.
package com.onebody.widscan58.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

class RhGateTest {
    private var ms = 0L

    @Test
    fun `a lap rule change waits for the poll cycle in flight`() {
        val rig = rig()
        rig.link.blockCycle {
            rig.polling.countDown()
            assertTrue("the cycle was never released", rig.released.await(3, TimeUnit.SECONDS))
        }
        val cycle = Thread { rig.model.poll() }
        cycle.start()
        assertTrue("the poll never reached the seat read", rig.polling.await(3, TimeUnit.SECONDS))

        val done = changeRules(rig.model)
        assertFalse("the rules changed while a poll cycle was in flight", done.await(150, TimeUnit.MILLISECONDS))

        rig.released.countDown()
        assertTrue(done.await(3, TimeUnit.SECONDS))
        cycle.join(3000)
    }

    @Test
    fun `a seat write waits for the poll cycle in flight`() {
        val rig = rig()
        val written = CountDownLatch(1)
        rig.link.blockCycle {
            rig.polling.countDown()
            assertTrue("the cycle was never released", rig.released.await(3, TimeUnit.SECONDS))
        }
        val cycle = Thread { rig.model.poll() }
        cycle.start()
        assertTrue("the poll never reached the seat read", rig.polling.await(3, TimeUnit.SECONDS))

        val writer = Thread {
            rig.model.writeSeats(listOf("R2"))
            written.countDown()
        }
        writer.start()
        assertFalse("the seats were written while a poll cycle was in flight", written.await(150, TimeUnit.MILLISECONDS))

        rig.released.countDown()
        assertTrue(written.await(3, TimeUnit.SECONDS))
        cycle.join(3000)
        writer.join(3000)
        assertEquals("the plan's frequency did not reach the seat", Bands.mhz("R2"), rig.link.mhz)
    }

    @Test
    fun `polling while the sheet changes the rules and thresholds does not break either`() {
        val rig = rig()
        val failed = AtomicReference<Throwable>()
        val stop = AtomicBoolean(false)
        val poller = Thread {
            runCatching { while (!stop.get()) rig.model.poll() }.onFailure { failed.set(it) }
        }
        val sheet = Thread {
            runCatching {
                repeat(60) {
                    rig.model.setLapRules(rig.model.lapRules.of(minLapS = it.toDouble()))
                    rig.model.setThreshold(0, RhThreshold.ENTER, (120 + it % 20).toDouble())
                    rig.model.writeSeats(listOf("R1"))
                }
            }.onFailure { failed.set(it) }
        }
        poller.start()
        sheet.join()
        stop.set(true)
        poller.join(3000)
        assertNull(failed.get()?.toString(), failed.get())
    }

    private fun changeRules(model: RhModel): CountDownLatch {
        val done = CountDownLatch(1)
        Thread {
            model.setLapRules(model.lapRules.of(minLapS = 3.0))
            done.countDown()
        }.start()
        return done
    }

    private fun rig(): Rig {
        val store = Store({ ms })
        val link = GateLink()
        val model = RhModel(store, GatePrefs) { RhMetric.RSSI }
        model.start(link, listOf("R1"))
        ms = 1_000
        return Rig(link, model)
    }

    private class Rig(val link: GateLink, val model: RhModel) {
        val polling = CountDownLatch(1)
        val released = CountDownLatch(1)
    }

    private object GatePrefs : RhSettings {
        override fun load(): RhSaved = RhSaved()

        override fun save(saved: RhSaved) {}
    }

    /** A board with one seat on: every read answers at once, and the first pass read can be held open. */
    private class GateLink : RhLink {
        override var closed = false
        override var lastRequest = 0L
        override var lastResponse = 0L
        var block: (() -> Unit)? = null
        private var armed = false
        private var seat = 0
        /** The frequency the last seat was asked for: what a write set, not what the plan says. */
        var mhz = 0

        fun blockCycle(what: () -> Unit) {
            block = what
            armed = true
        }

        override fun read(cmd: Int, size: Int): ByteArray {
            if (cmd == Rh.READ_LAP_PASS_STATS && armed) {
                armed = false
                block?.invoke()
            }
            return when (cmd) {
                Rh.READ_REVISION_CODE -> byteArrayOf(RH_API_MARKER.toByte(), RH_API_LEVEL.toByte())
                Rh.READ_MULTINODE_COUNT -> byteArrayOf(RH_SEATS.toByte())
                Rh.READ_TIME_MILLIS -> byteArrayOf(0, 0, 1, 0)
                Rh.READ_FREQUENCY -> bytes(rhU16(mhzForSeat()))
                Rh.TEST_RX_REGISTER -> byteArrayOf(1)
                Rh.READ_LAP_PASS_STATS -> byteArrayOf(0, 0, 0, 100, 0, 0, 0, 0x27)
                Rh.READ_LAP_EXTREMUMS -> byteArrayOf(0, -1, -1, 0, 0, 0, 0, 0)
                else -> throw RhLinkError("unexpected read 0x${cmd.toString(16)}")
            }
        }

        override fun write(cmd: Int, payload: IntArray) {
            when (cmd) {
                Rh.WRITE_CURNODE_INDEX -> seat = payload[0]
                // Only the polled seat's frequency matters; the plan leaves the rest off.
                Rh.WRITE_FREQUENCY -> if (seat == 0) mhz = rhBe16(bytes(payload))
            }
        }

        override fun text(line: String): String =
            if (line == "CONFIG?") "CONFIG {\"metrics\":\"RSSI\"}" else "OK"

        /** The polled seat's frequency, and the off marker for every seat the plan left empty. */
        private fun mhzForSeat() = if (seat == 0 && mhz > 0) mhz else Rh_FREQ_OFF

        private fun bytes(v: IntArray) = ByteArray(v.size) { v[it].toByte() }

        private fun rhBe16(b: ByteArray) = ((b[0].toInt() and 0xff) shl 8) or (b[1].toInt() and 0xff)
    }
}
