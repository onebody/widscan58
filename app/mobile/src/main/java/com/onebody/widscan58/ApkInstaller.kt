// Getting our own published APK onto the phone: streamed straight into an installer session,
// so nothing is left on shared storage and no FileProvider is needed. GPL-3.0-or-later.
package com.onebody.widscan58

import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.IntentSender
import android.content.pm.PackageInstaller
import android.net.Uri
import android.os.Build
import android.provider.Settings
import com.onebody.widscan58.core.AppRelease
import java.io.IOException
import java.io.InputStream
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest
import java.security.NoSuchAlgorithmException

/** The bytes that arrived are not the bytes the release claims to be. */
class ChecksumMismatch : IOException("sha256 mismatch")

/** The session was refused. commit() throws a type that is not public API, so it gets one here. */
class InstallRefused(reason: String) : IOException(reason)

class ApkInstaller(private val context: Context) {

    /** Android 8 and later will not let us raise the installer until the user has allowed it. */
    fun mayInstall(): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.O ||
            context.packageManager.canRequestPackageInstalls()

    /** The setting that holds the switch, opened on this app's own entry. */
    fun grantIntent(): Intent = Intent(
        Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
        Uri.parse("package:${context.packageName}"),
    )

    /**
     * Copy [release] into a staging session and hand it to the system installer. The checksum is
     * the gate: a session that does not match is abandoned rather than committed, so a truncated
     * download or a swapped asset never reaches the installer.
     *
     * Runs on the calling thread, which is why the caller keeps it off the main one.
     */
    @Throws(IOException::class)
    fun install(release: AppRelease, onProgress: (fraction: Double) -> Unit) {
        val connection = (URL(release.downloadUrl).openConnection() as HttpURLConnection).apply {
            setRequestProperty("User-Agent", AGENT)
            connectTimeout = CONNECT_MS
            readTimeout = READ_MS
        }
        val total = contentLength(connection, release)
        // createSession answers a number and openSession turns it into the writable session.
        val staging = context.packageManager.packageInstaller
        val session = staging.openSession(staging.createSession(sessionParams(total)))
        var handedOver = false
        try {
            connection.inputStream.use { body ->
                val digest = stage(session, body, total, onProgress)
                if (release.sha256.isNotEmpty() && toHex(digest) != release.sha256) {
                    throw ChecksumMismatch()
                }
            }
            try {
                session.commit(statusSender())
                handedOver = true
            } catch (refused: RuntimeException) {
                // commit() reports a bad session with a type that is not public API.
                throw InstallRefused(refused.message ?: "session refused")
            }
        } finally {
            // Anything that did not reach the installer is destroyed rather than left staged;
            // close() only releases our handle, and how it treats an uncommitted session is up to
            // the Android version, so the abandonment is spelled out.
            if (!handedOver) session.abandon()
            session.close()
            connection.disconnect()
        }
    }

    /** Writes the body into the session and returns the digest over exactly those bytes. */
    @Throws(IOException::class)
    private fun stage(
        session: PackageInstaller.Session,
        body: InputStream,
        total: Long,
        onProgress: (fraction: Double) -> Unit,
    ): ByteArray {
        val sha = sha256()
        val sink = session.openWrite(NAME, 0, total)
        try {
            val buffer = ByteArray(BLOCK)
            var written = 0L
            while (true) {
                val read = body.read(buffer)
                if (read < 0) break
                sink.write(buffer, 0, read)
                sha.update(buffer, 0, read)
                written += read
                if (total > 0) onProgress((written.toDouble() / total).coerceIn(0.0, 1.0))
            }
            // The package has to be on disk before the installer is allowed to look at it.
            session.fsync(sink)
            return sha.digest()
        } finally {
            sink.close()
        }
    }

    /**
     * The installer's answer arrives as a broadcast rather than by relaunching a screen: this app
     * is the one being replaced, so it may not have a window left to report into.
     *
     * The pending intent has to stay mutable, because the installer reports by filling in this
     * intent; an immutable one arrives with no extras at all and the update never starts.
     */
    fun statusSender(): IntentSender {
        val mutable = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) PendingIntent.FLAG_MUTABLE else 0
        return PendingIntent.getBroadcast(
            context,
            0,
            Intent(ACTION_STATUS).setPackage(context.packageName),
            PendingIntent.FLAG_UPDATE_CURRENT or mutable,
        ).intentSender
    }

    /** Telling the installer the size up front is what lets it refuse a full storage before
     * a single byte has been pulled over the radio. */
    private fun sessionParams(total: Long) =
        PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL).apply {
            if (total > 0) setSize(total)
        }

    /** GitHub answers the size in the header; the release list said it too, so take either. */
    private fun contentLength(connection: HttpURLConnection, release: AppRelease): Long =
        connection.contentLengthLong.takeIf { it > 0 } ?: release.size.coerceAtLeast(0L)

    private fun sha256(): MessageDigest =
        try {
            MessageDigest.getInstance("SHA-256")
        } catch (absent: NoSuchAlgorithmException) {
            // Every Android ships it; a device without it could verify nothing, so it must install nothing.
            throw IOException("no sha-256")
        }

    /** Lowercase hex, and never locale-dependent: a Turkish system must not turn B into İ. */
    private fun toHex(bytes: ByteArray): String =
        bytes.joinToString("") { (it.toInt() and 0xff).toString(16).padStart(2, '0') }

    companion object {
        /** The broadcast the installer answers on; only this package receives it. */
        const val ACTION_STATUS = "com.onebody.widscan58.INSTALL_STATUS"

        private const val NAME = "widscan58.apk"
        private const val AGENT = "widscan58-android"
        private const val BLOCK = 64 * 1024
        private const val CONNECT_MS = 15_000
        private const val READ_MS = 30_000
    }
}
