// The raw I/Q snapshot as a sheet: the LO and size to ask for, then the constellation, the
// leading samples and the capture's own FFT. Mirrors web/src/views/rawiq.ts — the arithmetic
// is core/RawIq.kt's, this file only sends the request and draws what came back. GPL-3.0-or-later.
package com.onebody.widscan58

import android.app.Activity
import android.app.AlertDialog
import android.app.Dialog
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import com.onebody.widscan58.core.RawIq
import com.onebody.widscan58.core.RawIqAnalysis
import com.onebody.widscan58.core.Store
import com.onebody.widscan58.view.IqConstellationView
import com.onebody.widscan58.view.IqSpectrumView
import com.onebody.widscan58.view.IqTraceView
import com.onebody.widscan58.view.Style
import java.util.Locale
import kotlin.math.roundToInt

/**
 * One capture at a time: the board stops scanning, holds the aperture, and ships the samples in
 * chunks. The web keeps this card among the others; here it is a sheet, because three plots
 * stacked under each other is the only way they fit a phone.
 */
class RawIqSheet(
    private val activity: Activity,
    private val store: Store,
    private val send: (String) -> Unit,
) {
    private val theme = Style(activity)
    private val lo = EditText(activity)
    private val info = TextView(activity)
    private val constellation = IqConstellationView(activity)
    private val iqTrace = IqTraceView(activity)
    private val spectrum = IqSpectrumView(activity)
    private val sizeButtons = LinkedHashMap<Int, Button>()
    private var samples = DEFAULT_SAMPLES
    private var selected = -1

    /** The snapshot on screen; a new one is the only thing that makes these views rework. */
    private var shown: RawIq? = null
    private var styledMax = -1
    private var styledSamples = -1
    private var dialog: Dialog? = null

    fun show(selected: Int) {
        // A channel already tuned is the capture worth asking for first; its LO seeds the field.
        this.selected = selected
        channelLo()?.let { writeLo(it) }
        lo.inputType = InputType.TYPE_CLASS_NUMBER
        lo.textSize = 12f
        lo.setSingleLine(true)
        info.textSize = 9.5f
        info.setTextColor(theme.muted)
        dialog = AlertDialog.Builder(activity)
            .setTitle(R.string.rawiq_title)
            .setView(
                ScrollView(activity).apply {
                    addView(
                        LinearLayout(activity).apply {
                            orientation = LinearLayout.VERTICAL
                            setPadding(pad(10f), 0, pad(10f), pad(6f))
                            addView(loRow())
                            addView(sizeRow(), above(4f))
                            addView(info, above(4f))
                            addView(constellation, framed(150f, 6f))
                            addView(iqTrace, framed(108f, 4f))
                            addView(spectrum, framed(126f, 4f))
                        },
                    )
                },
            )
            .setPositiveButton(R.string.close, null)
            .create().apply { show() }
        update()
    }

    /** Called with the instrument's own frames: a capture lands between frames, not on the click. */
    fun update() {
        if (dialog?.isShowing != true) return
        limitSizes()
        val r = store.rawiq
        if (r !== shown) {
            shown = r
            render(r)
        }
    }

    fun dismiss() {
        dialog?.dismiss()
        dialog = null
    }

    // ---- the request ----

    private fun loRow(): View = row().apply {
        addView(muted(str(R.string.rawiq_lo)))
        addView(lo, LinearLayout.LayoutParams(pad(92f), WRAP).apply { marginStart = pad(4f) })
        addView(button(str(R.string.rawiq_from_channel)) { channelLo()?.let { writeLo(it) } }, margin(4f))
        addView(button(str(R.string.rawiq_capture)) { capture() }, margin(4f))
    }

    /** The web's four sizes and its rule: above the board's capture buffer, a size cannot be asked for. */
    private fun sizeRow(): View = row(Gravity.CENTER_VERTICAL).apply {
        addView(muted(str(R.string.rawiq_samples)))
        for (n in SIZES) {
            val button = button(n.toString()) {
                samples = n
                limitSizes()
            }
            sizeButtons[n] = button
            addView(button, margin(3f))
        }
    }

    private fun limitSizes() {
        val max = store.status?.rawiqMax ?: Int.MAX_VALUE
        val largest = SIZES.lastOrNull { it <= max } ?: 0
        if (samples > largest) samples = largest
        if (max == styledMax && samples == styledSamples) return
        styledMax = max
        styledSamples = samples
        for ((n, button) in sizeButtons) {
            button.isEnabled = n <= max
            // A disabled Button keeps the colour it was handed, so the fade is what says "too big".
            button.alpha = if (n <= max) 1f else OFF_LIMIT_ALPHA
            button.setTextColor(
                when {
                    n == samples -> theme.accent
                    n > max -> theme.muted
                    else -> theme.ink
                },
            )
        }
    }

    private fun capture() {
        val khz = lo.text.toString().toLongOrNull() ?: channelLo() ?: return
        send("RAWIQ $khz $samples")
    }

    /** The channel the rest of the screen is on — with none picked, the plan's first one. */
    private fun channelLo(): Long? =
        store.channels.getOrNull(selected)?.loKhz?.takeIf { it > 0 }
            ?: store.profile?.channels?.getOrNull(selected)?.khz?.takeIf { it > 0 }
            ?: store.profile?.channels?.firstOrNull()?.khz?.takeIf { it > 0 }

    private fun writeLo(khz: Long) {
        lo.setText(khz.toString())
        lo.setSelection(lo.text.length)
    }

    // ---- the snapshot ----

    private fun render(r: RawIq?) {
        constellation.show(r)
        iqTrace.show(r)
        spectrum.show(r)
        if (r == null) {
            info.text = ""
            return
        }
        // The views work out the same statistics for their scales; a capture is at most 16 kB.
        val s = RawIqAnalysis.stats(r)
        val lsb = s.scale(r.shift)
        val text = str(
            R.string.rawiq_info,
            fixed(r.loKhz / 1000.0, 3),
            fixed(r.rateHz / 1e6, 1),
            r.n,
            fixed(lsb, 0),
            fixed(s.meanI * lsb, 1),
            fixed(s.meanQ * lsb, 1),
            fixed(s.rms * lsb, 1),
            fixed(s.peak * lsb, 0),
        )
        info.text = if (r.conjugated) "$text · ${str(R.string.rawiq_conjugated)}" else text
    }

    // ---- small helpers ----

    private fun fixed(v: Double, places: Int) = String.format(Locale.US, "%.${places}f", v)

    private fun row(gravity: Int = Gravity.NO_GRAVITY) = LinearLayout(activity).apply {
        orientation = LinearLayout.HORIZONTAL
        this.gravity = gravity
    }

    private fun muted(value: String) = TextView(activity).apply {
        textSize = 11f
        setTextColor(theme.muted)
        text = value
    }

    private fun button(value: String, run: () -> Unit) = Button(activity).apply {
        text = value
        isAllCaps = false
        textSize = 11f
        minWidth = 0
        minimumWidth = 0
        minHeight = 0
        setPadding(pad(7f), 0, pad(7f), 0)
        layoutParams = LinearLayout.LayoutParams(WRAP, pad(32f))
        setOnClickListener { run() }
    }

    private fun framed(heightDp: Float, topDp: Float) =
        LinearLayout.LayoutParams(MATCH, pad(heightDp)).apply { topMargin = pad(topDp) }

    private fun str(res: Int) = activity.getString(res)

    private fun str(res: Int, vararg args: Any) = activity.getString(res, *args)

    private fun above(dp: Float) = LinearLayout.LayoutParams(MATCH, WRAP).apply { topMargin = pad(dp) }

    private fun margin(startDp: Float) =
        LinearLayout.LayoutParams(WRAP, WRAP).apply { marginStart = pad(startDp) }

    private fun pad(dp: Float) = theme.dp(dp).roundToInt()

    companion object {
        private const val MATCH = LinearLayout.LayoutParams.MATCH_PARENT
        private const val WRAP = LinearLayout.LayoutParams.WRAP_CONTENT
        private const val OFF_LIMIT_ALPHA = 0.4f

        /** The web's four sizes, and its default. */
        private val SIZES = intArrayOf(1024, 4096, 8192, 16380)
        private const val DEFAULT_SAMPLES = 8192
    }
}
