// RotorHazard levels: every seat polled over the last seconds, with the selected seat's
// EnterAt/ExitAt lines, the peaks the node queued between polls, and a mark for each pass.
// Mirrors the RhGraph card of web/src/views/rh.ts. GPL-3.0-or-later.
package com.onebody.widscan58.view

import android.content.Context
import android.graphics.Canvas
import android.graphics.DashPathEffect
import android.graphics.Paint
import android.graphics.Path
import android.util.AttributeSet
import android.view.View
import com.onebody.widscan58.R
import com.onebody.widscan58.core.RhEventKind
import com.onebody.widscan58.core.RhMetric
import com.onebody.widscan58.core.RhModel
import com.onebody.widscan58.core.RhSeat
import com.onebody.widscan58.core.Store
import java.util.Locale
import kotlin.math.abs
import kotlin.math.ceil
import kotlin.math.floor
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt

class RhLevelsView @JvmOverloads constructor(context: Context, attrs: AttributeSet? = null) :
    View(context, attrs) {

    private val theme = Style(context)

    var store: Store? = null
    var model: RhModel? = null
    var spanSeconds = 30.0
    var seat = 0

    private val trace = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeJoin = Paint.Join.ROUND
    }
    private val line = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE; strokeWidth = theme.dp(1f) }
    private val dashed = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = theme.dp(1.5f)
        pathEffect = DashPathEffect(floatArrayOf(theme.dp(5f), theme.dp(4f)), 0f)
    }
    private val dot = Paint(Paint.ANTI_ALIAS_FLAG)
    private val axisLabel = theme.text(9f, theme.muted)
    private val caption = theme.text(8f, theme.muted)

    private var left = 0f
    private var right = 1f
    private var top = 0f
    private var bottom = 1f
    private var y0 = 140.0
    private var y1 = 220.0

    private fun sx(ago: Double) = right - (ago / spanSeconds).toFloat() * (right - left)

    private fun sy(v: Double) = bottom - ((v.coerceIn(y0, y1) - y0) / (y1 - y0)).toFloat() * (bottom - top)

    override fun onDraw(canvas: Canvas) {
        canvas.drawColor(theme.surface)
        val model = model
        val now = store?.now() ?: 0.0
        if (model == null) return
        left = theme.dp(30f)
        right = width - theme.dp(8f)
        top = theme.dp(16f)
        bottom = height - theme.dp(15f)
        val seats = model.seats.filter { it.channel != null }
        val sel = model.seats.getOrElse(seat) { model.seats.first() }
        range(seats, sel, now)
        drawGrid(canvas)
        drawThresholds(canvas, sel)
        canvas.save()
        canvas.clipRect(left.roundToInt(), top.roundToInt(), right.roundToInt(), bottom.roundToInt())
        for (s in seats) drawSeat(canvas, model, s, sel, now)
        drawPasses(canvas, model, now, sel)
        canvas.restore()
        caption.color = theme.muted
        caption.textAlign = Paint.Align.LEFT
        canvas.drawText(
            context.getString(if (model.metric == RhMetric.RSSI) R.string.rh_axis_rssi else R.string.rh_axis_snr),
            theme.dp(2f),
            top - theme.dp(5f),
            caption,
        )
        if (seats.isEmpty()) {
            axisLabel.textAlign = Paint.Align.CENTER
            canvas.drawText(context.getString(R.string.rh_no_seats), width / 2f, height / 2f, axisLabel)
            axisLabel.textAlign = Paint.Align.LEFT
        }
    }

    /** The web's own window: a little above the peak and below the nadir, thresholds included. */
    private fun range(seats: List<RhSeat>, sel: RhSeat, now: Double) {
        var lo = Double.POSITIVE_INFINITY
        var hi = Double.NEGATIVE_INFINITY
        for (s in seats) s.history.forEachWithin(now, spanSeconds) { _, v ->
            lo = min(lo, v.toDouble())
            hi = max(hi, v.toDouble())
        }
        if (sel.channel != null) {
            lo = min(lo, sel.exit.toDouble())
            hi = max(hi, sel.enter.toDouble())
        }
        if (!lo.isFinite()) {
            lo = 140.0
            hi = 220.0
        }
        y0 = max(0.0, floor((lo - 5) / 10) * 10)
        y1 = min(260.0, ceil((hi + 5) / 10) * 10)
        if (y1 <= y0) y1 = y0 + 10
    }

    private fun drawGrid(canvas: Canvas) {
        line.color = theme.grid
        axisLabel.color = theme.muted
        axisLabel.textAlign = Paint.Align.RIGHT
        val step = niceStep(y1 - y0, ((bottom - top) / theme.dp(24f)).roundToInt().coerceIn(2, 6))
        var v = ceil(y0 / step) * step
        while (v <= y1 + 1e-9) {
            val y = sy(v)
            canvas.drawLine(left, y, right, y, line)
            canvas.drawText(v.roundToInt().toString(), left - theme.dp(4f), y + theme.dp(3f), axisLabel)
            v += step
        }
        axisLabel.textAlign = Paint.Align.CENTER
        val secs = niceStep(spanSeconds, ((right - left) / theme.dp(48f)).roundToInt().coerceIn(2, 5))
        var ago = spanSeconds
        while (ago > 1e-9) {
            val x = sx(ago)
            canvas.drawLine(x, top, x, bottom, line)
            canvas.drawText(String.format(Locale.US, "-%d", ago.roundToInt()), x, bottom + theme.dp(9f), axisLabel)
            ago -= secs
        }
        canvas.drawText("0", right, bottom + theme.dp(9f), axisLabel)
        line.color = theme.axis
        canvas.drawLine(left, bottom, right, bottom, line)
    }

    private fun drawThresholds(canvas: Canvas, sel: RhSeat) {
        if (sel.channel == null) return
        for ((v, color) in listOf(sel.enter.toDouble() to theme.good, sel.exit.toDouble() to theme.warning)) {
            dashed.color = color
            val y = sy(v)
            canvas.drawLine(left, y, right, y, dashed)
        }
    }

    private fun drawSeat(canvas: Canvas, model: RhModel, s: RhSeat, sel: RhSeat, now: Double) {
        val selected = s === sel
        trace.color = theme.channelColor(s.index)
        trace.strokeWidth = theme.dp(if (selected) 2.4f else 1.3f)
        trace.alpha = if (selected) 255 else 180
        val path = Path()
        var pen = false
        var lastX = -1f
        s.history.forEachWithin(now, spanSeconds) { t, v ->
            val x = sx(now - t)
            if (pen && abs(x - lastX) < 0.5f) return@forEachWithin
            val y = sy(v.toDouble())
            if (pen) path.lineTo(x, y) else path.moveTo(x, y)
            pen = true
            lastX = x
        }
        if (pen) canvas.drawPath(path, trace)
        trace.alpha = 255
        dot.color = theme.channelColor(s.index)
        val radius = theme.dp(if (selected) 2.5f else 1.6f)
        for (p in s.peaks) {
            if (!p.peak || now - p.t > spanSeconds) continue
            canvas.drawCircle(sx(now - p.t), sy(p.level.toDouble()), radius, dot)
        }
    }

    /** A pass is a triangle at the top of the chart, in its seat's colour. */
    private fun drawPasses(canvas: Canvas, model: RhModel, now: Double, sel: RhSeat) {
        val h = theme.dp(7f)
        for (e in model.events) {
            if (e.kind != RhEventKind.PASS || now - e.t > spanSeconds) continue
            val x = sx(now - e.t)
            trace.color = theme.channelColor(e.seat)
            trace.strokeWidth = theme.dp(1f)
            val path = Path().apply {
                moveTo(x - h / 2, top - h)
                lineTo(x + h / 2, top - h)
                lineTo(x, top - h / 3f)
                close()
            }
            dot.color = theme.channelColor(e.seat)
            canvas.drawPath(path, dot)
            if (e.seat == sel.index) {
                line.color = theme.alpha(theme.channelColor(e.seat), 0.4)
                line.strokeWidth = theme.dp(1f)
                canvas.drawLine(x, top, x, bottom, line)
            }
        }
    }
}
