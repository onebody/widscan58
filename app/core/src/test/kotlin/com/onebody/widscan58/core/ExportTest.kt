// GPL-3.0-or-later.
package com.onebody.widscan58.core

import org.junit.Assert.assertEquals
import org.junit.Test

class ExportTest {
    private var ms = 0L
    private val clock = { ms }

    /** The columns after id, name, freq and enabled: a channel with no metrics yet leaves them all empty. */
    private val blanks = ",".repeat(CHANNEL_BLANKS)

    private fun store(vararg channels: ProfileChannel): Store {
        val s = Store(clock)
        s.setProfile(profileOf(*channels))
        return s
    }

    @Test
    fun `a channel row is the header order and the device's own numbers`() {
        val s = store(channel(0), channel(1), channel(2))
        s.ingest(
            frame(
                FrameType.METRICS,
                metricsOf(
                    MetricChannel(
                        id = 2, rssi = -55.0, flags = ChannelFlag.SIGNAL, rssiEma = -54.5,
                        floor = -90.0, snr = 35.0, clearFloor = -88.0, peak = -52.0,
                        centroidHz = -150_000, obwHz = -4_500_000L..4_500_000L,
                    ),
                ),
            ),
        )
        ms = 500

        assertEquals(
            listOf(
                HEADER,
                "0,R1,5658000,1$blanks",
                "1,R2,5695000,1$blanks",
                "2,R3,5732000,1,1,-55.00,-54.50,-90.00,35.00,-88.00,-52.00,0,-150000,-4500000,4500000,0.50",
            ),
            channelsCsv(s).trim().lines(),
        )
    }

    @Test
    fun `a disabled channel says so`() {
        val s = store(channel(0), channel(1))
        s.setStatus(statusOf(5_658_000, enabled = 0b01))
        assertEquals(
            listOf("0,R1,5658000,1$blanks", "1,R2,5695000,0$blanks"),
            channelsCsv(s).trim().lines().drop(1),
        )
    }

    @Test
    fun `a plan name with a comma is quoted the way a spreadsheet reads it`() {
        val s = store(channel(0, name = "my,own"))
        assertEquals("0,\"my,own\",5658000,1$blanks", channelsCsv(s).trim().lines().last())
    }

    @Test
    fun `no profile means a header and nothing else`() {
        assertEquals(listOf(HEADER), channelsCsv(Store(clock)).trim().lines())
    }

    @Test
    fun `spectrum rows are the panorama's own points across the measured span`() {
        val s = store(channel(0))
        s.ingest(spectrumFrame(5_658_000, specCode(-50.0), specCode(-70.0)))

        assertEquals(
            listOf(
                "freq_khz,live_dbfs,max_hold_dbfs,avg_dbfs,noise_floor_dbfs",
                "5638052.083,-50.00,-50.00,-50.00,",
                "5638156.250,-70.00,-70.00,-70.00,",
                // no segment covers the third point, so it stays empty rather than inventing a level
                "5638260.417,,,,",
            ),
            spectrumCsv(s, columns = 3).trim().lines(),
        )
    }

    @Test
    fun `the holds travel into the export with the live trace`() {
        val s = store(channel(0))
        s.ingest(spectrumFrame(5_658_000, specCode(-40.0), specCode(-40.0)))
        s.ingest(spectrumFrame(5_658_000, specCode(-80.0), specCode(-80.0)))

        assertEquals(
            // the average decays in the power domain, so it still sits near the loud first frame
            listOf("5638052.083,-80.00,-40.00,-40.22,", "5638156.250,-80.00,-40.00,-40.22,", "5638260.417,,,,"),
            spectrumCsv(s, columns = 3).trim().lines().drop(1),
        )
    }

    @Test
    fun `an empty instrument exports its header at the profile band`() {
        val s = store(channel(0))
        assertEquals(
            listOf("5704250.000,,,,", "5870750.000,,,,"),
            spectrumCsv(s, columns = 2).trim().lines().drop(1),
        )
    }

    companion object {
        private const val CHANNEL_BLANKS = 12
        private const val HEADER =
            "id,name,freq_khz,enabled,flags,rssi_dbfs,rssi_ema_dbfs,noise_dbfs,snr_db,clear_floor_dbfs," +
                "peak_dbfs,peak_offset_hz,centroid_offset_hz,obw_lo_hz,obw_hi_hz,age_s"
    }
}
