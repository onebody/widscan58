// Device data model. Mirrors web/src/proto/frames.ts and firmware/src/proto/frames.h.
// GPL-3.0-or-later.
package com.onebody.widscan58.core

object FrameType {
    const val METRICS = 1
    const val SPECTRUM = 2
    const val TELEMETRY = 3
    const val RAWIQ = 4
    const val LOG = 5
    const val LEVELS = 6
}

object ChannelFlag {
    const val SIGNAL = 1
    const val SATURATED = 2
    const val MASKED = 4 // DC or spur bins excluded in the search window
    const val EDGE = 8
    const val NO_CENTROID = 16
    const val SPUR = 32
}

/** Where a protocol-console line came from: the web's LogLine dirs. */
enum class LogDir {
    RX,
    TX,
    INFO,
    ERROR,
    ;

    /** The web's own prefix: a sent command points out, everything else points in. */
    val arrow: String get() = if (this == TX) ">" else "<"
}

/** One console line, `t` seconds after the store opened. */
class LogLine(val t: Double, val dir: LogDir, val text: String)

/** What the device measures per channel (METRICS command). */
enum class MetricsMode {
    FULL,
    RSSI,
    SNR,
    ;

    companion object {
        fun of(code: Int): MetricsMode = entries.getOrElse(code) { RSSI }
    }
}

class ChannelMetrics(
    val id: Int,
    val flags: Int,
    val rssi: Double, // dBFS
    // null: not measured in this metrics mode.
    val rssiEma: Double?,
    val floor: Double?, // noise in the RSSI window, dBFS
    val snr: Double?, // (S+N)/N dB
    val clearFloor: Double?, // also null before the channel was ever clear
    val peak: Double?, // per-bin dBFS, this hop
    val peakOffHz: Long?,
    val centroidOffHz: Long?,
    val obwLoHz: Long?,
    val obwHiHz: Long?,
) {
    fun has(flag: Int) = flags and flag != 0
}

class Metrics(
    val seq: Long,
    val tMs: Long,
    val loKhz: Long,
    val tuneUs: Long,
    val captureUs: Long,
    val dspUs: Long,
    val saturated: Int,
    val mode: MetricsMode,
    val floorBin: Double?, // dBFS per FFT bin
    val dc: Double?, // dBFS
    val gain: Int, // 255 = hardware AGC
    val hopIndex: Int,
    val hops: Int,
    val channels: List<ChannelMetrics>,
)

class Spectrum(
    val seq: Long,
    val loKhz: Long,
    val startOffHz: Long,
    val binHz: Long,
    val detector: Int,
    val db: DoubleArray, // dBFS per display bin
) {
    val bins: Int get() = db.size

    /** First and last frequency of this segment, in kHz. */
    fun startKhz(): Double = loKhz + startOffHz / 1000.0

    fun endKhz(): Double = startKhz() + bins * binHz / 1000.0
}

class Telemetry(
    val tMs: Long,
    val rotations: Long,
    val hopsTotal: Long,
    val rotationHz: Double,
    val hopUsAvg: Long,
    val hopUsMax: Long,
    val tuneUsAvg: Long,
    val tuneUsMax: Long,
    val captureUsAvg: Long,
    val dspUsAvg: Long,
    val dspUsMax: Long,
    val linkFill: Double, // 0..1
    val heapFree: Long,
    val drops: Long,
    val captureErrors: Long,
    val running: Boolean,
)

class RawIq(
    val loKhz: Long,
    val rateHz: Long,
    val n: Int,
    val conjugated: Boolean,
    val shift: Int, // samples are 10-bit values >> shift
    val i: ByteArray,
    val q: ByteArray,
)

class RawIqChunk(
    val loKhz: Long,
    val rateHz: Long,
    val n: Int,
    val offset: Int,
    val count: Int,
    val conjugated: Boolean,
    val shift: Int,
    val payload: ByteArray,
) {
    fun isLast(): Boolean = offset + count >= n
}

// ---- Text (JSON) replies, decoded by the platform layer.

class ProfileChannel(val id: Int, val name: String, val khz: Long)

class Profile(
    val name: String,
    val description: String,
    val target: String,
    val bandKhz: LongArray, // [lo, hi]
    val baseKhz: Long,
    val equivBaseKhz: Long,
    val scale: Double,
    val rateHz: Long,
    val usableHz: Long,
    val passbandHz: Long,
    val rssiHz: Long,
    val searchHz: Long,
    val channels: List<ProfileChannel>,
)

class PlanHop(val loKhz: Long, val ch: IntArray, val offsetHz: LongArray)

class Status(
    val profile: String,
    val running: Int,
    val enabled: Int,
    val mode: String,
    val captureSamples: Int,
    val nfft: Int,
    val segments: Int,
    val rateCode: Int,
    val filter: Int,
    val conjugate: Int,
    val iqCorr: Int,
    val settleUs: Int,
    val tskip: Int?,
    val gainMode: String,
    val gain: Int,
    val gainMax: Int,
    val rawiqMax: Int?,
    val recal: String,
    val metrics: MetricsMode?,
    val rssiHz: Long,
    val searchHz: Long,
    val dcGuard: Int,
    // STATUS reports the smoothing factors as fractions; the EMA command takes thousandths.
    val ema: Double,
    val clearEma: Double,
    val signalDb: Double,
    val avgShift: Int,
    val specEvery: Int,
    val specBins: Int,
    val specSpanHz: Long,
    val specDetector: Int,
    val plan: List<PlanHop>,
) {
    /** LOs the scan plan visits; a spectrum segment whose LO is gone is stale. */
    fun planLokhz(): Set<Long> = plan.map { it.loKhz }.toSet()
}
