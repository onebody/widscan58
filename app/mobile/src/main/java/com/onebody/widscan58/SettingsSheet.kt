// Scanner configuration sheet: one control per firmware command, and every control
// re-reads STATUS? after it writes, so the form shows the device's state rather than
// what we last asked for. Mirrors web/src/views/controls.ts. GPL-3.0-or-later.
package com.onebody.widscan58

import android.app.Activity
import android.app.AlertDialog
import android.app.Dialog
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.inputmethod.EditorInfo
import android.widget.Button
import android.widget.EditText
import android.widget.HorizontalScrollView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.SeekBar
import android.widget.TextView
import com.onebody.widscan58.core.DeviceCommands
import com.onebody.widscan58.core.GainMode
import com.onebody.widscan58.core.LoPlan
import com.onebody.widscan58.core.MetricsMode
import com.onebody.widscan58.core.RecalMode
import com.onebody.widscan58.core.Status
import com.onebody.widscan58.view.Style
import java.util.Locale
import kotlin.math.roundToInt

/**
 * Full-screen form over the board's sweep parameters. [send] applies one command and
 * re-reads STATUS?; [redraw] repaints the instrument after a display-only change.
 * [planNames] is the plan the board dials now and [onPlan] replaces it.
 */
class SettingsSheet(
    private val activity: Activity,
    private val prefs: Prefs,
    private val send: (String) -> Unit,
    private val redraw: () -> Unit,
    private val planNames: () -> List<String>,
    private val onPlan: (List<String>) -> Unit,
) {
    private val theme = Style(activity)
    private val body = LinearLayout(activity).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(pad(10f), pad(4f), pad(10f), pad(8f))
    }
    private val resync = mutableListOf<() -> Unit>()
    private var status: Status? = null
    private var dialog: Dialog? = null

    fun show(current: Status?) {
        status = current
        body.removeAllViews()
        // Shown again after a plan change, and a view may only sit in one dialog.
        (body.parent as? ViewGroup)?.removeView(body)
        resync.clear()
        build()
        dialog = AlertDialog.Builder(activity)
            .setTitle(R.string.settings_title)
            .setView(ScrollView(activity).apply {
                // The platform dialog is a mid grey; the sheet's own labels are tuned for the page.
                setBackgroundColor(theme.surface)
                addView(body)
            })
            .setPositiveButton(R.string.close, null)
            .create().apply { show() }
    }

    /** Fold a fresh STATUS? into the open form, leaving a field the user is typing in alone. */
    fun update(fresh: Status?) {
        if (dialog?.isShowing != true || fresh == null) return
        status = fresh
        resync.forEach { it() }
    }

    fun dismiss() {
        dialog?.dismiss()
        dialog = null
    }

    private val gainMax: Int
        get() = status?.gainMax?.takeIf { it > 0 } ?: DeviceCommands.GAIN_FALLBACK_MAX

    private val detectorMax: Boolean
        get() = (status?.specDetector ?: 1) != 0

    private fun build() {
        val names = planNames()
        planRow(names)
        enableRow(names)

        chips(R.string.set_mode, modeOptions(), { status?.mode }) { v ->
            prefs.loPlan = LoPlan.valueOf(v)
            send(DeviceCommands.mode(prefs.loPlan))
        }

        chips(R.string.set_gain_mode, gainModeOptions(), { status?.gainMode }) { v ->
            prefs.gainMode = GainMode.valueOf(v)
            send(DeviceCommands.gain(prefs.gainMode, prefs.gain, gainMax))
        }
        slider(R.string.set_gain_value, 0, gainMax, { status?.gain ?: prefs.gain }, { it.toString() }) { value ->
            prefs.setGain(value, gainMax)
            send(DeviceCommands.gain(GainMode.MANUAL, value, gainMax))
        }

        slider(R.string.set_filter, DeviceCommands.FILTER_AUTO, DeviceCommands.FILTER_MAX,
            { status?.filter ?: DeviceCommands.FILTER_AUTO },
            { if (it < 0) str(R.string.set_auto) else it.toString() }) { send(DeviceCommands.filter(it)) }

        chips(R.string.set_fft, sizeOptions(DeviceCommands.FFT_SIZES), { status?.nfft?.toString() }) {
            send(DeviceCommands.fft(it.toInt()))
        }
        chips(R.string.set_capture, sizeOptions(DeviceCommands.CAPTURE_SIZES), { status?.captureSamples?.toString() }) {
            send(DeviceCommands.capture(it.toInt()))
        }
        slider(R.string.set_segments, 1, 64, { status?.segments ?: 1 }, { it.toString() }) {
            send(DeviceCommands.segments(it))
        }
        chips(R.string.set_metrics, MetricsMode.entries.map { it.name to it.name },
            { (status?.metrics ?: MetricsMode.FULL).name }) {
            send(DeviceCommands.metrics(MetricsMode.valueOf(it)))
        }

        numbers(R.string.set_windows,
            listOf(str(R.string.set_window_rssi) to { status?.rssiHz?.div(1000.0) },
                str(R.string.set_window_search) to { status?.searchHz?.div(1000.0) })) { v ->
            send(DeviceCommands.windows(v[0].khzToHz(), v[1].khzToHz()))
        }
        numbers(R.string.set_ema,
            listOf(str(R.string.set_ema_rssi) to { status?.ema },
                str(R.string.set_ema_clear) to { status?.clearEma }),
            decimals = true) { v -> send(DeviceCommands.ema(v[0], v[1])) }

        numbers(R.string.set_spectrum,
            listOf(str(R.string.set_spec_every) to { status?.specEvery?.toDouble() },
                str(R.string.set_spec_bins) to { status?.specBins?.toDouble() },
                str(R.string.set_spec_span) to { status?.specSpanHz?.div(1000.0) })) { v ->
            send(DeviceCommands.spectrum(v[0].roundToInt(), v[1].roundToInt(), v[2].khzToHz(), detectorMax))
        }
        chips(R.string.set_spec_detector, detectorOptions(), { if (detectorMax) "MAX" else "MEAN" }) {
            send(specKeeping(it == "MAX"))
        }

        chips(R.string.set_recal_mode, RecalMode.entries.map { it.name to it.name },
            { RecalMode.of(status?.recal)?.name },
            extra = smallButton(str(R.string.recal_now)).apply {
                setOnClickListener { send(DeviceCommands.RECAL) }
            },
        ) { send(DeviceCommands.recalMode(RecalMode.valueOf(it))) }
        slider(R.string.set_settle, 0, 20_000, { status?.settleUs ?: 0 }, { "$it µs" }) {
            send(DeviceCommands.settle(it))
        }

        toggles(R.string.set_tune_skip, skipOptions(), { status?.tskip }) { bit ->
            status?.tskip?.let { send(DeviceCommands.tuneSkip(it, bit)) }
        }
        chips(R.string.set_conjugate, onOffOptions(), { if ((status?.conjugate ?: 1) != 0) "1" else "0" }) {
            send(DeviceCommands.conjugate(it == "1"))
        }
        chips(R.string.set_iq_corr, onOffOptions(), { if ((status?.iqCorr ?: 0) != 0) "1" else "0" }) {
            send(DeviceCommands.iqCorrection(it == "1"))
        }

        numbers(R.string.set_display_db,
            listOf(str(R.string.set_db_min) to { prefs.dbMin.toDouble() },
                str(R.string.set_db_max) to { prefs.dbMax.toDouble() }), signed = true) { v ->
            prefs.dbMin = v[0].roundToInt()
            prefs.dbMax = v[1].roundToInt()
            redraw()
        }
        buttons(R.string.set_device, listOf(
            str(R.string.reboot) to { confirm(R.string.reboot_confirm, DeviceCommands.REBOOT) },
            str(R.string.bootloader) to { confirm(R.string.bootloader_confirm, DeviceCommands.BOOTLOADER) },
        ))
    }

    /** A detector change keeps the rest of the SPEC line as the device has it. */
    private fun specKeeping(max: Boolean) = DeviceCommands.spectrum(
        status?.specEvery ?: 4, status?.specBins ?: 256, status?.specSpanHz ?: 40_000_000L, max,
    )

    private fun confirm(messageRes: Int, line: String) = AlertDialog.Builder(activity)
        .setMessage(messageRes)
        .setPositiveButton(R.string.yes) { _, _ -> send(line) }
        .setNegativeButton(R.string.no, null)
        .show()

    private fun modeOptions() = listOf(
        "GROUP" to str(R.string.set_mode_group), "SINGLE" to str(R.string.set_mode_single),
    )

    private fun gainModeOptions() = listOf(
        "MANUAL" to str(R.string.set_manual), "HARDWARE" to str(R.string.set_agc),
    )

    private fun detectorOptions() = listOf(
        "MAX" to str(R.string.set_detector_max), "MEAN" to str(R.string.set_detector_mean),
    )

    private fun onOffOptions() = listOf("1" to str(R.string.set_on), "0" to str(R.string.set_off))

    private fun sizeOptions(sizes: IntArray) = sizes.map { it.toString() to it.toString() }

    private fun skipOptions() = listOf(
        1 to str(R.string.set_skip_pll), 2 to str(R.string.set_skip_rx), 4 to str(R.string.set_skip_gain),
    )

    // ---- rows ----

    /** A label and a single-choice row; the value the device reports is the tinted one. */
    private fun chips(
        labelRes: Int,
        options: List<Pair<String, String>>,
        current: () -> String?,
        extra: View? = null,
        pick: (String) -> Unit,
    ) {
        val buttons = options.map { (value, label) ->
            smallButton(label).apply { setOnClickListener { pick(value) } } to value
        }
        val strip = scrollable(row(buttons.map { it.first } + listOfNotNull(extra)))
        var shown: String? = null
        val paint = {
            val now = current()
            buttons.forEach { (button, value) ->
                button.setTextColor(if (value == now) theme.accent else theme.muted)
            }
            // The strip is wider than the dialog, so a chosen size can sit off screen.
            if (now != shown) {
                shown = now
                buttons.firstOrNull { it.second == now }?.let { (button, _) ->
                    strip.post { reveal(strip, button) }
                }
            }
        }
        paint()
        add(labelRes, strip)
        resync.add(paint)
    }

    /** Scroll the strip only when the tinted chip is not already where the user can see it. */
    private fun reveal(strip: HorizontalScrollView, target: View) {
        val visible = strip.scrollX..strip.scrollX + strip.width
        if (target.left in visible && target.right in visible) return
        strip.scrollTo(((target.left + target.right) / 2 - strip.width / 2).coerceAtLeast(0), 0)
    }

    /** The plan the board dials, and the editor that replaces it. */
    private fun planRow(names: List<String>) {
        val summary = TextView(activity).apply {
            text = names.joinToString(" ")
            textSize = 12f
            setTextColor(theme.ink)
            isSingleLine = true
        }
        val edit = smallButton(str(R.string.set_plan_edit)).apply { setOnClickListener { editPlan() } }
        add(R.string.set_plan, row(listOf(summary, edit), weightFirst = true))
    }

    /** Rebuild the form after a plan change: the enable chips and their ids are new with it. */
    private fun editPlan() = ChannelPlanSheet(activity) { picked ->
        onPlan(picked)
        dismiss()
        show(status)
    }.show(planNames())

    /** Per-channel switches: the board measures the tinted ones. */
    private fun enableRow(names: List<String>) {
        val chips = names.mapIndexed { id, name ->
            smallButton(name).apply { setOnClickListener { write(currentMask(names.size) xor (1 shl id)) } } to id
        }
        val paint = {
            val mask = currentMask(names.size)
            chips.forEach { (button, id) ->
                button.setTextColor(if (mask and (1 shl id) != 0) theme.accent else theme.muted)
            }
        }
        paint()
        val all = smallButton(str(R.string.set_all)).apply {
            setOnClickListener { write((1 shl names.size) - 1) }
        }
        val none = smallButton(str(R.string.set_none)).apply { setOnClickListener { write(0) } }
        // All/None stay outside the scrolling strip: with a full plan the chips fill it.
        add(R.string.set_channels, row(listOf(scrollable(row(chips.map { it.first })), all, none), weightFirst = true))
        resync.add(paint)
    }

    /** The mask the board reports, trimmed to a plan shorter than the one it was set for. */
    private fun currentMask(size: Int) =
        (status?.enabled ?: prefs.enabledMask) and ((1 shl size.coerceAtLeast(1)) - 1)

    private fun write(mask: Int) {
        prefs.enabledMask = mask
        send(DeviceCommands.enable(mask))
    }

    /** Multi-select bits, dimmed whole when the firmware reports no mask. */
    private fun toggles(
        labelRes: Int,
        options: List<Pair<Int, String>>,
        mask: () -> Int?,
        toggle: (Int) -> Unit,
    ) {
        val buttons = options.map { (bit, label) ->
            smallButton(label).apply { setOnClickListener { toggle(bit) } } to bit
        }
        val paint = {
            val bits = mask()
            buttons.forEach { (button, bit) ->
                button.isEnabled = bits != null
                button.setTextColor(if (bits != null && bits and bit != 0) theme.accent else theme.muted)
            }
        }
        paint()
        add(labelRes, row(buttons.map { it.first }))
        resync.add(paint)
    }

    private fun slider(
        labelRes: Int,
        min: Int,
        max: Int,
        value: () -> Int,
        format: (Int) -> String,
        commit: (Int) -> Unit,
    ) {
        val readout = TextView(activity).apply {
            text = format(value().coerceIn(min, max))
            textSize = 12f
            setTextColor(theme.ink2)
            minWidth = pad(48f)
            gravity = Gravity.END
        }
        val bar = SeekBar(activity).apply {
            this.max = max - min
            progress = value().coerceIn(min, max) - min
            setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
                override fun onProgressChanged(sb: SeekBar, progress: Int, fromUser: Boolean) {
                    readout.text = format(progress + min)
                }

                override fun onStartTrackingTouch(sb: SeekBar) = Unit

                override fun onStopTrackingTouch(sb: SeekBar) = commit(sb.progress + min)
            })
        }
        add(labelRes, row(listOf(bar, readout), weightFirst = true))
        resync.add {
            // A reply that lands mid-drag must not yank the thumb out from under the finger.
            if (!bar.isPressed) {
                val v = value().coerceIn(min, max)
                bar.progress = v - min
                readout.text = format(v)
            }
        }
    }

    /**
     * One row of numeric fields. A commit carries every field in the row because WIN,
     * EMA and SPEC each need all of their arguments at once.
     */
    private fun numbers(
        labelRes: Int,
        fields: List<Pair<String, () -> Double?>>,
        decimals: Boolean = false,
        signed: Boolean = false,
        commitAll: (List<Double>) -> Unit,
    ) {
        val views = fields.map { (hint, read) ->
            EditText(activity).apply {
                setText(read()?.let { numberText(it, decimals) }.orEmpty())
                this.hint = hint
                textSize = 12f
                setTextColor(theme.ink)
                setBackgroundColor(theme.surface2)
                inputType = numberInput(decimals, signed)
                minWidth = pad(58f)
                maxLines = 1
            }
        }

        // Every field of the row commits together: WIN, EMA and SPEC take all their arguments at once.
        fun submit() {
            val values = views.mapNotNull { it.text.toString().toDoubleOrNull() }
            if (values.size == views.size) commitAll(values)
        }

        views.forEach { field ->
            field.setOnEditorActionListener { _, action, _ ->
                if (action == EditorInfo.IME_ACTION_DONE) {
                    field.clearFocus()
                    true
                } else false
            }
            field.setOnFocusChangeListener { _, focused -> if (!focused) submit() }
        }

        add(labelRes, row(views))
        resync.add {
            views.forEachIndexed { i, field ->
                if (!field.isFocused) {
                    field.setText(fields[i].second()?.let { numberText(it, decimals) }.orEmpty())
                }
            }
        }
    }

    private fun buttons(labelRes: Int, actions: List<Pair<String, () -> Unit>>) =
        add(labelRes, row(actions.map { (label, act) -> smallButton(label).apply { setOnClickListener { act() } } }))

    private fun add(labelRes: Int, content: View) {
        val block = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
        block.addView(TextView(activity).apply {
            setText(labelRes)
            textSize = 11f
            setTextColor(theme.muted)
        })
        block.addView(content)
        body.addView(block, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT,
        ).apply { topMargin = pad(9f) })
    }

    private fun scrollable(content: LinearLayout) = HorizontalScrollView(activity).apply {
        isFillViewport = false
        addView(content)
    }

    private fun row(views: List<View>, weightFirst: Boolean = false) = LinearLayout(activity).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        views.forEachIndexed { i, v ->
            addView(v, LinearLayout.LayoutParams(
                if (weightFirst && i == 0) 0 else LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
            ).apply {
                if (weightFirst && i == 0) weight = 1f
                marginStart = if (i == 0) 0 else pad(4f)
            })
        }
    }

    private fun smallButton(label: String) = Button(activity).apply {
        text = label
        isAllCaps = false
        textSize = 11f
        minWidth = 0
        minHeight = 0
        setPadding(pad(7f), 0, pad(7f), 0)
    }

    private fun str(res: Int) = activity.getString(res)

    private fun pad(dp: Float) = theme.dp(dp).roundToInt()
}

private fun numberInput(decimals: Boolean, signed: Boolean): Int {
    var type = InputType.TYPE_CLASS_NUMBER
    if (decimals) type = type or InputType.TYPE_NUMBER_FLAG_DECIMAL
    if (signed) type = type or InputType.TYPE_NUMBER_FLAG_SIGNED
    return type
}

private fun numberText(v: Double, decimals: Boolean) =
    if (decimals) String.format(Locale.US, "%.3f", v) else v.roundToInt().toString()

/** The field holds kilohertz; the firmware wants hertz, and clamps anything wilder. */
private fun Double.khzToHz() = (this * 1000).roundToInt().toLong()
