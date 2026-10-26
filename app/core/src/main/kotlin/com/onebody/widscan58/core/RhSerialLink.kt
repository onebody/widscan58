// The transactions of [RhLink] over a plain byte pipe: [send] puts the command on the wire, the
// platform's reader thread hands whatever comes back to [push]. One transaction at a time, the buffer
// flushed before every request, a short timeout and a few retries — serial_node.py's discipline, so
// the model above it only ever sees a verified payload or an error.
// Mirrors web/src/serial/rhLink.ts. GPL-3.0-or-later.
package com.onebody.widscan58.core

class RhSerialLink(
    private val clockMs: () -> Long,
    private val send: (ByteArray) -> Unit,
) : RhLink, RhLinkStats {
    private val monitor = Object()
    private val buf = ArrayList<Byte>(64)

    /** What the receiver card reports about the link. */
    override var reads = 0
        private set
    override var writes = 0
        private set
    override var errors = 0
        private set

    override var closed = false
    override var lastRequest = 0L
    override var lastResponse = 0L

    /** Bytes as the reader thread gets them. */
    fun push(chunk: ByteArray) = synchronized(monitor) {
        for (b in chunk) buf += b
        monitor.notifyAll()
    }

    /** The port is gone: wake whatever is waiting so it fails now instead of at its timeout. */
    fun close() {
        closed = true
        synchronized(monitor) { monitor.notifyAll() }
    }

    override fun read(cmd: Int, size: Int): ByteArray = synchronized(monitor) {
        for (attempt in 0..READ_RETRIES) {
            val t0 = clockMs()
            val reply = exchange(byteArrayOf(cmd.toByte()), size + 1, READ_TIMEOUT_MS)
            reads++
            val payload = reply?.let { rhCheckReply(it, size) }
            if (payload != null) {
                lastRequest = t0
                lastResponse = clockMs()
                return@read payload
            }
            errors++
            if (closed) break
        }
        throw RhLinkError("RotorHazard read 0x${cmd.toString(16)}: no valid reply")
    }

    /** A write command: the protocol owes nothing, so there is nothing to wait for. */
    override fun write(cmd: Int, payload: IntArray) {
        synchronized(monitor) {
            exchange(rhEncodeWrite(cmd, payload), 0, 0L)
            writes++
        }
    }

    override fun text(line: String): String = synchronized(monitor) {
        val reply = exchange((line + "\n").toByteArray(Charsets.UTF_8), LINE, TEXT_TIMEOUT_MS)
            ?: throw RhLinkError("timeout: $line")
        String(reply, Charsets.UTF_8).trimEnd('\r', '\n')
    }

    /** [size] bytes, or one line, or nothing at all for a write; null once the timeout says so. */
    private fun exchange(data: ByteArray, size: Int, timeoutMs: Long): ByteArray? {
        buf.clear() // serial_node.py flushes the input before every request
        send(data)
        return when {
            size == 0 -> null
            size == LINE -> awaitLine(timeoutMs)
            else -> awaitBytes(size, timeoutMs)
        }
    }

    private fun awaitBytes(n: Int, timeoutMs: Long): ByteArray? {
        if (!await({ buf.size >= n }, timeoutMs)) return null
        return ByteArray(n) { buf.removeAt(0) }
    }

    private fun awaitLine(timeoutMs: Long): ByteArray? {
        if (!await({ buf.contains(NEWLINE) }, timeoutMs)) return null
        val end = buf.indexOf(NEWLINE)
        return ByteArray(end + 1) { buf.removeAt(0) }
    }

    private fun await(full: () -> Boolean, timeoutMs: Long): Boolean {
        val deadline = clockMs() + timeoutMs
        while (!full()) {
            if (closed) return false
            val left = deadline - clockMs()
            if (left <= 0L) return false
            try {
                monitor.wait(left.coerceAtMost(MAX_WAIT_MS))
            } catch (e: InterruptedException) {
                Thread.currentThread().interrupt()
                return false
            }
        }
        return true
    }

    private companion object {
        const val READ_TIMEOUT_MS = 250L // serial_node.py's
        const val READ_RETRIES = 2
        const val TEXT_TIMEOUT_MS = 2_000L
        const val MAX_WAIT_MS = 50L // a slice, so an interrupted thread still notices in time
        const val LINE = -1
        val NEWLINE = '\n'.code.toByte()
    }
}
