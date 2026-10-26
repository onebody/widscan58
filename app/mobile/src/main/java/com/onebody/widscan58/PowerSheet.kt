// Power meter mode's page: the attenuator the VTX hangs behind, and one card per planned
// channel with its power at the board and at the VTX. Mirrors web/src/views/power.ts;
// the arithmetic is the engine's (core/Power.kt), this file only draws it. GPL-3.0-or-later.
package com.onebody.widscan58

import android.app.Activity
import android.app.AlertDialog
import android.app.Dialog
import android.text.InputType
import android.text.Spannable
import android.text.SpannableStringBuilder
import android.text.style.ForegroundColorSpan
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.inputmethod.EditorInfo
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import com.onebody.widscan58.core.DeviceCommands
import com.onebody.widscan58.core.Power
import com.onebody.widscan58.core.Reading
import com.onebody.widscan58.core.Store
import com.onebody.widscan58.view.Style
import java.util.Locale
import kotlin.math.roundToInt

/**
 * The meter's own page. It is a mode rather than a dialog over the instrument because the board
 * is running a different scan while it is open: one LO, manual gain 0, a ±15 MHz window — the
 * scan the calibration was measured with, and the only one whose dBFS means dBm.
 */
class PowerSheet(
    private val activity: Activity,
    private val prefs: Prefs,
    private val store: Store,
    private val send: (String) -> Unit,
    private val onPlan: (List<String>) -> Unit,
) {
    private val theme = Style(activity)
    private val note = label(10f, theme.warning)
    private val cards = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
    private var cardRow: List<Card> = emptyList()
    private var planKey = ""
    private var attenField: EditText? = null
    private var smoothRow: LinearLayout? = null
    private var dialog: Dialog? = null

    /** The mode ends with the page: the board's scan goes back with it. */
    var onDismissed: (() -> Unit)? = null

    /** [render] assigns the checkboxes as it paints; a listener awake during that would write back. */
    private var painting = false

    /** One planned channel, built once per plan and painted from [update]. */
    private inner class Card(val id: Int) {
        val root = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(theme.surface2)
            setPadding(pad(8f), pad(6f), pad(8f), pad(6f))
        }
        val name = label(12f, theme.ink, bold = true)
        val freq = label(11f, theme.muted)
        val measure = CheckBox(activity).apply { contentDescription = str(R.string.power_measure) }
        val main = label(22f, theme.ink, bold = true)
        val unit = label(11f, theme.muted)
        val sub = label(11f, theme.ink2)
        val kv = label(10f, theme.muted)
        val badges = label(10f, theme.warning, bold = true)

        init {
            // View.apply has its own `id` (View.NO_ID), so the channel's has to be named in full.
            val key = View(activity).apply { setBackgroundColor(theme.channelColor(this@Card.id)) }
            root.addView(
                row(Gravity.CENTER_VERTICAL).apply {
                    addView(key, LinearLayout.LayoutParams(pad(10f), pad(10f)))
                    addView(name, start(6f))
                    addView(
                        freq,
                        LinearLayout.LayoutParams(0, WRAP, 1f).apply { marginStart = pad(6f) },
                    )
                    addView(measure)
                },
            )
            root.addView(
                row(Gravity.BOTTOM).apply {
                    addView(main)
                    addView(unit, start(3f))
                    addView(
                        sub,
                        LinearLayout.LayoutParams(0, WRAP, 1f).apply { marginStart = pad(6f) },
                    )
                },
                above(2f),
            )
            root.addView(kv, above(2f))
            root.addView(badges, above(2f))
        }
    }

    fun show() {
        (cards.parent as? ViewGroup)?.removeView(cards)
        cardRow = emptyList()
        planKey = ""
        val body = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad(10f), 0, pad(10f), pad(6f))
            addView(label(11f, theme.critical, bold = true).apply { text = str(R.string.power_guide_warn) })
            addView(settingsRow(), above(6f))
            addView(smoothLine(), above(4f))
            addView(planRow(), above(4f))
            addView(note, above(6f))
            addView(cards, above(2f))
            addView(label(9f, theme.muted).apply { text = str(R.string.power_disclaimer) }, above(8f))
        }
        val alert = AlertDialog.Builder(activity)
            .setTitle(R.string.power_title)
            .setView(
                ScrollView(activity).apply {
                    // The platform dialog is a mid grey; the sheet's own labels are tuned for the page.
                    setBackgroundColor(theme.surface)
                    addView(body)
                },
            )
            .setNeutralButton(R.string.power_guide, null)
            // The one way out of the mode, and it restores the scan on the way.
            .setPositiveButton(R.string.power_exit, null)
            .create()
        dialog = alert
        alert.setOnDismissListener { onDismissed?.invoke() }
        alert.show()
        // The guide is the one thing worth more room than this sheet has left for it.
        alert.getButton(AlertDialog.BUTTON_NEUTRAL).setOnClickListener { showGuide() }
        render()
    }

    /** Closing from the code is not leaving the mode: only the page's own ways out say so. */
    fun dismiss() {
        val d = dialog ?: return
        dialog = null
        d.setOnDismissListener(null)
        d.dismiss()
    }

    /** From the instrument's own refresh: the readings follow the frames as they land. */
    fun update() {
        if (dialog?.isShowing == true) render()
    }

    // ---- settings ----

    private fun settingsRow(): View {
        val field = EditText(activity).apply {
            inputType = InputType.TYPE_CLASS_NUMBER
            imeOptions = EditorInfo.IME_ACTION_DONE
            setText(prefs.powerAttenDb.toString())
            setTextColor(theme.ink)
            textSize = 13f
            maxLines = 1
            minWidth = pad(46f)
        }
        attenField = field
        field.setOnFocusChangeListener { _, focused -> if (!focused) commitAtten() }
        field.setOnEditorActionListener { _, action, _ ->
            if (action != EditorInfo.IME_ACTION_DONE) false else {
                commitAtten()
                true
            }
        }
        return row(Gravity.CENTER_VERTICAL).apply {
            addView(label(11f, theme.ink2).apply { text = str(R.string.power_attenuator) })
            addView(field, LinearLayout.LayoutParams(pad(64f), WRAP).apply { marginStart = pad(6f) })
            addView(label(11f, theme.muted).apply { text = str(R.string.power_db) }, start(4f))
        }
    }

    /** A blank or nonsense entry is the stored one back, not a change to it. */
    private fun commitAtten() {
        val field = attenField ?: return
        val v = field.text.toString().toIntOrNull()
        if (v == null) field.setText(prefs.powerAttenDb.toString()) else prefs.powerAttenDb = v
        render()
    }

    private fun smoothLine() = row(Gravity.CENTER_VERTICAL).apply {
        addView(label(11f, theme.ink2).apply { text = str(R.string.power_smooth) })
        val row = row(Gravity.CENTER_VERTICAL)
        smoothRow = row
        Power.SMOOTHING_SECONDS.forEach { seconds ->
            row.addView(chip(smoothLabel(seconds)).apply {
                setOnClickListener {
                    prefs.powerSmoothS = seconds
                    syncSmooth()
                    render()
                }
            })
        }
        addView(row, start(6f))
        syncSmooth()
    }

    private fun smoothLabel(seconds: Double) =
        if (seconds == 0.0) str(R.string.power_smooth_off) else "${trim(seconds)} s"

    /** "0.1", "3" — the chip is one line only, and five of them share the row. */
    private fun trim(v: Double) = v.toString().removeSuffix(".0")

    private fun syncSmooth() {
        val row = smoothRow ?: return
        Power.SMOOTHING_SECONDS.forEachIndexed { j, seconds ->
            val on = seconds == prefs.powerSmoothS
            (row.getChildAt(j) as? Button)?.setTextColor(if (on) theme.accent else theme.muted)
        }
    }

    private fun planRow() = row(Gravity.CENTER_VERTICAL).apply {
        addView(button(str(R.string.set_plan_edit)) { editPlan() })
        addView(button(str(R.string.power_all)) { setMask(-1) }, start(4f))
        addView(button(str(R.string.power_none)) { setMask(0) }, start(4f))
    }

    private fun editPlan() {
        ChannelPlanSheet(activity) { names -> onPlan(names) }.show(prefs.channels)
    }

    /** The mask is the board's, so every write is followed by the read-back that proves it. */
    private fun setMask(mask: Int) {
        val channels = store.profile?.channels?.size ?: return
        send(DeviceCommands.enable(if (mask == -1) (1 shl channels) - 1 else mask))
        send("STATUS?")
    }

    private fun toggleTo(id: Int, on: Boolean) {
        val st = store.status ?: return
        setMask(if (on) st.enabled or (1 shl id) else st.enabled and (1 shl id).inv())
    }

    // ---- the attenuator guide ----

    /** A dialog of its own: this one is read before the VTX is plugged in, not while measuring. */
    private fun showGuide() {
        val body = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad(14f), pad(8f), pad(14f), pad(8f))
            addView(label(11f, theme.ink).apply { text = str(R.string.power_guide_intro) })
            addView(guideTable(), above(10f))
            addView(label(10f, theme.muted).apply { text = str(R.string.power_guide_range) }, above(10f))
        }
        AlertDialog.Builder(activity)
            .setTitle(R.string.power_guide)
            .setView(ScrollView(activity).apply { addView(body) })
            .setPositiveButton(R.string.close, null)
            .show()
    }

    private fun guideTable(): View {
        val table = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
        table.addView(
            row().apply {
                listOf(
                    R.string.power_col_vtx, R.string.power_col_atten,
                    R.string.power_col_board, R.string.power_col_rating,
                ).forEachIndexed { j, res -> addView(guideCell(str(res), j), guideColumn(j)) }
            },
        )
        Power.ATTENUATOR_TABLE.forEach { r ->
            table.addView(
                row().apply {
                    listOf(
                        "${r.vtx} (${r.dbm} dBm)",
                        "${r.attenDb} dB",
                        "${signed(r.dbm - r.attenDb.toDouble(), 0)} dBm",
                        str(R.string.power_rating, r.rating),
                    ).forEachIndexed { j, text -> addView(guideCell(text, j), guideColumn(j)) }
                },
                above(4f),
            )
        }
        return table
    }

    /** The columns share the row by weight: the first one carries "500 mW (27 dBm)" unwrapped. */
    private fun guideColumn(j: Int) = LinearLayout.LayoutParams(0, WRAP, GUIDE_WEIGHTS[j]).apply {
        if (j > 0) marginStart = pad(4f)
    }

    /** The attenuator column is the one the user has to get right. */
    private fun guideCell(text: String, column: Int) =
        label(if (column == 0) 10f else 11f, if (column == 1) theme.ink else theme.ink2, bold = column == 1)
            .apply { this.text = text }

    // ---- drawing ----

    private fun render() {
        painting = true
        try {
            paintSettings()
            paintCards()
        } finally {
            painting = false
        }
    }

    private fun paintSettings() {
        val field = attenField ?: return
        // The web does the same: only a field nobody is typing in follows the stored value.
        if (!field.isFocused) field.setText(prefs.powerAttenDb.toString())
    }

    private fun paintCards() {
        val p = store.profile
        if (p == null) {
            note.text = str(R.string.plug_in)
            note.visibility = View.VISIBLE
            cards.removeAllViews()
            cardRow = emptyList()
            planKey = ""
            return
        }
        if (store.planKey != planKey || p.channels.size != cardRow.size) build(p.channels.size)
        val st = store.status
        val ready = Power.scanIsReady(st)
        val calibrated = Power.calOffset(p.name, 5800) != null
        note.text = when {
            !calibrated -> str(R.string.power_no_calibration, p.name)
            st != null && !ready -> str(R.string.power_not_ready)
            else -> ""
        }
        note.visibility = if (note.text.isEmpty()) View.GONE else View.VISIBLE
        val now = store.now()
        val atten = prefs.powerAttenDb.toDouble()
        val smooth = prefs.powerSmoothS
        cardRow.forEachIndexed { id, card ->
            val channel = p.channels[id]
            val enabled = st == null || st.enabled and (1 shl id) != 0
            card.measure.isChecked = enabled
            card.root.alpha = if (enabled) 1f else 0.45f
            card.name.text = channel.name
            val mhz = ((channel.khz + 500) / 1000).toInt()
            card.freq.text = "$mhz MHz"
            val cs = if (enabled && ready) store.channels.getOrNull(id) else null
            paint(card, cs?.let { Power.readout(it, p.name, mhz, atten, smooth, now) })
        }
    }

    private fun paint(card: Card, r: Reading?) {
        if (r == null) {
            card.main.text = "—"
            card.unit.text = ""
            card.sub.text = ""
            card.kv.text = ""
            card.badges.text = ""
            return
        }
        // The engine's readings live in another module, so each one is taken once here.
        val vtx = r.vtxDbm
        val board = r.boardDbm
        val floor = r.noiseDbfs
        // The web splits "400 mW" across a big and a small type: same readout, its own sizes.
        val parts = if (vtx == null || r.clipping) null else Power.formatMw(vtx).split(" ", limit = 2)
        card.main.text = when {
            r.clipping -> str(R.string.power_over)
            parts == null -> "—"
            else -> parts[0]
        }
        card.unit.text = parts?.getOrNull(1)?.let { " $it" } ?: ""
        card.sub.text = if (parts == null || vtx == null) "" else "${signed(vtx, 1)} dBm"
        card.kv.text = listOf(
            "${str(R.string.power_at_board)} ${if (board == null) "—" else signed(board, 1) + " dBm"}",
            "${str(R.string.power_level)} ${fixed(r.levelDbfs, 1)} dBFS",
            "${str(R.string.power_noise)} ${if (floor == null) "—" else fixed(floor, 1) + " dBFS"}",
        ).joinToString("  ·  ")
        val badges = buildList {
            if (r.stale) add(str(R.string.power_stale) to theme.warning)
            if (r.clipping) add(str(R.string.power_clipping) to theme.critical)
            if (!r.clipping && r.signalDbfs == null) add(str(R.string.power_no_signal) to theme.warning)
            if (r.overRange) add(str(R.string.power_over_range) to theme.critical)
            if (!r.calibrated) add(str(R.string.power_uncalibrated) to theme.serious)
        }
        card.badges.text = SpannableStringBuilder().apply {
            badges.forEach { (tag, color) ->
                if (isNotEmpty()) append("  ")
                val from = length
                append(tag)
                setSpan(ForegroundColorSpan(color), from, length, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE)
            }
        }
    }

    private fun build(size: Int) {
        cards.removeAllViews()
        cardRow = (0 until size).map { id ->
            Card(id).also {
                it.measure.setOnCheckedChangeListener { _, on ->
                    if (painting) return@setOnCheckedChangeListener
                    if (on != (store.status?.enabled?.and(1 shl id) != 0)) toggleTo(id, on)
                }
                cards.addView(it.root, LinearLayout.LayoutParams(MATCH, WRAP).apply { topMargin = pad(5f) })
            }
        }
        planKey = store.planKey
    }

    // ---- small helpers ----

    /** The web's minus sign: a reading is never the hyphen in a word. */
    private fun signed(v: Double, places: Int) = fixed(v, places).replace('-', '\u2212')

    private fun fixed(v: Double, places: Int) = String.format(Locale.US, "%.${places}f", v)

    private fun label(sizeSp: Float, color: Int, bold: Boolean = false) = TextView(activity).apply {
        textSize = sizeSp
        setTextColor(color)
        if (bold) setTypeface(typeface, android.graphics.Typeface.BOLD)
    }

    private fun row(gravity: Int = Gravity.NO_GRAVITY) = LinearLayout(activity).apply {
        orientation = LinearLayout.HORIZONTAL
        this.gravity = gravity
    }

    private fun chip(text: String) = Button(activity).apply {
        this.text = text
        isAllCaps = false
        textSize = 11f
        minWidth = 0
        minimumWidth = 0
        minHeight = 0
        setPadding(pad(7f), 0, pad(7f), 0)
        layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT, pad(32f))
    }

    private fun button(text: String, run: () -> Unit) = chip(text).apply { setOnClickListener { run() } }

    private fun str(res: Int) = activity.getString(res)

    private fun str(res: Int, vararg args: Any) = activity.getString(res, *args)

    private fun above(dp: Float) = LinearLayout.LayoutParams(MATCH, WRAP).apply { topMargin = pad(dp) }

    private fun start(dp: Float) = LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT, WRAP)
        .apply { marginStart = pad(dp) }

    private fun pad(dp: Float) = theme.dp(dp).roundToInt()

    companion object {
        private const val MATCH = LinearLayout.LayoutParams.MATCH_PARENT
        private const val WRAP = LinearLayout.LayoutParams.WRAP_CONTENT

        /** VTX column carries "500 mW (27 dBm)", the other two their English headers. */
        private val GUIDE_WEIGHTS = floatArrayOf(1.3f, 0.85f, 1.05f, 0.8f)
    }
}
