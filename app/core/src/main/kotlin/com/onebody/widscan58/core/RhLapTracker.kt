// Laps of one RotorHazard seat, exactly as web/src/model/laps.ts counts them: a pass closer than the
// minimum lap to the previous counted pass is ignored (a false or double detection), and the fastest
// `count` consecutive laps are kept. A break (a channel change, a device restart or a reconnect) ends
// the run: the next pass has no lap time. The pass times since the last clear are kept, so a change of
// the rules recomputes everything from them. GPL-3.0-or-later.
package com.onebody.widscan58.core

// The phone dialog's limits, the web's.
const val RH_LAP_COUNT_MIN = 2
const val RH_LAP_COUNT_MAX = 99
const val RH_MIN_LAP_MAX_S = 999

data class RhLapRules(
    val fastestOn: Boolean,
    val count: Int,
    val minLapOn: Boolean,
    val minLapS: Double,
) {
    /** What the stored or entered values say, kept inside the dialog's limits. */
    fun of(
        fastestOn: Boolean = this.fastestOn,
        count: Int = this.count,
        minLapOn: Boolean = this.minLapOn,
        minLapS: Double = this.minLapS,
    ): RhLapRules = RhLapRules(
        fastestOn = fastestOn,
        count = count.coerceIn(RH_LAP_COUNT_MIN, RH_LAP_COUNT_MAX),
        minLapOn = minLapOn,
        minLapS = if (minLapS.isFinite()) minLapS.coerceIn(0.0, RH_MIN_LAP_MAX_S.toDouble()) else this.minLapS,
    )
}

val DEFAULT_RH_LAP_RULES = RhLapRules(fastestOn = true, count = 3, minLapOn = true, minLapS = 8.0)

class FastestRun(val totalS: Double, val laps: List<Double>)

/** A pass: ignored (with the time since the last counted pass), or counted with its lap and a new record. */
class LapResult(
    val ignoredS: Double?,
    val lapS: Double? = null,
    val fastest: FastestRun? = null,
    val previous: FastestRun? = null,
) {
    val ignored: Boolean get() = ignoredS != null
}

class RhLapTracker {
    /** Counted passes since the last clear. */
    var passes = 0
        private set
    var lastLapS: Double? = null
        private set
    var fastest: FastestRun? = null
        private set
    private var last: Double? = null
    private val run = ArrayList<Double>()
    private val history = ArrayList<Double?>()

    fun pass(t: Double, rules: RhLapRules): LapResult {
        history += t
        return step(t, rules)
    }

    /** End the current run: the next pass starts a new one. */
    fun breakRun() {
        if (history.isNotEmpty() && history.last() != null) history += null
        endRun()
    }

    fun clear() {
        history.clear()
        reset()
    }

    /** Re-apply the rules to the passes kept; the events already shown are left alone. */
    fun recompute(rules: RhLapRules) {
        reset()
        for (t in history) {
            if (t == null) endRun() else step(t, rules)
        }
    }

    private fun step(t: Double, rules: RhLapRules): LapResult {
        val previousPass = last
        if (rules.minLapOn && previousPass != null && t - previousPass < rules.minLapS) return LapResult(ignoredS = t - previousPass)
        val lapS = previousPass?.let { t - it }
        last = t
        passes++
        lastLapS = lapS
        if (lapS == null) return LapResult(ignoredS = null)
        if (!rules.fastestOn) return LapResult(ignoredS = null, lapS = lapS)
        run += lapS
        if (run.size > rules.count) run.removeAt(0)
        if (run.size < rules.count) return LapResult(ignoredS = null, lapS = lapS)
        val totalS = run.sum()
        val record = fastest
        if (record != null && totalS >= record.totalS) return LapResult(ignoredS = null, lapS = lapS)
        val fastest = FastestRun(totalS, run.toList())
        this.fastest = fastest
        return LapResult(ignoredS = null, lapS = lapS, fastest = fastest, previous = record)
    }

    private fun endRun() {
        last = null
        run.clear()
    }

    private fun reset() {
        passes = 0
        lastLapS = null
        fastest = null
        endRun()
    }
}
