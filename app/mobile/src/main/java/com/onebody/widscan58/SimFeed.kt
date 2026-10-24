// A simulated device, so the instrument can be laid out and demoed without
// hardware: `adb shell am start -n com.onebody.widscan58/.Widscan58Activity --ez sim true`.
// It writes the same SPECTRUM / METRICS / TELEMETRY payloads the firmware does
// (firmware/src/proto/frames.h) for a band of VTXs plus the board spurs and a
// wide interference bump. Only that intent extra starts it, and it never touches
// the USB stack. Mirrors web/src/mock/mockDevice.ts. GPL-3.0-or-later.
package com.onebody.widscan58

import android.os.Handler
import com.onebody.widscan58.core.Bands
import com.onebody.widscan58.core.ChannelFlag
import com.onebody.widscan58.core.FrameType
import com.onebody.widscan58.core.MAX_CHANNELS
import com.onebody.widscan58.core.MetricsMode
import com.onebody.widscan58.core.PlanHop
import com.onebody.widscan58.core.Profile
import com.onebody.widscan58.core.ProfileChannel
import com.onebody.widscan58.core.Status
import com.onebody.widscan58.core.Store
import com.onebody.widscan58.core.StreamItem
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.exp
import kotlin.math.roundToInt
import kotlin.random.Random

/** dBFS of each channel's transmitter; a value at the floor means no transmitter. */
private val VTX_DB = doubleArrayOf(-58.0, -70.5, -84.0, -95.5, -25.0, -96.0, -66.0, -90.0)
private val VTX_FLAGS = intArrayOf(
    ChannelFlag.SIGNAL,
    ChannelFlag.SIGNAL,
    ChannelFlag.SIGNAL,
    0,
    ChannelFlag.SIGNAL or ChannelFlag.SATURATED,
    ChannelFlag.SPUR,
    ChannelFlag.SIGNAL or ChannelFlag.EDGE,
    0,
)

