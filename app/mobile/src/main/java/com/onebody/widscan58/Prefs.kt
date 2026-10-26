// What the instrument remembers between runs: the channel plan and gain it
// pushes on connect, and the display choices. GPL-3.0-or-later.
package com.onebody.widscan58

import android.content.Context
import com.onebody.widscan58.core.Bands
import com.onebody.widscan58.core.DeviceCommands
import com.onebody.widscan58.core.DeviceSettings
import com.onebody.widscan58.core.GainMode
import com.onebody.widscan58.core.LoPlan
import com.onebody.widscan58.core.RH_DEFAULT_ENTER
import com.onebody.widscan58.core.RH_DEFAULT_EXIT
import com.onebody.widscan58.core.RH_SEATS
import com.onebody.widscan58.core.RhLapRules
import com.onebody.widscan58.core.RhSaved
import com.onebody.widscan58.core.RhSettings
import kotlin.math.roundToInt

/** [RhSettings]: the seats' thresholds and the lap rules, which the model keeps between runs. */
class Prefs(context: Context) : RhSettings {
    private val sp = context.getSharedPreferences("widscan58", Context.MODE_PRIVATE)

    /** The board is put in this state on every connect. */
    fun deviceSettings(): DeviceSettings = DeviceSettings.of(
        channels = channels,
        mode = loPlan,
        gainMode = gainMode,
        gain = gain,
        enabled = enabledMask,
    )

    val channels: List<String>
        get() = sp.getString("channels", "")!!.split(',').filter { it.isNotEmpty() }.ifEmpty { Bands.names("R") }

    /** Store a plan the device can tune; the band chips and the editor both write here. */
    fun setChannels(names: List<String>) = put("channels", Bands.normalize(names).joinToString(","))

    /** Which channel ids of the plan the device measures, as an 8-bit mask. */
    var enabledMask: Int
        get() = sp.getInt("enabled", 0xff) and 0xff
        set(value) = sp.edit().putInt("enabled", value and 0xff).apply()

    var loPlan: LoPlan
        get() = LoPlan.entries.firstOrNull { it.name == sp.getString("loPlan", LoPlan.GROUP.name) } ?: LoPlan.GROUP
        set(value) = sp.edit().putString("loPlan", value.name).apply()

    var gainMode: GainMode
        get() = GainMode.entries.firstOrNull { it.name == sp.getString("gainMode", GainMode.MANUAL.name) }
            ?: GainMode.MANUAL
        set(value) = sp.edit().putString("gainMode", value.name).apply()

    val gain: Int get() = sp.getInt("gain", 45)

    /** The ceiling is the device's own when it reports one; manual gain never leaves it. */
    fun setGain(gain: Int, max: Int = GAIN_MAX) =
        sp.edit().putInt("gain", gain.coerceIn(0, max.coerceAtLeast(0))).apply()

    var dbMin: Int
        get() = sp.getInt("dbMin", -85)
        set(value) = sp.edit().putInt("dbMin", value).apply()
    var dbMax: Int
        get() = sp.getInt("dbMax", -20)
        set(value) = sp.edit().putInt("dbMax", value).apply()
    var showMaxHold: Boolean
        get() = sp.getBoolean("showMax", true)
        set(value) = sp.edit().putBoolean("showMax", value).apply()
    var showAverage: Boolean
        get() = sp.getBoolean("showAvg", true)
        set(value) = sp.edit().putBoolean("showAvg", value).apply()

    // ---- the app itself ----

    /** Which language the app's own words are read in: the tag [AppLanguage] wraps the screen in. */
    var language: String
        get() = sp.getString("language", LANGUAGE_ZH)!!
        set(value) = put("language", value)

    /** The history card's window; the store's ring is deep enough for the widest choice. */
    var historySeconds: Int
        get() = sp.getInt("historySeconds", 30)
        set(value) = sp.edit().putInt("historySeconds", value).apply()

    /** The EMA when the device reports one, the raw RSSI when it does not. */
    var historySmoothed: Boolean
        get() = sp.getBoolean("historyEma", true)
        set(value) = sp.edit().putBoolean("historyEma", value).apply()

    // ---- RotorHazard mode ----

