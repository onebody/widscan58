// 关于: what is installed, what GitHub has published, and the hand-off to the system installer.
// The one screen in the app that talks to the network. GPL-3.0-or-later.
package com.onebody.widscan58

import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageInstaller
import android.graphics.Typeface
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.ScrollView
import android.widget.TextView
import com.onebody.widscan58.core.AppRelease
import com.onebody.widscan58.core.upgradeFrom
import com.onebody.widscan58.view.Style
import java.io.IOException
import kotlin.math.roundToInt

/**
 * The update check runs only when the button is pressed: nothing reaches for the network when the
 * app opens and nothing does so in the background. A worker thread does the fetching and the
 * copying, and every answer comes back through [main] so the views are touched only by the UI thread.
 */
class AboutActivity : Activity() {
    /** Lazy, not a field initializer: reading colours needs resources, and those wait for attachBaseContext. */
    private val theme by lazy { Style(this) }
    private val main = Handler(Looper.getMainLooper())
    private val installer = ApkInstaller(this)

    private lateinit var status: TextView
    private lateinit var bar: ProgressBar
    private lateinit var checkButton: Button
    private lateinit var installButton: Button
    private lateinit var grantButton: Button

    /** The build worth offering, remembered so 安装 does not have to ask GitHub again. */
    private var pending: AppRelease? = null
    private var busy = false

    /** Same override the main screen uses: without it, this page follows the phone's language instead of the app's. */
    override fun attachBaseContext(newBase: Context) {
        super.attachBaseContext(AppLanguage.wrap(newBase))
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(build())
        padContentForSystemBars()
    }

    /** What the installer answers with; see [ApkInstaller.statusSender]. */
    private val installStatus = object : BroadcastReceiver() {
        override fun onReceive(from: Context, report: Intent) = reported(report)
    }
    private var listening = false

    private fun build(): View {
        checkButton = button(R.string.about_check) { check() }
        installButton = button { install() }
        grantButton = button(R.string.about_go_settings) { openGrant() }
        val releaseButton = button(R.string.about_open_releases) { openReleases() }
        installButton.visibility = View.GONE
        grantButton.visibility = View.GONE
        installButton.setTextColor(theme.good)

        bar = ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply {
            max = 100
            visibility = View.GONE
        }
        status = TextView(this).apply {
            textSize = 12f
            setTextColor(theme.ink2)
        }
        val card = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(theme.surface)
            setPadding(pad(14f), pad(6f), pad(14f), pad(6f))
            addView(row(R.string.about_app, getString(R.string.app_name)))
            addView(row(R.string.about_version, BuildConfig.VERSION_NAME))
            addView(row(R.string.about_build, BuildConfig.VERSION_CODE.toString()))
            addView(row(R.string.about_repo, GitHubReleases.repo))
            addView(row(R.string.about_license, LICENSE))
        }
        // Two rows rather than one, so a long English label cannot push a button off the screen.
        val primary = strip(checkButton, installButton)
        val secondary = strip(releaseButton, grantButton)

