// General settings: what the app decides for itself, which — unlike the scanner's parameters —
// needs no board connected. GPL-3.0-or-later.
package com.onebody.widscan58

import android.app.Activity
import android.app.AlertDialog
import android.app.Dialog
import android.view.Gravity
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import com.onebody.widscan58.view.Style
import kotlin.math.roundToInt

/**
 * The one choice the app makes for its own sake. [onLanguage] runs after the tag is stored:
 * every view on screen was built with the resources read in the old language, so the screen
 * has to be created again rather than re-labelled.
 */
class GeneralSettingsSheet(
    private val activity: Activity,
    private val prefs: Prefs,
    private val onLanguage: () -> Unit,
) {
    private val theme = Style(activity)
    private var dialog: Dialog? = null

    fun show() {
        val buttons = options().map { (tag, labelRes) ->
            button(activity.getString(labelRes)) {
                prefs.language = tag
                onLanguage()
            } to tag
        }
        buttons.forEach { (button, tag) ->
            button.setTextColor(if (tag == prefs.language) theme.accent else theme.muted)
        }

        val strip = LinearLayout(activity).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            buttons.forEachIndexed { i, (button, _) ->
                addView(button, LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT,
                ).apply { marginStart = if (i == 0) 0 else pad(4f) })
            }
        }

        val body = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad(10f), pad(4f), pad(10f), pad(8f))
            addView(TextView(activity).apply {
                setText(R.string.general_language)
                textSize = 11f
                setTextColor(theme.muted)
            })
            addView(strip)
        }

        dialog = AlertDialog.Builder(activity)
            .setTitle(R.string.general_title)
            .setView(ScrollView(activity).apply {
                // The platform dialog is a mid grey; the sheet's own labels are tuned for the page.
                setBackgroundColor(theme.surface)
                addView(body)
            })
            .setPositiveButton(R.string.close, null)
            .create().apply { show() }
    }

    fun dismiss() {
        dialog?.dismiss()
        dialog = null
    }

    private fun options() = listOf(
        Prefs.LANGUAGE_ZH to R.string.lang_zh,
        Prefs.LANGUAGE_EN to R.string.lang_en,
        Prefs.LANGUAGE_SYSTEM to R.string.lang_system,
    )

    private fun button(label: String, onClick: () -> Unit) = Button(activity).apply {
        text = label
        isAllCaps = false
        textSize = 12f
        minWidth = 0
        minHeight = 0
        setPadding(pad(10f), 0, pad(10f), 0)
        setOnClickListener { onClick() }
    }

    private fun pad(dp: Float) = theme.dp(dp).roundToInt()
}
