// RSSI history as a full-screen sheet: the span and metric chips above the chart,
// the channel legend and the crosshair read-out under it. GPL-3.0-or-later.
package com.onebody.widscan58

import android.app.Activity
import android.app.AlertDialog
import android.app.Dialog
import android.text.SpannableStringBuilder
import android.text.Spanned
import android.text.style.ForegroundColorSpan
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import com.onebody.widscan58.core.Store
import com.onebody.widscan58.core.historySeries
import com.onebody.widscan58.core.historyVisible
import com.onebody.widscan58.core.valueAt
import com.onebody.widscan58.view.HistoryView
import com.onebody.widscan58.view.Style
import java.util.Locale
import kotlin.math.roundToInt

/** The phone's version of the web history card: hover becomes a drag, the tooltip a row. */
class HistorySheet(
    private val activity: Activity,
    private val prefs: Prefs,
    private val store: Store,
) {
    private val theme = Style(activity)
    private val chart = HistoryView(activity)
    private val legend = TextView(activity)
    private val readout = TextView(activity)
    private var dialog: Dialog? = null
    private var legendFor = ""

    /** [selected] is the channel the rest of the screen is on, drawn thicker. */
    fun show(selected: Int) {
        chart.selectedId = selected
        syncChoices()
        buildLegend(force = true)
        readout.text = str(R.string.history_hint)
        dialog = AlertDialog.Builder(activity)
            .setTitle(R.string.history_title)
            .setView(
                LinearLayout(activity).apply {
                    orientation = LinearLayout.VERTICAL
                    setPadding(pad(10f), 0, pad(10f), pad(6f))
                    chart.minimumHeight = pad(180f)
                    addView(spanRow(), below(pad(4f)))
                    addView(chart, LinearLayout.LayoutParams(MATCH, 0, 1f).apply { topMargin = pad(6f) })
                    addView(legend, below(pad(6f)))
                    addView(readout, below(pad(4f)))
                }
            )
            .setPositiveButton(R.string.close, null)
            .create().apply { show() }
        chart.onCursor = ::renderReadout
    }

    /** Called with the instrument's own frames, so the traces keep moving. */
    fun update() {
        if (dialog?.isShowing != true) return
        syncChoices()
        buildLegend(force = false)
        chart.invalidate()
        renderReadout()
    }

    fun dismiss() {
        dialog?.dismiss()
        dialog = null
    }

    private fun syncChoices() {
        chart.store = store
        chart.smoothed = prefs.historySmoothed
        chart.spanSeconds = prefs.historySeconds.toDouble()
        tintSpans()
    }

    // ---- choices ----

    private val spanButtons = mutableMapOf<Int, Button>()
    private var emaButton: Button? = null
    private var rawButton: Button? = null

    private fun spanRow(): View {
        val row = chipRow()
        for (seconds in SPANS) {
            val button = chip(activity.getString(R.string.history_span, seconds)) {
                prefs.historySeconds = seconds
                syncChoices()
                renderReadout()
            }
            spanButtons[seconds] = button
            row.addView(button)
        }
        row.addView(space())
        emaButton = chip(str(R.string.history_ema)) { chooseMetric(true) }
        rawButton = chip(str(R.string.history_raw)) { chooseMetric(false) }
        row.addView(emaButton)
        row.addView(rawButton)
        row.addView(clearChip())
        tintSpans()
        return row
    }

    private fun chooseMetric(smoothed: Boolean) {
        prefs.historySmoothed = smoothed
        syncChoices()
        renderReadout()
    }

    /** The web tooltip is a hover; here the crosshair stays where the finger left it. */
    private fun clearChip() = chip(str(R.string.history_clear)) {
        chart.clearCursor()
        renderReadout()
    }

    private fun tintSpans() {
        spanButtons.forEach { (seconds, button) ->
            button.setTextColor(if (seconds == prefs.historySeconds) theme.accent else theme.muted)
        }
        emaButton?.setTextColor(if (prefs.historySmoothed) theme.accent else theme.muted)
        rawButton?.setTextColor(if (!prefs.historySmoothed) theme.accent else theme.muted)
    }

    private fun chipRow() = LinearLayout(activity).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
    }

    private fun chip(label: String, click: () -> Unit) = Button(activity).apply {
        text = label
        isAllCaps = false
        textSize = 11f
        minWidth = 0
        minimumWidth = 0
        minHeight = 0
        setPadding(pad(6f), 0, pad(6f), 0)
        layoutParams = LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.WRAP_CONTENT,
            pad(32f),
        ).apply { marginEnd = pad(3f) }
        setOnClickListener { click() }
    }

    private fun space() = View(activity).apply {
        layoutParams = LinearLayout.LayoutParams(pad(4f), 1)
    }

    // ---- legend and read-out ----

    /** One swatch per channel of the plan, as the web legend; rebuilt when the plan changes. */
    private fun buildLegend(force: Boolean) {
        val plan = store.profile?.channels.orEmpty()
        if (!force && legendFor == store.planKey) return
        legendFor = store.planKey
        val text = SpannableStringBuilder()
        plan.forEachIndexed { id, channel ->
            if (text.isNotEmpty()) text.append("  ")
            swatch(text, theme.channelColor(id), "━ ")
            text.append(channel.name)
        }
        legend.setText(text, TextView.BufferType.SPANNABLE)
    }

    private fun renderReadout() {
        val ago = chart.cursorAgo
        val now = store.now()
        if (ago == null) {
            readout.text = str(R.string.history_hint)
            return
        }
        val target = now - ago
        val text = SpannableStringBuilder()
        text.append(String.format(Locale.US, "−%.2f s", ago))
        var rows = 0
        store.channels.indices.forEach { id ->
            if (!historyVisible(store, id)) return@forEach
            val value = historySeries(store, id, prefs.historySmoothed)?.valueAt(target, within = CROSSHAIR_S)
                ?: return@forEach
            text.append("\n")
            swatch(text, theme.channelColor(id), "● ")
            val name = store.profile?.channels?.getOrNull(id)?.name.orEmpty()
            text.append(String.format(Locale.US, "%s %.1f dBFS", name, value))
            rows++
        }
        if (rows == 0) text.append("\n").append(str(R.string.waiting_history))
        readout.setText(text, TextView.BufferType.SPANNABLE)
    }

    private fun swatch(text: SpannableStringBuilder, color: Int, glyph: String) {
        val at = text.length
        text.append(glyph)
        text.setSpan(ForegroundColorSpan(color), at, text.length, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
    }

    private fun str(res: Int) = activity.getString(res)

    private fun below(bottom: Int) =
        LinearLayout.LayoutParams(MATCH, LinearLayout.LayoutParams.WRAP_CONTENT).apply {
            bottomMargin = bottom
        }

    private fun pad(dp: Float) = theme.dp(dp).roundToInt()

    companion object {
        private const val MATCH = LinearLayout.LayoutParams.MATCH_PARENT
        /** The web tooltip only lists a channel with a sample this close to the cursor. */
        private const val CROSSHAIR_S = 0.5
        private val SPANS = intArrayOf(10, 30, 60, 120)
    }
}
