// The protocol console as a sheet: the ring of link lines, the quick commands
// monitor mode answers, and a command input. Mirrors class Console in
// web/src/views/debug.ts, whose hover-free layout fits a phone unchanged.
// GPL-3.0-or-later.
package com.onebody.widscan58

import android.app.Activity
import android.app.AlertDialog
import android.app.Dialog
import android.graphics.Typeface
import android.text.SpannableStringBuilder
import android.text.Spanned
import android.text.style.ForegroundColorSpan
import android.view.View
import android.view.inputmethod.EditorInfo
import android.view.inputmethod.InputMethodManager
import android.widget.Button
import android.widget.EditText
import android.widget.HorizontalScrollView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import com.onebody.widscan58.core.LogDir
import com.onebody.widscan58.core.Store
import com.onebody.widscan58.view.Style
import java.util.Locale
import kotlin.math.roundToInt

class ConsoleSheet(
    private val activity: Activity,
    private val store: Store,
    private val send: (String) -> Unit,
) {
    private val theme = Style(activity)
    private val body = TextView(activity)
    private val scroller = ScrollView(activity)
    private val input = EditText(activity)
    private var shown = -1
    private var dialog: Dialog? = null

    fun show() {
        body.textSize = 9.5f
        body.typeface = Typeface.MONOSPACE
        body.setTextColor(theme.ink)
        body.setBackgroundColor(theme.surface2)
        body.setPadding(pad(6f), pad(5f), pad(6f), pad(5f))
        body.setTextIsSelectable(true)
        scroller.addView(body)
        input.hint = str(R.string.console_hint)
        input.textSize = 12f
        input.setSingleLine(true)
        input.imeOptions = EditorInfo.IME_ACTION_SEND
        input.setOnEditorActionListener { _, _, _ -> submit(); true }
        val sendButton = smallButton(str(R.string.console_send)) { submit() }
        dialog = AlertDialog.Builder(activity)
            .setTitle(R.string.console_title)
            .setView(LinearLayout(activity).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(pad(10f), 0, pad(10f), pad(6f))
                addView(scroller, LinearLayout.LayoutParams(MATCH, pad(280f)))
                addView(quickRow(), above(pad(8f)))
                addView(LinearLayout(activity).apply {
                    orientation = LinearLayout.HORIZONTAL
                    addView(input, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
                    addView(sendButton, LinearLayout.LayoutParams(WRAP, LinearLayout.LayoutParams.MATCH_PARENT))
                }, above(pad(4f)))
            })
            .setPositiveButton(R.string.close, null)
            .create().apply { show() }
        shown = -1
        render()
    }

    /** Called with the instrument's own frames, so the console follows the link. */
    fun update() {
        if (dialog?.isShowing != true) return
        render()
    }

    fun dismiss() {
        dialog?.dismiss()
        dialog = null
    }

    private fun submit() {
        val line = input.text.toString().trim()
        if (line.isEmpty()) return
        input.setText("")
        (activity.getSystemService(InputMethodManager::class.java))
            ?.hideSoftInputFromWindow(input.windowToken, 0)
        send(line)
    }

    /** The web's monitor-mode buttons; CONFIG? and METRICS? belong to RotorHazard. */
    private fun quickRow(): View {
        val row = LinearLayout(activity).apply { orientation = LinearLayout.HORIZONTAL }
        for (cmd in QUICK) row.addView(smallButton(cmd) { send(cmd) }, marginEnd(pad(4f)))
        return HorizontalScrollView(activity).apply { addView(row) }
    }

    private fun smallButton(label: String, onClick: () -> Unit) = Button(activity).apply {
        text = label
        isAllCaps = false
        textSize = 11f
        setTextColor(theme.ink)
        minWidth = 0
        minimumWidth = 0
        minHeight = 0
        setPadding(pad(8f), 0, pad(8f), 0)
        layoutParams = LinearLayout.LayoutParams(WRAP, pad(32f))
        setOnClickListener { onClick() }
    }

    private fun render() {
        val lines = store.log
        // Once the ring is full its size stops changing, so compare on the last entry too.
        if (lines.size == shown && lines.size != Store.LOG_MAX) return
        shown = lines.size
        val pinned = scroller.isAtBottom()
        val text = SpannableStringBuilder()
        if (lines.isEmpty()) {
            text.append(str(R.string.console_waiting))
        } else {
            for (l in lines.subList(maxOf(0, lines.size - Store.LOG_SHOWN), lines.size)) {
                val at = text.length
                text.append(String.format(Locale.US, "%8.2f %s %s\n", l.t, l.dir.arrow, l.text))
                text.setSpan(ForegroundColorSpan(colorOf(l.dir)), at, text.length, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
            }
        }
        body.setText(text, TextView.BufferType.SPANNABLE)
        if (pinned) scroller.post { scroller.fullScroll(View.FOCUS_DOWN) }
    }

    /** The web's console colours: sent in accent, device chatter in ink, its own notes amber. */
    private fun colorOf(dir: LogDir) = when (dir) {
        LogDir.TX -> theme.accent
        LogDir.RX -> theme.ink
        LogDir.INFO -> theme.warning
        LogDir.ERROR -> theme.critical
    }

    private fun ScrollView.isAtBottom(): Boolean {
        val child = if (childCount == 0) null else getChildAt(childCount - 1)
        return child == null || scrollY + height >= child.height - AT_BOTTOM_PX
    }

    private fun str(res: Int) = activity.getString(res)
    private fun above(top: Int) =
        LinearLayout.LayoutParams(MATCH, LinearLayout.LayoutParams.WRAP_CONTENT).apply { topMargin = top }

    private fun marginEnd(end: Int) = LinearLayout.LayoutParams(WRAP, LinearLayout.LayoutParams.MATCH_PARENT).apply {
        marginEnd = end
    }

    private fun pad(dp: Float) = theme.dp(dp).roundToInt()

    companion object {
        private const val MATCH = LinearLayout.LayoutParams.MATCH_PARENT
        private const val WRAP = LinearLayout.LayoutParams.WRAP_CONTENT
        private const val AT_BOTTOM_PX = 12
        private val QUICK = listOf("INFO", "STATUS?", "TIMING?", "SPURS?", "GAIN?")
    }
}
