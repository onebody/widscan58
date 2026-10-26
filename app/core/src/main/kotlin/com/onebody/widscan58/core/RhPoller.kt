// The poll loop RotorHazard's server runs, on its own thread: start on a board already in this mode,
// then one cycle at a time until the link or the user ends it. The transactions are blocking serial
// I/O, so they stay off the UI thread; the model touches the store, which is what tells the screen to
// draw. Mirrors the setInterval in web/src/main.ts's enterRh(). GPL-3.0-or-later.
package com.onebody.widscan58.core

class RhPoller(
    private val clockMs: () -> Long,
    private val model: RhModel,
    private val intervalMs: Long = RhModel.POLL_MS,
) {
    @Volatile private var thread: Thread? = null

    val running: Boolean get() = thread != null

    /** The board is set up here too: the whole of the RH mode's I/O stays on this thread. */
    fun start(link: RhLink, channels: List<String>) {
        stop()
        val t = Thread({ run(link, channels) }, "widscan58-rh")
        thread = t
        t.isDaemon = true
        t.start()
    }

    fun stop() {
        val t = synchronized(this) { thread.also { thread = null } } ?: return
        t.interrupt()
        runCatching { t.join(JOIN_MS) }
        model.stop()
    }

    private fun run(link: RhLink, channels: List<String>) {
        model.start(link, channels)
        while (model.running) {
            val t0 = clockMs()
            model.poll()
            val left = intervalMs - (clockMs() - t0)
            if (left > 0) runCatching { Thread.sleep(left) }
        }
        synchronized(this) { if (thread === Thread.currentThread()) thread = null }
    }

    private companion object {
        const val JOIN_MS = 1_000L
    }
}
