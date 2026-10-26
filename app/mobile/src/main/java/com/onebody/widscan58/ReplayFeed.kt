// Plays a recorded session back at the rate it was captured: the bytes go to the same
// sink a board's USB reads go to, so the parser, the store and the instrument cannot tell
// the difference. Mirrors ReplayTransport in web/src/serial/recorder.ts. GPL-3.0-or-later.
package com.onebody.widscan58

import android.os.Handler
import android.os.SystemClock
import com.onebody.widscan58.core.SessionChunk

class ReplayFeed(
    private val main: Handler,
    private val onBytes: (ByteArray) -> Unit,
    private val onFinished: () -> Unit,
) {
    private var chunks: List<SessionChunk> = emptyList()
    private var at = 0
    private var startedAt = 0L
    /** Milliseconds of the session already handed over, kept across a pause. */
    private var playedMs = 0L
    private var running = false

    /** The file being played, for the instrument's status line. */
    var name = ""
        private set

    /** A session was handed over and has not finished: a pause is still a replay. */
    var started = false
        private set
    val paused: Boolean get() = started && !running
    val durationMs: Long get() = chunks.lastOrNull()?.tMs ?: 0L
    val positionMs: Long get() = if (running) SystemClock.elapsedRealtime() - startedAt else playedMs

    fun start(name: String, chunks: List<SessionChunk>) {
        stop()
        this.name = name
        this.chunks = chunks
        at = 0
        playedMs = 0
        startedAt = SystemClock.elapsedRealtime()
        started = true
        running = true
        main.post(step)
    }

    fun pause() {
        if (!running) return
        running = false
        main.removeCallbacks(step)
        playedMs = SystemClock.elapsedRealtime() - startedAt
    }

    /** Picks up where [pause] left off, rather than replaying the session from the top. */
    fun resume() {
        if (running || !started || at >= chunks.size) return
        running = true
        startedAt = SystemClock.elapsedRealtime() - playedMs
        main.post(step)
    }

    fun stop() {
        running = false
        started = false
        main.removeCallbacks(step)
        chunks = emptyList()
        at = 0
        playedMs = 0
        name = ""
    }

    /** The record that is due now, then a wait for the next one — a stalled UI catches up in order. */
    private val step = object : Runnable {
        override fun run() {
            if (!running) return
            val due = SystemClock.elapsedRealtime() - startedAt
            while (at < chunks.size) {
                val c = chunks[at]
                if (c.tMs > due) {
                    main.postDelayed(this, c.tMs - due)
                    return
                }
                at++
                playedMs = c.tMs
                onBytes(c.data)
            }
            stop()
            onFinished()
        }
    }
}
