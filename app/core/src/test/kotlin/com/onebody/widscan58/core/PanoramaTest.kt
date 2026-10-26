// GPL-3.0-or-later.
package com.onebody.widscan58.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class PanoramaTest {
    private var ms = 0L
    private val store = Store({ ms }).apply { setProfile(profileOf()) }

    /** One column whose centre is exactly this frequency. */
    private fun probe(khz: Double): Composite = composite(store, khz - 0.5, khz + 0.5, 1)

    /** 5658000 covers bin centres 5638000..5677843.75 and 5695000 covers 5675000..5714843.75. */
    private fun twoSegments() {
        val a = IntArray(256) { specCode(-90.0) }
        a[237] = specCode(-40.0) // 5675000 kHz, in the overlap
        a[255] = specCode(-40.0) // 5677843.75 kHz, also in it
        store.ingest(spectrumFrame(5_658_000, *a))
        val b = IntArray(256) { specCode(-80.0) }
        b[0] = specCode(-20.0)
        b[18] = specCode(-20.0)
        store.ingest(spectrumFrame(5_695_000, *b))
    }

    @Test
    fun `every point comes from the segment whose lo is nearest`() {
        twoSegments()
        assertEquals(-40.0, probe(5_675_000.0).live[0], 1e-6) // the other LO has -20 at the same frequency
        assertEquals(-20.0, probe(5_677_843.75).live[0], 1e-6) // and there the higher LO is nearer
        assertEquals(-90.0, probe(5_640_000.0).live[0], 1e-6)
        assertEquals(-80.0, probe(5_714_000.0).live[0], 1e-6)
        assertTrue("nothing covers 5620000", probe(5_620_000.0).live[0].isNaN())
        assertTrue("nothing covers 5720000", probe(5_720_000.0).live[0].isNaN())
    }

    @Test
    fun `the band span comes from the segment extents`() {
        val empty = Store({ ms }).apply { setProfile(profileOf()) }
        assertEquals(listOf(5_621_000.0, 5_954_000.0), bandRange(empty).let { listOf(it.lo, it.hi) })
        twoSegments()
        val r = bandRange(store)
        assertEquals(5_638_000.0, r.lo, 1e-6) // bin 0 of the lowest LO
        assertEquals(5_715_000.0, r.hi, 1e-6) // past the last bin of the highest
    }

    @Test
    fun `holds and the per-lo noise floor come through the composite`() {
        twoSegments()
        val low = IntArray(256) { specCode(-100.0) }
        store.ingest(spectrumFrame(5_658_000, *low))
        store.ingest(frame(FrameType.LEVELS, levelsOf(5_658_000, 2, specCode(-95.0), levelOf(0, rssiDb = -50.0, snrDb = 20.0))))
        val c = probe(5_640_000.0)
        assertEquals(-100.0, c.live[0], 1e-6)
        assertEquals(-90.0, c.max[0], 1e-6) // held from the earlier sweep
        assertEquals(-95.0, c.floor[0], 1e-6)
        assertTrue("the other LO has no floor measured", probe(5_700_000.0).floor[0].isNaN())
    }
}
