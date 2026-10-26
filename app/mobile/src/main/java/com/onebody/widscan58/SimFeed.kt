// A simulated device, so the instrument can be laid out and demoed without
// hardware: `adb shell am start -n com.onebody.widscan58/.Widscan58Activity --ez sim true`.
// It puts the same bytes on the wire as the firmware does (frames.h) for a band of
// VTXs plus the board spurs and a wide interference bump, and answers the sweep
// commands the settings sheet sends, so the link's own parser, recorder and console
// see it exactly as they see a board. Only that intent extra starts it, and it never
// touches the USB stack. Mirrors web/src/mock/mockDevice.ts. GPL-3.0-or-later.
package com.onebody.widscan58

import android.os.Handler
import com.onebody.widscan58.core.Bands
import com.onebody.widscan58.core.ChannelFlag
import com.onebody.widscan58.core.FrameType
import com.onebody.widscan58.core.Hop
import com.onebody.widscan58.core.MAX_CHANNELS
import com.onebody.widscan58.core.MetricsMode
import com.onebody.widscan58.core.PlanHop
import com.onebody.widscan58.core.Profile
import com.onebody.widscan58.core.ProfileChannel
import com.onebody.widscan58.core.Status
import com.onebody.widscan58.core.encodeFrame
import org.json.JSONObject
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.exp
import kotlin.math.pow
import kotlin.math.roundToInt
import kotlin.math.sin
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
    private val main: Handler,
    /** The same sink the USB link writes into: bytes, not decoded items. */
    private val sink: (ByteArray) -> Unit,
    private val onChanged: () -> Unit,
) {
    private val rnd = Random(20261025)
    private val cfg = Cfg()
    private var plan = planOf(Bands.names("R"))
    private var hop = 0
    private var ticks = 0
    private var rotations = 0L

    var running = true
        private set
    val gain: Int get() = cfg.gain

    /** GPIO? reports one character per pin, in pin order: '-' unused, 'L', 'H', 'x' flash or USB. */
    private val gpioActive = CharArray(GPIO_PINS) { if (it in GPIO_RESERVED) 'x' else '-' }
    private var gpioSaved = gpioActive.copyOf()

    /** The last rejected GPIO write, in the firmware's own words. */
    private var gpioError: String? = null

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
        val p = line.split(' ')
        val verb = p.firstOrNull()
        val word = p.getOrNull(1)
        var republish = true
        var known = true
        when (verb) {
            "SCAN" -> running = word == "START"
            "MODE" -> if (word == "GROUP" || word == "SINGLE") cfg.mode = word else republish = false
            "GAIN" -> when (word) {
                "HARDWARE" -> cfg.gainMode = "HARDWARE"
                "MANUAL" -> {
                    cfg.gainMode = "MANUAL"
                    cfg.gain = (p.getOrNull(2)?.toIntOrNull() ?: cfg.gain).coerceIn(0, cfg.gainMax)
                }
                else -> republish = false
            }
            "FILTER" -> cfg.filter =
                if (word == "AUTO") -1 else (word?.toIntOrNull() ?: cfg.filter).coerceIn(0, FILTER_MAX)
            "FFT" -> word?.toIntOrNull()?.let { cfg.nfft = it.coerceIn(64, 2048) }
            "CAPN" -> word?.toIntOrNull()?.let { cfg.captureSamples = it.coerceAtLeast(cfg.nfft) }
            "SEGMENTS" -> word?.toIntOrNull()?.let { cfg.segments = it.coerceIn(1, 64) }
            "METRICS" -> MetricsMode.entries.firstOrNull { it.name == word }?.let { cfg.metrics = it }
            "WIN" -> {
                cfg.rssiHz = window(word?.toLong())
                cfg.searchHz = window(p.getOrNull(2)?.toLong())
            }
            "EMA" -> {
                cfg.ema = alpha(word?.toIntOrNull())
                cfg.clearEma = alpha(p.getOrNull(2)?.toIntOrNull())
            }
            "SPEC" -> if (p.size >= 5) {
                cfg.specEvery = (p[1].toIntOrNull() ?: cfg.specEvery).coerceIn(0, 255)
                cfg.specBins = (p[2].toIntOrNull() ?: cfg.specBins).coerceIn(16, 1024)
                cfg.specSpanHz = (p[3].toLongOrNull() ?: cfg.specSpanHz).coerceIn(100_000, 40_000_000)
                cfg.specDetector = if (p[4] == "MAX") 1 else 0
            }
            "SETTLE" -> word?.toIntOrNull()?.let { cfg.settleUs = it.coerceIn(0, 20_000) }
            "RECALMODE" -> when (word) {
                "CACHE", "ONCE", "EVERY" -> cfg.recal = word
                else -> republish = false
            }
            "TUNESKIP" -> word?.toIntOrNull()?.let { cfg.tskip = it and 7 }
            "CONJ" -> cfg.conjugate = if (word == "1") 1 else 0
            "IQCORR" -> cfg.iqCorr = if (word == "1") 1 else 0
            "ENABLE" -> word?.toIntOrNull()?.let { cfg.enabled = it and 0xff }
            "RAWIQ" -> {
                republish = false
                val lo = word?.toLongOrNull()
                val samples = p.getOrNull(2)?.toIntOrNull()
                if (lo != null && samples != null) rawiq(lo, samples) else known = false
            }
            "GPIO?" -> republish = false
            "GPIO" -> {
                republish = false
                val pin = word?.toIntOrNull()
                val state = p.getOrNull(2)?.let { GPIO_STATE[it] }
                when {
                    pin == null && word == "SAVE" -> gpioSaved = gpioActive.copyOf()
                    pin == null -> known = false
                    state == null -> gpioError = "gpio_state"
                    pin >= gpioActive.size || gpioActive[pin] == 'x' -> gpioError = "gpio_pin"
                    else -> gpioActive[pin] = state
                }
            }
            "CHANNELS" -> {
                val names = Bands.normalize(p.drop(1).map { it.substringBefore('=') })
                if (names.isNotEmpty()) {
                    plan = planOf(names)
                    hop = 0
                    // parse_channels() switches the whole new plan on.
                    cfg.enabled = (1 shl names.size) - 1
                }
            }
            else -> {
                known = false
                republish = false
            }
        }
        if (republish) publish()
        reply(line, known)
    }

    /** What the board's `handle()` would put back on the wire for this command. */
    private fun reply(line: String, known: Boolean) {
        val refused = gpioError
        gpioError = null
        val text = refused ?: when (line.substringBefore(' ')) {
            "INFO" -> "INFO widscan58|esp32-c5|1|0.0.0-sim|0000000|sim"
            "GAIN?" -> "GAIN ${cfg.gainMode} ${cfg.gain} 0 ${cfg.gainMax}"
            "SPURS?" -> "SPURS 0 guard 0"
            "TIMING?" -> "TIMING psd 900 (prep 210 fft 520 power 170) metrics 260 spectrum 180"
            "GPIO?" -> gpioReply()
            // STATUS? and CONFIG? are the JSON the sim already applies directly; a
            // hand-written copy would only be a worse version of it. RAWIQ is answered by
            // its chunks, and the last one is the reply.
            "STATUS?", "CONFIG?", "PROFILE?", "RAWIQ" -> return
            else -> if (known) "OK" else "ERR command"
        }
        line(text)
    }

    /** The same wire a reply takes: one line, terminated the way the firmware terminates it. */
    private fun line(text: String) = sink((text + "\n").toByteArray(Charsets.UTF_8))

    /** The same shape as the firmware's send_gpio(): the pin count and one character each. */
    private fun gpioReply(): String {
        val o = JSONObject()
            .put("count", gpioActive.size)
            .put("active", String(gpioActive))
            .put("saved", String(gpioSaved))
        return "GPIO $o"
    }

    // ---- what the radio sees ----

    /** The channels the plan measures: the enabled mask, or the first one if all are off. */
    private fun hops(): List<ProfileChannel> = plan.filter { cfg.enabled and (1 shl it.id) != 0 }
        .ifEmpty { plan.take(1) }

    private fun emitHop() {
        val hops = hops()
        val ch = hops[hop % hops.size]
        if (cfg.specEvery == 0 || hop % cfg.specEvery == 0) {
            sink(encodeFrame(FrameType.SPECTRUM, spectrumAt(ch)))
        }
        // Only FULL gets METRICS frames: RSSI/SNR send the compact LEVELS frames lap
        // timing uses, four bytes of level per channel instead of twenty-six.
        val cost = hopCost()
        if (cfg.metrics == MetricsMode.FULL) {
            sink(encodeFrame(FrameType.METRICS, metricsAt(ch, hops, cost)))
        } else {
            sink(encodeFrame(FrameType.LEVELS, levelsAt(ch, hops, cost)))
        }
        hop++
        if (hop % hops.size == 0) rotations++
    }

    /** A hop costs a different amount each time, and once in a while the FFT waits for a
     *  settle: a flat band would make the per-hop chart look broken. */
    private fun hopCost(): Hop = Hop(
        tuneUs = rnd.nextLong(450, 780),
        captureUs = if (rnd.nextDouble() < 0.06) rnd.nextLong(1_400, 2_600) else rnd.nextLong(700, 1_150),
        dspUs = rnd.nextLong(260, 520),
    )

    /** One segment centred on the hop's LO, as wide as the display spectrum asks for. */
    private fun spectrumAt(ch: ProfileChannel): ByteArray {
        val bins = cfg.specBins
        val binHz = 2 * cfg.specSpanHz / bins
        val codes = IntArray(bins)
        val base = FLOOR_DB + rnd.nextDouble(-1.0, 1.0)
        for (j in 0 until bins) {
            val hz = ch.khz * 1000.0 + (j - bins / 2) * binHz
            var db = base + rnd.nextDouble(0.0, 2.5)
            db += lobe(hz, ch.khz * 1000.0, 1.1, signalAboveFloor(ch))
            db += lobe(hz, INTERFERENCE_HZ, 8.0, 9.0)
            db += lobe(hz, SPUR_A_HZ, 0.5, 22.0)
            db += lobe(hz, SPUR_B_HZ, 0.5, 17.0)
            codes[j] = specCode(db)
        }
        return spectrumPayload(ch.khz, binHz, codes)
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

    private fun metricsAt(ch: ProfileChannel, hops: List<ProfileChannel>, cost: Hop): ByteArray {
        val records = hops.map { c ->
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
        return metricsPayload(ch.khz, hop % hops.size, hops.size, cfg.gain, records, cost)
    }

    /**
     * A LEVELS frame: the byte codes lap timing runs on, which bottom out at -63.75 dBFS.
     * RSSI mode carries no floor, and without one the board cannot call anything a signal.
     */
    private fun levelsAt(ch: ProfileChannel, hops: List<ProfileChannel>, cost: Hop): ByteArray {
        val snrMode = cfg.metrics == MetricsMode.SNR
        val records = hops.map { c ->
            val rssi = (VTX_DB.getOrElse(c.id) { FLOOR_DB }) + rnd.nextDouble(-0.8, 0.8)
            var flags = VTX_FLAGS.getOrElse(c.id) { 0 }
            if (!snrMode) flags = flags and ChannelFlag.SIGNAL.inv()
            LevelRecord(
                id = c.id,
                flags = flags,
                rssi = rssi,
                snr = if (snrMode) (rssi - LEVEL_FLOOR_DB).coerceAtLeast(0.0) else 0.0,
            )
        }
        return levelsPayload(
            ch.khz, hop % hops.size, hops.size, cfg.gain, cfg.metrics, records, cost,
            // The per-bin floor is a spectrum code, and the firmware leaves it out of RSSI mode.
            floorBin = if (snrMode) specCode(LEVEL_FLOOR_DB - 24) else null,
        )
    }

    private fun emitTelemetry() {
        val hops = hops()
        sink(encodeFrame(FrameType.TELEMETRY, telemetryPayload(rotations, running, hops.size)))
    }

    /**
     * A raw capture at [loKhz]: the transmitters inside the passband, the board's own spurs,
     * its DC offset and the ADC's dither - in chunks of 1024 samples, as the firmware sends them.
     */
    private fun rawiq(loKhz: Long, samples: Int) {
        val n = samples.coerceIn(RAWIQ_CHUNK, RAWIQ_MAX)
        val loHz = loKhz * 1000.0
        val tones = (plan.map { it.khz * 1000.0 to VTX_DB.getOrElse(it.id) { FLOOR_DB } } +
            listOf(INTERFERENCE_HZ to -70.0, SPUR_A_HZ to -78.0))
            .filter { abs(it.first - loHz) < PASSBAND_HZ }
            .map { (it.first - loHz) / RATE_HZ to iqAmplitude(it.second) }
        val phases = DoubleArray(tones.size) { rnd.nextDouble(0.0, 2 * PI) }
        val i = ByteArray(n)
        val q = ByteArray(n)
        for (j in 0 until n) {
            var re = IQ_DC_I + rnd.nextDouble(-IQ_NOISE, IQ_NOISE)
            var im = IQ_DC_Q + rnd.nextDouble(-IQ_NOISE, IQ_NOISE)
            tones.forEachIndexed { k, (cycles, amp) ->
                val ph = phases[k] + 2 * PI * cycles * j
                re += amp * cos(ph)
                im += amp * sin(ph)
            }
            i[j] = re.roundToInt().coerceIn(-128, 127).toByte()
            q[j] = im.roundToInt().coerceIn(-128, 127).toByte()
        }
        for (off in 0 until n step RAWIQ_CHUNK) {
            sink(encodeFrame(FrameType.RAWIQ, rawiqPayload(loKhz, n, off, minOf(RAWIQ_CHUNK, n - off), i, q)))
        }
    }

    /**
     * A dBFS level in transferred counts. The 71 dB between the sim's loudest channel and its
     * floor is squeezed to half that slope: at the true one the whole band would sit under the
     * dither, and the view would only ever show noise.
     */
    private fun iqAmplitude(dbfs: Double): Double =
        if (dbfs <= FLOOR_DB + 2) 0.0
        else (IQ_NOISE * 10.0.pow((dbfs - FLOOR_DB) / 40)).coerceAtMost(IQ_FULL - 8.0)

    private fun rawiqPayload(loKhz: Long, n: Int, offset: Int, len: Int, i: ByteArray, q: ByteArray): ByteArray {
        val p = ByteArray(14 + 2 * len)
        ByteBuffer.wrap(p).order(ByteOrder.LITTLE_ENDIAN)
            .putInt(0, loKhz.toInt())
            .putInt(4, RATE_HZ.toInt())
            .putShort(8, n.toShort())
            .putShort(10, offset.toShort())
            .put(12, cfg.conjugate.toByte())
            .put(13, IQ_SHIFT.toByte())
        for (j in 0 until len) {
            p[14 + 2 * j] = i[offset + j]
            p[15 + 2 * j] = q[offset + j]
        }
        return p
    }

    /** Tell the engine which band it is scanning and how, exactly as PROFILE? and STATUS? would. */
    private fun publish() {
        val hops = hops()
        val first = plan.first()
        val profile = Profile(
            // The board's own profile name, as the web's mock device uses it: the dBm calibration
            // is keyed by it, so a simulator called "sim" could never be a power meter.
            name = "c5-race",
            description = "Raceband R1-R8 [SIMULATED]",
            target = "esp32-c5",
            bandKhz = longArrayOf(plan.minOf { it.khz } - 40_000, plan.maxOf { it.khz } + 40_000),
            baseKhz = first.khz,
            equivBaseKhz = first.khz,
            scale = 1.0,
            rateHz = RATE_HZ,
            usableHz = 40_000_000,
            passbandHz = PASSBAND_HZ.toLong(),
            rssiHz = cfg.rssiHz,
            searchHz = cfg.searchHz,
            channels = plan,
        )
        val status = Status(
            profile = "c5-race",
            running = if (running) 1 else 0,
            enabled = cfg.enabled,
            mode = cfg.mode,
            captureSamples = cfg.captureSamples,
            nfft = cfg.nfft,
            segments = cfg.segments,
            rateCode = 1,
            filter = cfg.filter,
            conjugate = cfg.conjugate,
            iqCorr = cfg.iqCorr,
            settleUs = cfg.settleUs,
            tskip = cfg.tskip,
            gainMode = cfg.gainMode,
            gain = cfg.gain,
            gainMax = cfg.gainMax,
            rawiqMax = RAWIQ_MAX,
            recal = cfg.recal,
            metrics = cfg.metrics,
            rssiHz = cfg.rssiHz,
            searchHz = cfg.searchHz,
            dcGuard = 3,
            ema = cfg.ema,
            clearEma = cfg.clearEma,
            signalDb = 6.0,
            avgShift = 1,
            specEvery = cfg.specEvery,
            specBins = cfg.specBins,
            specSpanHz = cfg.specSpanHz,
            specDetector = cfg.specDetector,
            plan = hops.map { PlanHop(it.khz, intArrayOf(it.id), longArrayOf(0)) },
        )
        line("PROFILE " + JsonWrites.profile(profile))
        line("STATUS " + JsonWrites.status(status))
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

    /** One `frame_level_t`: the levels as the board codes them, and no location at all. */
    private class LevelRecord(
        val id: Int,
        val flags: Int,
        val rssi: Double,
        val snr: Double,
    )

    /** The settings the board would hold, with the firmware's own defaults. */
    private class Cfg {
        /* The boot state of a real board: scanner_defaults() with the c5-race profile. */
        var mode = "GROUP"
        var gainMode = "MANUAL"
        var gain = 45
        var gainMax = 82
        var filter = 0
        var nfft = 1024
        var captureSamples = 4096
        var segments = 4
        var metrics = MetricsMode.FULL
        var rssiHz = 5_000_000L
        var searchHz = 12_000_000L
        var ema = 0.25
        var clearEma = 0.02
        var specEvery = 4
        var specBins = 256
        var specSpanHz = 40_000_000L // one-sided, as the firmware stores it
        var specDetector = 1
        var settleUs = 100
        var recal = "CACHE"
        var tskip = 3
        var conjugate = 1
        var iqCorr = 1
        var enabled = 0xff
    }

    companion object {
        private const val FLOOR_DB = -96.0

        /** The bottom of a level code's range: the quietest RSSI a LEVELS frame can carry. */
        private const val LEVEL_FLOOR_DB = -63.75
        private const val INTERFERENCE_HZ = 5790e6
        private const val SPUR_A_HZ = 5680e6 // the board spurs listed in PLAN.md
        private const val SPUR_B_HZ = 5840e6
        private const val HOP_MS = 130L
        private const val TELEMETRY_EVERY = 4
        private const val FILTER_MAX = 63
        private const val RATE_HZ = 80_000_000L
        private const val PASSBAND_HZ = 18_000_000.0

        /** The capture wire: 1024 samples a chunk, 16380 a buffer, and 2 bits of shift off 10. */
        private const val RAWIQ_CHUNK = 1024
        private const val RAWIQ_MAX = 16_380
        private const val IQ_SHIFT = 2
        private const val IQ_FULL = 512.0 / (1 shl IQ_SHIFT)
        private const val IQ_NOISE = 1.5
        private const val IQ_DC_I = 1.5
        private const val IQ_DC_Q = -1.0

        /** The C5 has GPIO0-28; the USB and flash pins are the firmware's to keep. */
        private const val GPIO_PINS = 29
        private val GPIO_RESERVED = intArrayOf(13, 14, 16, 17, 18, 20, 21, 22)
        private val GPIO_STATE = mapOf("NONE" to '-', "LOW" to 'L', "HIGH" to 'H')

        private fun window(hz: Long?) = (hz ?: 5_000_000L).coerceIn(10_000, 20_000_000)

        private fun alpha(milli: Int?) = (milli ?: 250) / 1000.0

        private fun planOf(names: List<String>): List<ProfileChannel> =
            names.take(MAX_CHANNELS).mapIndexed { i, name -> ProfileChannel(i, name, Bands.mhz(name)!! * 1000L) }

        private fun specCode(db: Double): Int = ((db + 127.5) * 2).roundToInt().coerceIn(0, 255)

        /** A level code (0.25 dB): dBFS = code / 4 - 63.75, so anything quieter reads as the floor. */
        private fun levelCode(db: Double): Int = ((db - LEVEL_FLOOR_DB) * 4).roundToInt().coerceIn(0, 255)

        private fun spectrumPayload(loKhz: Long, binHz: Long, codes: IntArray): ByteArray {
            val p = ByteArray(20 + codes.size)
            val b = ByteBuffer.wrap(p).order(ByteOrder.LITTLE_ENDIAN)
            b.putInt(0, 1).putInt(4, loKhz.toInt()).putInt(8, -codes.size / 2 * binHz.toInt()).putInt(12, binHz.toInt())
                .putShort(16, codes.size.toShort())
            codes.forEachIndexed { i, c -> p[20 + i] = c.toByte() }
            return p
        }

        private fun metricsPayload(
            loKhz: Long,
            hopIndex: Int,
            hops: Int,
            gain: Int,
            channels: List<MetricRecord>,
            cost: Hop,
        ): ByteArray {
            val p = ByteArray(34 + 26 * channels.size)
            val b = ByteBuffer.wrap(p).order(ByteOrder.LITTLE_ENDIAN)
            b.putInt(0, 3).putInt(4, 100).putInt(8, loKhz.toInt())
                .putInt(12, cost.tuneUs.toInt()).putInt(16, cost.captureUs.toInt()).putInt(20, cost.dspUs.toInt())
                .putShort(24, 1).putShort(26, (FLOOR_DB * 100).roundToInt().toShort())
                .putShort(28, (-8400).toShort())
            p[30] = gain.toByte()
            p[31] = hopIndex.toByte()
            p[32] = hops.toByte()
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

        /** frame_levels_t plus one frame_level_t per channel: four bytes of level, no centi-dB. */
        private fun levelsPayload(
            loKhz: Long,
            hopIndex: Int,
            hops: Int,
            gain: Int,
            mode: MetricsMode,
            levels: List<LevelRecord>,
            cost: Hop,
            floorBin: Int?,
        ): ByteArray {
            val p = ByteArray(32 + 4 * levels.size)
            val b = ByteBuffer.wrap(p).order(ByteOrder.LITTLE_ENDIAN)
            b.putInt(0, 3).putInt(4, 100).putInt(8, loKhz.toInt())
                .putInt(12, cost.tuneUs.toInt()).putInt(16, cost.captureUs.toInt()).putInt(20, cost.dspUs.toInt())
                .putShort(24, 1)
            p[26] = gain.toByte()
            p[27] = hopIndex.toByte()
            p[28] = hops.toByte()
            p[29] = mode.ordinal.toByte()
            p[30] = (floorBin ?: 0).toByte()
            p[31] = levels.size.toByte()
            levels.forEachIndexed { i, c ->
                val o = 32 + 4 * i
                p[o] = c.id.toByte()
                p[o + 1] = c.flags.toByte()
                p[o + 2] = levelCode(c.rssi).toByte()
                p[o + 3] = (c.snr * 4).roundToInt().coerceIn(0, 255).toByte()
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
