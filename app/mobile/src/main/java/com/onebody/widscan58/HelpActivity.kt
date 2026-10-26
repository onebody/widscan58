// The tutorial, served from assets: a WebView pointed at a local page. GPL-3.0-or-later.
package com.onebody.widscan58

import android.annotation.SuppressLint
import android.app.Activity
import android.os.Bundle
import android.webkit.WebView
import android.widget.FrameLayout

/**
 * The whole app has no network code and asks for no permissions; this screen keeps that
 * promise by reading the manual out of the APK itself. Nothing but our own asset file is
 * ever loaded, and the page's script only runs the tap-to-enlarge overlay.
 */
class HelpActivity : Activity() {
    private lateinit var page: WebView

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        page = WebView(this).apply {
            setBackgroundColor(0xFF0D0D0D.toInt())
            settings.javaScriptEnabled = true
            settings.builtInZoomControls = true
            settings.displayZoomControls = false
            // Without this the viewport meta tag is ignored and the text renders tiny.
            settings.useWideViewPort = true
            settings.loadWithOverviewMode = false
            loadUrl(PAGE)
        }
        setContentView(
            FrameLayout(this).apply {
                setBackgroundColor(0xFF0D0D0D.toInt())
                addView(page, FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT)
            },
        )
        padContentForSystemBars()
    }

    /**
     * The page is one long document, so the anchor chips leave WebView history behind;
     * back means "leave the help", not "undo a jump".
     */
    @Suppress("OVERRIDE_DEPRECATION")
    override fun onBackPressed() = finish()

    override fun onDestroy() {
        // The WebView holds the activity context from inside its own view tree.
        (page.parent as? FrameLayout)?.removeView(page)
        page.destroy()
        super.onDestroy()
    }

    private companion object {
        const val PAGE = "file:///android_asset/help/index.html"
    }
}
