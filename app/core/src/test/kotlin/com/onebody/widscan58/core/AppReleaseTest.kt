// GPL-3.0-or-later.
package com.onebody.widscan58.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

private const val HEX = "345582b4000837630b18472be984adb378ce7ac8e0166ddc7a091884fbf22ff5"

private fun release(version: String, sha: String = HEX) =
    AppRelease(version, "app v$version", "https://example.invalid/$version.apk", 1_540_006, sha)

class AppReleaseTest {
    @Test
    fun `only the phone app's own tags carry a version`() {
        assertEquals("0.7", appVersionOf("app-v0.7"))
        assertEquals("1.2.3", appVersionOf("app-v1.2.3"))
        assertEquals("0.8-beta2", appVersionOf("app-v0.8-beta2"))
        assertNull("the firmware is tagged without the app prefix", appVersionOf("v0.7"))
        assertNull(appVersionOf("app-v"))
        assertNull(appVersionOf("app-v0.7."))
    }

    @Test
    fun `versions compare as numbers, not as text`() {
        assertEquals(0, compareVersions("0.7", "0.7"))
        assertTrue("0.10 is the tenth release, not the ninth's cousin", compareVersions("0.10", "0.9") > 0)
        assertTrue(compareVersions("1.2.1", "1.2") > 0)
        assertEquals("a missing tail segment is zero", 0, compareVersions("1.0", "1"))
        assertTrue(compareVersions("2", "10") < 0)
    }

    @Test
    fun `a dev suffix says nothing about order`() {
        assertEquals(0, compareVersions("0.7-dev", "0.7"))
        assertTrue(compareVersions("0.7-dev", "0.6") > 0)
    }

    @Test
    fun `the digest arrives prefixed and leaves bare`() {
        assertEquals(HEX, digestHex("sha256:$HEX"))
        assertEquals("bare hex needs no help", HEX, digestHex(HEX.uppercase()))
        assertEquals("an absent or malformed digest is no digest", "", digestHex("sha256:"))
        assertEquals("", digestHex(null))
    }

    @Test
    fun `the newest of a list is the highest version`() {
        val releases = listOf(release("0.7"), release("0.9"), release("0.10"), release("not-a-version"))
        assertEquals("0.10", newestRelease(releases)?.version)
        assertNull(newestRelease(emptyList()))
    }

    @Test
    fun `a re-published version keeps the first entry, which is the newer publish`() {
        val first = release("0.8", HEX)
        val second = release("0.8", "")
        assertEquals(first, newestRelease(listOf(first, second)))
    }

    @Test
    fun `an update is offered only when the published build is ahead`() {
        assertEquals("0.8", upgradeFrom("0.7", listOf(release("0.8")))?.version)
        assertNull("installed build is the one published", upgradeFrom("0.7", listOf(release("0.7"))))
        assertNull("a development build is never behind itself", upgradeFrom("0.8-dev", listOf(release("0.7"))))
        assertNull(upgradeFrom("0.7", emptyList()))
    }
}
