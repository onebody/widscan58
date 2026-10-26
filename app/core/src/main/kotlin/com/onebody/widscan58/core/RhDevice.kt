// A board in RotorHazard mode, seen from the host's side of the wire: the byte protocol of
// firmware/src/proto/rh.c (reads answered with a payload and a sum, writes never) and the command
// lines that file's dispatcher still allows in this mode. The seat levels come from RhLapSeat, so a
// seat behaves like the node's median filter, including its window filling and its pass plateau.
// GPL-3.0-or-later.
package com.onebody.widscan58.core

const val RH_DEFAULT_ENTER = 125 // -32.5 dBFS, the firmware's own default
const val RH_DEFAULT_EXIT = 105 // -37.5 dBFS
private const val MIN_FREQ = 100
private const val MAX_FREQ = 9999
private const val TEXT_BLOCK = 16 // bytes of a 0x3d..0x40 reply
private const val WRITE_TIMEOUT_MS = 100 // rh_poll's WRITE_TIMEOUT_US
private const val FFT_MIN = 64
private const val FFT_MAX = 2048

/** The lines the firmware's dispatcher answers in this mode (rh_allowed in commands.c). */
private val RH_ALLOWED = arrayOf(
    "PROTOCOL?", "PROTOCOL MONITOR", "PROTOCOL RH", "INFO", "CONFIG?", "METRICS RSSI", "METRICS SNR", "METRICS?",
    "GAIN?", "GAIN HARDWARE", "FILTER AUTO", "MODE GROUP", "MODE SINGLE", "CONFIG SAVE", "CONFIG RESET", "RECAL",
    "LEVELFILTER MEDIAN", "LEVELFILTER EMA", "LEVELFILTER?", "PHYIDLE?", "GPIO?",
)
private val RH_PREFIXES = arrayOf("GAIN MANUAL ", "FFT ", "FILTER ", "IQCORR ", "PHYIDLE ", "THROTTLE ", "GPIO ")

/** The dispatcher's gate in this mode: an exact line, or one starting with one of the few prefixes. */
private fun rhAllowed(line: String): Boolean = line in RH_ALLOWED || RH_PREFIXES.any { line.startsWith(it) }

/** One seat: the frequency the server wrote, whether the profile can tune it, and its detector. */
class RhDeviceSeat(val lap: RhLapSeat) {
    var freq = 0
    var set = false
    var mapped = false
    var lastUs = 0L

    /** A seat the scanner measures: only once the server has written a frequency it can tune. */
    val live: Boolean get() = set && mapped

    fun setFrequency(mhz: Int, inBand: (Int) -> Boolean) {
        if (mhz < MIN_FREQ || mhz > MAX_FREQ) return
        val changed = freq != mhz || !set
        freq = mhz
        set = true
        mapped = inBand(mhz)
        if (changed) {
            lap.reset()
            lastUs = 0L // the next update has no previous one to measure against
        }
    }
}

class RhConfig(
    val gain: Int,
    val gainMax: Int,
    val gainMode: GainMode,
    val nfft: Int,
    val mode: LoPlan,
    val metric: RhMetric,
    val levelFilter: RhLapFilter,
)

/**
 * The device half of the protocol. [clockMs] is the device's own uptime; [send] takes the bytes it
 * puts on the wire; [inBand] decides whether a frequency the server wrote is one this profile can
 * tune; [configJson] writes the CONFIG? body, which only the platform layer can do.
 */
