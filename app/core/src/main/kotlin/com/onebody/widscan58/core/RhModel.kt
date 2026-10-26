// RotorHazard mode from the host's side: the seat model, the 100 ms poll RotorHazard's server runs
// (per live seat, select it, read its pass stats and one queued extremum), the detection events that
// comes out of it, and the lap rules counted over it. Levels are the node's 0.25 dB codes. The CONFIG?
// reply is JSON, which only the platform can read, so the metric arrives through [configMetric].
// Mirrors web/src/model/rh.ts. GPL-3.0-or-later.
package com.onebody.widscan58.core

import java.util.concurrent.CopyOnWriteArrayList
import kotlin.math.roundToInt

/** What the node reports for a seat it has never measured; a real reading is always above it. */
private fun rhReading(code: Int): Int? = if (code > 1 && code < 255) code else null

private fun ByteArray.u8(o: Int = 0): Int = this[o].toInt() and 0xff

private fun IntArray.clamped(): IntArray = IntArray(size) { this[it].coerceIn(RH_LEVEL_MIN, RH_LEVEL_MAX) }

enum class RhThreshold { ENTER, EXIT }

enum class RhEventKind { ENTER, EXIT, PASS, FASTEST }

/** A peak or nadir the node queued for this seat. */
class RhPeak(val t: Double, val level: Int, val peak: Boolean)

class RhSeat(val index: Int) {
    /** The plan's channel for this seat, or null: the seat is switched off. */
    var channel: String? = null
    var mhz = 0
    var enter = RH_DEFAULT_ENTER
        private set
    var exit = RH_DEFAULT_EXIT
        private set
    var level: Int? = null
    var nodePeak: Int? = null
    var nodeNadir: Int? = null
    var passPeak: Int? = null
    var crossing = false
    /** The node's pass counter at the last read; null until the first one. */
    var lapId: Int? = null
    val laps = RhLapTracker()
    var loopUs = 0
    /** The polled levels: [Store.now] seconds, level code. */
    val history = Series(RhModel.HISTORY_SECONDS * 15)
    /** The extremums the node queued, oldest first: the levels view walks it while a poll appends. */
    val peaks = CopyOnWriteArrayList<RhPeak>()

    fun setThreshold(which: RhThreshold, value: Int) {
        if (which == RhThreshold.ENTER) enter = value else exit = value
    }
}

/** A crossing, a pass (counted or ignored) or a new fastest run. */
class RhEvent(
    val t: Double,
    val seat: Int,
    val kind: RhEventKind,
    val level: Int? = null,
    val peak: Int? = null,
    val lapS: Double? = null,
    /** Pass: how soon after the last counted pass this one came, so the list can say "ignored". */
    val ignoredS: Double? = null,
    val fastest: FastestRun? = null,
    val previous: FastestRun? = null,
) {
    val ignored: Boolean get() = ignoredS != null
}

/** What the phone keeps between runs: the per-seat thresholds and the lap rules. */
class RhSaved(
    val enter: IntArray = IntArray(RH_SEATS) { RH_DEFAULT_ENTER },
    val exit: IntArray = IntArray(RH_SEATS) { RH_DEFAULT_EXIT },
    val lapRules: RhLapRules = DEFAULT_RH_LAP_RULES,
)

interface RhSettings {
    fun load(): RhSaved

    fun save(saved: RhSaved)
}

