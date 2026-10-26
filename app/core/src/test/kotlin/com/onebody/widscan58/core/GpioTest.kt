// GPL-3.0-or-later.
package com.onebody.widscan58.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class GpioTest {
    @Test
    fun `a reply is one character per pin, in pin order`() {
        val pins = parseGpio(4, "-LHx", "--LH")
        assertEquals(listOf(GpioSetting.NONE, GpioSetting.LOW, GpioSetting.HIGH, GpioSetting.RESERVED), pins?.map { it.active })
        assertEquals(listOf(0, 1, 2, 3), pins?.map { it.n })
        assertEquals(listOf(GpioSetting.NONE, GpioSetting.NONE, GpioSetting.LOW, GpioSetting.HIGH), pins?.map { it.saved })
    }

    @Test
    fun `a reply that disagrees with its own count is dropped whole`() {
        assertNull(parseGpio(3, "-LH", "-L"))
        assertNull(parseGpio(3, "-L", "-LH"))
        assertNull(parseGpio(2, "-Z", "--"))
        assertNull(parseGpio(2, "--", "-z"))
        assertNull(parseGpio(-1, "", ""))
        assertEquals(emptyList<GpioPin>(), parseGpio(0, "", ""))
    }

    @Test
    fun `only a pin out of step with its saved setting needs saving`() {
        val pins = parseGpio(3, "L-x", "--x")
        assertEquals(listOf(0), gpioUnsaved(pins.orEmpty()).map { it.n })
        assertEquals(listOf("GPIO 0 NONE"), gpioUnsaved(pins.orEmpty()).map { DeviceCommands.gpioPin(it.n, it.saved) })
        assertEquals(emptyList<GpioPin>(), gpioUnsaved(parseGpio(3, "--x", "--x").orEmpty()))
    }

    @Test
    fun `the setting word is the one the firmware scans`() {
        assertEquals("GPIO 26 HIGH", DeviceCommands.gpioPin(26, GpioSetting.HIGH))
        assertEquals("GPIO 26 LOW", DeviceCommands.gpioPin(26, GpioSetting.LOW))
        assertEquals("GPIO 4 NONE", DeviceCommands.gpioPin(4, GpioSetting.NONE))
    }
}
