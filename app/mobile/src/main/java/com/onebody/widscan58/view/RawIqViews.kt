// The raw snapshot's three plots: the constellation that shows the I/Q imbalance, the
// leading samples in time, and the averaged Hann FFT of the whole capture.
// Mirrors the three canvases in web/src/views/rawiq.ts. GPL-3.0-or-later.
package com.onebody.widscan58.view

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.Path
import android.util.AttributeSet
import android.view.View
import com.onebody.widscan58.R
import com.onebody.widscan58.core.IqStats
import com.onebody.widscan58.core.RawIq
import com.onebody.widscan58.core.RawIqAnalysis
import java.util.Locale
import kotlin.math.ceil
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt

/** A snapshot and the two numbers every one of its three plots is scaled by. */
class Plotted(val iq: RawIq, val stats: IqStats, val range: Double)

/** The box a plot draws in, with the domain-to-pixel maps the web's axes() handed back. */
class IqPlot(
    val left: Float,
    val top: Float,
    val right: Float,
    val bottom: Float,
    val x0: Double,
    val x1: Double,
    val y0: Double,
    val y1: Double,
) {
    fun sx(x: Double) = left + ((x - x0) / (x1 - x0)).toFloat() * (right - left)

    /** Clamped, so a sample outside the frame draws against the edge instead of off-canvas. */
    fun sy(y: Double) =
        bottom - ((min(y1, max(y0, y)) - y0) / (y1 - y0)).toFloat() * (bottom - top)
}

/** Shared plumbing: the snapshot arrives once per capture, and every plot scales itself to it. */
abstract class IqPlotView @JvmOverloads constructor(context: Context, attrs: AttributeSet? = null) :
    View(context, attrs) {

    protected val theme = Style(context)
    protected val fill = Paint(Paint.ANTI_ALIAS_FLAG)
    protected val frame = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = theme.dp(1f)
    }
    protected val trace = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = theme.dp(1.2f)
        strokeJoin = Paint.Join.ROUND
    }
    protected val label = theme.text(8.5f, theme.muted)
    protected val caption = theme.text(8.5f, theme.muted)

    protected var plotted: Plotted? = null

    fun show(r: RawIq?) {
        plotted = r?.let {
            val s = RawIqAnalysis.stats(it)
            Plotted(it, s, RawIqAnalysis.range(s.peak))
        }
        onSnapshot(plotted)
        invalidate()
    }

    /** A subclass that derives more from the capture does it here, once per capture, not per frame. */
    protected open fun onSnapshot(p: Plotted?) = Unit

    final override fun onDraw(canvas: Canvas) {
        fill.color = theme.surface
        canvas.drawRect(0f, 0f, width.toFloat(), height.toFloat(), fill)
        val p = plotted
        if (p == null) {
            label.textAlign = Paint.Align.CENTER
            canvas.drawText(str(emptyText), width / 2f, height / 2f, label)
            label.textAlign = Paint.Align.LEFT
            return
        }
        drawPlotted(canvas, p)
    }

    protected abstract val emptyText: Int
    protected abstract fun drawPlotted(canvas: Canvas, p: Plotted)

    /** The framed plot: its box, grid lines and tick labels, with the maps to place the data. */
    protected fun grid(topDp: Float, x0: Double, x1: Double, y0: Double, y1: Double) =
        IqPlot(theme.dp(30f), theme.dp(topDp), width - theme.dp(6f), height - theme.dp(15f), x0, x1, y0, y1)

    protected fun drawGrid(canvas: Canvas, box: IqPlot, xs: (Double) -> String, ys: (Double) -> String) {
        frame.color = theme.grid
        val yStep = niceStep(box.y1 - box.y0, ((box.bottom - box.top) / theme.dp(22f)).roundToInt().coerceIn(2, 5))
        var v = ceil(box.y0 / yStep) * yStep
        label.textAlign = Paint.Align.RIGHT
        while (v <= box.y1 + 1e-9) {
            val y = box.sy(v)
            canvas.drawLine(box.left, y, box.right, y, frame)
            canvas.drawText(ys(v), box.left - theme.dp(4f), y + theme.dp(3f), label)
            v += yStep
        }
        val xStep = niceStep(box.x1 - box.x0, ((box.right - box.left) / theme.dp(52f)).roundToInt().coerceIn(2, 6))
        v = ceil(box.x0 / xStep) * xStep
        label.textAlign = Paint.Align.CENTER
        while (v <= box.x1 + 1e-9) {
            val text = xs(v)
            // The outermost tick's label is half outside the frame if it stays centred on it.
            val half = label.measureText(text) / 2f
            val x = box.sx(v).coerceIn(theme.dp(2f) + half, width - theme.dp(2f) - half)
            canvas.drawLine(box.sx(v), box.top, box.sx(v), box.bottom, frame)
            canvas.drawText(text, x, box.bottom + theme.dp(9f), label)
            v += xStep
        }
        label.textAlign = Paint.Align.LEFT
        frame.color = theme.axis
        canvas.drawLine(box.left, box.bottom, box.right, box.bottom, frame)
    }

    /** One branch of the capture against time, in the web's 1.2 px stroke. */
    protected fun stroke(canvas: Canvas, box: IqPlot, data: ByteArray, rateHz: Long, color: Int) {
        trace.color = color
        val path = Path()
        val n = min(RawIqAnalysis.TRACE_SAMPLES, data.size)
        for (j in 0 until n) {
            val x = box.sx(j * 1e6 / rateHz)
            val y = box.sy(data[j].toDouble())
            if (j == 0) path.moveTo(x, y) else path.lineTo(x, y)
        }
        canvas.drawPath(path, trace)
    }

    protected fun clip(canvas: Canvas, box: IqPlot, block: () -> Unit) {
        canvas.save()
        canvas.clipRect(box.left.roundToInt(), box.top.roundToInt(), box.right.roundToInt(), box.bottom.roundToInt())
        block()
        canvas.restore()
    }

    protected fun drawCaption(canvas: Canvas, text: String) {
        caption.color = theme.muted
        canvas.drawText(text, theme.dp(2f), theme.dp(10f), caption)
    }

    protected fun str(res: Int) = context.getString(res)
}

