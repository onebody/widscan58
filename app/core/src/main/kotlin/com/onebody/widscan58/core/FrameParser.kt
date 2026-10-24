// Binary frame decoders: one function per frame type, same offsets as the
// firmware header. GPL-3.0-or-later.
package com.onebody.widscan58.core

private const val METRICS_HDR = 34 // sizeof(frame_metrics_t)
private const val CHANNEL_SIZE = 26 // sizeof(frame_channel_t)
private const val LEVELS_HDR = 32 // sizeof(frame_levels_t)
private const val LEVEL_SIZE = 4 // sizeof(frame_level_t)
private const val RAWIQ_HDR = 14

// Level codes (0.25 dB): dBFS = code / 4 - 63.75; SNR dB = code / 4. The
// header's per-bin floor is a spectrum code: dBFS = code / 2 - 127.5.
private fun levelDb(code: Int) = code / 4.0 - 63.75
private fun specDb(code: Int) = code / 2.0 - 127.5
private fun cdB(v: Int) = v / 100.0

fun parseMetrics(p: ByteArray): Metrics {
    val r = ByteReader(p)
    val channels = ArrayList<ChannelMetrics>()
    val nch = r.u8(METRICS_HDR - 1)
    for (j in 0 until nch) {
        val o = METRICS_HDR + j * CHANNEL_SIZE
        if (o + CHANNEL_SIZE > p.size) break
        val clear = r.i16(o + 10)
        channels += ChannelMetrics(
            id = r.u8(o),
            flags = r.u8(o + 1),
            rssi = cdB(r.i16(o + 2)),
            rssiEma = cdB(r.i16(o + 4)),
            floor = cdB(r.i16(o + 6)),
            snr = cdB(r.i16(o + 8)),
            clearFloor = if (clear == Short.MIN_VALUE.toInt()) null else cdB(clear),
            peak = cdB(r.i16(o + 12)),
            peakOffHz = r.i32(o + 14).toLong(),
            centroidOffHz = r.i32(o + 18).toLong(),
            obwLoHz = r.i16(o + 22) * 1000L,
            obwHiHz = r.i16(o + 24) * 1000L,
        )
    }
    return Metrics(
        seq = r.u32(0),
        tMs = r.u32(4),
        loKhz = r.u32(8),
        tuneUs = r.u32(12),
        captureUs = r.u32(16),
        dspUs = r.u32(20),
        saturated = r.u16(24),
        mode = MetricsMode.FULL,
        floorBin = cdB(r.i16(26)),
        dc = cdB(r.i16(28)),
        gain = r.u8(30),
        hopIndex = r.u8(31),
        hops = r.u8(32),
        channels = channels,
    )
}

fun parseLevels(p: ByteArray): Metrics {
    val r = ByteReader(p)
    val mode = MetricsMode.of(r.u8(29))
    val snrMode = mode == MetricsMode.SNR
    val channels = ArrayList<ChannelMetrics>()
    val nch = r.u8(LEVELS_HDR - 1)
    for (j in 0 until nch) {
        val o = LEVELS_HDR + j * LEVEL_SIZE
        if (o + LEVEL_SIZE > p.size) break
        val rssi = levelDb(r.u8(o + 2))
        val snr = if (snrMode) r.u8(o + 3) / 4.0 else null
        channels += ChannelMetrics(
            id = r.u8(o),
            flags = r.u8(o + 1),
            rssi = rssi,
            rssiEma = null,
            floor = if (snr == null) null else rssi - snr,
            snr = snr,
            clearFloor = null,
            peak = null,
            peakOffHz = null,
            centroidOffHz = null,
            obwLoHz = null,
            obwHiHz = null,
        )
    }
    return Metrics(
        seq = r.u32(0),
        tMs = r.u32(4),
        loKhz = r.u32(8),
        tuneUs = r.u32(12),
        captureUs = r.u32(16),
        dspUs = r.u32(20),
        saturated = r.u16(24),
        gain = r.u8(26),
        hopIndex = r.u8(27),
        hops = r.u8(28),
        mode = mode,
        floorBin = if (snrMode) specDb(r.u8(30)) else null,
        dc = null,
        channels = channels,
    )
}

fun parseSpectrum(p: ByteArray): Spectrum {
    val r = ByteReader(p)
    val n = r.u16(16)
    val db = DoubleArray(n) { specDb(r.u8(20 + it)) }
    return Spectrum(
        seq = r.u32(0),
        loKhz = r.u32(4),
        startOffHz = r.i32(8).toLong(),
        binHz = r.u32(12),
        detector = r.u8(18),
        db = db,
    )
}

fun parseTelemetry(p: ByteArray): Telemetry {
    val r = ByteReader(p)
    return Telemetry(
        tMs = r.u32(0),
        rotations = r.u32(4),
        hopsTotal = r.u32(8),
        rotationHz = r.u32(12) / 1000.0,
        hopUsAvg = r.u32(16),
        hopUsMax = r.u32(20),
        tuneUsAvg = r.u32(24),
        tuneUsMax = r.u32(28),
        captureUsAvg = r.u32(32),
        dspUsAvg = r.u32(36),
        dspUsMax = r.u32(40),
        linkFill = r.u16(44) / 1000.0,
        heapFree = r.u32(46),
        drops = r.u32(50),
        captureErrors = r.u32(54),
        running = r.u8(58) != 0,
    )
}

fun parseRawIqChunk(p: ByteArray): RawIqChunk {
    val r = ByteReader(p)
    return RawIqChunk(
        loKhz = r.u32(0),
        rateHz = r.u32(4),
        n = r.u16(8),
        offset = r.u16(10),
        count = (p.size - RAWIQ_HDR) / 2,
        conjugated = r.u8(12) != 0,
        shift = r.u8(13),
        payload = p,
    )
}

/** Collects chunks into a snapshot; add() returns it once the last one is in. */
class RawIqAssembler {
    private var cur: RawIq? = null
    private var next = 0

    fun add(payload: ByteArray): RawIq? {
        val c = parseRawIqChunk(payload)
        if (c.offset == 0) {
            cur = RawIq(c.loKhz, c.rateHz, c.n, c.conjugated, c.shift, ByteArray(c.n), ByteArray(c.n))
            next = 0
        }
        val r = cur
        if (r == null || c.offset != next || c.n != r.n || c.offset + c.count > r.n) {
            cur = null // a chunk went missing: drop the snapshot
            return null
        }
        for (j in 0 until c.count) {
            r.i[c.offset + j] = payload[RAWIQ_HDR + 2 * j]
            r.q[c.offset + j] = payload[RAWIQ_HDR + 1 + 2 * j]
        }
        next = c.offset + c.count
        if (next < r.n) return null
        cur = null
        return r
    }
}
