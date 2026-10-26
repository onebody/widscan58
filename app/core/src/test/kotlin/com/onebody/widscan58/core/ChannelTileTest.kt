// Channel tile derivation: level, bar against the floor, badges and the
// sparkline window. GPL-3.0-or-later.
package com.onebody.widscan58.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ChannelTileTest {
    private var ms = 0L
    private val store = Store({ ms })

    private fun levels(mode: Int, vararg l: IntArray) {
        store.ingest(frame(FrameType.LEVELS, levelsOf(5_658_000, mode, 0, *l)))
    }

    @Test
    fun `no tile before the profile arrives`() {
        assertNull(channelTile(store, 0, -1))
    }

    @Test
    fun `the bar spans ten dB below the floor to forty above it`() {
        store.setProfile(profileOf())
        // SNR mode: rssi -50 with a 20 dB SNR puts the floor at -70.
        levels(2, levelOf(id = 0, rssiDb = -50.0, snrDb = 20.0))
        val t = channelTile(store, 0, -1)!!
        assertEquals(-50.0, t.level!!, 1e-9)
        assertEquals(-70.0, t.floor!!, 1e-9)
        assertEquals(20.0, t.snr!!, 1e-9)
        assertEquals(0.6, t.bar!!, 1e-9)
        assertEquals("R1", t.name)
        assertEquals(5_658_000L, t.khz)
    }

    @Test
    fun `a mode without a floor falls back to the quietest level in view`() {
        store.setProfile(profileOf())
        levels(1, levelOf(id = 0, rssiDb = -60.0))
        ms += 100
        levels(1, levelOf(id = 0, rssiDb = -40.0))
        val t = channelTile(store, 0, -1)!!
        assertEquals(-40.0, t.level!!, 1e-9)
        assertNull(t.floor) // the device never measured one
        assertEquals(-60.0, t.sparkFloor, 1e-9)
        assertEquals(0.6, t.bar!!, 1e-9) // (-40 - (-70)) / 50
        assertEquals(-65.0, t.sparkLo, 1e-9)
        assertTrue(t.sparkHi >= -30.0)
    }

    @Test
    fun `a channel that stopped reporting is flagged stale`() {
        store.setProfile(profileOf())
        levels(1, levelOf(id = 0, rssiDb = -50.0))
        assertTrue(channelTile(store, 0, -1)!!.badges.isEmpty())
        ms += 1_500
        assertTrue(channelTile(store, 0, -1)!!.badges.any { it.kind == BadgeKind.STALE })
    }

    @Test
    fun `spurs read as masked with a peak and only flagged without one`() {
        store.setProfile(profileOf())
        val flags = ChannelFlag.SIGNAL or ChannelFlag.SPUR
        levels(1, levelOf(id = 0, rssiDb = -50.0, flags = flags))
        assertEquals(BadgeKind.SPUR, channelTile(store, 0, -1)!!.badges.last().kind)

        store.ingest(frame(FrameType.METRICS, metricsOf(MetricChannel(0, -50.0, flags = flags, peak = -30.0))))
        val masked = channelTile(store, 0, -1)!!.badges.first { it.kind == BadgeKind.SPUR_MASKED }
        assertEquals(BadgeTone.SERIOUS, masked.tone)
    }

    @Test
    fun `centre and bandwidth keep their measurements and only occupancy says whether to show them`() {
        store.setProfile(profileOf())
        store.ingest(
            frame(
                FrameType.METRICS,
                metricsOf(
                    MetricChannel(
                        id = 0, rssi = -50.0, flags = ChannelFlag.SIGNAL,
                        centroidHz = 150_000, obwHz = -6_000_000L..6_000_000L,
                    ),
                ),
            ),
        )
        val occupied = channelTile(store, 0, -1)!!
        assertTrue(occupied.occupied)
        assertTrue(occupied.smoothed)
        assertEquals(150_000L, occupied.centroidHz!!)
        assertEquals(12_000_000L, occupied.obwHz!!)

        // The device measures an unlocated channel too; the app shows an em dash.
        store.ingest(
            frame(
                FrameType.METRICS,
                metricsOf(
                    MetricChannel(
                        id = 0, rssi = -50.0, flags = ChannelFlag.SIGNAL or ChannelFlag.NO_CENTROID,
                        centroidHz = 150_000, obwHz = -6_000_000L..6_000_000L,
                    ),
                ),
            ),
        )
        val unlocated = channelTile(store, 0, -1)!!
        assertTrue(unlocated.occupied.not())
        assertEquals(150_000L, unlocated.centroidHz!!)
        assertEquals(12_000_000L, unlocated.obwHz!!)

        // A LEVELS frame carries neither, so there is nothing to show at all.
        levels(1, levelOf(id = 0, rssiDb = -50.0, flags = ChannelFlag.SIGNAL))
        val unmeasured = channelTile(store, 0, -1)!!
        assertTrue(unmeasured.occupied)
        assertTrue(unmeasured.smoothed.not())
        assertNull(unmeasured.centroidHz)
        assertNull(unmeasured.obwHz)
        assertNull(unmeasured.clearFloor)
    }

    @Test
    fun `a clear floor is missing until the channel has been quiet`() {
        store.setProfile(profileOf())
        store.ingest(frame(FrameType.METRICS, metricsOf(MetricChannel(id = 0, rssi = -50.0))))
        val busy = channelTile(store, 0, -1)!!
        assertTrue(busy.smoothed) // the mode measures it; this channel has simply never been clear
        assertNull(busy.clearFloor)

        store.ingest(frame(FrameType.METRICS, metricsOf(MetricChannel(id = 0, rssi = -50.0, clearFloor = -88.5))))
        assertEquals(-88.5, channelTile(store, 0, -1)!!.clearFloor!!, 1e-9)
    }

    @Test
    fun `a disabled channel keeps its readout and reports itself off`() {
        store.setProfile(profileOf())
        store.setStatus(statusOf(5_658_000L, enabled = 0b01))
        levels(1, levelOf(id = 0, rssiDb = -50.0), levelOf(id = 1, rssiDb = -55.0))
        assertTrue(channelTile(store, 0, -1)!!.enabled)
        assertTrue(channelTile(store, 1, -1)!!.enabled.not())
    }
}
