// 5.8 GHz FPV bands and the device settings the app restores on connect.
// Mirrors web/src/model/channelPlan.ts. GPL-3.0-or-later.
package com.onebody.widscan58.core

class Band(val letter: String, val name: String, val mhz: IntArray)

/** The firmware scans at most this many channels. */
const val MAX_CHANNELS = 8

object Bands {
    // Raceband first, then the order of the usual FPV frequency charts.
    val ALL: List<Band> = listOf(
        Band("R", "Raceband", intArrayOf(5658, 5695, 5732, 5769, 5806, 5843, 5880, 5917)),
        Band("A", "Boscam A", intArrayOf(5865, 5845, 5825, 5805, 5785, 5765, 5745, 5725)),
        Band("B", "Boscam B", intArrayOf(5733, 5752, 5771, 5790, 5809, 5828, 5847, 5866)),
        Band("E", "Boscam E", intArrayOf(5705, 5685, 5665, 5645, 5885, 5905, 5925, 5945)),
        Band("F", "Fatshark", intArrayOf(5740, 5760, 5780, 5800, 5820, 5840, 5860, 5880)),
        Band("L", "Low band", intArrayOf(5362, 5399, 5436, 5473, 5510, 5547, 5584, 5621)),
    )

    fun byLetter(letter: String): Band? = ALL.find { it.letter == letter }

    /** All eight names of a band, in chart order. */
    fun names(letter: String): List<String> {
        val b = byLetter(letter) ?: return emptyList()
        return (1..b.mhz.size).map { "${b.letter}$it" }
    }

    /** Frequency (MHz) of a channel name such as "R1", or null. */
    fun mhz(name: String): Int? {
        val band = byLetter(name.take(1)) ?: return null
        val n = name.drop(1).toIntOrNull() ?: return null
        if (n < 1 || n > band.mhz.size) return null
        return band.mhz[n - 1]
    }

    /** Known names only, no duplicates, at most MAX_CHANNELS, sorted by frequency
     *  (ties keep chart order) so channel ids run up the band like the panorama. */
    fun normalize(names: List<String>): List<String> {
        val known = names.distinct().filter { mhz(it) != null }.take(MAX_CHANNELS)
        return known.sortedWith(
            compareBy({ mhz(it)!! }, { CHART_ORDER.indexOf(it) })
        )
    }

    /** CHANNELS command for a plan: each channel by its 5.8 GHz frequency. */
    fun channelsCommand(names: List<String>): String =
        "CHANNELS " + normalize(names).joinToString(" ") { "$it=${mhz(it)}" }

    private val CHART_ORDER: List<String> = ALL.flatMap { b -> (1..b.mhz.size).map { "${b.letter}$it" } }
}

enum class LoPlan { GROUP, SINGLE }
enum class GainMode { MANUAL, HARDWARE }

class DeviceSettings(
    val channels: List<String>,
    val mode: LoPlan,
    val gainMode: GainMode,
    val gain: Int,
) {
    companion object {
        val DEFAULT = DeviceSettings(
            channels = Bands.names("R"),
            mode = LoPlan.GROUP,
            gainMode = GainMode.MANUAL,
            gain = 45,
        )

        /** Keep a plan the device can actually tune: known channels, at most 8. */
        fun of(channels: List<String>, mode: LoPlan, gainMode: GainMode, gain: Int): DeviceSettings {
            val plan = Bands.normalize(channels).ifEmpty { DEFAULT.channels }
            return DeviceSettings(plan, mode, gainMode, if (gain < 0) DEFAULT.gain else gain)
        }
    }
}

/** Commands that put a freshly connected (or rebooted) device in the stored state. */
fun DeviceSettings.restoreCommands(): List<String> = listOf(
    Bands.channelsCommand(channels),
    "MODE ${mode.name}",
    if (gainMode == GainMode.MANUAL) "GAIN MANUAL $gain" else "GAIN HARDWARE",
)
