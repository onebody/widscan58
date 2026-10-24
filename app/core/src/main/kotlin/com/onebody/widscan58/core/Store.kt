// Application state built from the device stream: channel history, spectrum
// segments with max-hold and average, per-LO noise floor. Views read from here.
// Mirrors web/src/model/store.ts. GPL-3.0-or-later.
package com.onebody.widscan58.core

import kotlin.math.pow

/** PROFILE/STATUS answers carry JSON, which only the platform layer can parse. */
interface ReplyCodec {
    fun profile(json: String): Profile?

    fun status(json: String): Status?
}

object NoReplies : ReplyCodec {
    override fun profile(json: String): Profile? = null

    override fun status(json: String): Status? = null
}

class ChannelState(capacity: Int) {
    var latest: ChannelMetrics? = null
    var loKhz = 0L

    /** [Store.now] of the last metrics for this channel. */
    var updated = 0.0
    val rssi = Series(capacity)
    val ema = Series(capacity)
    val floor = Series(capacity)
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
    historySeconds: Int = 30,
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

    /** Per-bin noise floor (dBFS) measured at each LO. */
    val floorByLo = LinkedHashMap<Long, Double>()
    var spursKhz: List<Double> = emptyList()
        private set

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
            is StreamItem.Text -> ingestText(item.line)
            is StreamItem.Frame -> when (item.type) {
                FrameType.METRICS -> onMetrics(parseMetrics(item.payload))
                FrameType.LEVELS -> onMetrics(parseLevels(item.payload))
                FrameType.SPECTRUM -> onSpectrum(parseSpectrum(item.payload))
                FrameType.TELEMETRY -> telemetry = parseTelemetry(item.payload)
                else -> Unit // raw IQ and device log lines have no view on the phone
            }
        }
        version++
    }

    private fun ingestText(line: String) {
        when {
            line.startsWith("PROFILE ") -> codec.profile(line.substringAfter(' '))?.let(::setProfile)
            line.startsWith("STATUS ") -> codec.status(line.substringAfter(' '))?.let(::setStatus)
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
        val floor = m.floorBin
        if (floor == null) floorByLo.remove(m.loKhz) else floorByLo[m.loKhz] = floor
        val t = now()
        for (c in m.channels) {
            val st = channels.getOrNull(c.id) ?: continue
            st.latest = c
            st.loKhz = m.loKhz
            st.updated = t
            st.rssi.push(t, c.rssi.toFloat())
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
    }
}

internal fun Double.pow10over10(): Double = 10.0.pow(this / 10.0)
