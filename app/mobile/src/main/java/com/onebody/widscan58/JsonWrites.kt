// The inverse of JsonReplies: the store's decoded PROFILE and STATUS written back as the
// same JSON lines the board sends. A recording begins with them, and the simulator puts them
// on its wire, so either rebuilds the instrument in this app or the browser UI with no command.
// GPL-3.0-or-later.
package com.onebody.widscan58

import com.onebody.widscan58.core.PlanHop
import com.onebody.widscan58.core.Profile
import com.onebody.widscan58.core.RhConfig
import com.onebody.widscan58.core.Status
import com.onebody.widscan58.core.Store
import org.json.JSONArray
import org.json.JSONObject

object JsonWrites {
    fun profile(p: Profile): String = JSONObject().apply {
        put("name", p.name)
        put("description", p.description)
        put("target", p.target)
        put("band_khz", JSONArray(p.bandKhz.toList()))
        put("base_khz", p.baseKhz)
        put("equiv_base_khz", p.equivBaseKhz)
        put("scale", p.scale)
        put("rate_hz", p.rateHz)
        put("usable_hz", p.usableHz)
        put("passband_hz", p.passbandHz)
        put("rssi_hz", p.rssiHz)
        put("search_hz", p.searchHz)
        put(
            "channels",
            JSONArray(p.channels.map { JSONObject().put("id", it.id).put("name", it.name).put("khz", it.khz) }),
        )
    }.toString()

    fun status(s: Status): String = JSONObject().apply {
        put("profile", s.profile)
        put("running", s.running)
        put("enabled", s.enabled)
        put("mode", s.mode)
        put("capture_samples", s.captureSamples)
        put("nfft", s.nfft)
        put("segments", s.segments)
        put("rate_code", s.rateCode)
        put("filter", s.filter)
        put("conjugate", s.conjugate)
        put("iq_corr", s.iqCorr)
        put("settle_us", s.settleUs)
        s.tskip?.let { put("tskip", it) }
        put("gain_mode", s.gainMode)
        put("gain", s.gain)
        put("gain_max", s.gainMax)
        s.rawiqMax?.let { put("rawiq_max", it) }
        put("recal", s.recal)
        s.metrics?.let { put("metrics", it.name) }
        put("rssi_hz", s.rssiHz)
        put("search_hz", s.searchHz)
        put("dc_guard", s.dcGuard)
        put("ema", s.ema)
        put("clear_ema", s.clearEma)
        put("signal_db", s.signalDb)
        put("avg_shift", s.avgShift)
        put("spec_every", s.specEvery)
        put("spec_bins", s.specBins)
        put("spec_span_hz", s.specSpanHz)
        put("spec_detector", s.specDetector)
        put("plan", JSONArray(s.plan.map { planHop(it) }))
    }.toString()

    private fun planHop(h: PlanHop) = JSONObject()
        .put("lo_khz", h.loKhz)
        .put("ch", JSONArray(h.ch.toList()))
        .put("offset_hz", JSONArray(h.offsetHz.toList()))

    /** The simulator's CONFIG? body: the keys [JsonReplies.rhConfig] reads, named as the board names them. */
    fun rhConfig(c: RhConfig): String = JSONObject().apply {
        put("gain", c.gain)
        put("gain_max", c.gainMax)
        put("gain_mode", c.gainMode.name)
        put("nfft", c.nfft)
        put("mode", c.mode.name)
        put("metrics", c.metric.name)
        put("level_filter", c.levelFilter.name)
    }.toString()

    /** What a recording starts with, in the order the board sent it. */
    fun sessionPreamble(store: Store): List<String> = buildList {
        store.profile?.let { add("PROFILE ${profile(it)}") }
        store.status?.let { add("STATUS ${status(it)}") }
        if (store.spursKhz.isNotEmpty()) {
            val values = store.spursKhz.joinToString(" ") { k -> if (k % 1.0 == 0.0) k.toLong().toString() else "$k" }
            add("SPURS ${store.spursKhz.size} $values")
        }
    }
}