class RhDevice(
    private val clockMs: () -> Long,
    private val send: (ByteArray) -> Unit,
    private val inBand: (Int) -> Boolean,
    private val configJson: (RhConfig) -> String,
) {
    var protocol = "RH"
        private set
    var cur = 0
        private set
    val seats = (0 until RH_SEATS).map { RhDeviceSeat(RhLapSeat(RH_DEFAULT_ENTER, RH_DEFAULT_EXIT)) }

    var gain = 45
        private set
    var gainMax = 82
        private set
    var gainMode = GainMode.MANUAL
        private set
    var nfft = 256
        private set
    var mode = LoPlan.GROUP
        private set
    var metric = RhMetric.RSSI
        private set
    var levelFilter = RhLapFilter.MEDIAN
        private set

    private var wCmd = 0
    private var wNeed = 0
    private var wGot = 0
    private var wStart = 0L
    private val wBuf = IntArray(4)
    private var inText = false
    private val line = StringBuilder()

    init {
        // rh_init: the profile's channels until the server writes its own.
        val raceband = Bands.byLetter("R")!!.mhz
        seats.forEachIndexed { j, s -> s.setFrequency(raceband.getOrElse(j) { raceband[0] }, { true }) }
        seats.forEach { it.set = false }
    }

    /** One byte from the host. */
    fun input(b: Int) {
        if (protocol != "RH") return
        if (wNeed > 0 && clockMs() - wStart > WRITE_TIMEOUT_MS) wNeed = 0 // rh_poll: the payload stopped
        if (inText || (wNeed == 0 && isTextStart(b))) {
            textByte(b)
            return
        }
        if (wNeed > 0) {
            wBuf[wGot++] = b
            if (wGot < wNeed) return
            val cmd = wCmd
            val size = wNeed - 1
            wNeed = 0
            var sum = 0
            for (j in 0 until size) sum += wBuf[j]
            if ((sum and 0xff) == wBuf[size]) writeCmd(cmd, wBuf.copyOf(size))
            return
        }
        if (b <= 0x50) {
            readCmd(b)
            return
        }
        val n = writeSize(b)
        if (n == 0) return // an unknown write, as the node's own: ignored
        wCmd = b
        wNeed = n + 1
        wGot = 0
        wStart = clockMs()
    }

    /** One command line, however it arrived. Returns the reply, or null when the mode swallows it. */
    fun text(source: String): String? {
        val l = source.trim()
        if (l.isEmpty()) return null
        val reply = if (!rhAllowed(l)) {
            "ERR rh_mode"
        } else {
            when (l) {
                "PROTOCOL?" -> "PROTOCOL $protocol"
                "PROTOCOL RH" -> { protocol = "RH"; "OK" }
                "PROTOCOL MONITOR" -> { protocol = "MONITOR"; "OK" }
                "INFO" -> "INFO widscan58|esp32c5|2|0.3.0-sim|20261026|widscan58-phy"
                "CONFIG?" -> "CONFIG ${configJson(config)}"
                "METRICS RSSI" -> setMetric(RhMetric.RSSI)
                "METRICS SNR" -> setMetric(RhMetric.SNR)
                "METRICS?" -> "METRICS ${metric.name}"
                "GAIN HARDWARE" -> { gainMode = GainMode.HARDWARE; "OK" }
                "GAIN?" -> "GAIN ${gainMode.name.lowercase()} $gain $gainMax"
                "MODE GROUP" -> { mode = LoPlan.GROUP; "OK" }
                "MODE SINGLE" -> { mode = LoPlan.SINGLE; "OK" }
                "LEVELFILTER MEDIAN" -> setFilter(RhLapFilter.MEDIAN)
                "LEVELFILTER EMA" -> setFilter(RhLapFilter.EMA)
                "LEVELFILTER?" -> "LEVELFILTER ${levelFilter.name}"
                "CONFIG SAVE" -> "OK"
                "CONFIG RESET" -> resetConfig()
                "RECAL" -> "OK RECAL"
                "FILTER AUTO", "PHYIDLE?", "GPIO?" -> "OK"
                else -> prefixed(l)
            }
        }
        if (reply != null) send((reply + "\n").toByteArray(Charsets.US_ASCII))
        return reply
    }

    /** One level update for a seat, as the scanner task gives it: microseconds apart, 1..254. */
    fun feed(index: Int, tMs: Long, level: Int) {
        val s = seats.getOrNull(index) ?: return
        if (!s.live) return
        val us = tMs * 1000
        val loop = if (s.lastUs > 0) ((us - s.lastUs).toInt()).coerceIn(0, 65535) else 0
        s.lastUs = us
        s.lap.feed(tMs, level.coerceIn(1, 254), loop)
    }

    /** What CONFIG? reports. */
    val config: RhConfig get() = RhConfig(gain, gainMax, gainMode, nfft, mode, metric, levelFilter)

    private fun isTextStart(b: Int) = b in 0x41..0x50 || b == 'T'.code

    private fun textByte(b: Int) {
        if (!inText) {
            inText = true
            line.setLength(0)
        }
        if (b == '\n'.code) {
            inText = false
            text(line.toString())
        } else if (b != '\r'.code) line.append(b.toChar())
    }

    private fun prefixed(l: String): String? = when {
        l.startsWith("GAIN MANUAL ") -> l.after("GAIN MANUAL ")?.let { n ->
            if (n !in 0..gainMax) "ERR gain" else {
                gain = n
                gainMode = GainMode.MANUAL
                "OK"
            }
        } ?: "ERR gain"
        l.startsWith("FFT ") -> l.after("FFT ")?.let { n ->
            if (n < FFT_MIN || n > FFT_MAX || n and (n - 1) != 0) "ERR fft" else {
                nfft = n
                "OK"
            }
        } ?: "ERR fft"
        l.startsWith("FILTER ") || l.startsWith("IQCORR ") || l.startsWith("PHYIDLE ") ||
            l.startsWith("THROTTLE ") || l.startsWith("GPIO ") -> "OK"
        else -> "ERR rh_mode"
    }

    private fun String.after(prefix: String): Int? = substringAfter(prefix).trim().toIntOrNull()

    private fun setMetric(m: RhMetric): String {
        if (m != metric) resetLevels() // RSSI and SNR codes don't compare
        metric = m
        return "OK"
    }

    private fun setFilter(f: RhLapFilter): String {
        if (f != levelFilter) {
            seats.forEach {
                it.lap.setFilter(f)
                it.lastUs = 0L
            }
        }
        levelFilter = f
        return "OK"
    }

    /** rh_reset_levels: every seat's detector, and the interval it was measuring against. */
    private fun resetLevels() {
        seats.forEach {
            it.lap.reset()
            it.lastUs = 0L
        }
    }

    private fun resetConfig(): String {
        gain = 45
        gainMode = GainMode.MANUAL
        nfft = 256
        mode = LoPlan.GROUP
        setMetric(RhMetric.RSSI)
        setFilter(RhLapFilter.MEDIAN)
        return "OK"
    }

    // ---- The byte protocol, command by command. ----

    private fun readCmd(cmd: Int) {
        val out = ArrayList<Int>()
        val s = seats[cur]
        val now = clockMs()
        fun u16(v: Int) {
            out += (v shr 8) and 0xff
            out += v and 0xff
        }

        fun passStats() {
            out += s.lap.lap
            u16((now - s.lap.passMs).toInt())
            out += if (s.lap.valid) s.lap.level else 1 // a node with no reading yet says "one"
            out += s.lap.nodePeak
            out += s.lap.passPeak
            u16(s.lap.loopUs)
        }

        fun extremums() {
            val x = s.lap.popExtremum()
            out += (if (s.lap.crossing) FLAG_CROSSING else 0) or (if (x?.peak == true) FLAG_PEAK else 0)
            out += s.lap.passNadir
            out += s.lap.nodeNadir
            if (x == null) {
                out += 0
                u16(0)
                u16(0)
            } else {
                out += x.extremum.level
                u16((now - x.extremum.firstMs).toInt())
                u16(x.extremum.durationMs)
            }
        }

        fun block(t: String) {
            for (j in 0 until TEXT_BLOCK) out += if (j < t.length) t[j].code else 0
        }

        when (cmd) {
            Rh.READ_ADDRESS -> out += 0
            Rh.READ_FREQUENCY -> u16(s.freq)
            Rh.TEST_RX_REGISTER -> out += if (s.mapped) 1 else 0
            0x05 -> {
                passStats()
                extremums()
            }
            Rh.READ_LAP_PASS_STATS -> passStats()
            Rh.READ_LAP_EXTREMUMS -> extremums()
            0x11 -> u16(0) // no STM32, no in-app update
            Rh.READ_REVISION_CODE -> {
                out += RH_API_MARKER
                out += RH_API_LEVEL
            }
            Rh.READ_NODE_RSSI_PEAK -> out += s.lap.nodePeak
            Rh.READ_NODE_RSSI_NADIR -> out += s.lap.nodeNadir
            Rh.READ_ENTER_AT_LEVEL -> out += s.lap.enterAt
            Rh.READ_EXIT_AT_LEVEL -> out += s.lap.exitAt
            Rh.READ_TIME_MILLIS -> {
                u16(((now shr 16) and 0xffff).toInt())
                u16((now and 0xffff).toInt())
            }
            Rh.READ_MULTINODE_COUNT -> out += RH_SEATS
            Rh.READ_CURNODE_INDEX -> out += cur
            0x3c -> out += cur
            Rh.READ_FW_VERSION -> block("0.3.0-sim")
            0x3e -> block("Oct 26 2026")
            0x3f -> block("12:00:00")
            0x40 -> block("Widscan58 c5") // the firmware says "Widscan58 " + target[5:]
            else -> return // unknown: no reply, as the node's own
        }
        val bytes = ByteArray(out.size + 1)
        var sum = 0
        out.forEachIndexed { i, v ->
            bytes[i] = v.toByte()
            sum += v
        }
        bytes[bytes.size - 1] = (sum and 0xff).toByte()
        send(bytes)
    }

    private fun writeCmd(cmd: Int, d: IntArray) {
        val s = seats[cur]
        when (cmd) {
            Rh.WRITE_FREQUENCY -> s.setFrequency(rhBe16(d.toBytes()), inBand)
            Rh.WRITE_ENTER_AT_LEVEL -> s.lap.enterAt = d[0]
            Rh.WRITE_EXIT_AT_LEVEL -> s.lap.exitAt = d[0]
            Rh.FORCE_END_CROSSING -> s.lap.endCrossing()
            Rh.WRITE_CURNODE_INDEX -> if (d[0] < RH_SEATS) cur = d[0]
            else -> Unit // 0x75, 0x79, 0x7e: nothing for a widscan58 node to do
        }
    }

    /** The firmware's payload table (write_size in rh.c). */
    private fun writeSize(cmd: Int) = when (cmd) {
        Rh.WRITE_FREQUENCY, 0x75 -> 2
        Rh.WRITE_ENTER_AT_LEVEL, Rh.WRITE_EXIT_AT_LEVEL, Rh.FORCE_END_CROSSING, 0x79, Rh.WRITE_CURNODE_INDEX, 0x7e -> 1
        else -> 0
    }
}

private fun IntArray.toBytes(): ByteArray = ByteArray(size) { this[it].toByte() }
