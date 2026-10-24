// GPL-3.0-or-later.
package com.onebody.widscan58.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class BandsTest {
    @Test
    fun `channel names resolve to their chart frequency`() {
        assertEquals(5658, Bands.mhz("R1"))
        assertEquals(5917, Bands.mhz("R8"))
        assertEquals(5362, Bands.mhz("L1"))
        assertNull(Bands.mhz("R9"))
        assertNull(Bands.mhz("Z1"))
        assertNull(Bands.mhz("1"))
    }

    @Test
    fun `a raceband plan keeps chart order, ascending by frequency`() {
        val plan = Bands.normalize(Bands.names("R"))
        assertEquals(listOf("R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8"), plan)
    }

    @Test
    fun `unknown names drop out and the plan is capped at eight in input order`() {
        val plan = Bands.normalize(listOf("R1", "R1", "NOPE", "A1", "B1", "E1", "F1", "L1", "R3", "R4", "R5"))
        // R5 is the ninth known name, so it is the one that goes before sorting.
        assertEquals(listOf("L1", "R1", "E1", "R3", "B1", "F1", "R4", "A1"), plan)
    }

    @Test
    fun `the channels command sends every name with its megahertz`() {
        assertEquals(
            "CHANNELS R1=5658 R2=5695 R3=5732 R4=5769 R5=5806 R6=5843 R7=5880 R8=5917",
            Bands.channelsCommand(Bands.names("R")),
        )
    }

    @Test
    fun `restore commands set the plan, the lo mode and the gain`() {
        val s = DeviceSettings.of(listOf("R1", "R2"), LoPlan.SINGLE, GainMode.MANUAL, 40)
        assertEquals(
            listOf("CHANNELS R1=5658 R2=5695", "MODE SINGLE", "GAIN MANUAL 40"),
            s.restoreCommands(),
        )
    }

    @Test
    fun `an empty plan falls back to raceband and a bad gain to the default`() {
        val s = DeviceSettings.of(emptyList(), LoPlan.GROUP, GainMode.HARDWARE, -1)
        assertEquals(Bands.names("R"), s.channels)
        assertEquals(45, s.gain)
        assertEquals("GAIN HARDWARE", s.restoreCommands()[2])
    }
}
