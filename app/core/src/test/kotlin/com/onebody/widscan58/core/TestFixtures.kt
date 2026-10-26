// Wire payloads and device replies for the engine tests, built the way the
// firmware writes them. GPL-3.0-or-later.
package com.onebody.widscan58.core

import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.roundToInt

/** A spectrum code for a dBFS value (dBFS = code / 2 - 127.5). */
internal fun specCode(db: Double): Int = ((db + 127.5) * 2).roundToInt()

/** dBFS -> 0.25 dB level code. */
internal fun levelCode(db: Double): Int = ((db + 63.75) * 4).roundToInt()

internal fun spectrumOf(
    loKhz: Long,
    vararg codes: Int,
    startOffHz: Int = -20_000_000,
    binHz: Int = 156_250,
): ByteArray {
    val p = ByteArray(20 + codes.size)
    val b = ByteBuffer.wrap(p).order(ByteOrder.LITTLE_ENDIAN)
    b.putInt(0, 1).putInt(4, loKhz.toInt()).putInt(8, startOffHz).putInt(12, binHz)
        .putShort(16, codes.size.toShort())
    codes.forEachIndexed { i, c -> p[20 + i] = c.toByte() }
    return p
}

/** One LEVELS channel: id, flags, rssi code, snr code. */
internal fun levelOf(id: Int, flags: Int = 0, rssiDb: Double, snrDb: Double? = null) =
    intArrayOf(id, flags, levelCode(rssiDb), if (snrDb == null) 0 else (snrDb * 4).roundToInt())

internal fun levelsOf(
    loKhz: Long,
    mode: Int,
    perBinFloor: Int = 0,
    vararg levels: IntArray,
    tuneUs: Long = 100,
    captureUs: Long = 200,
    dspUs: Long = 50,
): ByteArray {
    val p = ByteArray(32 + 4 * levels.size)
    val b = ByteBuffer.wrap(p).order(ByteOrder.LITTLE_ENDIAN)
    b.putInt(0, 7).putInt(4, 100).putInt(8, loKhz.toInt())
    b.putInt(12, tuneUs.toInt()).putInt(16, captureUs.toInt()).putInt(20, dspUs.toInt())
    p[29] = mode.toByte()
    p[30] = perBinFloor.toByte()
    p[31] = levels.size.toByte()
    levels.forEachIndexed { i, l ->
        val o = 32 + 4 * i
        p[o] = l[0].toByte()
        p[o + 1] = l[1].toByte()
        p[o + 2] = l[2].toByte()
        p[o + 3] = l[3].toByte()
    }
    return p
}

internal fun channel(id: Int, name: String = "R${id + 1}", khz: Long = 5_658_000L + 37_000L * id) =
    ProfileChannel(id, name, khz)

/** One METRICS channel record, in dB (the wire stores hundredths). */
internal class MetricChannel(
    val id: Int,
    val rssi: Double,
    val flags: Int = 0,
    val rssiEma: Double? = null,
    val floor: Double? = null,
    val snr: Double? = null,
    val clearFloor: Double? = null,
    val peak: Double? = null,
    val centroidHz: Long? = null,
    val obwHz: LongRange? = null,
)

internal fun metricsOf(
    vararg channels: MetricChannel,
    loKhz: Long = 5_658_000,
    tuneUs: Long = 100,
    captureUs: Long = 200,
    dspUs: Long = 50,
): ByteArray {
    val p = ByteArray(34 + 26 * channels.size)
    val b = ByteBuffer.wrap(p).order(ByteOrder.LITTLE_ENDIAN)
    b.putInt(0, 3).putInt(4, 100).putInt(8, loKhz.toInt())
    b.putInt(12, tuneUs.toInt()).putInt(16, captureUs.toInt()).putInt(20, dspUs.toInt())
    p[30] = 45 // gain
    p[31] = 0 // hop index
    p[32] = 1 // hops
    p[33] = channels.size.toByte()
    fun i16(at: Int, db: Double?) {
        if (db != null) b.putShort(at, (db * 100).roundToInt().toShort())
    }
    channels.forEachIndexed { i, c ->
        val o = 34 + 26 * i
        p[o] = c.id.toByte()
        p[o + 1] = c.flags.toByte()
        i16(o + 2, c.rssi)
        // FULL metrics always smooths RSSI; an unmeasured clear floor is the wire's own sentinel.
        i16(o + 4, c.rssiEma ?: c.rssi)
        i16(o + 6, c.floor)
        i16(o + 8, c.snr)
        b.putShort(o + 10, (c.clearFloor?.let { (it * 100).roundToInt().toShort() } ?: Short.MIN_VALUE))
        i16(o + 12, c.peak)
        c.centroidHz?.let { b.putInt(o + 18, it.toInt()) }
        c.obwHz?.let { b.putShort(o + 22, (it.first / 1000).toShort()).putShort(o + 24, (it.last / 1000).toShort()) }
    }
    return p
}

internal fun profileOf(vararg channels: ProfileChannel, bandLo: Long = 5_621_000, bandHi: Long = 5_954_000) = Profile(
    name = "c5-race",
    description = "test",
    target = "esp32-c5",
    bandKhz = longArrayOf(bandLo, bandHi),
    baseKhz = 5_658_000,
    equivBaseKhz = 5_658_000,
    scale = 1.0,
    rateHz = 80_000_000,
    usableHz = 40_000_000,
    passbandHz = 18_000_000,
    rssiHz = 5_000_000,
    searchHz = 12_000_000,
    channels = if (channels.isEmpty()) (0 until 8).map { channel(it) } else channels.toList(),
)

/** Only the hop plan matters to the engine; the rest is plausible filler. */
internal fun statusOf(vararg planLoKhz: Long, enabled: Int = 0xff) = Status(
    profile = "c5-race",
    running = 1,
    enabled = enabled,
    mode = "GROUP",
    captureSamples = 128,
    nfft = 128,
    segments = 8,
    rateCode = 1,
    filter = 0,
    conjugate = 0,
    iqCorr = 0,
    settleUs = 40,
    tskip = null,
    gainMode = "MANUAL",
    gain = 45,
    gainMax = 63,
    rawiqMax = null,
    recal = "boot",
    metrics = MetricsMode.RSSI,
    rssiHz = 5_000_000,
    searchHz = 12_000_000,
    dcGuard = 3,
    ema = 0.008,
    clearEma = 0.0,
    signalDb = -75.0,
    avgShift = 1,
    specEvery = 4,
    specBins = 256,
    specSpanHz = 40_000_000,
    specDetector = 0,
    plan = planLoKhz.map { PlanHop(it, intArrayOf(0), longArrayOf(0)) },
)

internal fun spectrumFrame(loKhz: Long, vararg codes: Int) = frame(FrameType.SPECTRUM, spectrumOf(loKhz, *codes))

internal fun frame(type: Int, payload: ByteArray) = StreamItem.Frame(type, payload)
