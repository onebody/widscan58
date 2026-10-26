// Channel plan editor: the six FPV bands, up to eight channels picked across them.
// The narrow branch of web/src/views/channelPlanDialog.ts — a band per row, the
// channel numbers because the band letter already starts the row.
// GPL-3.0-or-later.
package com.onebody.widscan58

import android.app.Activity
import android.app.AlertDialog
import android.app.Dialog
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.HorizontalScrollView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import com.onebody.widscan58.core.Band
import com.onebody.widscan58.core.Bands
import com.onebody.widscan58.core.MAX_CHANNELS
import com.onebody.widscan58.view.Style
import kotlin.math.roundToInt

/** [onPick] receives the plan the editor settled on, normalised by [Bands]. */
class ChannelPlanSheet(private val activity: Activity, private val onPick: (List<String>) -> Unit) {
    private val theme = Style(activity)
    private val selected = linkedSetOf<String>()
    private val chips = mutableListOf<Pair<String, Button>>()
    private var summary: TextView? = null
    private var dialog: Dialog? = null

    fun show(current: List<String>) {
        summary?.removeCallbacks(restore)
        selected.clear()
        selected += current.filter { Bands.mhz(it) != null }
        chips.clear()
        summary = TextView(activity).apply { textSize = 12f; setTextColor(theme.ink) }
        val body = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad(12f), pad(2f), pad(12f), pad(2f))
            addView(text(R.string.plan_hint, theme.muted, 11f), above(pad(8f)))
            addView(
                row(listOf(summary!!, button(activity.getString(R.string.plan_clear)) {
                    selected.clear()
                    sync()
                })),
                above(pad(6f)),
            )
            for (band in Bands.ALL) addView(bandRow(band), above(pad(8f)))
        }
        dialog = AlertDialog.Builder(activity)
            .setTitle(R.string.plan_title)
            .setView(ScrollView(activity).apply {
                setBackgroundColor(theme.surface)
                addView(body)
            })
            // An empty plan is not a plan: the board refuses CHANNELS with no channels.
            .setPositiveButton(R.string.yes) { _, _ ->
                val plan = Bands.normalize(selected.toList())
                if (plan.isNotEmpty()) onPick(plan)
            }
            .setNegativeButton(R.string.no, null)
            .create().apply { show() }
        sync()
    }

    fun dismiss() {
        summary?.removeCallbacks(restore)
        dialog?.dismiss()
        dialog = null
    }

    /** One band: its name switches the whole band; the eight numbers join or leave the plan. */
    private fun bandRow(band: Band): LinearLayout {
        val cells = band.mhz.indices.map { n ->
            val name = "${band.letter}${n + 1}"
            button((n + 1).toString()) { toggle(name) }.also { chips += name to it }
        }
        val strip = HorizontalScrollView(activity).apply {
            isHorizontalScrollBarEnabled = false
            addView(row(cells))
        }
        return LinearLayout(activity).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            addView(button("${band.letter} ${band.name}") { takeAll(band.letter) })
            addView(strip, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f).apply {
                marginStart = pad(3f)
            })
        }
    }

    /** Replacing the selection is the point: eight channels of one band fills the plan. */
    private fun takeAll(letter: String) {
        selected.clear()
        selected += Bands.names(letter)
        sync()
    }

    private fun toggle(name: String) {
        if (selected.remove(name)) sync()
        else if (selected.size < MAX_CHANNELS) {
            selected += name
            sync()
        } else flash(activity.getString(R.string.plan_full))
    }

    /** Say why nothing happened where the counter sits, then restore it. */
    private fun flash(msg: String) {
        val s = summary ?: return
        s.removeCallbacks(restore)
        s.text = msg
        s.setTextColor(theme.warning)
        s.postDelayed(restore, 1800)
    }

    private val restore = Runnable { sync() }

    private fun sync() {
        summary?.removeCallbacks(restore)
        summary?.setTextColor(theme.ink)
        chips.forEach { (name, chip) ->
            chip.setTextColor(if (selected.contains(name)) theme.accent else theme.muted)
        }
        summary?.text = activity.getString(
            R.string.plan_count, selected.size, MAX_CHANNELS,
            Bands.normalize(selected.toList()).joinToString(" "),
        )
    }

    private fun text(res: Int, color: Int, size: Float) = TextView(activity).apply {
        setText(res)
        textSize = size
        setTextColor(color)
    }

    private fun row(views: List<View>) = LinearLayout(activity).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        views.forEachIndexed { i, v ->
            addView(v, LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT,
            ).apply { marginStart = if (i == 0) 0 else pad(3f) })
        }
    }

    private fun button(label: String, click: () -> Unit) = Button(activity).apply {
        text = label
        isAllCaps = false
        textSize = 11f
        minWidth = 0
        minHeight = 0
        setPadding(pad(4f), 0, pad(4f), 0)
        setOnClickListener { click() }
    }

    private fun above(top: Int) = LinearLayout.LayoutParams(
        LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT,
    ).apply { topMargin = top }

    private fun pad(dp: Float) = theme.dp(dp).roundToInt()
}
