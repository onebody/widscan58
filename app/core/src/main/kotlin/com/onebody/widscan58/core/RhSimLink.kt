// A RotorHazard board with no hardware behind it: the transactions go to [RhDevice], the same code
// that answers the protocol, and its seats get levels from a race of four transmitters around the
// band. So the phone's instrument, the recorder and the lap counting see exactly what a board does,
// one 100 ms poll at a time. Mirrors the RotorHazard half of web/src/mock/mockDevice.ts.
// GPL-3.0-or-later.
package com.onebody.widscan58.core

import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.exp
import kotlin.math.ln
import kotlin.math.max
import kotlin.math.sqrt
import kotlin.math.roundToInt
import kotlin.random.Random

private const val LEVEL_STEP_MS = 5L // one level update per seat per 5 ms, as the scanner gives them
private const val CATCHUP_MS = 2_000L // a paused sim does not replay the whole gap
private const val SIM_LOW_MHZ = 5300
private const val SIM_HI_MHZ = 5950

class RhSimLink(
    private val clockMs: () -> Long,
    private val configJson: (RhConfig) -> String,
) : RhLink, RhLinkStats {
    private val replies = ArrayList<ByteArray>()
    private val device = RhDevice(clockMs, { replies += it }, { it in SIM_LOW_MHZ..SIM_HI_MHZ }, configJson)
    private val rnd = Random(20261026)
    /** When each seat's next pass goes by, and the frequency it was scheduled for. */
    private val nextPassMs = LongArray(RH_SEATS)
    private val scheduledFor = IntArray(RH_SEATS)
    private var fedMs = -1L

    override var closed = false
    override var lastRequest = 0L
    override var lastResponse = 0L

    /** The same counters a real link reports, so the receiver card reads the same line. */
    override var reads = 0
        private set
    override var writes = 0
        private set
    override var errors = 0
        private set

    override fun read(cmd: Int, size: Int): ByteArray {
        tick()
        replies.clear()
        lastRequest = clockMs()
        device.input(cmd)
        lastResponse = clockMs()
        reads++
        val reply = replies.firstOrNull() ?: fail("no reply for 0x${cmd.toString(16)}")
        return rhCheckReply(reply, size) ?: fail("bad checksum for 0x${cmd.toString(16)}")
    }

    override fun write(cmd: Int, payload: IntArray) {
        tick()
        replies.clear()
        writes++
        rhEncodeWrite(cmd, payload).forEach { device.input(it.toInt() and 0xff) }
    }

    override fun text(line: String): String {
        tick()
        replies.clear()
        lastRequest = clockMs()
        val reply = device.text(line) ?: fail("no reply: $line")
        lastResponse = clockMs()
        return reply
    }

    private fun fail(message: String): Nothing {
        errors++
        throw RhLinkError(message)
    }

    /** Feed the levels the board would have measured up to now; the board itself does the rest. */
    private fun tick() {
        val now = clockMs()
        if (fedMs < 0) fedMs = now
        var t = max(fedMs, now - CATCHUP_MS) + LEVEL_STEP_MS
        while (t <= now) {
            feed(t)
            fedMs = t
            t += LEVEL_STEP_MS
        }
    }

    private fun feed(t: Long) {
        val snr = device.metric == RhMetric.SNR
        for (j in 0 until RH_SEATS) {
            val s = device.seats[j]
            if (!s.live) continue
            if (s.freq != scheduledFor[j]) {
                scheduledFor[j] = s.freq
                nextPassMs[j] = t + 1_500 + rnd.nextLong(4_000)
            }
            // Gone by more than the pass itself: the pilot is on the next lap, so schedule that one.
            if (t > nextPassMs[j] + 1_500) nextPassMs[j] = t + 5_000 + rnd.nextLong(4_000) + j * 300L
            val peakDb = 16.0 + (j % 4) * 4.0
            val x = (t - nextPassMs[j]) / 220.0
            val db = peakDb * exp(-x * x) + gauss() * 0.6
            val code = if (snr) max(1.0, 4 * db + 1) else 4 * (db - 47.5) + 255
            device.feed(j, t, code.roundToInt().coerceIn(1, 254))
        }
    }

    private fun gauss(): Double {
        val u = max(1e-12, 1.0 - rnd.nextDouble())
        return sqrt(-2.0 * ln(u)) * cos(2 * PI * rnd.nextDouble())
    }
}