class SimFeed(
    private val store: Store,
    private val main: Handler,
    private val onChanged: () -> Unit,
) {
    private val rnd = Random(20261025)
    private var plan = planOf(Bands.names("R"))
    private var hop = 0
    private var ticks = 0
    private var rotations = 0L

    var running = true
        private set
    var gain = 45
        private set

    private val step = object : Runnable {
        override fun run() {
            if (running) emitHop()
            if (ticks++ % TELEMETRY_EVERY == 0) emitTelemetry()
            onChanged()
            main.postDelayed(this, HOP_MS)
        }
    }

    fun start() {
        publish()
        // onResume calls this again: drop any pending tick so the loop never doubles.
        main.removeCallbacks(step)
        main.post(step)
    }

    fun stop() {
        main.removeCallbacks(step)
    }

    /** The instrument's own commands, answered the way the board would. */
    fun onCommand(line: String) {
        when {
            line == "SCAN START" -> running = true
            line == "SCAN STOP" -> running = false
            line.startsWith("GAIN MANUAL") -> gain = line.substringAfterLast(' ').toIntOrNull() ?: gain
            line.startsWith("CHANNELS") -> {
                val names = Bands.normalize(line.split(' ').drop(1).map { it.substringBefore('=') })
                if (names.isNotEmpty()) {
                    plan = planOf(names)
                    hop = 0
                    publish()
                }
            }
        }
    }

    // ---- what the radio sees ----

    private fun emitHop() {
        val ch = plan[hop % plan.size]
        store.ingest(StreamItem.Frame(FrameType.SPECTRUM, spectrumAt(ch)))
        store.ingest(StreamItem.Frame(FrameType.METRICS, metricsAt(ch)))
        hop++
        if (hop % plan.size == 0) rotations++
    }

    /** One 40 MHz segment centred on the hop's LO: noise, this channel's VTX, and the fixtures. */
    private fun spectrumAt(ch: ProfileChannel): ByteArray {
        val codes = IntArray(BINS)
        val base = FLOOR_DB + rnd.nextDouble(-1.0, 1.0)
        for (j in 0 until BINS) {
            val hz = ch.khz * 1000.0 + (j - BINS / 2) * BIN_HZ
            var db = base + rnd.nextDouble(0.0, 2.5)
            db += lobe(hz, ch.khz * 1000.0, 1.1, signalAboveFloor(ch))
            db += lobe(hz, INTERFERENCE_HZ, 8.0, 9.0)
            db += lobe(hz, SPUR_A_HZ, 0.5, 22.0)
            db += lobe(hz, SPUR_B_HZ, 0.5, 17.0)
            codes[j] = specCode(db)
        }
        return spectrumPayload(ch.khz, codes)
    }

    /** A Gaussian rise of [peakDb] above the floor, [widthMHz] wide, centred on [centreHz]. */
    private fun lobe(hz: Double, centreHz: Double, widthMHz: Double, peakDb: Double): Double {
        if (peakDb <= 0.0) return 0.0
        val x = (hz - centreHz) / (widthMHz * 1e6)
        return peakDb * exp(-x * x)
    }

    private fun signalAboveFloor(ch: ProfileChannel): Double {
        val level = VTX_DB.getOrElse(ch.id) { return 0.0 }
        return if (level <= FLOOR_DB + 2) 0.0 else (level - FLOOR_DB) + rnd.nextDouble(-1.0, 1.0)
    }

    private fun metricsAt(ch: ProfileChannel): ByteArray {
        val records = plan.map { c ->
            val rssi = (VTX_DB.getOrElse(c.id) { FLOOR_DB }) + rnd.nextDouble(-0.8, 0.8)
            val flags = VTX_FLAGS.getOrElse(c.id) { 0 }
            val occupied = flags and ChannelFlag.SIGNAL != 0
            MetricRecord(
                id = c.id,
                flags = flags,
                rssi = rssi,
                rssiEma = rssi + rnd.nextDouble(-0.3, 0.3),
                peak = rssi + 1.5,
                centroidHz = if (occupied) rnd.nextLong(-400_000, 400_000) else 0,
                obwHz = if (occupied) -4_500_000L..4_500_000L else null,
            )
        }
        return metricsPayload(ch.khz, gain, records)
    }

    private fun emitTelemetry() {
        store.ingest(StreamItem.Frame(FrameType.TELEMETRY, telemetryPayload(rotations, running, plan.size)))
    }

    /** Tell the engine which band it is scanning, exactly as PROFILE? and STATUS? would. */
    private fun publish() {
        store.setProfile(
            Profile(
                name = "sim-${plan.first().name.take(1)}",
                description = "Raceband R1-R8 [SIMULATED]",
                target = "esp32-c5",
                bandKhz = longArrayOf(plan.minOf { it.khz } - 40_000, plan.maxOf { it.khz } + 40_000),
                baseKhz = plan.first().khz,
                equivBaseKhz = plan.first().khz,
                scale = 1.0,
                rateHz = 80_000_000,
                usableHz = 40_000_000,
                passbandHz = 18_000_000,
                rssiHz = 5_000_000,
                searchHz = 12_000_000,
                channels = plan,
            ),
        )
        store.setStatus(
            Status(
                profile = "sim",
                running = if (running) 1 else 0,
                enabled = 0xff,
                mode = "GROUP",
                captureSamples = 128,
                nfft = 128,
                segments = 8,
                rateCode = 1,
                filter = 0,
                conjugate = 0,
                iqCorr = 0,
                settleUs = 40,
                tskip = null,
                gainMode = "MANUAL",
                gain = gain,
                gainMax = 63,
                rawiqMax = null,
                recal = "boot",
                metrics = MetricsMode.FULL,
                rssiHz = 5_000_000,
                searchHz = 12_000_000,
                dcGuard = 3,
                ema = 8,
                clearEma = 0,
                signalDb = -75.0,
                avgShift = 1,
                specEvery = 4,
                specBins = BINS,
                specSpanHz = BINS.toLong() * BIN_HZ,
                specDetector = 0,
                plan = plan.map { PlanHop(it.khz, intArrayOf(it.id), longArrayOf(0)) },
            ),
        )
    }

    private class MetricRecord(
        val id: Int,
        val flags: Int,
        val rssi: Double,
        val rssiEma: Double,
        val peak: Double,
        val centroidHz: Long,
        val obwHz: LongRange?,
    )

    companion object {
        private const val BINS = 256
        private const val BIN_HZ = 156_250
        private const val FLOOR_DB = -96.0
        private const val INTERFERENCE_HZ = 5790e6
        private const val SPUR_A_HZ = 5680e6 // the board spurs listed in PLAN.md
        private const val SPUR_B_HZ = 5840e6
        private const val HOP_MS = 130L
        private const val TELEMETRY_EVERY = 4

        private fun planOf(names: List<String>): List<ProfileChannel> =
            names.take(MAX_CHANNELS).mapIndexed { i, name -> ProfileChannel(i, name, Bands.mhz(name)!! * 1000L) }

        private fun specCode(db: Double): Int = ((db + 127.5) * 2).roundToInt().coerceIn(0, 255)

        private fun spectrumPayload(loKhz: Long, codes: IntArray): ByteArray {
            val p = ByteArray(20 + codes.size)
            val b = ByteBuffer.wrap(p).order(ByteOrder.LITTLE_ENDIAN)
            b.putInt(0, 1).putInt(4, loKhz.toInt()).putInt(8, -BINS / 2 * BIN_HZ).putInt(12, BIN_HZ)
                .putShort(16, codes.size.toShort())
            codes.forEachIndexed { i, c -> p[20 + i] = c.toByte() }
            return p
        }

        private fun metricsPayload(loKhz: Long, gain: Int, channels: List<MetricRecord>): ByteArray {
            val p = ByteArray(34 + 26 * channels.size)
            val b = ByteBuffer.wrap(p).order(ByteOrder.LITTLE_ENDIAN)
            b.putInt(0, 3).putInt(4, 100).putInt(8, loKhz.toInt())
                .putInt(12, 600).putInt(16, 900).putInt(20, 350)
                .putShort(24, 1).putShort(26, (FLOOR_DB * 100).roundToInt().toShort())
                .putShort(28, (-8400).toShort())
            p[30] = gain.toByte()
            p[31] = 0
            p[32] = 1
            p[33] = channels.size.toByte()
            fun i16(at: Int, db: Double) = b.putShort(at, (db * 100).roundToInt().toShort())
            channels.forEachIndexed { i, c ->
                val o = 34 + 26 * i
                p[o] = c.id.toByte()
                p[o + 1] = c.flags.toByte()
                i16(o + 2, c.rssi)
                i16(o + 4, c.rssiEma)
                i16(o + 6, FLOOR_DB)
                i16(o + 8, c.rssi - FLOOR_DB)
                i16(o + 10, -100.0)
                i16(o + 12, c.peak)
                b.putInt(o + 14, 0).putInt(o + 18, c.centroidHz.toInt())
                c.obwHz?.let {
                    b.putShort(o + 22, (it.first / 1000).toShort()).putShort(o + 24, (it.last / 1000).toShort())
                }
            }
            return p
        }

        private fun telemetryPayload(rotations: Long, running: Boolean, hops: Int): ByteArray {
            val p = ByteArray(59)
            val b = ByteBuffer.wrap(p).order(ByteOrder.LITTLE_ENDIAN)
            b.putInt(0, 100).putInt(4, rotations.toInt()).putInt(8, (rotations * hops).toInt())
                .putInt(12, if (running) (1_000_000 / (HOP_MS * hops)).toInt() else 0)
                .putInt(16, 4100).putInt(20, 5200).putInt(24, 600).putInt(28, 900)
                .putInt(32, 900).putInt(36, 350).putInt(40, 480).putShort(44, 120)
                .putInt(46, 180_000).putInt(50, 3).putInt(54, 0)
            p[58] = if (running) 1 else 0
            return p
        }
    }
}
