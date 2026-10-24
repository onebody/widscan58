// Shared colours and metrics for the instrument views: the same palette as the
// web UI (repo/web/src/styles.css). GPL-3.0-or-later.
package com.onebody.widscan58.view

import android.content.Context
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Typeface
import com.onebody.widscan58.R
import kotlin.math.floor
import kotlin.math.log10
import kotlin.math.pow
import kotlin.math.roundToInt

/** Axis tick spacing rounded to 1/2/5 x 10^n, so labels land on round numbers. */
fun niceStep(range: Double, target: Int): Double {
    val raw = range / maxOf(1, target)
    val mag = 10.0.pow(floor(log10(raw)))
    val n = raw / mag
    return (if (n < 1.5) 1.0 else if (n < 3.0) 2.0 else if (n < 7.0) 5.0 else 10.0) * mag
}

class Style(context: Context) {
    private val res = context.resources
    val density = res.displayMetrics.density

    /** scaledDensity is deprecated: text size is density x the user's font scale. */
    private val sp = density * res.configuration.fontScale

    fun dp(value: Float) = value * density
    fun sp(value: Float) = value * sp

    private fun argb(id: Int) = res.getColor(id, null)

    val page = argb(R.color.page)
    val surface = argb(R.color.surface)
    val surface2 = argb(R.color.surface2)
    val ink = argb(R.color.ink)
    val ink2 = argb(R.color.ink2)
    val muted = argb(R.color.muted)
    val grid = argb(R.color.grid)
    val axis = argb(R.color.axis)
    val accent = argb(R.color.accent)
    val good = argb(R.color.good)
    val warning = argb(R.color.warning)
    val serious = argb(R.color.serious)
    val critical = argb(R.color.critical)

    /** Channel colours by plan index; the same eight slots the web UI uses. */
    val series = listOf(
        argb(R.color.s1), argb(R.color.s2), argb(R.color.s3), argb(R.color.s4),
        argb(R.color.s5), argb(R.color.s6), argb(R.color.s7), argb(R.color.s8),
    )

    fun channelColor(id: Int) = series.getOrElse(id) { muted }

    val tone = mapOf(
        com.onebody.widscan58.core.BadgeTone.GOOD to good,
        com.onebody.widscan58.core.BadgeTone.WARNING to warning,
        com.onebody.widscan58.core.BadgeTone.SERIOUS to serious,
        com.onebody.widscan58.core.BadgeTone.CRITICAL to critical,
    )

    /** Waterfall ramp: the sequential stops interpolated to 256 colours. */
    val ramp: IntArray = run {
        val stops = listOf(
            argb(R.color.seq0), argb(R.color.seq1), argb(R.color.seq2), argb(R.color.seq3),
            argb(R.color.seq4), argb(R.color.seq5), argb(R.color.seq6),
        )
        IntArray(256) { j ->
            val x = (j / 255.0) * (stops.size - 1)
            val at = floor(x).toInt().coerceAtMost(stops.size - 2)
            blend(stops[at], stops[at + 1], x - at)
        }
    }

    fun text(sizeSp: Float, color: Int, bold: Boolean = false): Paint = Paint().apply {
        isAntiAlias = true
        textSize = sizeSp * this@Style.sp
        typeface = if (bold) Typeface.DEFAULT_BOLD else Typeface.DEFAULT
        setColor(color)
    }

    /** Same colour with the web UI's window alpha applied. */
    fun alpha(color: Int, fraction: Double): Int = Color.argb(
        (255 * fraction).roundToInt().coerceIn(0, 255),
        Color.red(color), Color.green(color), Color.blue(color),
    )

    private fun blend(a: Int, b: Int, f: Double): Int = Color.rgb(
        (Color.red(a) + (Color.red(b) - Color.red(a)) * f).toInt(),
        (Color.green(a) + (Color.green(b) - Color.green(a)) * f).toInt(),
        (Color.blue(a) + (Color.blue(b) - Color.blue(a)) * f).toInt(),
    )
}

/** dBFS with one decimal, or the web UI's em dash when the device did not measure it. */
fun fmtDb(v: Double?): String = if (v == null) "—" else String.format("%.1f", v)

fun fmtDbfs(v: Double?): String = if (v == null) "—" else String.format("%.1f dBFS", v)

fun fmtMHz(khz: Double): String = String.format("%.1f", khz / 1000.0)

