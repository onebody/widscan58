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

    /**
     * The points at most [maxAge] seconds before [now], oldest to newest.
     * Times only ever increase here, so the window opens where a binary search says.
     */
    fun forEachWithin(now: Double, maxAge: Double, action: (time: Double, value: Float) -> Unit) {
        val start = ((head - size) % capacity + capacity) % capacity
        val cutoff = now - maxAge
        var lo = 0
        var hi = size
        while (lo < hi) {
            val mid = (lo + hi) / 2
            if (t[(start + mid) % capacity] < cutoff) lo = mid + 1 else hi = mid
        }
        for (j in lo until size) {
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