        val body = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad(14f), pad(6f), pad(14f), pad(18f))
            addView(TextView(this@AboutActivity).apply {
                setText(R.string.about_title)
                textSize = 17f
                setTextColor(theme.ink)
                setTypeface(typeface, Typeface.BOLD)
            })
            addView(card, match().apply { topMargin = pad(10f) })
            addView(status, match().apply { topMargin = pad(14f) })
            addView(bar, match().apply { topMargin = pad(8f) })
            addView(primary, match().apply { topMargin = pad(12f) })
            addView(secondary, match().apply { topMargin = pad(6f) })
            addView(TextView(this@AboutActivity).apply {
                setText(R.string.about_privacy)
                textSize = 11f
                setTextColor(theme.muted)
            }, match().apply { topMargin = pad(14f) })
        }
        return ScrollView(this).apply {
            setBackgroundColor(theme.page)
            isFillViewport = true
            addView(body)
        }
    }

    private fun check() {
        if (busy) return
        busy = true
        pending = null
        installButton.visibility = View.GONE
        checkButton.isEnabled = false
        status.setText(R.string.about_checking)
        Thread {
            val releases = try {
                GitHubReleases.fetch()
            } catch (failure: IOException) {
                tell(failure.message ?: ERROR)
                return@Thread
            }
            main.post { checked(releases) }
        }.start()
    }

    private fun checked(releases: List<AppRelease>) {
        busy = false
        if (isFinishing) return
        checkButton.isEnabled = true
        val upgrade = upgradeFrom(BuildConfig.VERSION_NAME, releases)
        when {
            releases.isEmpty() -> status.setText(R.string.about_no_releases)
            upgrade == null ->
                status.text = getString(R.string.about_latest, BuildConfig.VERSION_NAME)
            else -> offer(upgrade)
        }
    }

    private fun offer(release: AppRelease) {
        pending = release
        val size = if (release.size > 0) formatBytes(release.size) else getString(R.string.about_size_unknown)
        // A release without a published digest cannot be verified, and saying so beats installing quietly.
        val line = if (release.sha256.isEmpty()) R.string.about_found_unverified else R.string.about_found
        status.text = getString(line, release.version, size)
        installButton.text = getString(R.string.about_install, release.version)
        installButton.visibility = View.VISIBLE
    }

    private fun install() {
        val release = pending ?: return
        if (busy) return
        if (!installer.mayInstall()) {
            status.setText(R.string.about_need_permission)
            grantButton.visibility = View.VISIBLE
            return
        }
        busy = true
        installButton.isEnabled = false
        bar.progress = 0
        bar.visibility = View.VISIBLE
        listen()
        Thread {
            val failure = try {
                installer.install(release) { fraction -> main.post { progressing(fraction) } }
                null
            } catch (mismatch: ChecksumMismatch) {
                getString(R.string.about_checksum_failed)
            } catch (refused: InstallRefused) {
                // The staging session itself refused; saying 下载失败 would point at the wrong half.
                getString(R.string.about_install_rejected, refused.message ?: ERROR)
            } catch (network: IOException) {
                getString(R.string.about_install_failed, network.message ?: ERROR)
            }
            main.post { installed(failure) }
        }.start()
    }

    private fun progressing(fraction: Double) {
        if (isFinishing) return
        bar.progress = (fraction * 100).roundToInt()
        status.text = getString(R.string.about_downloading, bar.progress)
    }

    /** [failure] is the whole sentence the worker thread chose, or null when the installer took over. */
    private fun installed(failure: String?) {
        busy = false
        if (isFinishing) return
        bar.visibility = View.GONE
        installButton.isEnabled = true
        if (failure == null) status.setText(R.string.about_installer_opened)
        else status.text = failure
    }

    /** Android 14 and later want a receiver to say out loud whether the sender may reach it. */
    private fun listen() {
        if (listening) return
        val filter = IntentFilter(ApkInstaller.ACTION_STATUS)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(installStatus, filter, Context.RECEIVER_EXPORTED)
        } else {
            registerReceiver(installStatus, filter)
        }
        listening = true
    }

    private fun reported(report: Intent) {
        if (isFinishing) return
        val code = report.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE)
        when (code) {
            PackageInstaller.STATUS_SUCCESS -> status.setText(R.string.about_installed)
            PackageInstaller.STATUS_PENDING_USER_ACTION -> handOver(report)
            else -> {
                val detail = report.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE) ?: "status $code"
                status.text = getString(R.string.about_install_rejected, detail)
            }
        }
    }

    /**
     * Android will not commit our session by itself: the report carries the intent that asks the
     * user, which is the dialog the installer shows. The session still answers on the broadcast,
     * because this app is the one about to be replaced.
     */
    @Suppress("DEPRECATION")
    private fun handOver(report: Intent) {
        val confirm = report.getParcelableExtra<Intent>(Intent.EXTRA_INTENT)
        if (confirm == null) {
            status.setText(R.string.about_installer_opened)
            return
        }
        try {
            startActivity(confirm)
        } catch (absent: ActivityNotFoundException) {
            status.setText(R.string.about_no_installer)
        }
    }

    override fun onDestroy() {
        if (listening) unregisterReceiver(installStatus)
        listening = false
        super.onDestroy()
    }

    /**
     * The Settings page holding "allow installing unknown apps", opened on this app's own entry.
     * Flipping that switch kills this process, so the screen comes back empty and the check runs again.
     */
    private fun openGrant() {
        try {
            startActivity(installer.grantIntent())
        } catch (absent: ActivityNotFoundException) {
            status.setText(R.string.about_no_settings)
        }
    }

    /** The fallback when the in-app path is refused: the browser fetches the APK itself. */
    private fun openReleases() {
        val page = Intent(Intent.ACTION_VIEW, Uri.parse(GitHubReleases.releasePage))
        try {
            startActivity(page)
        } catch (absent: ActivityNotFoundException) {
            status.setText(R.string.about_no_browser)
        }
    }

    /** A worker-thread failure, said on the UI thread in the words this screen already uses. */
    private fun tell(reason: String) {
        main.post {
            busy = false
            if (!isFinishing) status.text = getString(R.string.about_check_failed, reason)
        }
    }

    private fun strip(vararg buttons: Button) = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        buttons.forEachIndexed { i, button ->
            addView(button, wrap().apply { marginStart = if (i == 0) 0 else pad(6f) })
        }
    }

    private fun button(label: Int, onClick: () -> Unit) = button(onClick).apply { setText(label) }

    private fun button(onClick: () -> Unit) = Button(this).apply {
        isAllCaps = false
        textSize = 12f
        minWidth = 0
        minHeight = 0
        setPadding(pad(10f), 0, pad(10f), 0)
        setOnClickListener { onClick() }
    }

    private fun row(label: Int, value: String) = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        addView(TextView(this@AboutActivity).apply {
            setText(label)
            textSize = 11f
            setTextColor(theme.muted)
        }, wrap())
        addView(TextView(this@AboutActivity).apply {
            text = value
            textSize = 13f
            setTextColor(theme.ink)
            gravity = Gravity.END
        }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
    }

    private fun match() = LinearLayout.LayoutParams(
        ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT,
    )

    private fun wrap() = LinearLayout.LayoutParams(
        ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT,
    )

    private fun pad(dp: Float) = theme.dp(dp).roundToInt()

    private companion object {
        const val LICENSE = "GPL-3.0-or-later"
        const val ERROR = "error"
    }
}
