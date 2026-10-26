// Application state built from the device stream: channel history, spectrum
// segments with max-hold and average, per-LO noise floor. Views read from here.
// Mirrors web/src/model/store.ts. GPL-3.0-or-later.
package com.onebody.widscan58.core

import java.util.concurrent.CopyOnWriteArrayList
import kotlin.math.pow

/** PROFILE/STATUS answers carry JSON, which only the platform layer can parse. */
interface ReplyCodec {
    fun profile(json: String): Profile?

    fun status(json: String): Status?

    fun gpio(json: String): List<GpioPin>?
}

object NoReplies : ReplyCodec {
    override fun profile(json: String): Profile? = null

    override fun status(json: String): Status? = null

    override fun gpio(json: String): List<GpioPin>? = null
}

class ChannelState(capacity: Int) {
    var latest: ChannelMetrics? = null
    var loKhz = 0L

    /** [Store.now] of the last metrics for this channel. */
    var updated = 0.0
    val rssi = Series(capacity)
    val ema = Series(capacity)
    val floor = Series(capacity)

    /** 1 where the capture clipped, at the same times as [rssi]: power averaging leaves those out. */
    val clipped = Series(capacity)
}

/** One LO's spectrum, with the holds derived from it. */
class Segment(var latest: Spectrum, val maxHold: DoubleArray, val avg: DoubleArray, var updated: Double)

/** The smoothed RSSI series, or the raw one in modes without an EMA. */
fun smoothedSeries(cs: ChannelState): Series {
    val m = cs.latest ?: return cs.ema
    return if (m.rssiEma == null) cs.rssi else cs.ema
}

