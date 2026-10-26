// One channel's readout: level, bar against its noise floor, the web's five metrics
// rows, badges and the 10 s sparkline. Mirrors the tile in web/src/views/channelGrid.ts.
// GPL-3.0-or-later.
package com.onebody.widscan58.view

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.Path
import android.graphics.RectF
import android.util.AttributeSet
import android.view.View
import com.onebody.widscan58.R
import com.onebody.widscan58.core.BadgeKind
import com.onebody.widscan58.core.ChannelTile
import java.util.Locale
import kotlin.math.max
import kotlin.math.min

class ChannelTileView @JvmOverloads constructor(context: Context, attrs: AttributeSet? = null) :
    View(context, attrs) {

    private val theme = Style(context)
    private val fill = Paint(Paint.ANTI_ALIAS_FLAG)
    private val frame = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = theme.dp(1f)
    }
    private val axisLine = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = theme.dp(1f)
    }
    private val sparkLine = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = theme.dp(1.2f)
        strokeJoin = Paint.Join.ROUND
    }
    private val title = theme.text(11f, theme.ink2, bold = true)
    private val freq = theme.text(9f, theme.muted)
    private val big = theme.text(18f, theme.ink)
    private val unit = theme.text(8f, theme.muted)
    private val kv = theme.text(9f, theme.muted)
    private val badgeText = theme.text(8f, theme.good, bold = true)

    private var tile: ChannelTile? = null
    private var sparkNow = 0.0

    /** The plan slot nearest a tap on the panorama, or a tap on this tile. */
    var onPick: (Int) -> Unit = {}

    init {
        setOnClickListener { tile?.let { onPick(it.id) } }
    }

    /** Level, bar and sparkline for one plan slot; [now] is the store clock in seconds. */
    fun bind(tile: ChannelTile?, now: Double) {
        this.tile = tile
        this.sparkNow = now
        invalidate()
    }

    override fun onDraw(canvas: Canvas) {
        val t = tile ?: return
        val pad = theme.dp(6f)
        val w = width.toFloat()
        val h = height.toFloat()
        val right = w - pad
        val color = theme.channelColor(t.id)
        val alpha = if (t.enabled) 255 else 90
        val box = RectF(0.5f, 0.5f, w - 0.5f, h - 0.5f)
        fill.color = if (t.selected) theme.surface2 else theme.surface
        canvas.drawRoundRect(box, theme.dp(6f), theme.dp(6f), fill)
        frame.color = if (t.selected) theme.accent else theme.grid
        canvas.drawRoundRect(box, theme.dp(6f), theme.dp(6f), frame)

        // Name, colour key and centre frequency.
        var y = pad + theme.sp(11f)
        fill.color = color
        fill.alpha = alpha
        canvas.drawRect(pad, y - theme.sp(9f), pad + theme.dp(7f), y, fill)
        title.color = theme.ink2
        title.alpha = alpha
        canvas.drawText(t.name, pad + theme.dp(11f), y, title)
        freq.color = theme.muted
        freq.alpha = alpha
        freq.textAlign = Paint.Align.RIGHT
        canvas.drawText(String.format("%d MHz", t.khz / 1000), right, y, freq)
        freq.textAlign = Paint.Align.LEFT

        // Level: the EMA when the device reports one, otherwise the raw RSSI.
        y += theme.dp(22f)
        val level = fmtDb(t.level)
        big.color = theme.ink
        big.alpha = alpha
        canvas.drawText(level, pad, y, big)
        unit.color = theme.muted
        unit.alpha = alpha
        canvas.drawText("dBFS", pad + big.measureText(level) + theme.dp(3f), y, unit)

        // Bar spans floor-10 .. floor+40 dB, so an empty channel sits near the left.
        y += theme.dp(8f)
        val bar = RectF(pad, y, right, y + theme.dp(6f))
        fill.color = theme.grid
        fill.alpha = alpha
        canvas.drawRoundRect(bar, theme.dp(3f), theme.dp(3f), fill)
        t.bar?.let {
            fill.color = color
            fill.alpha = alpha
            val frac = max(0.02, min(1.0, it)).toFloat()
            canvas.drawRoundRect(
                RectF(bar.left, bar.top, bar.left + frac * bar.width(), bar.bottom),
                theme.dp(3f), theme.dp(3f), fill,
            )
        }

        // One readout per line, as on the web: a 98 dp tile cannot fit the web's second
        // column at a legible size. A row the metrics mode does not measure is left out
        // entirely; a dash is a measurement that does not apply to this channel.
        y += theme.dp(16f)
        kv.color = theme.muted
        kv.alpha = alpha
        for (row in readouts(context, t)) {
            canvas.drawText(row, pad, y, kv)
            y += theme.dp(TEXT_LINE_DP)
        }

        if (t.badges.isNotEmpty()) {
            y += theme.dp(TEXT_LINE_DP)
            var bx = pad
            val top = y - theme.sp(9f)
            val bottom = y + theme.dp(2f)
            for (b in t.badges) {
                val tone = theme.tone.getValue(b.tone)
                badgeText.color = tone
                val label = context.getString(BADGE_LABELS.getValue(b.kind))
                val pillW = badgeText.measureText(label) + theme.dp(6f)
                if (bx + pillW > right) break
                fill.color = theme.alpha(tone, if (t.enabled) 0.18 else 0.08)
                canvas.drawRoundRect(RectF(bx, top, bx + pillW, bottom), theme.dp(6f), theme.dp(6f), fill)
                canvas.drawText(label, bx + theme.dp(3f), y, badgeText)
                bx += pillW + theme.dp(3f)
            }
        }

        drawSpark(canvas, t, pad, right, h - pad - theme.dp(SPARK_H_DP), h - pad, color, alpha)
    }

    /** The web's tile rows, in the web's order. */
    private fun readouts(context: Context, t: ChannelTile): List<String> {
        val rows = ArrayList<String>(5)
        t.snr?.let { rows += context.getString(R.string.tile_snr, String.format(Locale.US, "%.1f dB", it)) }
        t.floor?.let { rows += context.getString(R.string.tile_floor, fmtDb(it)) }
        if (t.smoothed) rows += context.getString(R.string.tile_clear, fmtDb(t.clearFloor))
        t.centroidHz?.let { rows += context.getString(R.string.tile_centre, if (t.occupied) fmtOffsetKHz(it) else DASH) }
        t.obwHz?.let { rows += context.getString(R.string.tile_obw, if (t.occupied) fmtBwMHz(it) else DASH) }
        return rows
    }

    /** Last 10 s of smoothed RSSI against the channel's floor. */
    private fun drawSpark(
        canvas: Canvas,
        t: ChannelTile,
        left: Float,
        right: Float,
        top: Float,
        bottom: Float,
        color: Int,
        alpha: Int,
    ) {
        val series = t.series ?: return
        if (series.size == 0 || right <= left || bottom <= top) return
        val lo = t.sparkLo.toFloat()
        val hi = max(t.sparkHi, t.sparkLo + 1.0).toFloat()
        val span = max(hi - lo, 1f)
        axisLine.color = theme.grid
        axisLine.alpha = alpha
        val floorY = (bottom - (t.sparkFloor.toFloat() - lo) / span * (bottom - top)).coerceIn(top, bottom)
        canvas.drawLine(left, floorY, right, floorY, axisLine)
        sparkLine.color = color
        sparkLine.alpha = alpha
        val path = Path()
        var pen = false
        series.forEach { time, value ->
            if (sparkNow - time > SPARK_WINDOW_S) return@forEach
            val x = right - ((sparkNow - time) / SPARK_WINDOW_S).toFloat() * (right - left)
            val v = max(lo, min(hi, value))
            val y = bottom - (v - lo) / span * (bottom - top)
            if (pen) path.lineTo(x, y) else {
                path.moveTo(x, y)
                pen = true
            }
        }
        if (pen) canvas.drawPath(path, sparkLine)
    }

    override fun performClick(): Boolean {
        super.performClick()
        return true
    }

    companion object {
        private const val DASH = "—"
        private const val SPARK_WINDOW_S = 10.0
        private const val SPARK_H_DP = 18f
        private const val TEXT_LINE_DP = 12f

        /** The engine says what a badge means; which words to show is the app's call. */
        private val BADGE_LABELS = mapOf(
            BadgeKind.STALE to R.string.badge_stale,
            BadgeKind.SIGNAL to R.string.badge_signal,
            BadgeKind.CLIPPING to R.string.badge_clipping,
            BadgeKind.SPUR to R.string.badge_spur,
            BadgeKind.SPUR_MASKED to R.string.badge_spur_masked,
            BadgeKind.FILTER_EDGE to R.string.badge_filter_edge,
        )
    }
}
