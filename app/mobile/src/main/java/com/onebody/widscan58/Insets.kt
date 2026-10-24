// Keep the readouts clear of the status and gesture bars. GPL-3.0-or-later.
package com.onebody.widscan58

import android.app.Activity
import android.graphics.Insets
import android.os.Build
import android.view.View
import android.view.WindowInsets

/**
 * Android 15 and later lay content under the system bars, so the content view pads
 * itself. Padding the content frame rather than the root layout keeps the layout's
 * own gutters intact.
 */
fun Activity.padContentForSystemBars() {
    findViewById<View>(android.R.id.content).setOnApplyWindowInsetsListener { view, insets ->
        val bars = systemBars(insets)
        view.setPadding(bars.left, bars.top, bars.right, bars.bottom)
        insets
    }
}

private fun systemBars(insets: WindowInsets): Insets =
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
        insets.getInsets(
            WindowInsets.Type.systemBars() or WindowInsets.Type.displayCutout(),
        )
    } else {
        @Suppress("DEPRECATION")
        Insets.of(
            insets.systemWindowInsetLeft,
            insets.systemWindowInsetTop,
            insets.systemWindowInsetRight,
            insets.systemWindowInsetBottom,
        )
    }
