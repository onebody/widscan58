// zlib-compatible CRC32 used by the widscan58 frame trailer. GPL-3.0-or-later.
package com.onebody.widscan58.core

private val TABLE = IntArray(256) { n ->
    var c = n
    for (k in 0 until 8) c = if (c and 1 != 0) 0xedb88320.toInt() xor (c ushr 1) else c ushr 1
    c
}

object Crc32 {
    /** Unsigned CRC32 of data[start, end). */
    fun of(data: ByteArray, start: Int = 0, end: Int = data.size): Long {
        var c = -1
        for (i in start until end) c = TABLE[(c xor data[i].toInt()) and 0xff] xor (c ushr 8)
        return (c xor -1).toLong() and 0xffffffffL
    }
}