class Store(
    private val clock: () -> Long,
    private val codec: ReplyCodec = NoReplies,
    /** Deep enough for the widest history span the UI offers, as in the web store. */
    historySeconds: Int = 120,
) {
    private val capacity = historySeconds * 250
    private val t0 = clock()
    private val listeners = mutableSetOf<() -> Unit>()

    var version = 0L
        private set
    /** Panorama rows are captured once per completed rotation. */
    var waterfallVersion = 0L
        private set
    var profile: Profile? = null
        private set
    var status: Status? = null
        private set
    var info = ""
        private set
    var telemetry: Telemetry? = null
        private set
    var lastMetrics: Metrics? = null
        private set
    var channels: List<ChannelState> = emptyList()
        private set
    val segments = LinkedHashMap<Long, Segment>()

    /** The last hops' time breakdown, oldest first: what the per-hop chart draws. */
    val hopTimes = ArrayList<Hop>()

    /** Per-bin noise floor (dBFS) measured at each LO. */
    val floorByLo = LinkedHashMap<Long, Double>()
    var spursKhz: List<Double> = emptyList()
        private set

    /** The board's boot-time output pins, as the last GPIO? answered them. */
    var gpio: List<GpioPin> = emptyList()
        private set

    /** The last finished raw I/Q capture, chunks and all: what the snapshot view draws. */
    var rawiq: RawIq? = null
        private set
    private val rawiqChunks = RawIqAssembler()

    /** The protocol console's ring: what the board said, what we asked it to do. The RH poller
     *  appends to it while the screen walks it, hence the snapshot-iterator list. */
    val log = CopyOnWriteArrayList<LogLine>()

    /** A console line. The web caps the text as well as the ring, so a chatty
     *  reply cannot push the useful lines out of sight. */
    fun addLog(dir: LogDir, text: String) {
        log += LogLine(now(), dir, if (text.length > LOG_TEXT_MAX) text.take(LOG_TEXT_MAX) + " …" else text)
        if (log.size > LOG_MAX) log.removeAt(0)
        touch()
    }

    /** Changes with the profile or its channel plan; per-channel state is keyed by plan index. */
    var planKey = ""
        private set

    /** Seconds since this store was created. */
    fun now(): Double = (clock() - t0) / 1000.0

    fun subscribe(fn: () -> Unit): () -> Unit {
        listeners.add(fn)
        return { listeners.remove(fn) }
    }

    fun touch() {
        version++
        listeners.forEach { it() }
    }

    fun setProfile(p: Profile) {
        val key = p.name + "/" + p.channels.joinToString(",") { "${it.name}=${it.khz}" }
        val changed = key != planKey
        profile = p
        planKey = key
        if (changed) {
            channels = p.channels.map { ChannelState(capacity) }
            segments.clear()
        }
        touch()
    }

    fun setStatus(s: Status) {
        status = s
    }

    /** Put every hold back to the live trace (the "Reset holds" button). */
    fun resetHolds() {
        for (seg in segments.values) {
            val db = seg.latest.db
            for (j in db.indices) {
                seg.maxHold[j] = db[j]
                seg.avg[j] = db[j].pow10over10()
            }
        }
        touch()
    }

    fun ingest(item: StreamItem) {
        when (item) {
            is StreamItem.Text -> {
                ingestText(item.line)
                addLog(if (item.line.startsWith("ERR")) LogDir.ERROR else LogDir.RX, item.line)
            }

            is StreamItem.Frame -> when (item.type) {
                FrameType.METRICS -> onMetrics(parseMetrics(item.payload))
                FrameType.LEVELS -> onMetrics(parseLevels(item.payload))
                FrameType.SPECTRUM -> onSpectrum(parseSpectrum(item.payload))
                FrameType.TELEMETRY -> telemetry = parseTelemetry(item.payload)
                FrameType.RAWIQ -> rawiqChunks.add(item.payload)?.let { rawiq = it }
                // The firmware has never used this type, but the web prints it, so we do too.
                FrameType.LOG -> addLog(LogDir.INFO, String(item.payload, Charsets.UTF_8).trim('\u0000', '\n', '\r'))
                else -> Unit
            }
        }
        version++
    }

    private fun ingestText(line: String) {
        fun badReply() = addLog(LogDir.ERROR, "bad ${line.substringBefore(' ')} reply; ignored")
        when {
            line.startsWith("PROFILE ") -> codec.profile(line.substringAfter(' '))?.let(::setProfile) ?: badReply()
            line.startsWith("STATUS ") -> codec.status(line.substringAfter(' '))?.let(::setStatus) ?: badReply()
            line.startsWith("GPIO ") -> codec.gpio(line.substringAfter(' '))?.let { gpio = it } ?: badReply()
            line.startsWith("INFO ") -> info = line.substring(5)
            line.startsWith("SPURS ") -> {
                val f = line.split(' ')
                val n = f[1].toDoubleOrNull()?.toInt() ?: 0
                spursKhz = f.drop(2).take(n).mapNotNull { it.toDoubleOrNull() }
            }
        }
    }

    private fun onMetrics(m: Metrics) {
        lastMetrics = m
        hopTimes += Hop(m.tuneUs, m.captureUs, m.dspUs)
        while (hopTimes.size > HOP_HISTORY) hopTimes.removeAt(0)
        val floor = m.floorBin
        if (floor == null) floorByLo.remove(m.loKhz) else floorByLo[m.loKhz] = floor
        val t = now()
        for (c in m.channels) {
            val st = channels.getOrNull(c.id) ?: continue
            st.latest = c
            st.loKhz = m.loKhz
            st.updated = t
            st.rssi.push(t, c.rssi.toFloat())
            st.clipped.push(t, if (c.has(ChannelFlag.SATURATED)) 1f else 0f)
            c.rssiEma?.let { st.ema.push(t, it.toFloat()) }
            c.floor?.let { st.floor.push(t, it.toFloat()) }
        }
    }

    private fun onSpectrum(s: Spectrum) {
        val wall = now()
        val seg = segments[s.loKhz]
        if (seg == null || seg.maxHold.size != s.db.size || seg.latest.binHz != s.binHz) {
            segments[s.loKhz] = Segment(s, s.db.copyOf(), DoubleArray(s.db.size) { s.db[it].pow10over10() }, wall)
        } else {
            seg.latest = s
            seg.updated = wall
            for (j in s.db.indices) {
                if (s.db[j] > seg.maxHold[j]) seg.maxHold[j] = s.db[j]
                seg.avg[j] += AVG_ALPHA * (s.db[j].pow10over10() - seg.avg[j])
            }
        }
        dropStaleSegments(wall)
        waterfallVersion++
    }

    /** Segments whose LO left the plan (after a replan) go once they stop updating. */
    private fun dropStaleSegments(now: Double) {
        val los = status?.planLokhz() ?: return
        val gone = segments.filter { (lo, seg) -> lo !in los && now - seg.updated > SEGMENT_TIMEOUT_S }.keys
        for (lo in gone.toList()) segments.remove(lo)
    }

    companion object {
        private const val AVG_ALPHA = 0.05
        private const val SEGMENT_TIMEOUT_S = 3.0
        /** The web keeps 400 hops and draws the last 300 of them. */
        const val HOP_HISTORY = 400
        /** The web's console ring, and its per-line text cap. */
        const val LOG_MAX = 500
        private const val LOG_TEXT_MAX = 300
        /** What the console shows: the web renders the same tail of the ring. */
        const val LOG_SHOWN = 200
    }
}

internal fun Double.pow10over10(): Double = 10.0.pow(this / 10.0)