class RhModel(
    private val store: Store,
    private val settings: RhSettings,
    private val configMetric: (String) -> RhMetric?,
) {
    val seats: List<RhSeat> = (0 until RH_SEATS).map { RhSeat(it) }
    /** The event ring: the poller's thread appends, the screen's thread walks it. */
    val events = CopyOnWriteArrayList<RhEvent>()
    var metric = RhMetric.RSSI
        private set
    /** The raw CONFIG? body: the platform's own settings sheet reads it. */
    var config: String? = null
        private set
    var running = false
        private set
    var error: String? = null
        private set
    /** How long the last poll took; the sheet shows it next to the interval. */
    var cycleMs = 0L
        private set
    var lapRules = DEFAULT_RH_LAP_RULES
        private set

    /** A seat counted a lap: the voice callout. */
    var onLap: (seat: RhSeat, lapS: Double) -> Unit = { _, _ -> }

    /**
     * One transaction at a time: the poll loop and the sheets' writes share the node's seat-select
     * pointer (0x7a), the seats' lap lists and the event ring, so a write waits for the cycle in
     * flight instead of landing in the middle of it. Reentrant, so the nested calls below are free.
     */
    private val gate = Any()

    private var link: RhLink? = null
    private var enter = IntArray(RH_SEATS) { RH_DEFAULT_ENTER }
    private var exit = IntArray(RH_SEATS) { RH_DEFAULT_EXIT }
    private var deviceMs = -1L
    /** [Store.now] at the start: extremums queued before it were not our run's. */
    private var since = 0.0

    /** Start on a board already switched to PROTOCOL RH. [channels] is the plan, in seat order. */
    fun start(link: RhLink, channels: List<String>) = synchronized(gate) {
        stop()
        val saved = settings.load()
        enter = saved.enter.clamped()
        exit = saved.exit.clamped()
        lapRules = saved.lapRules.of()
        for (s in seats) {
            s.lapId = null
            s.laps.breakRun() // passes while we were away are missed
            s.history.clear()
            s.peaks.clear()
            s.level = null
            s.nodePeak = null
            s.nodeNadir = null
            s.passPeak = null
            s.setThreshold(RhThreshold.ENTER, enter[s.index])
            s.setThreshold(RhThreshold.EXIT, exit[s.index])
        }
        this.link = link
        error = null
        deviceMs = -1L
        since = store.now()
        try {
            val rev = link.read(Rh.READ_REVISION_CODE, 2)
            if (rev.u8() != RH_API_MARKER) throw RhLinkError("unexpected revision code ${rev.u8()}.${rev.u8(1)}")
            val count = link.read(Rh.READ_MULTINODE_COUNT, 1).u8()
            if (count < RH_SEATS) throw RhLinkError("device reports $count seats")
            refreshConfig()
            writeSeats(channels)
        } catch (e: Exception) {
            error = e.message
            store.addLog(LogDir.ERROR, "RotorHazard start: $error")
            this.link = null
            return
        }
        running = true
    }

    fun stop() = synchronized(gate) {
        running = false
        link = null
    }

    /** One poll cycle: the whole point of the loop, so the caller only has to space them. */
    fun poll() = synchronized(gate) {
        val link = this.link ?: return
        if (!running) return
        val t0 = System.nanoTime()
        try {
            pollOnce(link)
            error = null
        } catch (e: Exception) {
            error = e.message
        }
        cycleMs = (System.nanoTime() - t0) / 1_000_000
        if (link.closed) running = false
        store.touch()
    }

    /** The plan onto the seats (off ones get the server's off frequency), thresholds with them. */
    fun writeSeats(channels: List<String>) = synchronized(gate) {
        val link = this.link ?: return
        for (s in seats) {
            val name = channels.getOrNull(s.index)?.takeIf { it.isNotEmpty() }
            val mhz = name?.let { Bands.mhz(it) } ?: 0
            val wanted = if (mhz > 0) name else null
            if (wanted != s.channel) s.laps.breakRun() // another pilot's seat
            s.channel = wanted
            s.mhz = mhz
            link.write(Rh.WRITE_CURNODE_INDEX, intArrayOf(s.index))
            link.write(Rh.WRITE_FREQUENCY, rhU16(mhz.takeIf { it > 0 } ?: Rh_FREQ_OFF))
            link.write(Rh.WRITE_ENTER_AT_LEVEL, intArrayOf(s.enter))
            link.write(Rh.WRITE_EXIT_AT_LEVEL, intArrayOf(s.exit))
            if (rhBe16(link.read(Rh.READ_FREQUENCY, 2)) != (mhz.takeIf { it > 0 } ?: Rh_FREQ_OFF))
                store.addLog(LogDir.ERROR, "seat ${s.index + 1}: frequency not set")
            if (mhz > 0 && link.read(Rh.TEST_RX_REGISTER, 1).u8() == 0)
                store.addLog(LogDir.ERROR, "seat ${s.index + 1}: $mhz MHz is outside this board's band")
        }
        store.touch()
    }

    fun setThreshold(seat: Int, which: RhThreshold, value: Double) = synchronized(gate) {
        val v = value.roundToInt().coerceIn(RH_LEVEL_MIN, RH_LEVEL_MAX)
        seats[seat].setThreshold(which, v)
        if (which == RhThreshold.ENTER) enter[seat] = v else exit[seat] = v
        saveSettings()
        val link = this.link ?: return
        link.write(Rh.WRITE_CURNODE_INDEX, intArrayOf(seat))
        link.write(
            if (which == RhThreshold.ENTER) Rh.WRITE_ENTER_AT_LEVEL else Rh.WRITE_EXIT_AT_LEVEL,
            intArrayOf(v),
        )
        store.touch()
    }

    /** The mean of the levels polled in the last [seconds] (what "set from current" offers). */
    fun recentLevel(seat: Int, seconds: Double = 1.0): Double? = synchronized(gate) {
        var sum = 0.0
        var n = 0
        seats[seat].history.forEachWithin(store.now(), seconds) { _, v ->
            sum += v.toDouble()
            n++
        }
        return if (n > 0) sum / n else null
    }

    fun setMetric(m: RhMetric) = synchronized(gate) {
        if (m == metric || link == null) return
        command("METRICS ${m.name}")
    }

    /** One of our command lines allowed in this mode, then the CONFIG? it may have changed. */
    fun command(line: String) = synchronized(gate) {
        if (link == null) return
        try {
            text(line)
            refreshConfig()
        } catch (e: Exception) {
            store.addLog(LogDir.ERROR, e.message ?: line)
        }
    }

    /** CONFIG?, taking the level metric from it. */
    fun refreshConfig() = synchronized(gate) {
        val reply = text("CONFIG?")
        config = reply.substringAfter(' ')
        val m = configMetric(reply)
        if (m != null && m != metric) {
            metric = m
            // RSSI and SNR codes don't compare: what the other metric read is not a level here.
            for (s in seats) {
                s.history.clear()
                s.peaks.clear()
                s.level = null
                s.nodePeak = null
                s.nodeNadir = null
                s.passPeak = null
            }
        }
        store.touch()
    }

    fun clearEvents() = synchronized(gate) {
        events.clear()
        for (s in seats) s.laps.clear()
        store.touch()
    }

    /** Change the rules; the seats' passes since the last clear are recomputed from them, no new events. */
    fun setLapRules(rules: RhLapRules) = synchronized(gate) {
        lapRules = rules.of()
        saveSettings()
        for (s in seats) s.laps.recompute(lapRules)
        store.touch()
    }

    private fun saveSettings() = settings.save(RhSaved(enter.copyOf(), exit.copyOf(), lapRules))

    private fun text(line: String): String {
        val link = this.link ?: throw RhLinkError("no RotorHazard link")
        store.addLog(LogDir.TX, line)
        val reply = link.text(line)
        store.addLog(if (reply.startsWith("ERR")) LogDir.ERROR else LogDir.RX, reply)
        if (reply.startsWith("ERR")) throw RhLinkError("$line: $reply")
        return reply
    }

    private fun pollOnce(link: RhLink) {
        // A board that restarted has forgotten its seats: set them up again.
        val ms = rhBe32(link.read(Rh.READ_TIME_MILLIS, 4))
        if (deviceMs >= 0 && ms < deviceMs) {
            store.addLog(LogDir.INFO, "RotorHazard: device restarted; setting the seats up again")
            refreshConfig() // its saved settings, maybe another metric
            writeSeats(seats.map { it.channel ?: "" })
            for (s in seats) {
                s.lapId = null
                s.laps.breakRun() // passes while it restarted are missed
            }
        }
        deviceMs = ms
        for (s in seats) {
            if (s.channel == null) continue
            link.write(Rh.WRITE_CURNODE_INDEX, intArrayOf(s.index))
            val ps = rhParsePassStats(link.read(Rh.READ_LAP_PASS_STATS, 8))
            // RotorHazard's read time: the middle of the round trip.
            val readTime = store.now() - (link.lastResponse - link.lastRequest) / 2000.0
            update(s, readTime, ps, rhParseExtremums(link.read(Rh.READ_LAP_EXTREMUMS, 8)))
        }
    }

    private fun update(s: RhSeat, now: Double, ps: RhPassStats, ex: RhExtremums) {
        rhReading(ps.level)?.let {
            s.level = it
            s.history.push(now, it.toFloat())
        }
        s.nodePeak = if (rhValidLevel(ps.nodePeak)) ps.nodePeak else null
        s.nodeNadir = if (rhValidLevel(ex.nodeNadir)) ex.nodeNadir else null
        s.loopUs = ps.loopUs
        // The node keeps its last eight peaks and nadirs while nobody polls, so the first reads after
        // a start drain those, from before our level history. Only the ones since the start count.
        val e = ex.extremum
        if (e != null) {
            val t = now - e.msAgo / 1000.0 + e.durationMs / 2000.0
            if (t >= since) {
                s.peaks += RhPeak(t, e.level, e.peak)
                while (s.peaks.isNotEmpty() && now - s.peaks[0].t > HISTORY_SECONDS) s.peaks.removeAt(0)
            }
        }
        if (ex.crossing != s.crossing) {
            s.crossing = ex.crossing
            // The first read only learns the state: it has nothing to compare the change to.
            if (s.lapId != null) {
                addEvent(RhEvent(now, s.index, if (ex.crossing) RhEventKind.ENTER else RhEventKind.EXIT, level = ps.level))
            }
        }
        if (s.lapId != null && ps.lapId != s.lapId) {
            val t = now - ps.msSinceLap / 1000.0
            val peak = if (rhValidLevel(ps.passPeak)) ps.passPeak else null
            val r = s.laps.pass(t, lapRules)
            if (r.ignored) addEvent(RhEvent(t, s.index, RhEventKind.PASS, peak = peak, ignoredS = r.ignoredS)) else {
                if (peak != null) s.passPeak = peak
                addEvent(RhEvent(t, s.index, RhEventKind.PASS, peak = s.passPeak, lapS = r.lapS))
                r.lapS?.let { onLap(s, it) }
                r.fastest?.let { addEvent(RhEvent(t, s.index, RhEventKind.FASTEST, fastest = it, previous = r.previous)) }
            }
        }
        s.lapId = ps.lapId
    }

    private fun addEvent(e: RhEvent) {
        events += e
        while (events.size > EVENTS_MAX) events.removeAt(0)
    }

    companion object {
        /** RotorHazard's RH_UPDATE_INTERVAL. */
        const val POLL_MS = 100L
        const val HISTORY_SECONDS = 120
        const val EVENTS_MAX = 500
        /** What the event list shows; the ring keeps the rest. */
        const val EVENTS_SHOWN = 200
    }
}
