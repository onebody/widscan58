// What the instrument remembers between runs: the channel plan and gain it
// pushes on connect, and the display choices. GPL-3.0-or-later.
package com.onebody.widscan58

import android.content.Context
import com.onebody.widscan58.core.Bands
import com.onebody.widscan58.core.DeviceSettings
import com.onebody.widscan58.core.GainMode
import com.onebody.widscan58.core.LoPlan

class Prefs(context: Context) {
    private val sp = context.getSharedPreferences("widscan58", Context.MODE_PRIVATE)

    /** The board is put in this state on every connect. */
    fun deviceSettings(): DeviceSettings = DeviceSettings.of(
        channels = channels,
        mode = LoPlan.GROUP,
        gainMode = GainMode.MANUAL,
        gain = gain,
    )

    val channels: List<String>
        get() = sp.getString("channels", "")!!.split(',').filter { it.isNotEmpty() }.ifEmpty { Bands.names("R") }

    /** One-tap band selection: all eight channels of the band. */
    fun setBand(letter: String) = setChannels(Bands.names(letter))

    fun setChannels(names: List<String>) = put("channels", Bands.normalize(names).joinToString(","))

    val gain: Int get() = sp.getInt("gain", 45)

    fun setGain(gain: Int) = sp.edit().putInt("gain", gain.coerceIn(0, GAIN_MAX)).apply()

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

    private fun put(key: String, value: String) = sp.edit().putString(key, value).apply()

    companion object {
        /** The firmware's manual gain range. */
        const val GAIN_MAX = 63
    }
}
