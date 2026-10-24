// Fixed-capacity time series for the per-channel sparklines.
// Mirrors the Series class in web/src/model/store.ts. GPL-3.0-or-later.
package com.onebody.widscan58.core

class Series(val capacity: Int) {
    private val t = DoubleArray(capacity)
    private val v = FloatArray(capacity)
    private var head = 0
    var size = 0
        private set

    fun push(time: Double, value: Float) {
        t[head] = time
        v[head] = value
        head = (head + 1) % capacity
        if (size < capacity) size++
    }

    /** Oldest to newest. */
    fun forEach(action: (time: Double, value: Float) -> Unit) {
        val start = ((head - size) % capacity + capacity) % capacity
        for (j in 0 until size) {
            val k = (start + j) % capacity
            action(t[k], v[k])
        }
    }

    fun newest(): Float? = if (size == 0) null else v[(head - 1 + capacity) % capacity]

    fun clear() {
        head = 0
        size = 0
    }
}
