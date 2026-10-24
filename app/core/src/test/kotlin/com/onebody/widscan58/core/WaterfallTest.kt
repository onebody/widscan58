// GPL-3.0-or-later.
package com.onebody.widscan58.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class WaterfallTest {
    private var ms = 0L
    private val store = Store({ ms }).apply {
        setProfile(profileOf())
        setStatus(statusOf(5_658_000, 5_695_000)) // two hops per rotation
    }

    private fun comp(vararg db: Double, f0: Double = 5_658_000.0, f1: Double = 5_695_000.0): Composite =
        Composite(f0, f1, doubleArrayOf(*db), arrayOfNA(db.size), arrayOfNA(db.size), arrayOfNA(db.size))

    private fun arrayOfNA(n: Int) = DoubleArray(n) { Double.NaN }

    /** One full rotation of spectrum frames. */
    private fun rotation() {
        store.ingest(spectrumFrame(5_658_000, 200))
        store.ingest(spectrumFrame(5_695_000, 200))
    }

    @Test
    fun `a row lands when the rotation is complete`() {
        val wf = Waterfall(rowWidth = 4)
        val c = comp(-70.0, -70.0, -70.0, Double.NaN)
        store.ingest(spectrumFrame(5_658_000, 200))
        assertFalse("half a rotation is not a row", wf.capture(store, c, -85.0, 16))
        store.ingest(spectrumFrame(5_695_000, 200))
        assertTrue(wf.capture(store, c, -85.0, 16))
        assertEquals(1, wf.rows.size)
        // -90 dBFS is the bottom of the ramp: (-70 - -90) / 45 * 255 = 113
        assertEquals(113, wf.rows[0][0].toInt() and 0xff)
        assertEquals("no data stays at the surface colour", 0, wf.rows[0][3].toInt() and 0xff)
        assertFalse("no new frames, no new row", wf.capture(store, c, -85.0, 16))
    }

    @Test
    fun `the ramp follows the measured floor and only drifts towards it`() {
        val wf = Waterfall(rowWidth = 2)
        store.ingest(frame(FrameType.LEVELS, levelsOf(5_658_000, 2, specCode(-60.0), levelOf(0, rssiDb = -30.0, snrDb = 20.0))))
        rotation()
        // floor -60 -> lo -65: -65 is the bottom, -20 the top
        assertTrue(wf.capture(store, comp(-65.0, -20.0), -85.0, 16))
        assertEquals(0, wf.rows[0][0].toInt() and 0xff)
        assertEquals(255, wf.rows[0][1].toInt() and 0xff)

        store.ingest(frame(FrameType.LEVELS, levelsOf(5_695_000, 2, specCode(-50.0), levelOf(0, rssiDb = -30.0, snrDb = 20.0))))
        rotation()
        // median of {-60, -50} is the higher one, which the slow ema moves 10% towards: -59, so lo -64.
        assertTrue(wf.capture(store, comp(-64.0, -19.0), -85.0, 16))
        assertEquals(0, wf.rows[0][0].toInt() and 0xff)
        assertEquals(255, wf.rows[0][1].toInt() and 0xff)
    }

    @Test
    fun `rows are the newest first and trimmed to the plot height`() {
        val wf = Waterfall(rowWidth = 1)
        rotation()
        wf.capture(store, comp(-60.0), -85.0, 2)
        ms = 1_000
        rotation()
        wf.capture(store, comp(-70.0), -85.0, 2)
        ms = 2_000
        rotation()
        wf.capture(store, comp(-80.0), -85.0, 2)
        assertEquals(2, wf.rows.size)
        assertEquals("-80 with lo -90 is the newest row", 57, wf.rows[0][0].toInt() and 0xff)
        assertEquals(113, wf.rows[1][0].toInt() and 0xff)
        assertEquals(2.0, wf.rowTimes[0], 1e-9)
        assertEquals(1.0, wf.rowTimes[1], 1e-9)
    }

    @Test
    fun `a different span starts the waterfall over`() {
        val wf = Waterfall(rowWidth = 2)
        rotation()
        wf.capture(store, comp(-70.0, -70.0), -85.0, 8)
        rotation()
        wf.capture(store, comp(-70.0, -70.0, f1 = 5_700_000.0), -85.0, 8)
        assertEquals("the old rows do not line up with the new span", 1, wf.rows.size)
    }
}
