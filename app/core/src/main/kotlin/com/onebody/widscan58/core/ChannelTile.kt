// Per-channel readouts for the instrument's channel tiles, derived exactly like
// the web grid. Mirrors web/src/views/channelGrid.ts. GPL-3.0-or-later.
package com.onebody.widscan58.core

import kotlin.math.max
import kotlin.math.min

enum class BadgeTone { GOOD, WARNING, SERIOUS, CRITICAL }

/** What a badge means. The label is a UI concern: the app resolves these to text. */
enum class BadgeKind { STALE, SIGNAL, CLIPPING, SPUR, SPUR_MASKED, FILTER_EDGE }

class TileBadge(val kind: BadgeKind, val tone: BadgeTone)

/** In FULL metrics a spur is masked out of the search; in RSSI/SNR it is only flagged. */
fun badgesFor(flags: Int, stale: Boolean, masked: Boolean): List<TileBadge> {
    val out = ArrayList<TileBadge>(5)
    if (stale) out += TileBadge(BadgeKind.STALE, BadgeTone.WARNING)
    if (flags and ChannelFlag.SIGNAL != 0) out += TileBadge(BadgeKind.SIGNAL, BadgeTone.GOOD)
    if (flags and ChannelFlag.SATURATED != 0) out += TileBadge(BadgeKind.CLIPPING, BadgeTone.CRITICAL)
    if (flags and ChannelFlag.SPUR != 0) {
        out += if (masked) {
            TileBadge(BadgeKind.SPUR_MASKED, BadgeTone.SERIOUS)
        } else {
            TileBadge(BadgeKind.SPUR, BadgeTone.WARNING)
        }
    }
    if (flags and ChannelFlag.EDGE != 0) out += TileBadge(BadgeKind.FILTER_EDGE, BadgeTone.WARNING)
    return out
}

class ChannelTile(
    val id: Int,
    val name: String,
    val khz: Long,
    val enabled: Boolean,
    val selected: Boolean,
    val level: Double?, // dBFS, the EMA when the device reports one
    val bar: Double?, // 0..1 across floor-10 .. floor+40 dB
    val snr: Double?,
    val floor: Double?,
    val clearFloor: Double?,
    val centroidHz: Long?, // null unless the channel is occupied
    val obwHz: Long?,
    val badges: List<TileBadge>,
    /** Last 10 s of smoothed RSSI, drawn between [sparkLo] and [sparkHi]. */
    val series: Series?,
    val sparkFloor: Double,
    val sparkLo: Double,
    val sparkHi: Double,
)

/** One plan slot's tile; null before the device has sent a profile. Metrics the
 *  channel has not reported yet leave their readouts empty. */
fun channelTile(store: Store, id: Int, selected: Int): ChannelTile? {
    val channel = store.profile?.channels?.getOrNull(id) ?: return null
    val state = store.channels.getOrNull(id)
    val series = state?.let { smoothedSeries(it) }
    val m = state?.latest
    val level = m?.let { it.rssiEma ?: it.rssi }
    // A metrics mode without a floor measurement uses the channel's quietest level in view.
    var floor = m?.floor
    if (m != null && floor == null) {
        var low = level!!
        series?.forEach { t, v -> if (store.now() - t <= SPARK_WINDOW_S && v < low) low = v.toDouble() }
        floor = low
    }
    val stale = m != null && store.now() - state!!.updated > STALE_S
    val occupied = m != null && m.has(ChannelFlag.SIGNAL) && !m.has(ChannelFlag.NO_CENTROID)
    return ChannelTile(
        id = id,
        name = channel.name,
        khz = channel.khz,
        enabled = store.status?.let { it.enabled and (1 shl id) != 0 } ?: true,
        selected = selected == id,
        level = level,
        bar = if (level == null) null else
            max(0.0, min(1.0, (level - (floor!! - BAR_FLOOR_BELOW_DB)) / BAR_RANGE_DB)),
        snr = m?.snr,
        floor = m?.floor,
        clearFloor = m?.clearFloor,
        centroidHz = if (occupied) m!!.centroidOffHz else null,
        obwHz = if (occupied) m!!.let { it.obwHiHz?.minus(it.obwLoHz ?: return@let null) } else null,
        badges = m?.let { badgesFor(it.flags, stale, it.peak != null) } ?: emptyList(),
        series = series,
        sparkFloor = floor ?: DEFAULT_FLOOR_DB,
        sparkLo = (floor ?: DEFAULT_FLOOR_DB) - SPARK_FLOOR_BELOW_DB,
        sparkHi = max((floor ?: DEFAULT_FLOOR_DB) + SPARK_TOP_DB, (level ?: DEFAULT_FLOOR_DB) + SPARK_HEAD_DB),
    )
}

private const val STALE_S = 1.0
private const val BAR_FLOOR_BELOW_DB = 10.0
private const val BAR_RANGE_DB = 50.0
private const val SPARK_WINDOW_S = 10.0
private const val SPARK_FLOOR_BELOW_DB = 5.0
private const val SPARK_TOP_DB = 30.0
private const val SPARK_HEAD_DB = 3.0
private const val DEFAULT_FLOOR_DB = -90.0
