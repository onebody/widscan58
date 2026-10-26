// GPL-3.0-or-later.
package com.onebody.widscan58.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class CommandRunnerTest {
    private class FakeScheduler : Scheduler {
        val tasks = mutableListOf<() -> Unit>()
        override fun schedule(delayMs: Long, task: () -> Unit): Cancellable {
            tasks.add(task)
            return Cancellable { tasks.remove(task) }
        }

        /** Fire the oldest scheduled timeout. */
        fun expireOne() = tasks.take(1).forEach { it() }
    }

    private val written = mutableListOf<String>()
    private val scheduler = FakeScheduler()
    private val runner = CommandRunner({ written.add(String(it, Charsets.US_ASCII)) }, scheduler)
    private val replies = mutableListOf<CommandResult>()

    private fun send(line: String) = runner.send(line) { replies.add(it) }

    @Test
    fun `commands go out one at a time`() {
        send("CHANNELS R1=5658")
        send("SCAN START")
        assertEquals(listOf("CHANNELS R1=5658\n"), written)
        runner.onItem(StreamItem.Text("OK"))
        assertEquals(listOf("CHANNELS R1=5658\n", "SCAN START\n"), written)
    }

    @Test
    fun `a query is answered by its keyword, not by an unrelated ok`() {
        send("PROFILE?")
        runner.onItem(StreamItem.Text("OK whatever"))
        assertTrue(replies.isEmpty())
        runner.onItem(StreamItem.Text("PROFILE {\"name\":\"c5-race\"}"))
        val ok = replies.single() as CommandResult.Ok
        assertEquals("PROFILE {\"name\":\"c5-race\"}", (ok.item as StreamItem.Text).line)
    }

    @Test
    fun `an ERR reply fails the command and still frees the queue`() {
        send("CAPN 8")
        send("SCAN START")
        runner.onItem(StreamItem.Text("ERR bad_arg"))
        val failed = replies.removeAt(0) as CommandResult.Failed
        assertEquals("CAPN 8: ERR bad_arg", failed.why)
        assertEquals(listOf("CAPN 8\n", "SCAN START\n"), written)
    }

    @Test
    fun `frames do not answer a command but the last raw iq chunk does`() {
        send("SCAN START")
        runner.onItem(StreamItem.Frame(FrameType.SPECTRUM, ByteArray(20)))
        assertTrue("a spectrum frame must not end SCAN START", replies.isEmpty())
        runner.onItem(StreamItem.Text("OK"))
        replies.clear()

        send("RAWIQ 5658000 1024")
        runner.onItem(StreamItem.Frame(FrameType.RAWIQ, rawIqPayload(offset = 0, samples = 2)))
        assertTrue("an unfinished snapshot keeps RAWIQ in flight", replies.isEmpty())
        runner.onItem(StreamItem.Frame(FrameType.RAWIQ, rawIqPayload(offset = 2, samples = 2)))
        assertEquals(1, replies.size)
        assertTrue(replies.single() is CommandResult.Ok)
    }

    @Test
    fun `a timeout fails the command and the next one runs`() {
        send("INFO")
        send("STATUS?")
        scheduler.expireOne()
        assertEquals("timeout: INFO", (replies.removeAt(0) as CommandResult.Failed).why)
        assertEquals(listOf("INFO\n", "STATUS?\n"), written)
    }

    @Test
    fun `losing the port fails everything outstanding`() {
        send("INFO")
        send("SCAN START")
        runner.failAll("device gone")
        assertEquals(2, replies.size)
        assertTrue(runner.busy.not())
    }

    /** One RAWIQ chunk of `samples` IQ pairs out of a four-sample snapshot. */
    private fun rawIqPayload(offset: Int, samples: Int): ByteArray {
        val p = ByteArray(14 + samples * 2)
        p[8] = 4.toByte() // n
        p[10] = offset.toByte()
        return p
    }
}
