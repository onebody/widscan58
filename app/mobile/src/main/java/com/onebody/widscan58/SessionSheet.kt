// The session file sheet: record the wire, replay a capture, export what was measured.
// The web has the same actions in its top bar; here they need storage access, so the
// activity owns the file dialogs and this sheet only asks for them. GPL-3.0-or-later.
package com.onebody.widscan58

import android.app.Activity
import android.app.AlertDialog
import android.app.Dialog
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import com.onebody.widscan58.core.MAX_SESSION_BYTES
import com.onebody.widscan58.core.SessionRecorder
import com.onebody.widscan58.view.Style
import java.util.Locale
import kotlin.math.roundToInt

enum class ExportKind { CHANNELS, SPECTRUM }

/** Everything this sheet can ask the instrument to do. */
interface SessionActions {
    /** A source worth recording: a board on the cable, or the simulator. */
    fun recordable(): Boolean

    fun replayable(): Boolean

    fun startRecording()

    fun stopAndSave()

    fun saveLast()

    fun pickReplay()

    fun stopReplay()

    fun export(kind: ExportKind)

    fun note(): String
}

class SessionSheet(
    private val activity: Activity,
    private val recorder: SessionRecorder,
    private val feed: ReplayFeed,
    private val actions: SessionActions,
) {
    private val theme = Style(activity)
    private val stateLine = TextView(activity)
    private val noteLine = TextView(activity)
    private var recordButton: Button? = null
    private var replayButton: Button? = null
    private var stopReplayButton: Button? = null
    private var dialog: Dialog? = null

    fun show() {
        stateLine.textSize = 12f
        stateLine.setTextColor(theme.ink)
        noteLine.textSize = 11f
        noteLine.setTextColor(theme.muted)
        val body = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad(14f), 0, pad(14f), pad(8f))
            addView(hint(str(R.string.session_intro)))
            addView(stateLine, above(10f))
        }
        recordButton = addButton(body, R.string.session_record) {
            // The one button carries the whole save cycle: stop, then retry a save the user cancelled.
            when {
                recorder.active -> actions.stopAndSave()
                recorder.bytes > 0 -> actions.saveLast()
                else -> actions.startRecording()
            }
        }
        replayButton = addButton(body, R.string.session_replay, actions::pickReplay)
        stopReplayButton = addButton(body, R.string.session_stop_replay, actions::stopReplay)
        addButton(body, R.string.session_export_channels) { actions.export(ExportKind.CHANNELS) }
        addButton(body, R.string.session_export_spectrum) { actions.export(ExportKind.SPECTRUM) }
        body.addView(noteLine, above(10f))
        dialog = AlertDialog.Builder(activity)
            .setTitle(R.string.session_title)
            .setView(ScrollView(activity).apply { setBackgroundColor(theme.surface); addView(body) })
            .setPositiveButton(R.string.close, null)
            .create().apply { show() }
        update()
    }

    fun dismiss() {
        dialog?.dismiss()
        dialog = null
    }

    /** The instrument repaints at frame rate, so a replay's position moves without any input. */
    fun update() {
        if (dialog?.isShowing != true) return
        val replaying = feed.started
        recordButton?.text = str(
            when {
                recorder.active -> R.string.session_stop
                recorder.bytes > 0 -> R.string.session_save_last
                else -> R.string.session_record
            },
        )
        recordButton?.isEnabled = !replaying && (recorder.active || actions.recordable())
        replayButton?.isEnabled = !replaying && actions.replayable()
        stopReplayButton?.isEnabled = replaying
        stateLine.text = state()
        noteLine.text = actions.note()
    }

    private fun state(): String {
        val main = when {
            feed.started -> if (feed.paused) {
                str(R.string.session_replay_paused, feed.name)
            } else {
                str(R.string.session_replaying, feed.name, seconds(feed.positionMs), seconds(feed.durationMs))
            }

            recorder.active -> str(R.string.session_recording, formatBytes(recorder.bytes))
            recorder.bytes > 0 -> str(R.string.session_stored, formatBytes(recorder.bytes))
            else -> str(R.string.session_idle)
        }
        return if (recorder.overflowed) main + "\n" + str(R.string.session_overflow, formatBytes(MAX_SESSION_BYTES)) else main
    }

    private fun hint(text: String) = TextView(activity).apply {
        this.text = text
        textSize = 11f
        setTextColor(theme.muted)
    }

    /** A wide button per action: these are one-tap decisions, not a menu to scan. */
    private fun addButton(body: LinearLayout, label: Int, onClick: () -> Unit): Button = Button(activity).apply {
        text = str(label)
        isAllCaps = false
        textSize = 13f
        setTextColor(theme.ink)
        setOnClickListener { onClick() }
    }.also { body.addView(it, LinearLayout.LayoutParams(MATCH, pad(44f)).apply { topMargin = pad(6f) }) }

    private fun seconds(ms: Long): String = String.format(Locale.US, "%.0f s", ms / 1000.0)

    private fun str(res: Int) = activity.getString(res)

    private fun str(res: Int, vararg args: Any) = activity.getString(res, *args)

    private fun above(topDp: Float) = LinearLayout.LayoutParams(MATCH, LinearLayout.LayoutParams.WRAP_CONTENT)
        .apply { topMargin = pad(topDp) }

    private fun pad(dp: Float) = theme.dp(dp).roundToInt()

    companion object {
        private const val MATCH = LinearLayout.LayoutParams.MATCH_PARENT
    }
}

/** What a capture or an export weighs, for the sheet's state line and the activity's note. */
internal fun formatBytes(bytes: Long): String = when {
    bytes >= 1024 * 1024 -> String.format(Locale.US, "%.1f MiB", bytes / 1048576.0)
    bytes >= 1024 -> String.format(Locale.US, "%.0f KiB", bytes / 1024.0)
    else -> "$bytes B"
}
