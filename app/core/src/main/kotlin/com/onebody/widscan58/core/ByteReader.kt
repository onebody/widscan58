// Little-endian reads over a frame payload; the wire format is LE. GPL-3.0-or-later.
package com.onebody.widscan58.core

internal class ByteReader(private val b: ByteArray) {
    fun u8(o: Int): Int = b[o].toInt() and 0xff

    fun u16(o: Int): Int = u8(o) or (u8(o + 1) shl 8)

    fun i16(o: Int): Int = u16(o).let { if (it >= 0x8000) it - 0x10000 else it }

    /** Unsigned 32-bit, widened so sequence numbers and timings stay positive. */
    fun u32(o: Int): Long = u16(o).toLong() or (u16(o + 2).toLong() shl 16)

    fun i32(o: Int): Int = u32(o).toInt()
}
