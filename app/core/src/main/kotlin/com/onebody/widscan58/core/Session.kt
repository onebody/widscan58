// The web's session file: the raw device byte stream, with the time each arrival
// came in, so a capture replays in this app or in the browser UI. A record is
// u32 ms | u32 length | bytes, little-endian, exactly as web/src/serial/recorder.ts
// writes it. GPL-3.0-or-later.
package com.onebody.widscan58.core

import java.io.InputStream
import java.io.OutputStream

/** One arrival: [tMs] after the recording opened, [data] as the link delivered it. */
class SessionChunk(val tMs: Long, val data: ByteArray)

/** A phone has less memory to spare than a browser tab, so a capture stops here. */
const val MAX_SESSION_BYTES = 32L shl 20

/** Far above the firmware's largest frame: a longer record is a corrupt file, not a capture. */
private const val MAX_RECORD_BYTES = 1L shl 20

class SessionRecorder(
    private val clock: () -> Long,
    /** Lowered by the tests; the app uses the file cap above. */
    private val maxBytes: Long = MAX_SESSION_BYTES,
) {
    var active = false
        private set
    /** Payload bytes kept so far, without the record headers. */
    var bytes = 0L
        private set
    /** The capture hit [maxBytes], so its tail is missing. */
    var overflowed = false
        private set

    private val chunks = ArrayList<SessionChunk>()
    private var openedAt = 0L

    /** Profile and status lines go in first, so a replay rebuilds the instrument without commands. */
    @Synchronized
    fun begin(preamble: List<String>) {
        chunks.clear()
        bytes = 0
        overflowed = false
        openedAt = clock()
        active = true
        preamble.forEach { add((it + "\n").toByteArray(Charsets.UTF_8)) }
    }

    /** The link's reader thread and the UI's replay timer both land here. */
    @Synchronized
    fun add(data: ByteArray, size: Int = data.size) {
        if (!active || size == 0) return
        if (bytes + size > maxBytes) {
            overflowed = true
            active = false
            return
        }
        bytes += size
        chunks += SessionChunk((clock() - openedAt).coerceAtLeast(0L), data.copyOf(size))
    }

    /** Stops taking bytes but keeps the capture, so a cancelled save can be retried. */
    @Synchronized
    fun stop() {
        active = false
    }

    /** Throws the capture away, once it is safely in a file. */
    @Synchronized
    fun discard() {
        chunks.clear()
        bytes = 0
        overflowed = false
        active = false
    }

    @Synchronized
    fun writeTo(out: OutputStream) {
        val head = ByteArray(8)
        for (c in chunks) {
            putU32LE(head, 0, c.tMs)
            putU32LE(head, 4, c.data.size.toLong())
            out.write(head)
            out.write(c.data)
        }
    }

    private fun putU32LE(into: ByteArray, at: Int, v: Long) {
        val x = v.coerceAtMost(0xffffffffL)
        into[at] = (x and 0xff).toByte()
        into[at + 1] = (x shr 8 and 0xff).toByte()
        into[at + 2] = (x shr 16 and 0xff).toByte()
        into[at + 3] = (x shr 24 and 0xff).toByte()
    }
}

/**
 * The records of a session file, oldest first. A last record whose bytes are not
 * there — the web does the same — ends the read rather than failing the file.
 */
fun readSession(input: InputStream, maxBytes: Long = MAX_SESSION_BYTES): List<SessionChunk> {
    val out = ArrayList<SessionChunk>()
    val head = ByteArray(8)
    var total = 0L
    while (true) {
        if (!readFully(input, head)) break
        val length = u32LE(head, 4)
        if (length > MAX_RECORD_BYTES || total + length > maxBytes) break
        val data = ByteArray(length.toInt())
        if (data.isNotEmpty() && !readFully(input, data)) break
        out += SessionChunk(u32LE(head, 0), data)
        total += length
    }
    return out
}

private fun readFully(input: InputStream, into: ByteArray): Boolean {
    var got = 0
    while (got < into.size) {
        val n = input.read(into, got, into.size - got)
        if (n < 0) return false
        got += n
    }
    return true
}

private fun u32LE(from: ByteArray, at: Int): Long =
    (from[at].toLong() and 0xff) or
        ((from[at + 1].toLong() and 0xff) shl 8) or
        ((from[at + 2].toLong() and 0xff) shl 16) or
        ((from[at + 3].toLong() and 0xff) shl 24)
