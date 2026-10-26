// RSSI history: every measured channel across a window of seconds, with a
// crosshair that reads the values back. Mirrors web/src/views/history.ts, where
// the hover tooltip becomes a drag because a phone has no hover. GPL-3.0-or-later.
package com.onebody.widscan58.view

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.Path
import android.util.AttributeSet
import android.view.MotionEvent
import android.view.View
import com.onebody.widscan58.R
import com.onebody.widscan58.core.Store
import com.onebody.widscan58.core.historyRange
import com.onebody.widscan58.core.historySeries
import com.onebody.widscan58.core.historyVisible
import java.util.Locale
import kotlin.math.abs
import kotlin.math.ceil
import kotlin.math.roundToInt

class HistoryView @JvmOverloads constructor(context: Context, attrs: AttributeSet? = null) :
    View(context, attrs) {

    private val theme = Style(context)

    var store: Store? = null
    var selectedId = -1
    var spanSeconds = 30.0
    var smoothed = true

    /** Cursor as "seconds ago", kept after the finger lifts so the numbers can be read. */
    var cursorAgo: Double? = null
        private set

    /** The sheet redraws its readout from the cursor. */
    var onCursor: (() -> Unit)? = null

    private val fill = Paint(Paint.ANTI_ALIAS_FLAG)
    private val line = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE; strokeWidth = theme.dp(1f) }
    private val trace = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeJoin = Paint.Join.ROUND
    }
    private val axisLabel = theme.text(9f, theme.muted)
    private val unit = theme.text(8f, theme.muted)

    private var left = 0f
    private var right = 1f
    private var top = 0f
    private var bottom = 1f
    private var y0 = -70.0
    private var y1 = -30.0

    /** Seconds ago -> pixel. The newest sample is always at the right edge. */
    private fun sx(ago: Double) = right - (ago / spanSeconds).toFloat() * (right - left)

    private fun sy(db: Double) =
        bottom - ((db.coerceIn(y0, y1) - y0) / (y1 - y0)).toFloat() * (bottom - top)

    override fun onDraw(canvas: Canvas) {
        fill.color = theme.surface
        canvas.drawRect(0f, 0f, width.toFloat(), height.toFloat(), fill)
        val store = store
        if (store == null || store.channels.isEmpty()) {
            axisLabel.textAlign = Paint.Align.CENTER
            canvas.drawText(context.getString(R.string.waiting_history), width / 2f, height / 2f, axisLabel)
            axisLabel.textAlign = Paint.Align.LEFT
            return
        }
        val now = store.now()
        left = theme.dp(30f)
        right = width - theme.dp(8f)
        top = theme.dp(16f)
        bottom = height - theme.dp(15f)
        val (lo, hi) = historyRange(store, now, spanSeconds, smoothed)
        y0 = lo
        y1 = if (hi > lo) hi else lo + 10.0

        drawGrid(canvas)
        canvas.save()
        canvas.clipRect(left.roundToInt(), top.roundToInt(), right.roundToInt(), bottom.roundToInt())
        for (id in store.channels.indices) {
            if (!historyVisible(store, id)) continue
            drawChannel(canvas, store, id, now)
        }
        drawCursor(canvas)
        canvas.restore()

        unit.textAlign = Paint.Align.LEFT
        unit.color = theme.muted
        canvas.drawText(context.getString(R.string.history_axis), theme.dp(2f), top - theme.dp(5f), unit)
    }

    private fun drawChannel(canvas: Canvas, store: Store, id: Int, now: Double) {
        val series = historySeries(store, id, smoothed) ?: return
        val selected = id == selectedId
        trace.color = theme.channelColor(id)
        trace.strokeWidth = theme.dp(if (selected) 2.4f else 1.4f)
        trace.alpha = if (selected) 255 else 200
        val path = Path()
        var pen = false
        var lastX = -1f
        series.forEachWithin(now, spanSeconds) { t, v ->
            val x = sx(now - t)
            // Two samples per pixel is all a phone can show; the rest only cost frames.
            if (pen && abs(x - lastX) < 0.5f) return@forEachWithin
            val y = sy(v.toDouble())
            if (pen) path.lineTo(x, y) else path.moveTo(x, y)
            pen = true
            lastX = x
        }
        if (pen) canvas.drawPath(path, trace)
        trace.alpha = 255
    }

    private fun drawGrid(canvas: Canvas) {
        line.color = theme.grid
        axisLabel.color = theme.muted
        axisLabel.textAlign = Paint.Align.RIGHT
        val dbStep = niceStep(y1 - y0, ((bottom - top) / theme.dp(24f)).roundToInt().coerceIn(2, 6))
        var db = ceil(y0 / dbStep) * dbStep
        while (db <= y1 + 1e-9) {
            val y = sy(db)
            canvas.drawLine(left, y, right, y, line)
            canvas.drawText(db.roundToInt().toString(), left - theme.dp(4f), y + theme.dp(3f), axisLabel)
            db += dbStep
        }
        axisLabel.textAlign = Paint.Align.CENTER
        val step = niceStep(spanSeconds, ((right - left) / theme.dp(48f)).roundToInt().coerceIn(2, 5))
        var ago = spanSeconds
        while (ago > 1e-9) {
            val x = sx(ago)
            canvas.drawLine(x, top, x, bottom, line)
            canvas.drawText(String.format(Locale.US, "-%d", ago.roundToInt()), x, bottom + theme.dp(9f), axisLabel)
            ago -= step
        }
        canvas.drawText("0", right, bottom + theme.dp(9f), axisLabel)
        line.color = theme.axis
        canvas.drawLine(left, bottom, right, bottom, line)
    }

    private fun drawCursor(canvas: Canvas) {
        val ago = cursorAgo ?: return
        val x = sx(ago).roundToInt() + 0.5f
        line.color = theme.ink2
        line.strokeWidth = theme.dp(1f)
        canvas.drawLine(x, top, x, bottom, line)
    }

    override fun onTouchEvent(event: MotionEvent): Boolean {
        if (width == 0 || right <= left) return false
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN, MotionEvent.ACTION_MOVE -> {
                parent?.requestDisallowInterceptTouchEvent(true)
                val ago = ((right - event.x) / (right - left) * spanSeconds).coerceIn(0.0, spanSeconds)
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

    fun clearCursor() {
        cursorAgo = null
        invalidate()
    }
}
