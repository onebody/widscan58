// What the app knows about its own published builds — the update check without the network
// part, so the ordering rules can be tested against fixtures instead of GitHub. GPL-3.0-or-later.
package com.onebody.widscan58.core

/** A published APK the phone could install. */
data class AppRelease(
    val version: String,
    val title: String,
    val downloadUrl: String,
    /** Bytes, or -1 when the host did not say. */
    val size: Long,
    /** Bare lowercase hex, empty when no digest was published. */
    val sha256: String,
)

/** The tag the phone app is published under, so firmware releases are not mistaken for it. */
const val APP_TAG_PREFIX = "app-v"

private val VERSION = Regex("""\d+(\.\d+)*(-[0-9A-Za-z.]+)?""")
private val HEX64 = Regex("""[0-9a-f]{64}""")

fun isAppVersion(text: String): Boolean = VERSION.matches(text)

/** `app-v0.7` → `0.7`; any other tag has no version this app may compare itself to. */
fun appVersionOf(tag: String): String? =
    tag.takeIf { it.startsWith(APP_TAG_PREFIX) }
        ?.removePrefix(APP_TAG_PREFIX)
        ?.takeIf { isAppVersion(it) }

/**
 * Segment by segment as numbers, with a short tail read as zeros and anything after `-`
 * ignored: 0.10 is newer than 0.9, and `0.7-dev` is the same release as `0.7`.
 */
fun compareVersions(left: String, right: String): Int {
    val a = segments(left)
    val b = segments(right)
    for (i in 0 until maxOf(a.size, b.size)) {
        val x = a.getOrElse(i) { 0L }
        val y = b.getOrElse(i) { 0L }
        if (x != y) return if (x < y) -1 else 1
    }
    return 0
}

private fun segments(version: String): List<Long> =
    version.substringBefore('-').split('.').map { it.toLongOrNull() ?: 0L }

/** The highest version among [releases]; tags this app cannot read are not candidates. */
fun newestRelease(releases: List<AppRelease>): AppRelease? =
    releases.filter { isAppVersion(it.version) }.reduceOrNull { best, candidate ->
        if (compareVersions(candidate.version, best.version) > 0) candidate else best
    }

/** The build worth offering, or null when [installed] is already at or ahead of everything. */
fun upgradeFrom(installed: String, releases: List<AppRelease>): AppRelease? =
    newestRelease(releases)?.takeIf { compareVersions(it.version, installed) > 0 }

/** GitHub writes `sha256:<hex>`; the installer wants the hex on its own, and nothing else. */
fun digestHex(digest: String?): String =
    digest?.substringAfter(':', digest)?.trim()?.lowercase()?.takeIf { HEX64.matches(it) } ?: ""