    /** What the model keeps between runs: the seats' thresholds and the lap rules. */
    override fun load(): RhSaved = RhSaved(
        enter = IntArray(RH_SEATS) { sp.getInt("rhEnter$it", RH_DEFAULT_ENTER) },
        exit = IntArray(RH_SEATS) { sp.getInt("rhExit$it", RH_DEFAULT_EXIT) },
        lapRules = RhLapRules(
            fastestOn = sp.getBoolean("rhFastestOn", true),
            count = sp.getInt("rhFastestCount", 3),
            minLapOn = sp.getBoolean("rhMinLapOn", true),
            minLapS = sp.getInt("rhMinLapS", 8).toDouble(),
        ),
    )

    override fun save(saved: RhSaved) {
        val e = sp.edit()
        saved.enter.forEachIndexed { j, v -> e.putInt("rhEnter$j", v) }
        saved.exit.forEachIndexed { j, v -> e.putInt("rhExit$j", v) }
        e.putBoolean("rhFastestOn", saved.lapRules.fastestOn)
        e.putInt("rhFastestCount", saved.lapRules.count)
        e.putBoolean("rhMinLapOn", saved.lapRules.minLapOn)
        e.putInt("rhMinLapS", saved.lapRules.minLapS.roundToInt())
        e.apply()
    }

    /** The seat the levels chart and the threshold steppers are on. */
    var rhSeat: Int
        get() = sp.getInt("rhSeat", 0)
        set(value) = sp.edit().putInt("rhSeat", value.coerceIn(0, RH_SEATS - 1)).apply()

    var rhSpanSeconds: Int
        get() = sp.getInt("rhSpan", 30)
        set(value) = sp.edit().putInt("rhSpan", value).apply()

    var rhShowCrossings: Boolean
        get() = sp.getBoolean("rhShowCrossings", false)
        set(value) = sp.edit().putBoolean("rhShowCrossings", value).apply()

    var rhShowFastest: Boolean
        get() = sp.getBoolean("rhShowFastest", true)
        set(value) = sp.edit().putBoolean("rhShowFastest", value).apply()

    /** Each lap's channel and time, spoken as the web UI's option does. */
    var voiceOn: Boolean
        get() = sp.getBoolean("voiceOn", false)
        set(value) = sp.edit().putBoolean("voiceOn", value).apply()

    /** The callout, in the web dialog's units: 0-100, and tenths of the normal rate and pitch. */
    var voiceVolume: Int
        get() = sp.getInt("voiceVolume", 90)
        set(value) = sp.edit().putInt("voiceVolume", value.coerceIn(0, 100)).apply()

    var voiceRate: Int
        get() = sp.getInt("voiceRate", 10)
        set(value) = sp.edit().putInt("voiceRate", value.coerceIn(1, 20)).apply()

    var voicePitch: Int
        get() = sp.getInt("voicePitch", 10)
        set(value) = sp.edit().putInt("voicePitch", value.coerceIn(5, 20)).apply()

    // ---- Power meter mode ----

    /** What sits between the VTX and the board: the meter adds it back to get the VTX's own power. */
    var powerAttenDb: Int
        get() = sp.getInt("powerAttenDb", 30)
        set(value) = sp.edit().putInt("powerAttenDb", value.coerceIn(0, POWER_ATTEN_MAX)).apply()

    /** Seconds of linear-power averaging; 0 shows the latest hop as it is. */
    var powerSmoothS: Double
        get() = sp.getFloat("powerSmoothS", 0.5f).toDouble()
        set(value) = sp.edit().putFloat("powerSmoothS", value.toFloat()).apply()

    private fun put(key: String, value: String) = sp.edit().putString(key, value).apply()

    companion object {
        /** The ceiling used until the board reports its own `gain_max`. */
        const val GAIN_MAX = DeviceCommands.GAIN_FALLBACK_MAX

        /** The largest attenuator the guide table rates anyone would fit. */
        const val POWER_ATTEN_MAX = 90

        /** The two languages the app ships, and the choice that leaves it to the phone. */
        const val LANGUAGE_ZH = "zh"
        const val LANGUAGE_EN = "en"
        const val LANGUAGE_SYSTEM = "system"
    }
}