/** I against Q: the circle a balanced receiver draws, the squashed one an unbalanced one. */
class IqConstellationView @JvmOverloads constructor(context: Context, attrs: AttributeSet? = null) :
    IqPlotView(context, attrs) {

    private val dot = Paint()

    override val emptyText = R.string.rawiq_empty

    override fun drawPlotted(canvas: Canvas, p: Plotted) {
        val size = min(width, height).toFloat()
        if (size <= 0f) return
        canvas.save()
        canvas.translate(((width - size) / 2f).roundToInt().toFloat(), 0f)
        frame.color = theme.grid
        canvas.drawRect(0.5f, 0.5f, size - 1f, size - 1f, frame)
        canvas.drawLine(size / 2f, 0f, size / 2f, size, frame)
        canvas.drawLine(0f, size / 2f, size, size / 2f, frame)
        // Many samples land on one pixel, so the low alpha is what makes the density readable.
        dot.color = theme.alpha(theme.accent, POINT_ALPHA)
        val r = p.iq
        val half = size / 2f
        val step = half / p.range.toFloat()
        for (j in 0 until r.n) {
            val x = half + r.i[j] * step
            val y = half - r.q[j] * step
            canvas.drawRect(x - 1f, y - 1f, x + 1f, y + 1f, dot)
        }
        caption.color = theme.muted
        canvas.drawText(
            context.getString(R.string.rawiq_constellation, p.range.roundToInt()),
            theme.dp(4f), caption.textSize, caption,
        )
        canvas.restore()
    }

    companion object {
        private const val POINT_ALPHA = 0.25
    }
}

/** The leading samples of both branches in time: what the capture's aperture really held. */
class IqTraceView @JvmOverloads constructor(context: Context, attrs: AttributeSet? = null) :
    IqPlotView(context, attrs) {

    override val emptyText = R.string.rawiq_empty

    override fun drawPlotted(canvas: Canvas, p: Plotted) {
        val r = p.iq
        val us = min(RawIqAnalysis.TRACE_SAMPLES, r.n) * 1e6 / r.rateHz
        val box = grid(14f, 0.0, us, -p.range, p.range)
        drawGrid(
            canvas, box,
            { v -> String.format(Locale.US, "%.1f", v) },
            { v -> v.roundToInt().toString() },
        )
        clip(canvas, box) {
            stroke(canvas, box, r.i, r.rateHz, theme.series[0])
            stroke(canvas, box, r.q, r.rateHz, theme.series[1])
        }
        drawCaption(canvas, str(R.string.rawiq_trace))
    }
}

/** dBFS against absolute megahertz: the capture's own span, from the averaged Hann FFT. */
class IqSpectrumView @JvmOverloads constructor(context: Context, attrs: AttributeSet? = null) :
    IqPlotView(context, attrs) {

    private var dbs = DoubleArray(0)
    private var floorDb = -100.0
    private var ceilingDb = 0.0

    override val emptyText = R.string.rawiq_empty

    /** The transform is this view's expensive part, so it belongs to the capture, not the frame. */
    override fun onSnapshot(p: Plotted?) {
        if (p == null) {
            dbs = DoubleArray(0)
            return
        }
        dbs = RawIqAnalysis.spectrum(p.iq, p.stats)
        val bounds = RawIqAnalysis.bounds(dbs)
        floorDb = bounds.first
        ceilingDb = bounds.second
    }

    override fun drawPlotted(canvas: Canvas, p: Plotted) {
        if (dbs.isEmpty()) return
        val r = p.iq
        val f0 = r.loKhz - r.rateHz / 2000.0
        val f1 = r.loKhz + r.rateHz / 2000.0
        val box = grid(14f, f0, f1, floorDb, ceilingDb)
        drawGrid(
            canvas, box,
            { v -> String.format(Locale.US, "%.1f", v / 1000.0) },
            { v -> v.roundToInt().toString() },
        )
        clip(canvas, box) {
            trace.color = theme.ink
            val path = Path()
            dbs.forEachIndexed { k, v ->
                val x = box.sx(f0 + (k + 0.5) / dbs.size * (f1 - f0))
                if (k == 0) path.moveTo(x, box.sy(v)) else path.lineTo(x, box.sy(v))
            }
            canvas.drawPath(path, trace)
        }
        drawCaption(
            canvas,
            context.getString(R.string.rawiq_spectrum, String.format(Locale.US, "%.1f", r.rateHz / 1e6)),
        )
    }
}
