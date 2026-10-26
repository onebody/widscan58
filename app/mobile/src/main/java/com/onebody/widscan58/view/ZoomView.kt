// Channel zoom: the spectrum around one channel, with the occupancy markers the
// numbers alone cannot place. Mirrors web/src/views/zoom.ts. GPL-3.0-or-later.
package com.onebody.widscan58.view

import android.content.Context
import android.graphics.Canvas
import android.graphics.DashPathEffect
import android.graphics.Paint
import android.graphics.Path
import android.util.AttributeSet
import android.view.View
import com.onebody.widscan58.R
import com.onebody.widscan58.core.ChannelFlag
import com.onebody.widscan58.core.ChannelMetrics
import com.onebody.widscan58.core.Store
import com.onebody.widscan58.core.composite
import java.util.Locale
import kotlin.math.ceil
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt

class ZoomView @JvmOverloads constructor(context: Context, attrs: AttributeSet? = null) :
    View(context, attrs) {

    private val theme = Style(context)

    var store: Store? = null
    /** Plan slot this chart is dialled to. */
    var slot = 0
    var showMaxHold = true
    var showAverage = true
    var dbMin = -85.0
    var dbMax = -20.0

    private val fill = Paint(Paint.ANTI_ALIAS_FLAG)
    private val line = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE; strokeWidth = theme.dp(1f) }
    private val trace = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = theme.dp(1.2f)
        strokeJoin = Paint.Join.ROUND
    }
    private val axisLabel = theme.text(9f, theme.muted)
    private val unit = theme.text(8f, theme.muted)

    // Geometry of the current draw: the plot maps kHz and dBFS onto pixels.
    private var f0 = 0.0
    private var f1 = 1.0
    private var y0 = -85.0
    private var y1 = -20.0
    private var left = 0f
    private var right = 1f
    private var top = 0f
    private var bottom = 1f

    private fun sx(khz: Double) = left + ((khz - f0) / (f1 - f0)).toFloat() * (right - left)

    private fun sy(db: Double) =
        bottom - ((max(y0, min(y1, db)) - y0) / (y1 - y0)).toFloat() * (bottom - top)

    override fun onDraw(canvas: Canvas) {
        fill.color = theme.surface
        canvas.drawRect(0f, 0f, width.toFloat(), height.toFloat(), fill)
        val store = store
        val profile = store?.profile
        val ch = profile?.channels?.getOrNull(slot)
        if (store == null || profile == null || ch == null || store.segments.isEmpty()) {
            axisLabel.textAlign = Paint.Align.CENTER
            canvas.drawText(context.getString(R.string.waiting_spectrum), width / 2f, height / 2f, axisLabel)
            axisLabel.textAlign = Paint.Align.LEFT
            return
        }

        val nominal = ch.khz.toDouble()
        val status = store.status
        val rssiKhz = (status?.rssiHz ?: profile.rssiHz) / 1000.0
        val searchKhz = (status?.searchHz ?: profile.searchHz) / 1000.0
        // A little more than the search window, so a signal's shoulders are in frame.
        val half = searchKhz * ZOOM_FACTOR
        f0 = nominal - half
        f1 = nominal + half
        left = theme.dp(34f)
        right = width - theme.dp(8f)
        top = theme.dp(16f)
        bottom = height - theme.dp(15f)
        y0 = dbMin
        y1 = if (dbMax > dbMin + 1.0) dbMax else dbMin + 65.0

        val comp = composite(store, f0, f1, (right - left).roundToInt().coerceIn(64, MAX_COLUMNS))
        fill.color = theme.alpha(theme.channelColor(slot), 0.14)
        canvas.drawRect(sx(nominal - rssiKhz), top, sx(nominal + rssiKhz), bottom, fill)

        drawGrid(canvas, nominal)

        canvas.save()
        canvas.clipRect(left.roundToInt(), top.roundToInt(), right.roundToInt(), bottom.roundToInt())
        drawFloor(canvas, store)
        trace.pathEffect = null
        if (showAverage) stroke(canvas, comp.avg, theme.accent, theme.dp(1.5f))
        if (showMaxHold) stroke(canvas, comp.max, theme.warning, theme.dp(1f))
        stroke(canvas, comp.live, theme.ink, theme.dp(1.2f))
        drawMarkers(canvas, store.channels.getOrNull(slot)?.latest, nominal)
        canvas.restore()

        unit.textAlign = Paint.Align.LEFT
        unit.color = theme.muted
        canvas.drawText(context.getString(R.string.zoom_axis), theme.dp(2f), top - theme.dp(5f), unit)
    }

    private fun drawGrid(canvas: Canvas, nominal: Double) {
        axisLabel.color = theme.muted
        line.color = theme.grid
        val dbStep = niceStep(y1 - y0, ((bottom - top) / theme.dp(24f)).roundToInt().coerceIn(2, 6))
        var v = ceil(y0 / dbStep) * dbStep
        axisLabel.textAlign = Paint.Align.RIGHT
        while (v <= y1 + 1e-9) {
            val y = sy(v)
            canvas.drawLine(left, y, right, y, line)
            canvas.drawText(v.roundToInt().toString(), left - theme.dp(4f), y + theme.dp(3f), axisLabel)
            v += dbStep
        }
        // The axis reads as distance from the channel, which is the whole point of the zoom.
        val mhzStep = niceStep((f1 - f0) / 1000.0, ((right - left) / theme.dp(56f)).roundToInt().coerceIn(2, 5))
        var m = ceil((f0 - nominal) / 1000.0 / mhzStep) * mhzStep
        axisLabel.textAlign = Paint.Align.CENTER
        while (nominal + m * 1000.0 <= f1 + 1e-9) {
            val x = sx(nominal + m * 1000.0)
            canvas.drawLine(x, top, x, bottom, line)
            canvas.drawText(String.format(Locale.US, "%+.1f", m), x, bottom + theme.dp(9f), axisLabel)
            m += mhzStep
        }
        line.color = theme.axis
        canvas.drawLine(left, bottom, right, bottom, line)
    }

    /** The noise floor this LO was measured against, as a level the peaks can be read from. */
    private fun drawFloor(canvas: Canvas, store: Store) {
        val state = store.channels.getOrNull(slot) ?: return
        val floor = store.floorByLo[state.loKhz] ?: return
        line.color = theme.muted
        line.pathEffect = DashPathEffect(floatArrayOf(theme.dp(4f), theme.dp(3f)), 0f)
        canvas.drawLine(left, sy(floor), right, sy(floor), line)
        line.pathEffect = null
    }

    private fun stroke(canvas: Canvas, data: DoubleArray, color: Int, widthPx: Float) {
        trace.color = color
        trace.strokeWidth = widthPx
        val step = (right - left) / data.size
        val path = Path()
        var pen = false
        for (x in data.indices) {
            val v = data[x]
            if (v.isNaN()) {
                pen = false
                continue
            }
            val px = left + (x + 0.5f) * step
            if (pen) path.lineTo(px, sy(v)) else path.moveTo(px, sy(v))
            pen = true
        }
        canvas.drawPath(path, trace)
    }

    /** Nominal, the most occupied offset, the video centre, and the 99 % occupied band. */
    private fun drawMarkers(canvas: Canvas, m: ChannelMetrics?, nominal: Double) {
        vline(canvas, nominal, theme.ink2, dash = true)
        if (m == null) return
        val occupied = m.has(ChannelFlag.SIGNAL) && !m.has(ChannelFlag.NO_CENTROID)
        if (!occupied) return
        m.peakOffHz?.let { vline(canvas, nominal + it / 1000.0, theme.good) }
        m.centroidOffHz?.let { vline(canvas, nominal + it / 1000.0, theme.critical) }
        val peak = m.peak
        val lo = m.obwLoHz
        val hi = m.obwHiHz
        if (peak == null || lo == null || hi == null) return
        // A bracket under the peak, because the band itself is the width of the signal.
        val y = sy(peak - OBW_DROP_DB)
        val xa = sx(nominal + lo / 1000.0)
        val xb = sx(nominal + hi / 1000.0)
        val tick = theme.dp(4f)
        line.color = theme.good
        line.strokeWidth = theme.dp(1.5f)
        canvas.drawLine(xa, y - tick, xa, y, line)
        canvas.drawLine(xa, y, xb, y, line)
        canvas.drawLine(xb, y, xb, y - tick, line)
        line.strokeWidth = theme.dp(1f)
    }

    private fun vline(canvas: Canvas, khz: Double, color: Int, dash: Boolean = false) {
        val x = sx(khz).roundToInt() + 0.5f
        line.color = color
        line.strokeWidth = theme.dp(1.5f)
        if (dash) line.pathEffect = DashPathEffect(floatArrayOf(theme.dp(2f), theme.dp(3f)), 0f)
        canvas.drawLine(x, top, x, bottom, line)
        line.pathEffect = null
        line.strokeWidth = theme.dp(1f)
    }

    companion object {
        /** Search windows are where the metrics come from; the frame shows a little beyond. */
        private const val ZOOM_FACTOR = 1.6
        /** The web UI brackets the occupied band this far under the hop's own peak. */
        private const val OBW_DROP_DB = 10.0
        private const val MAX_COLUMNS = 700
    }
}
