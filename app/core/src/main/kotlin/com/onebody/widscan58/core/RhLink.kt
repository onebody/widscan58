// The transactions a host runs against a board in RotorHazard mode, one at a time: a read command
// byte answered by its payload and a sum, a write command with its payload and sum answered by
// nothing, and our own command lines answered by one text line. The link owns the retries and the
// flushing the firmware needs (serial_node.py); what it hands back is a payload or an error.
// GPL-3.0-or-later.
package com.onebody.widscan58.core

/** A transaction the board did not answer the way the protocol says it must. */
class RhLinkError(message: String) : Exception(message)

/** What a link has spent getting its answers: the receiver card's own line about it. */
interface RhLinkStats {
    val reads: Int
    val writes: Int
    val errors: Int
}

interface RhLink {
    /** Set once the port is gone: a poll loop that sees it stops instead of spinning on errors. */
    var closed: Boolean

    /** Milliseconds of the last read's request and response, so a level can be stamped at the
     *  middle of its round trip (RotorHazard's own read time). */
    var lastRequest: Long
    var lastResponse: Long

    /** [size] payload bytes, checksum verified. Throws once the retries are used up. */
    fun read(cmd: Int, size: Int): ByteArray

    /** A write command and its payload; the protocol gives nothing back. */
    fun write(cmd: Int, payload: IntArray)

    /** One command line and the line the board said back. */
    fun text(line: String): String
}
