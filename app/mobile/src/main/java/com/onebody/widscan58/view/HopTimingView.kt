// Per-hop time as a stacked area of tune + capture + DSP, the web's
// "PER-HOP TIME BREAKDOWN" card; its hover tooltip is a drag here.
// Mirrors renderTiming() in web/src/views/debug.ts. GPL-3.0-or-later.
package com.onebody.widscan58.view

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.Path
import android.util.AttributeSet
import android.view.MotionEvent
import android.view.View
import com.onebody.widscan58.R
import com.onebody.widscan58.core.Hop
import com.onebody.widscan58.core.Store
import kotlin.math.ceil
import kotlin.math.floor
import kotlin.math.max
import kotlin.math.roundToInt

class HopTimingView @JvmOverloads constructor(context: Context, attrs: AttributeSet? = null) :
    View(context, attrs) {

    private val theme = Style(context)
    var store: Store? = null

    /** The hop under the crosshair, counted back from the newest, or null with no cursor. */
    var cursorAgo: Int? = null
        private set

    var onCursor: (() -> Unit)? = null

    private val fill = Paint(Paint.ANTI_ALIAS_FLAG)
    private val line = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE; strokeWidth = theme.dp(1f) }
    private val axisLabel = theme.text(9f, theme.muted)
    private val unit = theme.text(8f, theme.muted)

    private var left = 0f
    private var right = 1f
    private var top = 0f
    private var bottom = 1f
    private var peakMs = 1.0

    /** The three bands stacked bottom-up, in the colours the web uses for them. */
    private val parts = listOf(theme.series[0], theme.series[1], theme.series[2])

    private fun hops(): List<Hop> {
        val all = store?.hopTimes ?: return emptyList()
        return all.subList(max(0, all.size - HOPS_SHOWN), all.size)
    }

    private fun partMs(h: Hop, at: Int): Double =
        (when (at) {
            0 -> h.tuneUs
            1 -> h.captureUs
            else -> h.dspUs
        } / 1000.0)

    private fun sx(j: Int, n: Int) = left + (j + 0.5f) / n * (right - left)
    private fun sy(ms: Double) = bottom - (ms / peakMs).toFloat() * (bottom - top)

    override fun onDraw(canvas: Canvas) {
        fill.color = theme.surface
        canvas.drawRect(0f, 0f, width.toFloat(), height.toFloat(), fill)
        val hops = hops()
        if (hops.isEmpty()) {
            axisLabel.textAlign = Paint.Align.CENTER
            canvas.drawText(context.getString(R.string.waiting_telemetry), width / 2f, height / 2f, axisLabel)
            axisLabel.textAlign = Paint.Align.LEFT
            return
        }
        left = theme.dp(30f)
        right = width - theme.dp(8f)
        top = theme.dp(14f)
        bottom = height - theme.dp(15f)
        peakMs = ceil(hops.maxOf { it.totalUs / 1000.0 } + 0.5)

        drawGrid(canvas, hops.size)
        canvas.save()
        canvas.clipRect(left.roundToInt(), top.roundToInt(), right.roundToInt(), bottom.roundToInt())
        drawStack(canvas, hops)
        drawCursor(canvas, hops.size)
        canvas.restore()

        unit.textAlign = Paint.Align.LEFT
        unit.color = theme.muted
        canvas.drawText(context.getString(R.string.hop_axis), theme.dp(2f), top - theme.dp(4f), unit)
    }

    private fun drawStack(canvas: Canvas, hops: List<Hop>) {
        val n = hops.size
        val base = DoubleArray(n)
        for (at in parts.indices) {
            fill.color = parts[at]
            val path = Path()
            path.moveTo(sx(0, n), sy(base[0] + partMs(hops[0], at)))
            for (j in 1 until n) path.lineTo(sx(j, n), sy(base[j] + partMs(hops[j], at)))
            for (j in n - 1 downTo 0) path.lineTo(sx(j, n), sy(base[j]))
            path.close()
            canvas.drawPath(path, fill)
            for (j in 0 until n) base[j] += partMs(hops[j], at)
        }
    }

    private fun drawGrid(canvas: Canvas, n: Int) {
        line.color = theme.grid
        axisLabel.color = theme.muted
        axisLabel.textAlign = Paint.Align.RIGHT
        val step = niceStep(peakMs, ((bottom - top) / theme.dp(22f)).roundToInt().coerceIn(2, 5))
        var ms = 0.0
        while (ms <= peakMs + 1e-9) {
            val y = sy(ms)
            canvas.drawLine(left, y, right, y, line)
            canvas.drawText(ms.roundToInt().toString(), left - theme.dp(4f), y + theme.dp(3f), axisLabel)
            ms += step
        }
        axisLabel.textAlign = Paint.Align.CENTER
        canvas.drawText(
            context.getString(R.string.hop_span, n),
            (left + right) / 2f, bottom + theme.dp(9f), axisLabel,
        )
        line.color = theme.axis
        canvas.drawLine(left, bottom, right, bottom, line)
    }

    private fun drawCursor(canvas: Canvas, n: Int) {
        val ago = cursorAgo ?: return
        val x = sx(n - 1 - ago, n).roundToInt() + 0.5f
        line.color = theme.ink2
        line.strokeWidth = theme.dp(1f)
        canvas.drawLine(x, top, x, bottom, line)
    }

    override fun onTouchEvent(event: MotionEvent): Boolean {
        val n = hops().size
        if (n == 0 || right <= left) return false
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN, MotionEvent.ACTION_MOVE -> {
                parent?.requestDisallowInterceptTouchEvent(true)
                val j = floor((event.x - left) / (right - left) * n).toInt().coerceIn(0, n - 1)
                val ago = n - 1 - j
                if (ago != cursorAgo) {
                    cursorAgo = ago
                    invalidate()
                    onCursor?.invoke()
                }
                return true
            }

            MotionEvent.ACTION_UP -> {
                parent?.requestDisallowInterceptTouchEvent(false)
                performClick()
                return true
            }
        }
        return super.onTouchEvent(event)
    }

    /** A drag that ends is a read, not a scroll: the sheet's own controls handle those. */
    override fun performClick(): Boolean = super.performClick() ?: true

    /** The hop under the crosshair, or null when the newest hops have scrolled past it. */
    fun cursorHop(): Hop? {
        val ago = cursorAgo ?: return null
        val hops = hops()
        return hops.getOrNull(hops.size - 1 - ago)
    }

    fun clearCursor() {
        cursorAgo = null
        invalidate()
    }

    companion object {
        /** The web keeps 400 hops and draws the last 300. */
        private const val HOPS_SHOWN = 300
    }
}
