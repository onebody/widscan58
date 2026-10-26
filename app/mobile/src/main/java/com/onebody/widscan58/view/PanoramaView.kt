// Band panorama over the stitched LO segments, with the rotation waterfall under
// it: both share one frequency axis. Mirrors web/src/views/panorama.ts. GPL-3.0-or-later.
package com.onebody.widscan58.view

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.DashPathEffect
import android.graphics.Paint
import android.graphics.Path
import android.graphics.Rect
import android.util.AttributeSet
import android.view.MotionEvent
import android.view.View
import android.view.ViewConfiguration
import com.onebody.widscan58.R
import com.onebody.widscan58.core.Composite
import com.onebody.widscan58.core.Store
import com.onebody.widscan58.core.Waterfall
import com.onebody.widscan58.core.bandRange
import com.onebody.widscan58.core.composite
import kotlin.math.abs
import kotlin.math.ceil
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt

class PanoramaView @JvmOverloads constructor(context: Context, attrs: AttributeSet? = null) :
    View(context, attrs) {

    private val theme = Style(context)

    var store: Store? = null
    var selected = -1
    var showMaxHold = true
    var showAverage = true
    var dbMin = -85.0
    var dbMax = -20.0

    /** The plan slot nearest to a tap, so the tiles follow the chart. */
    var onPick: (Int) -> Unit = {}

    /** A long press fits the dB range to what is on screen. */
    var onFit: (Double, Double) -> Unit = { _, _ -> }

    private val waterfall = Waterfall()
    private var lastComp: Composite? = null
    private val fill = Paint(Paint.ANTI_ALIAS_FLAG)
    private val line = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE; strokeWidth = theme.dp(1f) }
    private val trace = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = theme.dp(1.2f)
        strokeJoin = Paint.Join.ROUND
    }
    private val blit = Paint().apply { isFilterBitmap = false }
    private val axisLabel = theme.text(9f, theme.muted)
    private val name = theme.text(9f, theme.ink2, bold = true)
    private val unit = theme.text(8f, theme.muted)

    private var wfImage: Bitmap? = null
    private var wfPixels = IntArray(0)

    // Geometry of the last draw, for turning a tap into a frequency.
    private var plotLeft = 0f
    private var plotRight = 0f
    private var f0 = 0.0
    private var f1 = 1.0
    private var y0 = -85.0
    private var y1 = -20.0
    private var spectrumTop = 0f
    private var waterfallBottom = 0f
    private var rowPx = 1f

    override fun onDraw(canvas: Canvas) {
        val store = store
        val profile = store?.profile
        fill.color = theme.surface
        canvas.drawRect(0f, 0f, width.toFloat(), height.toFloat(), fill)
        if (store == null || profile == null || store.segments.isEmpty()) {
            lastComp = null
            val hint = context.getString(R.string.waiting_spectrum)
            axisLabel.textAlign = Paint.Align.CENTER
            canvas.drawText(hint, width / 2f, height / 2f, axisLabel)
            axisLabel.textAlign = Paint.Align.LEFT
            return
        }

        // The gutter is sized for the widest axis label in either language: -23.5秒 is 28 sp.
        val left = theme.dp(34f)
        val right = width - theme.dp(8f)
        val wfHeight = (height * 0.34f).coerceIn(theme.dp(56f), theme.dp(140f))
        spectrumTop = theme.dp(16f)
        val spectrumBottom = height - wfHeight - theme.dp(15f)
        val wfTop = height - wfHeight
        waterfallBottom = height - theme.dp(13f)
        plotLeft = left
        plotRight = right
        val range = bandRange(store)
        f0 = range.lo
        f1 = range.hi

        val comp = composite(store, f0, f1, (right - left).roundToInt().coerceIn(64, MAX_COLUMNS))
        lastComp = comp
        // A stored range that collapsed (bad prefs, a degenerate fit) would divide by zero.
        y0 = dbMin
        y1 = if (dbMax > dbMin + 1.0) dbMax else dbMin + 65.0
        val sx = sxFn()
        val sy = { v: Double ->
            spectrumBottom - ((max(y0, min(y1, v)) - y0) / (y1 - y0)).toFloat() * (spectrumBottom - spectrumTop)
        }

        drawChannelWindows(canvas, store, sx, left, right, spectrumTop, spectrumBottom)
        drawGrid(canvas, sx, sy, left, right, spectrumTop, spectrumBottom)
        drawMarks(canvas, store, sx, left, right, spectrumTop, spectrumBottom)

        canvas.save()
        canvas.clipRect(left.roundToInt(), spectrumTop.roundToInt(), right.roundToInt(), spectrumBottom.roundToInt())
        trace.pathEffect = DashPathEffect(floatArrayOf(theme.dp(4f), theme.dp(3f)), 0f)
        strokeTrace(canvas, comp.floor, sx, sy, theme.muted, theme.dp(1f))
        trace.pathEffect = null
        if (showAverage) strokeTrace(canvas, comp.avg, sx, sy, theme.accent, theme.dp(1.5f))
        if (showMaxHold) strokeTrace(canvas, comp.max, sx, sy, theme.warning, theme.dp(1f))
        strokeTrace(canvas, comp.live, sx, sy, theme.ink, theme.dp(1.2f))
        canvas.restore()

        unit.textAlign = Paint.Align.LEFT
        unit.color = theme.muted
        canvas.drawText(context.getString(R.string.axis_dbfs_bin), theme.dp(2f), spectrumTop - theme.dp(5f), unit)

        drawWaterfall(canvas, store, comp, left, right, wfTop, waterfallBottom)
    }

    private fun sxFn(): (Double) -> Float = {
        plotLeft + ((it - f0) / (f1 - f0)).toFloat() * (plotRight - plotLeft)
    }

    /** RSSI windows (filled) and search windows (dashed) for every plan channel. */
    private fun drawChannelWindows(
        canvas: Canvas,
        store: Store,
        sx: (Double) -> Float,
        left: Float,
        right: Float,
        top: Float,
        bottom: Float,
    ) {
        val profile = store.profile ?: return
        val status = store.status
        val rssiKhz = (status?.rssiHz ?: profile.rssiHz) / 1000.0
        val searchKhz = (status?.searchHz ?: profile.searchHz) / 1000.0
        canvas.save()
        canvas.clipRect(left.roundToInt(), top.roundToInt(), right.roundToInt(), bottom.roundToInt())
        profile.channels.forEachIndexed { id, c ->
            val enabled = status?.let { it.enabled and (1 shl id) != 0 } ?: true
            val color = theme.channelColor(id)
            fill.color = theme.alpha(color, if (!enabled) 0.05 else if (selected == id) 0.3 else 0.16)
            canvas.drawRect(sx(c.khz - rssiKhz), top, sx(c.khz + rssiKhz), bottom, fill)
            line.color = theme.alpha(color, if (enabled) 0.55 else 0.15)
            line.pathEffect = DashPathEffect(floatArrayOf(theme.dp(3f), theme.dp(3f)), 0f)
            canvas.drawRect(
                sx(c.khz - searchKhz) + 0.5f, top + 0.5f, sx(c.khz + searchKhz), bottom - 0.5f, line,
            )
            line.pathEffect = null
        }
        canvas.restore()
    }

    private fun drawGrid(
        canvas: Canvas,
        sx: (Double) -> Float,
        sy: (Double) -> Float,
        left: Float,
        right: Float,
        top: Float,
        bottom: Float,
    ) {
        axisLabel.color = theme.muted
        line.color = theme.grid
        // Label density follows the pixels available, so a short view never stacks its gridlines.
        val dbStep = niceStep(y1 - y0, ((bottom - top) / theme.dp(24f)).roundToInt().coerceIn(2, 6))
        var v = ceil(y0 / dbStep) * dbStep
        axisLabel.textAlign = Paint.Align.RIGHT
        while (v <= y1 + 1e-9) {
            val y = sy(v)
            canvas.drawLine(left, y, right, y, line)
            canvas.drawText(v.roundToInt().toString(), left - theme.dp(4f), y + theme.dp(3f), axisLabel)
            v += dbStep
        }
        val khzStep =
            niceStep((f1 - f0) / 1000.0, ((right - left) / theme.dp(60f)).roundToInt().coerceIn(2, 6)) * 1000.0
        var f = ceil(f0 / khzStep) * khzStep
        axisLabel.textAlign = Paint.Align.CENTER
        while (f <= f1 + 1e-9) {
            val x = sx(f)
            canvas.drawLine(x, top, x, bottom, line)
            canvas.drawText(String.format("%.1f", f / 1000.0), x, bottom + theme.dp(9f), axisLabel)
            f += khzStep
        }
        line.color = theme.axis
        canvas.drawLine(left, bottom, right, bottom, line)
    }

    /** Channel names along the top, the plan's LO markers at the bottom, board spurs as ticks. */
    private fun drawMarks(
        canvas: Canvas,
        store: Store,
        sx: (Double) -> Float,
        left: Float,
        right: Float,
        top: Float,
        bottom: Float,
    ) {
        val profile = store.profile ?: return
        profile.channels.forEachIndexed { id, c ->
            if (c.khz < f0 || c.khz > f1) return@forEachIndexed
            name.color = if (selected == id) theme.channelColor(id) else theme.ink2
            canvas.drawText(c.name, sx(c.khz.toDouble()) - name.measureText(c.name) / 2, top + theme.dp(8f), name)
        }
        store.status?.let { st ->
            line.color = theme.muted
            val w = theme.dp(2f)
            val h = theme.dp(5f)
            for (hop in st.plan) {
                val x = sx(hop.loKhz.toDouble())
                canvas.drawLine(x, bottom, x - w, bottom - h, line)
                canvas.drawLine(x, bottom, x + w, bottom - h, line)
            }
        }
        line.color = theme.serious
        for (spur in store.spursKhz) {
            if (spur < f0 || spur > f1) continue
            val x = sx(spur).roundToInt() + 0.5f
            canvas.drawLine(x, bottom - theme.dp(8f), x, bottom, line)
        }
    }

    private fun strokeTrace(
        canvas: Canvas,
        data: DoubleArray,
        sx: (Double) -> Float,
        sy: (Double) -> Float,
        color: Int,
        widthPx: Float,
    ) {
        trace.color = color
        trace.strokeWidth = widthPx
        val step = (plotRight - plotLeft) / data.size
        val path = Path()
        var pen = false
        for (x in data.indices) {
            val v = data[x]
            if (v.isNaN()) {
                pen = false
                continue
            }
            val px = plotLeft + (x + 0.5f) * step
            val py = sy(v)
            if (pen) path.lineTo(px, py) else path.moveTo(px, py)
            pen = true
        }
        canvas.drawPath(path, trace)
    }

    private fun drawWaterfall(
        canvas: Canvas,
        store: Store,
        comp: Composite,
        left: Float,
        right: Float,
        top: Float,
        bottom: Float,
    ) {
        // One device pixel per rotation, as on the web: the band then holds minutes of
        // history rather than seconds, and the rows older than that are the ramp's floor.
        rowPx = 1f
        val capacity = ((bottom - top) / rowPx).roundToInt().coerceIn(1, MAX_WATERFALL_ROWS)
        val added = waterfall.capture(store, comp, y0, capacity)
        val width = waterfall.rowWidth
        var image = wfImage
        if (image == null || image.height != capacity) {
            image?.recycle()
            image = Bitmap.createBitmap(width, capacity, Bitmap.Config.ARGB_8888)
            wfImage = image
            wfPixels = IntArray(width * capacity)
            paintRows(image, capacity, width)
        } else if (added) {
            paintRows(image, capacity, width)
        }
        canvas.drawBitmap(
            image, null,
            Rect(left.roundToInt(), top.roundToInt(), right.roundToInt(), bottom.roundToInt()), blit,
        )

        axisLabel.color = theme.muted
        axisLabel.textAlign = Paint.Align.RIGHT
        val now = store.now()
        val fm = axisLabel.fontMetrics
        val middle = (fm.ascent + fm.descent) / 2f
        val lineHeight = (fm.descent - fm.ascent).roundToInt()
        // The web's three age ticks, but keyed to the rows that exist: a band still
        // filling from the top would otherwise have no second label for minutes.
        val depth = waterfall.rowTimes.size
        var lastBaseline = Float.NEGATIVE_INFINITY
        for (y in listOf(0, depth / 2, depth - 1)) {
            val at = waterfall.rowTimes.getOrNull(y) ?: continue
            // Rows are a pixel tall, so each label has to sit on its row's own centre line.
            val baseline = (top + (y + 0.5f) * rowPx - middle).coerceIn(top - fm.ascent, bottom - fm.descent)
            // Two ticks closer than a line of text would print over each other; the
            // middle and oldest join as the band fills.
            if (baseline - lastBaseline < lineHeight) continue
            lastBaseline = baseline
            canvas.drawText(
                context.getString(R.string.waterfall_age, now - at),
                left - theme.dp(4f),
                baseline,
                axisLabel,
            )
        }
        val profile = store.profile ?: return
        axisLabel.textAlign = Paint.Align.CENTER
        profile.channels.forEachIndexed { id, c ->
            if (c.khz < f0 || c.khz > f1) return@forEachIndexed
            val x = sxFn()(c.khz.toDouble())
            fill.color = theme.channelColor(id)
            canvas.drawRect(x - theme.dp(1f), bottom, x + theme.dp(1f), bottom + theme.dp(4f), fill)
            name.color = if (selected == id) theme.channelColor(id) else theme.ink2
            canvas.drawText(c.name, x - name.measureText(c.name) / 2, bottom + theme.dp(11f), name)
        }
    }

    /** Newest row at the top; rows older than the history stay the ramp's floor colour. */
    private fun paintRows(bitmap: Bitmap, capacity: Int, width: Int) {
        val ramp = theme.ramp
        for (y in 0 until capacity) {
            val row = waterfall.rows.getOrNull(y)
            val base = y * width
            if (row == null) {
                wfPixels.fill(ramp[0], base, base + width)
                continue
            }
            for (x in 0 until width) wfPixels[base + x] = ramp[row[x].toInt() and 0xFF]
        }
        bitmap.setPixels(wfPixels, 0, width, 0, 0, width, capacity)
    }

    // ---- touch: tap picks the nearest channel, long press fits the dB scale ----
    private var downX = 0f
    private var downY = 0f
    private var longFired = false
    private val longPress = Runnable {
        if (isPlotHit(downX, downY)) {
            longFired = true
            autoScale()
        }
    }

    override fun onTouchEvent(event: MotionEvent): Boolean {
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                downX = event.x
                downY = event.y
                longFired = false
                postDelayed(longPress, LONG_PRESS_MS)
                return true
            }

            MotionEvent.ACTION_MOVE -> {
                val slop = ViewConfiguration.get(context).scaledTouchSlop
                if (abs(event.x - downX) > slop || abs(event.y - downY) > slop) removeCallbacks(longPress)
                return true
            }

            MotionEvent.ACTION_UP -> {
                removeCallbacks(longPress)
                if (!longFired) pick(event.x, event.y)
                performClick()
                return true
            }

            MotionEvent.ACTION_CANCEL -> removeCallbacks(longPress)
        }
        return super.onTouchEvent(event)
    }

    private fun isPlotHit(x: Float, y: Float) =
        y in spectrumTop..waterfallBottom && x in plotLeft..plotRight && plotRight > plotLeft

    private fun pick(x: Float, y: Float) {
        if (!isPlotHit(x, y)) return
        val profile = store?.profile ?: return
        val khz = f0 + ((x - plotLeft) / (plotRight - plotLeft)).toDouble() * (f1 - f0)
        var best = -1
        profile.channels.forEachIndexed { id, c ->
            if (best < 0 || abs(c.khz - khz) < abs(profile.channels[best].khz - khz)) best = id
        }
        if (best < 0) return
        selected = best
        onPick(best)
        invalidate()
    }

    /** The web UI's Auto scale button, for the menu: the long press is the same action. */
    fun autoScale() {
        fitRange()?.let { (lo, hi) -> onFit(lo, hi) }
    }

    /** The web UI's Auto scale: five dB of padding, rounded to the next 5 dB. */
    private fun fitRange(): Pair<Double, Double>? {
        val comp = lastComp ?: return null
        var lo = Double.POSITIVE_INFINITY
        var hi = Double.NEGATIVE_INFINITY
        for (j in comp.live.indices) {
            val v = comp.live[j]
            if (v.isNaN()) continue
            lo = min(lo, v)
            hi = max(hi, if (comp.max[j].isNaN()) v else comp.max[j])
        }
        if (!lo.isFinite()) return null
        return kotlin.math.floor((lo - 5) / 5) * 5 to kotlin.math.ceil((hi + 5) / 5) * 5
    }

    override fun performClick(): Boolean {
        super.performClick()
        return true
    }

    companion object {
        /** More columns than pixels buys nothing; the waterfall resamples to 600 anyway. */
        private const val MAX_COLUMNS = 700
        private const val MAX_WATERFALL_ROWS = 400
        private const val LONG_PRESS_MS = 450L
    }
}
