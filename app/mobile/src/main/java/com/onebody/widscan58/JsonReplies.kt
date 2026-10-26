// PROFILE, STATUS and GPIO replies are JSON; this is the bridge to the pure-Kotlin model.
// GPL-3.0-or-later.
package com.onebody.widscan58

import com.onebody.widscan58.core.DeviceCommands
import com.onebody.widscan58.core.GainMode
import com.onebody.widscan58.core.GpioPin
import com.onebody.widscan58.core.LoPlan
import com.onebody.widscan58.core.MetricsMode
import com.onebody.widscan58.core.PlanHop
import com.onebody.widscan58.core.Profile
import com.onebody.widscan58.core.ProfileChannel
import com.onebody.widscan58.core.ReplyCodec
import com.onebody.widscan58.core.RhConfig
import com.onebody.widscan58.core.RhLapFilter
import com.onebody.widscan58.core.RhMetric
import com.onebody.widscan58.core.Status
import com.onebody.widscan58.core.parseGpio
import org.json.JSONArray
import org.json.JSONObject

private fun JSONObject.ints(name: String): JSONArray? = optJSONArray(name)

private fun JSONArray.longs(): LongArray = LongArray(length()) { getLong(it) }

private fun JSONObject.longOrNull(name: String): Long? = if (has(name)) optLong(name) else null

/** null for an absent or malformed reply; the previous state then stands. */
class JsonReplies : ReplyCodec {
    override fun profile(json: String): Profile? = runCatching {
        val o = JSONObject(json)
        val band = o.ints("band_khz")
        val channels = o.ints("channels") ?: JSONArray()
        Profile(
            name = o.getString("name"),
            description = o.optString("description"),
            target = o.optString("target"),
            bandKhz = if (band != null && band.length() >= 2) band.longs() else longArrayOf(0, 0),
            baseKhz = o.optLong("base_khz"),
            equivBaseKhz = o.optLong("equiv_base_khz"),
            scale = o.optDouble("scale", 1.0),
            rateHz = o.optLong("rate_hz"),
            usableHz = o.optLong("usable_hz"),
            passbandHz = o.optLong("passband_hz"),
            rssiHz = o.optLong("rssi_hz"),
            searchHz = o.optLong("search_hz"),
            channels = List(channels.length()) {
                val c = channels.getJSONObject(it)
                ProfileChannel(c.getInt("id"), c.getString("name"), c.getLong("khz"))
            },
        )
    }.getOrNull()

    override fun status(json: String): Status? = runCatching {
        val o = JSONObject(json)
        val plan = o.ints("plan") ?: JSONArray()
        Status(
            profile = o.optString("profile"),
            running = o.optInt("running"),
            enabled = o.optInt("enabled"),
            mode = o.optString("mode", "GROUP"),
            captureSamples = o.optInt("capture_samples"),
            nfft = o.optInt("nfft"),
            segments = o.optInt("segments"),
            rateCode = o.optInt("rate_code"),
            filter = o.optInt("filter"),
            conjugate = o.optInt("conjugate"),
            iqCorr = o.optInt("iq_corr"),
            settleUs = o.optInt("settle_us"),
            tskip = o.longOrNull("tskip")?.toInt(),
            gainMode = o.optString("gain_mode", "MANUAL"),
            gain = o.optInt("gain"),
            gainMax = o.optInt("gain_max"),
            rawiqMax = o.longOrNull("rawiq_max")?.toInt(),
            recal = o.optString("recal"),
            metrics = o.optString("metrics").takeIf { it.isNotEmpty() }?.let { m ->
                MetricsMode.entries.firstOrNull { it.name == m }
            },
            rssiHz = o.optLong("rssi_hz"),
            searchHz = o.optLong("search_hz"),
            dcGuard = o.optInt("dc_guard"),
            ema = o.optDouble("ema", 0.0),
            clearEma = o.optDouble("clear_ema", 0.0),
            signalDb = o.optDouble("signal_db", 0.0),
            avgShift = o.optInt("avg_shift"),
            specEvery = o.optInt("spec_every"),
            specBins = o.optInt("spec_bins"),
            specSpanHz = o.optLong("spec_span_hz"),
            specDetector = o.optInt("spec_detector"),
            plan = List(plan.length()) {
                val h = plan.getJSONObject(it)
                val ch = h.ints("ch") ?: JSONArray()
                val off = h.ints("offset_hz") ?: JSONArray()
                PlanHop(h.optLong("lo_khz"), IntArray(ch.length()) { k -> ch.getInt(k) }, off.longs())
            },
        )
    }.getOrNull()

    /** The pin settings are one character each, so the strict shape check belongs to :core. */
    override fun gpio(json: String): List<GpioPin>? = runCatching {
        val o = JSONObject(json)
        parseGpio(o.getInt("count"), o.getString("active"), o.getString("saved"))
    }.getOrNull()

    /**
     * CONFIG?'s body: the same JSON as STATUS, since one reply serves both. Firmware before
     * LEVELFILTER has no `level_filter`, and that node only ever ran the median. A body whose
     * metric is neither RSSI nor SNR is monitor mode's, not a RotorHazard configuration.
     */
    fun rhConfig(json: String): RhConfig? = runCatching {
        val o = JSONObject(json)
        RhConfig(
            gain = o.optInt("gain"),
            gainMax = o.optInt("gain_max", DeviceCommands.GAIN_FALLBACK_MAX),
            gainMode = if (o.optString("gain_mode", "MANUAL") == "HARDWARE") GainMode.HARDWARE else GainMode.MANUAL,
            nfft = o.optInt("nfft"),
            mode = if (o.optString("mode", "GROUP") == "SINGLE") LoPlan.SINGLE else LoPlan.GROUP,
            metric = RhMetric.of(o.optString("metrics")) ?: error("metrics ${o.optString("metrics")}"),
            levelFilter = RhLapFilter.entries.firstOrNull { it.name == o.optString("level_filter") } ?: RhLapFilter.MEDIAN,
        )
    }.getOrNull()
}
