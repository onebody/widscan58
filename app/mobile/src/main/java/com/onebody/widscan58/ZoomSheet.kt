// Channel zoom as a full-screen sheet: the chart takes the room, the metric
// readout and the channel stepper sit under it. GPL-3.0-or-later.
package com.onebody.widscan58

import android.app.Activity
import android.app.AlertDialog
import android.app.Dialog
import android.text.SpannableStringBuilder
import android.text.Spanned
import android.text.style.ForegroundColorSpan
import android.view.Gravity
import android.view.View
import android.widget.LinearLayout
import android.widget.TextView
import com.onebody.widscan58.core.ChannelFlag
import com.onebody.widscan58.core.ChannelMetrics
import com.onebody.widscan58.core.Store
import com.onebody.widscan58.view.Style
import com.onebody.widscan58.view.ZoomView
import com.onebody.widscan58.view.fmtDb
import com.onebody.widscan58.view.fmtDbfs
import com.onebody.widscan58.view.fmtOffsetKHz
import java.util.Locale
import kotlin.math.roundToInt

/** [onPick] follows the stepper, so the panorama and the tiles land on the same channel. */
class ZoomSheet(
    private val activity: Activity,
    private val prefs: Prefs,
    private val store: Store,
    private val onPick: (Int) -> Unit,
) {
    private val theme = Style(activity)
    private val chart = ZoomView(activity)
    private val readout = TextView(activity)
    private val heading = TextView(activity)
    private var dialog: Dialog? = null
    private var id = 0

    fun show(start: Int) {
        val channels = store.profile?.channels ?: return
        if (channels.isEmpty()) return
        id = start.coerceIn(channels.indices)
        chart.slot = id
        syncPrefs()
        heading.text = channelTitle(id)
        readout.text = readoutText(store.channels.getOrNull(id)?.latest)

        val body = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad(10f), 0, pad(10f), pad(6f))
            // The chart takes what is left, but never so little that the trace is unreadable.
            chart.minimumHeight = pad(180f)
            addView(chart, LinearLayout.LayoutParams(MATCH, 0, 1f))
            addView(legend(), above(pad(6f)))
            addView(readout, above(pad(4f)))
            addView(stepper(), above(pad(6f)))
        }
        dialog = AlertDialog.Builder(activity)
            .setTitle(R.string.zoom_title)
            .setView(body)
            .setPositiveButton(R.string.close, null)
            .create().apply { show() }
    }

    /** Called with the instrument's own frames, so the live trace keeps moving. */
    fun update() {
        if (dialog?.isShowing != true) return
        syncPrefs()
        chart.invalidate()
        readout.text = readoutText(store.channels.getOrNull(id)?.latest)
        heading.text = channelTitle(id)
    }

    fun dismiss() {
        dialog?.dismiss()
        dialog = null
    }

    private fun syncPrefs() {
        chart.store = store
        chart.dbMin = prefs.dbMin.toDouble()
        chart.dbMax = prefs.dbMax.toDouble()
        chart.showMaxHold = prefs.showMaxHold
        chart.showAverage = prefs.showAverage
    }

    private fun stepper() = LinearLayout(activity).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        addView(arrow("◀", R.string.zoom_prev) { step(-1) })
        addView(heading, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f).apply {
            marginStart = pad(4f); marginEnd = pad(4f)
        })
        addView(arrow("▶", R.string.zoom_next) { step(1) })
    }

    private fun step(delta: Int) {
        val n = store.profile?.channels?.size ?: return
        if (n == 0) return
        id = ((id + delta) % n + n) % n
        chart.slot = id
        onPick(id)
        update()
    }

    private fun channelTitle(id: Int): String {
        val c = store.profile?.channels?.getOrNull(id) ?: return ""
        return activity.getString(R.string.zoom_channel, c.name, "%.3f".format(Locale.US, c.khz / 1000.0))
    }

    /** The marker colours, so the chart needs no labels drawn on it. */
    private fun legend(): View {
        val parts = listOf(
            theme.ink2 to str(R.string.zoom_nominal),
            theme.good to str(R.string.zoom_occupied),
            theme.critical to str(R.string.zoom_center),
        )
        val text = SpannableStringBuilder()
        parts.forEach { (color, label) ->
            if (text.isNotEmpty()) text.append("   ")
            swatch(text, color)
            text.append(label)
        }
        return TextView(activity).apply {
            textSize = 10f
            setTextColor(theme.muted)
            setText(text, TextView.BufferType.SPANNABLE)
        }
    }

    private fun swatch(text: SpannableStringBuilder, color: Int) {
        val at = text.length
        text.append("━ ")
        text.setSpan(ForegroundColorSpan(color), at, text.length, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
    }

    // ---- readout: the same items as the web, and the same blanks and dashes ----

    private fun readoutText(m: ChannelMetrics?): CharSequence {
        if (m == null) return str(R.string.waiting_metrics)
        val signal = m.has(ChannelFlag.SIGNAL)
        val occupied = signal && !m.has(ChannelFlag.NO_CENTROID)
        val items = mutableListOf(
            str(R.string.zoom_rssi) + " " + fmtDbfs(m.rssi) +
                (m.rssiEma?.let { " (${str(R.string.zoom_ema)} ${fmtDb(it)})" }.orEmpty()),
        )
        m.snr?.let { items += str(R.string.zoom_snr) + " " + String.format(Locale.US, "%.1f dB", it) }
        m.floor?.let { items += str(R.string.zoom_floor) + " " + fmtDbfs(it) }
        m.centroidOffHz?.let { items += str(R.string.zoom_center) + " " + if (occupied) fmtOffsetKHz(it) else DASH }
        m.peak?.let { items += str(R.string.zoom_peak) + " " + if (signal) fmtDbfs(it) else DASH }
        m.peakOffHz?.let { items += str(R.string.zoom_occupied) + " " + if (occupied) fmtOffsetKHz(it) else DASH }
        val lo = m.obwLoHz
        val hi = m.obwHiHz
        if (lo != null && hi != null) {
            val width = if (occupied)
                String.format(Locale.US, "%.2f MHz (%s … %s)", (hi - lo) / 1e6, fmtOffsetKHz(lo), fmtOffsetKHz(hi))
            else DASH
            items += str(R.string.zoom_obw) + " " + width
        }
        return items.joinToString("   ")
    }

    private fun arrow(glyph: String, label: Int, click: () -> Unit) = TextView(activity).apply {
        text = glyph
        contentDescription = str(label)
        textSize = 15f
        setTextColor(theme.accent)
        setPadding(pad(12f), pad(8f), pad(12f), pad(8f))
        gravity = Gravity.CENTER
        setOnClickListener { click() }
    }

    private fun str(res: Int) = activity.getString(res)

    private fun above(top: Int) =
        LinearLayout.LayoutParams(MATCH, LinearLayout.LayoutParams.WRAP_CONTENT).apply {
            topMargin = top
        }

    private fun pad(dp: Float) = theme.dp(dp).roundToInt()

    companion object {
        private const val MATCH = LinearLayout.LayoutParams.MATCH_PARENT
        /** Measured, with nothing there: distinct from an item the metrics mode omits. */
        private const val DASH = "—"
    }
}
