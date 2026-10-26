// RotorHazard's serial node protocol, as firmware/src/proto/rh.c implements it (node API level 36).
// One command byte: reads (<= 0x50) are answered with their payload and a checksum, writes (> 0x50)
// carry a payload and a checksum and are never answered. Multi-byte values are big-endian, the
// opposite of our own frames. GPL-3.0-or-later.
package com.onebody.widscan58.core

/** The seat count this app drives; the device reports its own in 0x39. */
const val RH_SEATS = 8

/** A level code the node will accept for a threshold or report as a reading: 0 and 255 are its own. */
const val RH_LEVEL_MIN = 1
const val RH_LEVEL_MAX = 254

/** High byte of the revision code, and the node API level it stands for. */
const val RH_API_MARKER = 0x25
const val RH_API_LEVEL = 36

/** What RotorHazard writes to switch a seat off. */
const val Rh_FREQ_OFF = 1111

object Rh {
    const val READ_ADDRESS = 0x00
    const val READ_FREQUENCY = 0x03
    const val TEST_RX_REGISTER = 0x04
    const val READ_LAP_STATS = 0x05
    const val READ_LAP_PASS_STATS = 0x0d
    const val READ_LAP_EXTREMUMS = 0x0e
    const val READ_RHFEAT_FLAGS = 0x11
    const val READ_REVISION_CODE = 0x22
    const val READ_NODE_RSSI_PEAK = 0x23
    const val READ_NODE_RSSI_NADIR = 0x24
    const val READ_ENTER_AT_LEVEL = 0x31
    const val READ_EXIT_AT_LEVEL = 0x32
    const val READ_TIME_MILLIS = 0x33
    const val READ_MULTINODE_COUNT = 0x39
    const val READ_CURNODE_INDEX = 0x3a
    const val READ_NODE_SLOTIDX = 0x3c
    const val READ_FW_VERSION = 0x3d
    const val READ_FW_BUILDDATE = 0x3e
    const val READ_FW_BUILDTIME = 0x3f
    const val READ_FW_PROCTYPE = 0x40
    const val WRITE_FREQUENCY = 0x51
    const val WRITE_ENTER_AT_LEVEL = 0x71
    const val WRITE_EXIT_AT_LEVEL = 0x72
    const val SEND_STATUS_MESSAGE = 0x75
    const val FORCE_END_CROSSING = 0x78
    const val RESET_PAIRED_NODE = 0x79
    const val WRITE_CURNODE_INDEX = 0x7a
    const val JUMP_TO_BOOTLOADER = 0x7e
}

const val FLAG_CROSSING = 0x01
const val FLAG_PEAK = 0x02

private fun sum(b: ByteArray, n: Int): Int {
    var s = 0
    for (j in 0 until n) s += b[j].toInt()
    return s and 0xff
}

/** Big-endian bytes of a 16-bit value. */
fun rhU16(v: Int): IntArray = intArrayOf((v shr 8) and 0xff, v and 0xff)

fun rhBe16(p: ByteArray, o: Int = 0): Int = ((p[o].toInt() and 0xff) shl 8) or (p[o + 1].toInt() and 0xff)

/** Widened: an uptime that reached 2^31 ms is still positive. */
fun rhBe32(p: ByteArray, o: Int = 0): Long = (rhBe16(p, o).toLong() shl 16) or rhBe16(p, o + 2).toLong()

/** Bytes of a write command: the command, its payload, and the payload's checksum. */
fun rhEncodeWrite(cmd: Int, payload: IntArray): ByteArray {
    var s = 0
    for (v in payload) s += v
    return ByteArray(payload.size + 2).also { b ->
        b[0] = cmd.toByte()
        payload.forEachIndexed { i, v -> b[i + 1] = v.toByte() }
        b[b.size - 1] = (s and 0xff).toByte()
    }
}

/** A read reply's payload, or null if the length or the checksum is wrong. */
fun rhCheckReply(reply: ByteArray, size: Int): ByteArray? {
    if (reply.size != size + 1 || sum(reply, size) != (reply[size].toInt() and 0xff)) return null
    return reply.copyOf(size)
}

class RhPassStats(
    val lapId: Int,
    val msSinceLap: Int,
    val level: Int,
    val nodePeak: Int,
    val passPeak: Int,
    val loopUs: Int,
)

fun rhParsePassStats(p: ByteArray): RhPassStats =
    RhPassStats(
        lapId = p[0].toInt() and 0xff,
        msSinceLap = rhBe16(p, 1),
        level = p[3].toInt() and 0xff,
        nodePeak = p[4].toInt() and 0xff,
        passPeak = p[5].toInt() and 0xff,
        loopUs = rhBe16(p, 6),
    )

/** The oldest queued peak or nadir: level, ms since its start, duration ms. */
class RhExtremum(val peak: Boolean, val level: Int, val msAgo: Int, val durationMs: Int)

class RhExtremums(
    val crossing: Boolean,
    val passNadir: Int,
    val nodeNadir: Int,
    val extremum: RhExtremum?,
)

fun rhParseExtremums(p: ByteArray): RhExtremums {
    val flags = p[0].toInt() and 0xff
    val level = p[3].toInt() and 0xff
    return RhExtremums(
        crossing = flags and FLAG_CROSSING != 0,
        passNadir = p[1].toInt() and 0xff,
        nodeNadir = p[2].toInt() and 0xff,
        extremum = if (level == 0) null else RhExtremum(flags and FLAG_PEAK != 0, level, rhBe16(p, 4), rhBe16(p, 6)),
    )
}

/** Levels the node treats as a measurement (0 and 255 are "none"). */
fun rhValidLevel(v: Int): Boolean = v > 0 && v < 255

/** Which of the node's two output values the device is reporting. */
enum class RhMetric {
    RSSI,
    SNR,
    ;

    companion object {
        fun of(name: String?): RhMetric? = entries.firstOrNull { it.name == name }
    }
}

/** A level code in display units: dBFS for RSSI, dB for SNR (firmware/src/scan/metrics.h). */
fun rhLevelDb(code: Int, metric: RhMetric): Double = if (metric == RhMetric.RSSI) code / 4.0 - 63.75 else code / 4.0
