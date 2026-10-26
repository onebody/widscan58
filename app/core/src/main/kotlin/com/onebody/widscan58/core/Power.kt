// Power meter mode's math: the board's dBFS-to-dBm calibration, the smoothing the
// meter averages with, and what one channel's card shows. Mirrors
// web/src/model/power.ts; the firmware knows nothing about dBm, so all of it lives here.
// GPL-3.0-or-later.
package com.onebody.widscan58.core

import java.util.Locale
import kotlin.math.log10
import kotlin.math.pow

/** dBm at the board's RF input minus dBFS, at one channel's frequency. [Offset.calibrated]
 *  is false where the calibration was not actually measured. */
data class Offset(val db: Double, val calibrated: Boolean)

/** One channel's card, as the web composes it inside `render()`. */
class Reading(
    val levelDbfs: Double,
    val noiseDbfs: Double?,
    /** Null within 3 dB of the window's noise: there is no signal to read. */
    val signalDbfs: Double?,
    /** dBm at the board's RF input; null with nothing to read, or while clipping. */
    val boardDbm: Double?,
    /** dBm at the VTX, that is [boardDbm] plus the user's attenuator. */
    val vtxDbm: Double?,
    val clipping: Boolean,
    val overRange: Boolean,
    val calibrated: Boolean,
    val stale: Boolean,
)

object Power {
    const val GAIN = 0
    const val WIN_HZ = 15_000_000L
    const val INPUT_MAX_DBM = -5.0

    /** The web's `powerCommands(usableHz)`; [usableHz] comes from the profile. */
    fun commands(usableHz: Long): List<String> = listOf(
        "MODE SINGLE",
        "GAIN MANUAL $GAIN",
        "WIN $WIN_HZ $WIN_HZ",
        "METRICS FULL",
        "SPEC 0 256 $usableHz MAX",
    )

    /** Monitor mode's windows and spectrum per profile, put back on leaving power meter mode
     *  when no snapshot of the user's own scan is at hand. */
    class MonitorRestore(val rssiHz: Long, val searchHz: Long, val spec: String) {
        fun commands(): List<String> = listOf(DeviceCommands.windows(rssiHz, searchHz), spec)
    }

    val MONITOR_DEFAULTS = mapOf(
        "c5-race" to MonitorRestore(5_000_000L, 12_000_000L, "SPEC 4 256 40000000 MAX"),
        "s3-race24" to MonitorRestore(1_410_000L, 3_390_000L, "SPEC 4 256 11300000 MAX"),
    )

    /** The scan this mode set up: the calibration's window, and no spectrum hops between LOs. */
    fun isPowerScan(status: Status?): Boolean =
        status != null && status.rssiHz == WIN_HZ && status.specEvery == 0

    /** The running scan worth snapshotting on entry; null when this already is the power scan,
     *  which would otherwise restore itself and lose the monitor windows for good. */
    fun monitorScan(status: Status?): MonitorRestore? =
        status?.takeIf { !isPowerScan(it) }?.let {
            MonitorRestore(
                it.rssiHz,
                it.searchHz,
                DeviceCommands.spectrum(it.specEvery, it.specBins, it.specSpanHz, it.specDetector != 0),
            )
        }

    /** dBm at the board's RF input minus dBFS at [GAIN], by channel MHz.
     *  From 2026-10-19 (`tools/powercal.py` against an ImmersionRC RF Power Meter v2, the mean
     *  of 25/100/400 mW, about ±1 dB); R1-R5 are 0.5 dB under what was measured, because the
     *  calibration ran at ADC rate 0 and the firmware now runs rate 1 at every LO. An estimate
     *  until the calibration is redone. */
    private val CALIBRATION: Map<String, List<Pair<Int, Double>>> = mapOf(
        "c5-race" to listOf(
            5658 to 27.4,
            5695 to 27.3,
            5732 to 26.8,
            5769 to 26.1,
            5806 to 25.9,
            5843 to 25.3,
            5880 to 25.2,
            5917 to 26.1,
        ),
    )

    /** Null: this profile was never calibrated, so the meter has nothing to show for it. */
    fun calOffset(profile: String, mhz: Int): Offset? {
        val pts = CALIBRATION[profile]?.takeIf { it.isNotEmpty() } ?: return null
        val (loF, loDb) = pts.first()
        val (hiF, hiDb) = pts.last()
        // Outside the measured points the offset stays at the nearest one's value, and beyond
        // 10 MHz of it the reading counts as uncalibrated.
        if (mhz <= loF) return Offset(loDb, calibrated = mhz >= loF - 10)
        if (mhz >= hiF) return Offset(hiDb, calibrated = mhz <= hiF + 10)
        for (j in 1 until pts.size) {
            val (f0, a) = pts[j - 1]
            val (f1, b) = pts[j]
            if (mhz <= f1) return Offset(a + (b - a) * (mhz - f0) / (f1 - f0), calibrated = true)
        }
        return null
    }

