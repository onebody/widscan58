// Splits the device byte stream into text lines and CRC-checked binary frames.
// Frame: 0xA5 'R' type version | u16 len | payload | u32 crc32 (zlib).
// Mirrors web/src/serial/parser.ts. GPL-3.0-or-later.
package com.onebody.widscan58.core

sealed class StreamItem {
    data class Text(val line: String) : StreamItem()
    class Frame(val type: Int, val payload: ByteArray) : StreamItem()
}

private const val SYNC0 = 0xa5
private const val SYNC1 = 0x52 // 'R'
private const val MAX_PAYLOAD = 65535
private const val TEXT_GAP_LIMIT = 4096

/** Firmware reply lines start with an uppercase command word. */
private val LINE_START = Regex("^[A-Z][A-Z0-9_?]*( |\$)")

class StreamParser {
    private var buf = ByteArray(1 shl 16)
    private var len = 0

    var bytes = 0L
        private set
    var crcErrors = 0L
        private set
    var resyncBytes = 0L
        private set

    fun push(chunk: ByteArray): List<StreamItem> {
        bytes += chunk.size
        if (len + chunk.size > buf.size) {
            val next = ByteArray(maxOf(buf.size * 2, len + chunk.size))
            buf.copyInto(next, 0, 0, len)
            buf = next
        }
        chunk.copyInto(buf, len)
        len += chunk.size

        val out = ArrayList<StreamItem>()
        val r = ByteReader(buf)
        var pos = 0
        while (pos < len) {
            if (buf[pos].toInt() and 0xff == SYNC0) {
                if (len - pos < 6) break
                if (buf[pos + 1].toInt() and 0xff != SYNC1) {
                    pos++
                    resyncBytes++
                    continue
                }
                val length = (buf[pos + 4].toInt() and 0xff) or ((buf[pos + 5].toInt() and 0xff) shl 8)
                val total = 6 + length + 4
                if (length > MAX_PAYLOAD) {
                    pos++
                    continue
                }
                if (len - pos < total) break
                val want = r.u32(pos + 6 + length)
                if (Crc32.of(buf, pos, pos + 6 + length) != want) {
                    crcErrors++
                    pos++
                    resyncBytes++
                    continue
                }
                out += StreamItem.Frame(buf[pos + 2].toInt() and 0xff, buf.copyOfRange(pos + 6, pos + 6 + length))
                pos += total
                continue
            }
            // Text: printable lines. Anything before a newline that contains a
            // sync byte is cut at the sync byte (a frame interrupted a partial line).
            var end = pos
            while (end < len && buf[end].toInt() != 0x0a && buf[end].toInt() and 0xff != SYNC0) end++
            if (end >= len) {
                if (len - pos > TEXT_GAP_LIMIT) {
                    // Garbage without a newline (e.g. joined mid-frame): drop it.
                    resyncBytes += len - pos
                    pos = len
                }
                break
            }
            var text = String(buf, pos, end - pos, Charsets.UTF_8)
            if (text.endsWith("\r")) text = text.substring(0, text.length - 1)
            if (buf[end].toInt() == 0x0a) end++
            // Lines from the firmware start with an uppercase command word; anything
            // else is the tail of a frame we joined part-way through.
            if (LINE_START.containsMatchIn(text)) out += StreamItem.Text(text)
            else if (text.isNotEmpty()) resyncBytes += text.length
            pos = end
        }
        buf.copyInto(buf, 0, pos, len)
        len -= pos
        return out
    }
}

/** Wraps a payload in the frame envelope; used by the tests and the simulator. */
fun encodeFrame(type: Int, payload: ByteArray, version: Int = 1): ByteArray {
    val out = ByteArray(6 + payload.size + 4)
    out[0] = SYNC0.toByte()
    out[1] = SYNC1.toByte()
    out[2] = type.toByte()
    out[3] = version.toByte()
    out[4] = (payload.size and 0xff).toByte()
    out[5] = (payload.size shr 8).toByte()
    payload.copyInto(out, 6)
    val crc = Crc32.of(out, 0, 6 + payload.size)
    out[6 + payload.size] = (crc and 0xff).toByte()
    out[7 + payload.size] = (crc shr 8 and 0xff).toByte()
    out[8 + payload.size] = (crc shr 16 and 0xff).toByte()
    out[9 + payload.size] = (crc shr 24 and 0xff).toByte()
    return out
}
