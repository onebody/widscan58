// The one outbound request the app makes: the release list of its own repository.
// GPL-3.0-or-later.
package com.onebody.widscan58

import com.onebody.widscan58.core.AppRelease
import com.onebody.widscan58.core.appVersionOf
import com.onebody.widscan58.core.digestHex
import org.json.JSONArray
import org.json.JSONException
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/**
 * What GitHub says has been published. The `/releases/latest` endpoint is answered by publish
 * time rather than by version, and this repository publishes the web app under its own tags
 * too, so the list is fetched and narrowed to the `app-v` prefix instead.
 */
object GitHubReleases {
    /** Which repository publishes this app; 关于 shows it so the source of an update is on screen. */
    val repo = BuildConfig.RELEASE_REPO

    /** Where 关于 can send a browser, which needs no permission of ours to fetch an APK. */
    val releasePage = "https://github.com/$repo/releases"

    private val API = "https://api.github.com/repos/$repo/releases?per_page=$COUNT"

    @Throws(IOException::class)
    fun fetch(): List<AppRelease> {
        val entries = getJson(API)
        return List(entries.length()) { entries.getJSONObject(it) }
            .filterNot(::publishedButNotOffered)
            .mapNotNull(::installable)
    }

    /** A draft is unfinished and a prerelease is not an offer every phone should take. */
    private fun publishedButNotOffered(entry: JSONObject) =
        entry.optBoolean("draft") || entry.optBoolean("prerelease")

    /** A release the phone can act on: our own tag, and an APK served over https to go with it. */
    private fun installable(entry: JSONObject): AppRelease? {
        val version = appVersionOf(entry.optString("tag_name")) ?: return null
        val assets = entry.optJSONArray("assets") ?: return null
        val apk = List(assets.length()) { assets.getJSONObject(it) }
            .firstOrNull { it.optString("name").endsWith(".apk") } ?: return null
        val download = apk.optString("browser_download_url")
        if (!download.startsWith("https://")) return null
        return AppRelease(
            version = version,
            title = entry.optString("name"),
            downloadUrl = download,
            size = apk.optLong("size", -1L),
            sha256 = digestHex(apk.optString("digest")),
        )
    }

    @Throws(IOException::class)
    private fun getJson(path: String): JSONArray {
        val connection = (URL(path).openConnection() as HttpURLConnection).apply {
            // The API answers only a request that names a client.
            setRequestProperty("User-Agent", AGENT)
            setRequestProperty("Accept", "application/vnd.github+json")
            connectTimeout = CONNECT_MS
            readTimeout = READ_MS
        }
        val body = try {
            val code = connection.responseCode
            if (code != HttpURLConnection.HTTP_OK) {
                val limited = code == HttpURLConnection.HTTP_FORBIDDEN &&
                    connection.getHeaderField("X-RateLimit-Remaining") == "0"
                throw IOException(if (limited) "HTTP $code rate limited" else "HTTP $code")
            }
            connection.inputStream.bufferedReader().use { it.readText() }
        } finally {
            connection.disconnect()
        }
        // A captive portal answers with an HTML page, which is a failure rather than no releases.
        return try {
            JSONArray(body)
        } catch (malformed: JSONException) {
            throw IOException("not a release list")
        }
    }

    private const val AGENT = "widscan58-android"
    private const val COUNT = 20
    private const val CONNECT_MS = 8_000
    private const val READ_MS = 15_000
}
