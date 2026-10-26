// CSV of what the board measured, for a spreadsheet. The web UI has no export at
// all; a phone is the one place a reading can leave the app. GPL-3.0-or-later.
package com.onebody.widscan58.core

import java.util.Locale

/** The panorama is drawn at a few hundred px; a thousand points keeps the shape without a huge file. */
const val SPECTRUM_COLUMNS = 1000

/** One row per plan channel: every metric the device reported for it on its last hop. */
fun channelsCsv(store: Store): String {
    val out = StringBuilder()
    row(
        out, "id", "name", "freq_khz", "enabled", "flags", "rssi_dbfs", "rssi_ema_dbfs",
        "noise_dbfs", "snr_db", "clear_floor_dbfs", "peak_dbfs", "peak_offset_hz",
        "centroid_offset_hz", "obw_lo_hz", "obw_hi_hz", "age_s",
    )
    val now = store.now()
    store.profile?.channels?.forEachIndexed { i, c ->
        val state = store.channels.getOrNull(i)
        val m = state?.latest
        row(
            out,
            i,
            c.name,
            c.khz,
            if (store.status?.let { it.enabled and (1 shl i) != 0 } ?: true) 1 else 0,
            m?.flags,
            m?.rssi,
            m?.rssiEma,
            m?.floor,
            m?.snr,
            m?.clearFloor,
            m?.peak,
            m?.peakOffHz,
            m?.centroidOffHz,
            m?.obwLoHz,
            m?.obwHiHz,
            // A channel the device has never measured has no age: its clock never started.
            if (m == null) null else now - state!!.updated,
        )
    }
    return out.toString()
}

/** The band as the panorama draws it, one row per point across the measured span. */
fun spectrumCsv(store: Store, columns: Int = SPECTRUM_COLUMNS): String {
    val span = bandRange(store)
    val c = composite(store, span.lo, span.hi, columns)
    val out = StringBuilder()
    row(out, "freq_khz", "live_dbfs", "max_hold_dbfs", "avg_dbfs", "noise_floor_dbfs")
    for (x in 0 until c.columns) {
        val f = c.f0 + ((x + 0.5) / c.columns) * (c.f1 - c.f0)
        row(out, num(f, 3), c.live[x], c.max[x], c.avg[x], c.floor[x])
    }
    return out.toString()
}

private fun row(out: StringBuilder, vararg cells: Any?) {
    cells.forEachIndexed { i, v ->
        if (i > 0) out.append(',')
        out.append(cell(v))
    }
    out.append('\n')
}

/** A dB reading with no value is an empty field: neither zero nor NaN means anything in a sheet. */
private fun cell(v: Any?): String = when (v) {
    null -> ""
    is Double -> if (v.isNaN()) "" else num(v, 2)
    is String -> if (v.any { it == ',' || it == '"' }) "\"${v.replace("\"", "\"\"")}\"" else v
    else -> v.toString()
}

private fun num(v: Double, digits: Int): String = String.format(Locale.US, "%.${digits}f", v)
