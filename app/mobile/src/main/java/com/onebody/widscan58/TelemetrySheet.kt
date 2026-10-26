// Scanner telemetry as a full-screen sheet: the nine numbers of the web card,
// then the per-hop time breakdown under a crosshair. Mirrors the Telemetry class
// in web/src/views/debug.ts, whose hover tooltip is a drag here. GPL-3.0-or-later.
package com.onebody.widscan58

import android.app.Activity
import android.app.AlertDialog
import android.app.Dialog
import android.graphics.Typeface
import android.text.SpannableStringBuilder
import android.text.Spanned
import android.text.style.ForegroundColorSpan
import android.view.View
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import com.onebody.widscan58.core.Hop
import com.onebody.widscan58.core.Store
import com.onebody.widscan58.view.HopTimingView
import com.onebody.widscan58.view.Style
import java.util.Locale
import kotlin.math.roundToInt

/** The link counters the phone measures itself, not the ones the board reports. */
class LinkCounters(
    val live: Boolean,
    val bytes: Long,
    val bytesPerSecond: Double,
    val crcErrors: Long,
    val resyncBytes: Long,
    val recentDrops: Long,
) {
    companion object {
        /** No serial link at all: the board's own numbers still show, the phone's do not. */
        val STANDBY = LinkCounters(false, 0L, 0.0, 0L, 0L, 0L)
    }
}

class TelemetrySheet(private val activity: Activity, private val store: Store) {
    private val theme = Style(activity)
    private val chart = HopTimingView(activity)
    private val readout = TextView(activity).apply {
        textSize = 11f
        setTextColor(theme.ink)
    }
    private var dialog: Dialog? = null
    private val value = mutableMapOf<String, TextView>()
    private val sub = mutableMapOf<String, TextView>()

