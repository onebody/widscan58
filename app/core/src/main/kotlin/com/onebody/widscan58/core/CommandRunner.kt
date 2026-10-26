// Command/reply client: ASCII lines out, OK/ERR or a keyword reply back.
// Mirrors the waiting logic in web/src/serial/device.ts, without the browser.
// GPL-3.0-or-later.
package com.onebody.widscan58.core

/** Injectable so the timeout path is testable without a real clock. */
fun interface Scheduler {
    fun schedule(delayMs: Long, task: () -> Unit): Cancellable
}

fun interface Cancellable {
    fun cancel()
}

sealed class CommandResult {
    class Ok(val item: StreamItem) : CommandResult()
    class Failed(val why: String) : CommandResult()
}

/** Which reply ends a command. Most commands answer OK/ERR; queries answer with
 *  their own keyword; RAWIQ answers with frames, done at the last chunk. */
fun replyMatcher(line: String): (StreamItem) -> Boolean {
    val word = line.trim().substringBefore(' ')
    val isError = { i: StreamItem -> i is StreamItem.Text && i.line.startsWith("ERR") }
    if (word == "RAWIQ") {
        return { i -> (i is StreamItem.Frame && i.type == FrameType.RAWIQ && parseRawIqChunk(i.payload).isLast()) || isError(i) }
    }
    val prefix = QUERIES[word]
    if (prefix != null) return { i -> (i is StreamItem.Text && i.line.startsWith(prefix)) || isError(i) }
    return { i -> i is StreamItem.Text && (i.line == "OK" || i.line.startsWith("OK ") || i.line.startsWith("ERR")) }
}

private val QUERIES = mapOf(
    "INFO" to "INFO ",
    "PROFILE?" to "PROFILE ",
    "STATUS?" to "STATUS ",
    "CONFIG?" to "CONFIG ",
    "GAIN?" to "GAIN ",
    "TIMING?" to "TIMING ",
    "SPURS?" to "SPURS ",
    "ENABLE?" to "ENABLE ",
    "PROTOCOL?" to "PROTOCOL ",
    "METRICS?" to "METRICS ",
    "GPIO?" to "GPIO ",
    "LEVELFILTER?" to "LEVELFILTER ",
    "FFTTYPE?" to "FFTTYPE ",
    "PHYIDLE?" to "PHYIDLE ",
    "SYNC" to "SYNC ",
)

/**
 * Sends one command at a time and resolves each with its reply. Items arriving out
 * of order (frames, unsolicited log lines) are left to the caller's stream listener.
 */
class CommandRunner(
    private val write: (ByteArray) -> Unit,
    private val scheduler: Scheduler,
    private val timeoutMs: Long = 3000,
) {
    private class Waiting(
        val line: String,
        val matcher: (StreamItem) -> Boolean,
        val reply: (CommandResult) -> Unit,
    ) {
        var timer: Cancellable? = null
        var done = false
    }

    private val queue = ArrayDeque<Waiting>()
    private var inFlight: Waiting? = null

    val busy: Boolean get() = inFlight != null || queue.isNotEmpty()

    fun send(line: String, reply: (CommandResult) -> Unit = {}) {
        queue.addLast(Waiting(line, replyMatcher(line), reply))
        if (inFlight == null) pump()
    }

    /** Feed every parsed stream item here; only the in-flight command can be answered. */
    fun onItem(item: StreamItem) {
        val w = inFlight ?: return
        if (!w.matcher(item)) return
        w.finish()
        inFlight = null
        val text = (item as? StreamItem.Text)?.line
        w.reply(
            if (text != null && text.startsWith("ERR")) CommandResult.Failed("${w.line}: $text")
            else CommandResult.Ok(item)
        )
        pump()
    }

    /** The port went away: nothing more will answer. */
    fun failAll(why: String) {
        val current = inFlight
        inFlight = null
        current?.let {
            it.finish()
            it.reply(CommandResult.Failed("${it.line}: $why"))
        }
        // Cleared before replying, so a reply that sends again cannot deadlock the queue.
        for (w in queue.toList()) {
            queue.remove(w)
            w.reply(CommandResult.Failed(why))
        }
    }

    private fun pump() {
        val w = queue.removeFirstOrNull() ?: return
        inFlight = w
        w.timer = scheduler.schedule(timeoutMs) { expire(w) }
        write((w.line + "\n").toByteArray(Charsets.US_ASCII))
    }

    private fun expire(w: Waiting) {
        if (inFlight !== w || w.done) return
        w.finish()
        inFlight = null
        w.reply(CommandResult.Failed("timeout: ${w.line}"))
        pump()
    }

    private fun Waiting.finish() {
        done = true
        timer?.cancel()
        timer = null
    }
}