    /** Smoothing choices, seconds of averaging; 0 is the latest hop. The labels are the app's. */
    val SMOOTHING_SECONDS = listOf(0.0, 0.1, 0.5, 1.0, 3.0)

    /** Attenuator sizes for the warning table: the board's input stays at or below
     *  [INPUT_MAX_DBM] with 2 dB to spare for a VTX above its label, and the first attenuator
     *  takes nearly all the power, so it is rated at least twice that. */
    class AttenuatorRow(val vtx: String, val dbm: Int, val attenDb: Int, val rating: String)

    val ATTENUATOR_TABLE = listOf(
        AttenuatorRow("25 mW", 14, 30, "0.5 W"),
        AttenuatorRow("500 mW", 27, 40, "1 W"),
        AttenuatorRow("1 W", 30, 40, "2 W"),
        AttenuatorRow("2 W", 33, 40, "5 W"),
    )

    /** Mean power (linear) of a dBFS series over the last [seconds], in dBFS, leaving out the
     *  values at the times in [skip]; the newest value when [seconds] is 0 or nothing falls
     *  in the window. */
    fun meanDb(series: Series, now: Double, seconds: Double, skip: Set<Double>? = null): Double? {
        val newest = series.newest() ?: return null
        if (seconds <= 0) return newest.toDouble()
        var sum = 0.0
        var n = 0
        series.forEachWithin(now, seconds) { t, v ->
            if (skip?.contains(t) != true) {
                sum += 10.0.pow(v / 10.0)
                n++
            }
        }
        return if (n > 0) 10 * log10(sum / n) else newest.toDouble()
    }

    /** The times of the clipped captures in the last [seconds], and the share of the window's
     *  captures they are. */
    class Clip(val times: Set<Double>, val share: Double)

    fun clippedIn(clipped: Series, now: Double, seconds: Double): Clip {
        val times = HashSet<Double>()
        var n = 0
        clipped.forEachWithin(now, seconds) { t, v ->
            n++
            if (v != 0f) times.add(t)
        }
        return Clip(times, if (n > 0) times.size.toDouble() / n else 0.0)
    }

    /** Signal power without the window's noise, dBFS; null within 3 dB of it. */
    fun lessNoise(db: Double, noiseDb: Double?): Double? {
        if (noiseDb == null) return db
        val p = 10.0.pow(db / 10) - 10.0.pow(noiseDb / 10)
        return if (p > 0 && db - noiseDb >= 3) 10 * log10(p) else null
    }

    /** "400 mW", "1.50 W" — the web's unit switch at 100 mW, 10, 1 and 1000 mW. */
    fun formatMw(dbm: Double): String {
        val mw = 10.0.pow(dbm / 10)
        return when {
            mw >= 1000 -> "${(mw / 1000).format(if (mw >= 10000) 1 else 2)} W"
            mw >= 100 -> "${mw.format(0)} mW"
            mw >= 10 -> "${mw.format(1)} mW"
            mw >= 1 -> "${mw.format(2)} mW"
            else -> "${mw.format(3)} mW"
        }
    }

    /** Fixed decimals, with a point: the readout is the same in every locale. */
    private fun Double.format(places: Int): String = String.format(Locale.US, "%.${places}f", this)

    /** Readings only mean anything with the scan the calibration was made with. */
    fun scanIsReady(status: Status?): Boolean =
        status != null &&
            status.gainMode == "MANUAL" &&
            status.gain == GAIN &&
            status.rssiHz == WIN_HZ &&
            status.mode == "SINGLE"

    /** [mhz] is the channel's, rounded as the web rounds it; null with nothing measured yet. */
    fun readout(
        cs: ChannelState,
        profile: String,
        mhz: Int,
        attenDb: Double,
        smoothS: Double,
        now: Double,
    ): Reading? {
        val latest = cs.latest ?: return null
        val off = calOffset(profile, mhz) ?: return null
        // Averaging leaves out clipped captures: a powered VTX's setup can clip single
        // captures far above its level. Off, the latest capture shows as it is.
        val clip = if (smoothS > 0) clippedIn(cs.clipped, now, smoothS) else null
        val over = if (clip != null) clip.share > 0.5 else latest.has(ChannelFlag.SATURATED)
        val rssi = meanDb(cs.rssi, now, smoothS, clip?.times) ?: return null
        val noise = meanDb(cs.floor, now, smoothS, clip?.times)
        val sig = lessNoise(rssi, noise)
        val input = if (over || sig == null) null else sig + off.db
        return Reading(
            levelDbfs = rssi,
            noiseDbfs = noise,
            signalDbfs = sig,
            boardDbm = input,
            vtxDbm = input?.plus(attenDb),
            clipping = over,
            overRange = input != null && input > INPUT_MAX_DBM,
            calibrated = off.calibrated,
            stale = now - cs.updated > 1.0,
        )
    }
}
