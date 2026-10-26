// GPL-3.0-or-later.
package com.onebody.widscan58.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class DeviceCommandsTest {
    @Test
    fun `manual gain is clamped to what the device reports and hardware gain asks for nothing`() {
        assertEquals("GAIN MANUAL 45", DeviceCommands.gain(GainMode.MANUAL, 45, 63))
        assertEquals("GAIN MANUAL 63", DeviceCommands.gain(GainMode.MANUAL, 900, 63))
        assertEquals("GAIN MANUAL 0", DeviceCommands.gain(GainMode.MANUAL, -7, 63))
        assertEquals("GAIN HARDWARE", DeviceCommands.gain(GainMode.HARDWARE, 45, 63))
    }

    @Test
    fun `a missing device ceiling falls back to the phy table depth, not to zero`() {
        assertEquals("GAIN MANUAL 60", DeviceCommands.gain(GainMode.MANUAL, 60, null))
        assertEquals("GAIN MANUAL 82", DeviceCommands.gain(GainMode.MANUAL, 90, null))
        assertEquals("GAIN MANUAL 0", DeviceCommands.gain(GainMode.MANUAL, 5, 0))
    }

    @Test
    fun `filter minus one hands the choice to the phy`() {
        assertEquals("FILTER AUTO", DeviceCommands.filter(DeviceCommands.FILTER_AUTO))
        assertEquals("FILTER AUTO", DeviceCommands.filter(-9))
        assertEquals("FILTER 60", DeviceCommands.filter(60))
        assertEquals("FILTER 63", DeviceCommands.filter(99))
    }

    @Test
    fun `fft, capture and display-bin picks snap to a size the firmware knows`() {
        assertEquals("FFT 256", DeviceCommands.fft(256))
        assertEquals("FFT 512", DeviceCommands.fft(400))
        assertEquals("FFT 64", DeviceCommands.fft(1))
        assertEquals("FFT 2048", DeviceCommands.fft(99_999))
        assertEquals("CAPN 16380", DeviceCommands.capture(16000))
        assertEquals("CAPN 256", DeviceCommands.capture(10))
        assertEquals("SPEC 4 1024 40000000 MAX", DeviceCommands.spectrum(4, 1000, 40_000_000, true))
    }

    @Test
    fun `segments, hops-per-frame and settle stay inside the device ranges`() {
        assertEquals("SEGMENTS 8", DeviceCommands.segments(8))
        assertEquals("SEGMENTS 64", DeviceCommands.segments(640))
        assertEquals("SEGMENTS 1", DeviceCommands.segments(0))
        assertEquals("SPEC 0 256 40000000 MEAN", DeviceCommands.spectrum(0, 256, 40_000_000, false))
        assertEquals("SPEC 255 64 100000 MAX", DeviceCommands.spectrum(300, 64, 1, true))
        assertEquals("SETTLE 40", DeviceCommands.settle(40))
        assertEquals("SETTLE 20000", DeviceCommands.settle(99_999))
        assertEquals("SETTLE 0", DeviceCommands.settle(-5))
    }

    @Test
    fun `metrics and lo plan send the enum names the parser reads back`() {
        assertEquals("METRICS FULL", DeviceCommands.metrics(MetricsMode.FULL))
        assertEquals("METRICS RSSI", DeviceCommands.metrics(MetricsMode.RSSI))
        assertEquals("METRICS SNR", DeviceCommands.metrics(MetricsMode.SNR))
        assertEquals("MODE GROUP", DeviceCommands.mode(LoPlan.GROUP))
        assertEquals("MODE SINGLE", DeviceCommands.mode(LoPlan.SINGLE))
    }

    @Test
    fun `windows are device hertz and cannot go below one bin or above the band`() {
        assertEquals("WIN 5000000 12000000", DeviceCommands.windows(5_000_000, 12_000_000))
        assertEquals("WIN 10000 20000000", DeviceCommands.windows(1, 99_999_999))
    }

    @Test
    fun `smoothing factors go out as thousandths and never as zero`() {
        assertEquals("EMA 800 50", DeviceCommands.ema(0.8, 0.05))
        assertEquals("EMA 1000 1", DeviceCommands.ema(1.0, 0.0))
        assertEquals("EMA 1 1", DeviceCommands.ema(0.0, 0.0))
    }

    @Test
    fun `tune skip bits toggle against the mask the device reported`() {
        assertEquals("TUNESKIP 1", DeviceCommands.tuneSkip(3, 2))
        assertEquals("TUNESKIP 3", DeviceCommands.tuneSkip(1, 2))
        assertEquals("TUNESKIP 0", DeviceCommands.tuneSkip(7 and 7, 7))
        assertEquals("TUNESKIP 7", DeviceCommands.tuneSkip(0, 1 or 2 or 4))
    }

    @Test
    fun `recal mode reads back whatever casing the firmware used`() {
        assertEquals(RecalMode.CACHE, RecalMode.of("cache"))
        assertEquals(RecalMode.EVERY, RecalMode.of("EVERY"))
        assertNull(RecalMode.of("boot"))
        assertNull(RecalMode.of(null))
        assertEquals("RECALMODE ONCE", DeviceCommands.recalMode(RecalMode.ONCE))
        assertEquals("RECAL", DeviceCommands.RECAL)
    }

    @Test
    fun `orientation, iq correction and the enable mask are eight bits wide`() {
        assertEquals("CONJ 1", DeviceCommands.conjugate(true))
        assertEquals("CONJ 0", DeviceCommands.conjugate(false))
        assertEquals("IQCORR 1", DeviceCommands.iqCorrection(true))
        assertEquals("ENABLE 255", DeviceCommands.enable(0xff))
        assertEquals("ENABLE 5", DeviceCommands.enable(5))
        assertEquals("ENABLE 255", DeviceCommands.enable(0x1ff))
    }

    @Test
    fun `the channel plan command is the band table one`() {
        assertEquals(
            "CHANNELS R1=5658 R2=5695",
            DeviceCommands.channelPlan(listOf("R2", "R1")),
        )
    }
}
