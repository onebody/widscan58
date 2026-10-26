// The poller thread writes these rings while the screen reads them, so a render that starts
// before an append lands and finishes after it must still complete. GPL-3.0-or-later.
package com.onebody.widscan58.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class RingSafetyTest {
    private var ms = 0L

    @Test
    fun `walking the console log does not break when the board appends mid-walk`() {
        val store = Store({ ms })
        store.addLog(LogDir.RX, "first")
        val walk = store.log.iterator()

        store.addLog(LogDir.TX, "appended while we were still reading")

        assertEquals("first", walk.next().text)
        assertEquals(2, store.log.size)
    }

    @Test
    fun `walking the event list does not break when a poll appends mid-walk`() {
        val model = model()
        model.events += RhEvent(0.0, 0, RhEventKind.ENTER)
        val walk = model.events.iterator()

        model.events += RhEvent(1.0, 1, RhEventKind.PASS)

        assertEquals(0, walk.next().seat)
        assertEquals(2, model.events.size)
    }

    @Test
    fun `walking a seat's peaks does not break when a poll appends mid-walk`() {
        val seat = model().seats[0]
        seat.peaks += RhPeak(0.0, 150, peak = true)
        val walk = seat.peaks.iterator()

        seat.peaks += RhPeak(1.0, 160, peak = true)

        assertEquals(150, walk.next().level)
        assertTrue(seat.peaks.isNotEmpty())
    }

    private fun model() = RhModel(Store({ ms }), RingSettings) { RhMetric.RSSI }

    private object RingSettings : RhSettings {
        override fun load(): RhSaved = RhSaved()

        override fun save(saved: RhSaved) {}
    }
}
