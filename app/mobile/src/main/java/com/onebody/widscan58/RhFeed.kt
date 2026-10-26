// The RotorHazard session: the link, the poll loop off the drawing thread, and the one worker
// the sheets' writes go through, so a change waits for the cycle in flight rather than landing
// between a seat select and its read. GPL-3.0-or-later.
package com.onebody.widscan58

import android.os.Handler
import android.os.SystemClock
import com.onebody.widscan58.core.LogDir
import com.onebody.widscan58.core.RhLink
import com.onebody.widscan58.core.RhModel
import com.onebody.widscan58.core.RhPoller
import com.onebody.widscan58.core.RhSerialLink
import com.onebody.widscan58.core.Store
import java.util.concurrent.Executors

class RhFeed(
    prefs: Prefs,
    val store: Store,
    val link: RhLink,
    private val main: Handler,
    replies: JsonReplies = JsonReplies(),
) {
    /** CONFIG? is JSON, which only this layer can read; it carries the level metric. */
    val model = RhModel(store, prefs) { line -> replies.rhConfig(line.substringAfter(' '))?.metric }
    private val poller = RhPoller({ SystemClock.elapsedRealtime() }, model)

    /** One write at a time: a slow board waits here, not on the drawing thread. */
    private val io = Executors.newSingleThreadExecutor()

    val running: Boolean get() = poller.running

    /** Kept so [resume] can restart the same cycle; a board never needs it, it ends instead. */
    private var channels: List<String> = emptyList()

    fun start(channels: List<String>) {
        this.channels = channels
        poller.start(link, channels)
    }

    /**
     * Stop the cycle without saying anything to the seat state. Only a simulator session is still
     * holding a working link when the activity pauses: a board's session ends with its port.
     */
    fun pause() {
        poller.stop()
    }

    fun resume() {
        if (channels.isNotEmpty() && !poller.running && !link.closed) poller.start(link, channels)
    }

    /** Bytes off the port. A simulator answers its own reads, so only a board has these. */
    fun push(chunk: ByteArray) {
        (link as? RhSerialLink)?.push(chunk)
    }

    /** A sheet's change, sent on the worker; a failure is the console's to show. */
    fun submit(what: (RhModel) -> Unit) {
        io.execute {
            runCatching { what(model) }.onFailure {
                store.addLog(LogDir.ERROR, it.message ?: "RotorHazard command failed")
            }
        }
    }

    /**
     * Hand the board back, then run [done] on the drawing thread once it is listening to text
     * again. The line goes out before the loop is closed, so the board is in monitor mode by then.
     */
    fun leave(done: () -> Unit) {
        io.execute {
            // Waiting for the cycle in flight is the point; off the drawing thread, since it may take
            // the poll loop's whole timeout to come back.
            poller.stop()
            runCatching { link.text("PROTOCOL MONITOR") }
                .onFailure { store.addLog(LogDir.ERROR, "leaving RotorHazard: ${it.message}") }
            (link as? RhSerialLink)?.close()
            link.closed = true
            main.post(done)
        }
        io.shutdown()
    }

    /**
     * The port went away: stop polling without another word to the board. The link is already marked
     * closed, so the cycle in flight fails its wait now instead of at its timeout.
     */
    fun abandon() {
        link.closed = true
        poller.stop()
        io.shutdownNow()
    }
}
