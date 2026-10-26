// The board's boot-time output pins, as the web's GPIO dialog has them: one row per
// pin, three choices each, and a save that decides which settings survive a restart.
// Mirrors web/src/views/gpioDialog.ts. GPL-3.0-or-later.
package com.onebody.widscan58

import android.app.Activity
import android.app.AlertDialog
import android.app.Dialog
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import com.onebody.widscan58.core.DeviceCommands
import com.onebody.widscan58.core.GpioPin
import com.onebody.widscan58.core.GpioSetting
import com.onebody.widscan58.core.Store
import com.onebody.widscan58.core.gpioUnsaved
import com.onebody.widscan58.view.Style
import kotlin.math.roundToInt

/**
 * Pins the device drives as soon as it starts, such as an antenna switch. The board is
 * the only authority on them, so every write is followed by a read-back and the sheet
 * draws what came of it rather than what was asked for.
 */
class GpioSheet(
    private val activity: Activity,
    private val store: Store,
    private val send: (String) -> Unit,
) {
    private val theme = Style(activity)
    private val rows = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
    private val empty = TextView(activity).apply {
        textSize = 11f
        setTextColor(theme.muted)
        text = str(R.string.gpio_none)
    }
    private val unsavedNote = TextView(activity).apply {
        textSize = 10f
        setTextColor(theme.warning)
        text = str(R.string.gpio_unsaved)
    }
    private var saveButton: Button? = null
    private var revertButton: Button? = null
    private var dialog: Dialog? = null

    /** The pins as last drawn. The instrument repaints at frame rate, and a row rebuilt
     *  between a press and its release loses the tap, so only a real change redraws. */
    private var shown: String? = null

    fun show() {
        shown = null
        (rows.parent as? ViewGroup)?.removeView(rows)
        val body = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad(10f), 0, pad(10f), pad(6f))
            addView(TextView(activity).apply {
                textSize = 11f
                setTextColor(theme.ink2)
                text = str(R.string.gpio_intro)
            })
            addView(unsavedNote, above(6f))
            addView(empty, above(8f))
            addView(rows, above(4f))
        }
        val alert = AlertDialog.Builder(activity)
            .setTitle(R.string.gpio_title)
            .setView(ScrollView(activity).apply {
                // The platform dialog is a mid grey; the sheet's own labels are tuned for the page.
                setBackgroundColor(theme.surface)
                addView(body)
            })
            // Save and Revert belong in the button bar: with 29 pins on the C5, a footer
            // inside the list is off the bottom of the screen exactly when it is needed.
            .setNeutralButton(R.string.gpio_revert, null)
            .setNegativeButton(R.string.gpio_save, null)
            .setPositiveButton(R.string.close, null)
            .create()
        dialog = alert
        alert.show()
        saveButton = alert.button(AlertDialog.BUTTON_NEGATIVE, R.string.gpio_save_hint) { write(listOf("GPIO SAVE")) }
        revertButton = alert.button(AlertDialog.BUTTON_NEUTRAL, R.string.gpio_revert_hint) {
            write(gpioUnsaved(store.gpio).map { DeviceCommands.gpioPin(it.n, it.saved) })
        }
        render()
        // Ask every time the sheet opens: a pin an antenna switch was wired for is
        // already high, and a sheet that remembered the last read would hide that.
        send("GPIO?")
    }

    /** A dialog button that keeps the sheet open: the read-back is the point of the tap. */
    private fun Dialog.button(which: Int, hint: Int, run: () -> Unit): Button? =
        (this as? AlertDialog)?.getButton(which)?.apply {
            contentDescription = str(hint)
            isAllCaps = false
            setOnClickListener { run() }
        }

    /** Called from the instrument's own refresh, so the rows follow the replies as they land. */
    fun update() {
        if (dialog?.isShowing == true) render()
    }

    fun dismiss() {
        dialog?.dismiss()
        dialog = null
        saveButton = null
        revertButton = null
    }

    /** One user action: every line it writes, then one read-back of what the board took. */
    private fun write(lines: List<String>) {
        lines.forEach(send)
        send("GPIO?")
    }

    private fun render() {
        val pins = store.gpio
        val sig = pins.joinToString("") { "${it.active.char}${it.saved.char}" }
        if (sig == shown) return
        shown = sig
        empty.visibility = if (pins.isEmpty()) View.VISIBLE else View.GONE
        rows.removeAllViews()
        pins.forEach { rows.addView(pinRow(it), LinearLayout.LayoutParams(MATCH, WRAP)) }
        val dirty = gpioUnsaved(pins).size
        unsavedNote.visibility = if (dirty == 0) View.GONE else View.VISIBLE
        saveButton?.isEnabled = dirty > 0
        revertButton?.isEnabled = dirty > 0
    }

    /** The three choices the firmware takes, or the reason a pin has none. */
    private fun pinRow(p: GpioPin): View {
        val line = LinearLayout(activity).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            addView(TextView(activity).apply {
                textSize = 11f
                // A pin out of step with its saved setting is the one the user has to notice.
                setTextColor(if (p.active == p.saved) theme.ink else theme.warning)
                text = str(R.string.gpio_pin, p.n)
                minWidth = pad(56f)
            })
            if (p.active == GpioSetting.RESERVED)
                addView(TextView(activity).apply {
                    textSize = 11f
                    setTextColor(theme.muted)
                    text = str(R.string.gpio_reserved)
                }, start(4f))
            else
                addView(
                    LinearLayout(activity).apply {
                        orientation = LinearLayout.HORIZONTAL
                        CHOICES.forEach { addView(choice(p, it)) }
                    },
                    start(0f),
                )
        }
        if (p.active == p.saved) return line
        // The saved setting goes under the pin, not beside it: a narrow phone has no
        // room for a third column, and only a changed pin needs one.
        return LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            addView(line)
            addView(TextView(activity).apply {
                textSize = 9f
                setTextColor(theme.muted)
                text = str(R.string.gpio_saved, str(labelOf(p.saved)))
            }, above(1f))
        }
    }

    private fun choice(p: GpioPin, s: GpioSetting) = Button(activity).apply {
        text = str(labelOf(s))
        isAllCaps = false
        textSize = 11f
        minWidth = 0
        minimumWidth = 0
        minHeight = 0
        // The platform's 48 dp touch height would show only ten of the C5's 29 pins.
        layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT, pad(34f))
        setPadding(pad(7f), 0, pad(7f), 0)
        setTextColor(if (s == p.active) theme.accent else theme.muted)
        setOnClickListener { if (s != p.active) write(listOf(DeviceCommands.gpioPin(p.n, s))) }
    }

    private fun labelOf(s: GpioSetting) = when (s) {
        GpioSetting.NONE -> R.string.gpio_unused
        GpioSetting.LOW -> R.string.gpio_low
        GpioSetting.HIGH -> R.string.gpio_high
        GpioSetting.RESERVED -> R.string.gpio_reserved_word
    }

    private fun str(res: Int) = activity.getString(res)

    private fun str(res: Int, vararg args: Any) = activity.getString(res, *args)

    private fun above(dp: Float) = LinearLayout.LayoutParams(MATCH, WRAP).apply { topMargin = pad(dp) }

    private fun start(dp: Float) = LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT, WRAP)
        .apply { marginStart = pad(dp) }

    private fun pad(dp: Float) = theme.dp(dp).roundToInt()

    companion object {
        private const val MATCH = LinearLayout.LayoutParams.MATCH_PARENT
        private const val WRAP = LinearLayout.LayoutParams.WRAP_CONTENT

        /** The firmware's three states; a reserved pin is not the sheet's to touch. */
        private val CHOICES = listOf(GpioSetting.NONE, GpioSetting.LOW, GpioSetting.HIGH)
    }
}