    fun show() {
        chart.store = store
        chart.onCursor = ::renderReadout
        readout.text = str(R.string.hop_hint)
        val body = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad(10f), 0, pad(10f), pad(6f))
            addView(statGrid())
            addView(heading(str(R.string.hop_timing_title)), above(pad(10f)))
            addView(chart, LinearLayout.LayoutParams(MATCH, pad(120f)).apply { topMargin = pad(4f) })
            addView(legend(), above(pad(4f)))
            addView(readout, above(pad(2f)))
            addView(clearChip())
        }
        dialog = AlertDialog.Builder(activity)
            .setTitle(R.string.telemetry_title)
            .setView(ScrollView(activity).apply { addView(body) })
            .setPositiveButton(R.string.close, null)
            .create().apply { show() }
        renderStats(LinkCounters.STANDBY)
        renderReadout()
    }

    /** Called with the instrument's own frames, so the numbers keep moving. */
    fun update(link: LinkCounters) {
        if (dialog?.isShowing != true) return
        chart.invalidate()
        renderStats(link)
        renderReadout()
    }

    fun dismiss() {
        dialog?.dismiss()
        dialog = null
    }

    // ---- the nine stats, in the web card's order ----

    private fun statGrid(): View {
        val grid = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
        for (row in STATS.chunked(2)) {
            val line = LinearLayout(activity).apply { orientation = LinearLayout.HORIZONTAL }
            for ((key, label) in row) {
                val cell = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
                cell.addView(TextView(activity).apply {
                    textSize = 9f
                    setTextColor(theme.muted)
                    text = str(label)
                })
                value[key] = addLine(cell, 15f, theme.ink, Typeface.DEFAULT_BOLD)
                sub[key] = addLine(cell, 9f, theme.muted)
                line.addView(cell, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
            }
            // A lone cell in the last row would otherwise stretch across the sheet.
            if (row.size == 1) line.addView(View(activity), LinearLayout.LayoutParams(0, 1, 1f))
            grid.addView(line, LinearLayout.LayoutParams(MATCH, LinearLayout.LayoutParams.WRAP_CONTENT).apply {
                topMargin = pad(4f)
            })
        }
        return grid
    }

    private fun addLine(parent: LinearLayout, size: Float, color: Int, face: Typeface = Typeface.DEFAULT) =
        TextView(parent.context).apply {
            textSize = size
            setTextColor(color)
            typeface = face
            parent.addView(this, LinearLayout.LayoutParams(MATCH, LinearLayout.LayoutParams.WRAP_CONTENT))
        }

    private fun set(key: String, at: String, under: String) {
        value.getValue(key).text = at
        sub.getValue(key).text = under
    }

    private fun renderStats(link: LinkCounters) {
        val tel = store.telemetry
        if (tel == null) {
            for ((key, _) in STATS) set(key, DASH, str(R.string.waiting_telemetry))
            return
        }
        val hops = store.status?.plan?.size ?: 0
        val ms = { us: Long -> String.format(Locale.US, "%.2f ms", us / 1000.0) }
        set("rot", "${fmt1(tel.rotationHz)} /s", str(R.string.sub_hops, hops, fmt0(tel.rotationHz * hops)))
        set("hop", ms(tel.hopUsAvg), str(R.string.sub_max, ms(tel.hopUsMax)))
        set("tune", ms(tel.tuneUsAvg), str(R.string.sub_max, ms(tel.tuneUsMax)))
        set("cap", ms(tel.captureUsAvg), str(R.string.sub_samples, store.status?.captureSamples ?: 0))
        set("dsp", ms(tel.dspUsAvg), str(R.string.sub_max, ms(tel.dspUsMax)))
        set("link", "${fmt0(tel.linkFill * 100)} %", str(R.string.sub_dropped, link.recentDrops, tel.drops))
        set(
            "usb",
            if (link.live) "${fmt1(link.bytesPerSecond / 1024)} KiB/s" else DASH,
            if (link.live) str(R.string.sub_total_mib, fmt1(link.bytes / 1048576.0)) else str(R.string.no_link),
        )
        set(
            "err",
            if (link.live) "${link.crcErrors} CRC" else DASH,
            if (link.live) str(R.string.sub_resync, tel.captureErrors, link.resyncBytes) else str(R.string.no_link),
        )
        set("heap", "${fmt0(tel.heapFree / 1024.0)} KiB", str(R.string.sub_internal_ram))
    }

    // ---- per-hop read-out ----

    /** The three stacked bands, each with the colour the chart drew it in. */
    private fun bands() = listOf(
        Band(theme.series[0], str(R.string.stat_tune)) { it.tuneUs },
        Band(theme.series[1], str(R.string.stat_capture)) { it.captureUs },
        Band(theme.series[2], str(R.string.stat_dsp)) { it.dspUs },
    )

    private fun renderReadout() {
        val hop = chart.cursorHop()
        if (hop == null) {
            readout.text = str(R.string.hop_hint)
            return
        }
        // Two lines: the dialog cannot carry the web's five-row tooltip below the fold.
        val text = SpannableStringBuilder()
        text.append(str(R.string.hop_cursor, chart.cursorAgo ?: 0))
        text.append(String.format(Locale.US, " · %s %.2f ms", str(R.string.hop_total), hop.totalUs / 1000.0))
        bands().forEachIndexed { at, band ->
            text.append(if (at == 0) "\n" else "   ")
            swatch(text, band.color)
            text.append(String.format(Locale.US, "%s %.2f", band.label, band.us(hop) / 1000.0))
        }
        readout.setText(text, TextView.BufferType.SPANNABLE)
    }

    private fun legend(): View {
        val text = SpannableStringBuilder()
        for (band in bands()) {
            if (text.isNotEmpty()) text.append("   ")
            swatch(text, band.color)
            text.append(band.label)
        }
        return TextView(activity).apply {
            textSize = 10f
            setTextColor(theme.muted)
            setText(text, TextView.BufferType.SPANNABLE)
        }
    }

    private fun swatch(text: SpannableStringBuilder, color: Int) {
        val at = text.length
        text.append("■ ")
        text.setSpan(ForegroundColorSpan(color), at, text.length, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
    }

    private fun heading(text: String) = TextView(activity).apply {
        textSize = 12f
        setTextColor(theme.ink)
        typeface = Typeface.DEFAULT_BOLD
        this.text = text
    }

    private fun clearChip() = Button(activity).apply {
        text = str(R.string.hop_clear)
        isAllCaps = false
        textSize = 11f
        setTextColor(theme.muted)
        minWidth = 0
        minimumWidth = 0
        minHeight = 0
        setPadding(pad(6f), 0, pad(6f), 0)
        layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT, pad(30f)).apply {
            topMargin = pad(6f)
        }
        setOnClickListener {
            chart.clearCursor()
            renderReadout()
        }
    }

    private fun fmt1(v: Double) = String.format(Locale.US, "%.1f", v)
    private fun fmt0(v: Double) = String.format(Locale.US, "%.0f", v)
    private fun str(res: Int) = activity.getString(res)
    private fun str(res: Int, vararg args: Any) = activity.getString(res, *args)

    private fun above(top: Int) =
        LinearLayout.LayoutParams(MATCH, LinearLayout.LayoutParams.WRAP_CONTENT).apply {
            topMargin = top
        }

    private fun pad(dp: Float) = theme.dp(dp).roundToInt()

    companion object {
        private const val MATCH = LinearLayout.LayoutParams.MATCH_PARENT
        private const val DASH = "—"

        private val STATS = listOf(
            "rot" to R.string.stat_rotation,
            "hop" to R.string.stat_hop,
            "tune" to R.string.stat_tune,
            "cap" to R.string.stat_capture,
            "dsp" to R.string.stat_dsp,
            "link" to R.string.stat_link,
            "usb" to R.string.stat_usb,
            "err" to R.string.stat_err,
            "heap" to R.string.stat_heap,
        )
    }
}

/** One stacked band of the per-hop chart: its colour, its name, and the hop field it draws. */
private class Band(val color: Int, val label: String, val us: (Hop) -> Long)
