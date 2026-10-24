// Handheld spectrum instrument: USB CDC link to the board, the shared engine in
// :core, and the chart/tiles/controls this screen draws. GPL-3.0-or-later.
package com.onebody.widscan58

import android.app.Activity
import android.content.Intent
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbManager
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.LinearLayout
import android.widget.SeekBar
import android.widget.TextView
import com.onebody.widscan58.core.Bands
import com.onebody.widscan58.core.Cancellable
import com.onebody.widscan58.core.CommandResult
import com.onebody.widscan58.core.CommandRunner
import com.onebody.widscan58.core.Scheduler
import com.onebody.widscan58.core.StreamItem
import com.onebody.widscan58.core.StreamParser
import com.onebody.widscan58.core.Store
import com.onebody.widscan58.core.channelTile
import com.onebody.widscan58.core.restoreCommands
import com.onebody.widscan58.usb.CdcLayout
import com.onebody.widscan58.usb.UsbAccess
import com.onebody.widscan58.usb.UsbSerialLink
import com.onebody.widscan58.usb.cdcLayoutOf
import com.onebody.widscan58.usb.findBoard
import com.onebody.widscan58.usb.usbExtra
import com.onebody.widscan58.view.ChannelTileView
import com.onebody.widscan58.view.PanoramaView
import com.onebody.widscan58.view.Style
import kotlin.math.roundToInt

class Widscan58Activity : Activity() {
    private val main = Handler(Looper.getMainLooper())

    /** Timeouts run on the main looper; the parser never blocks on a reply. */
    private val scheduler = Scheduler { delayMs, task ->
        val runnable = Runnable { task() }
        main.postDelayed(runnable, delayMs)
        Cancellable { main.removeCallbacks(runnable) }
    }

    private val prefs by lazy { Prefs(this) }
    private val store = Store({ SystemClock.elapsedRealtime() }, JsonReplies())

    private lateinit var theme: Style
    private lateinit var panorama: PanoramaView
    private lateinit var tiles: List<ChannelTileView>
    private lateinit var tileGrid: View
    private lateinit var bandRow: LinearLayout
    private lateinit var bandChips: List<Pair<String, Button>>
    private lateinit var statusLine: TextView
    private lateinit var scanButton: Button
    private lateinit var holdsButton: Button
    private lateinit var gainBar: SeekBar
    private lateinit var gainValue: TextView
    private lateinit var maxChip: Button
    private lateinit var avgChip: Button

    private var link: UsbSerialLink? = null
    private var sim: SimFeed? = null
    private var runner: CommandRunner? = null
    private var parser: StreamParser? = null
    private var selected = -1
    private var draggingGain = false
    private var lastFrameMs = 0L
    private var drawnVersion = -1L
    private var note = ""
    private var showSim = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        setContentView(R.layout.activity_instrument)
        padContentForSystemBars()
        theme = Style(this)
        statusLine = findViewById(R.id.status)
        bandRow = findViewById(R.id.bands)
        panorama = findViewById(R.id.panorama)
        tileGrid = findViewById(R.id.tiles)
        scanButton = findViewById(R.id.scan)
        holdsButton = findViewById(R.id.holds)
        gainBar = findViewById(R.id.gain)
        gainValue = findViewById(R.id.gainValue)
        tiles = TILE_IDS.map { findViewById<ChannelTileView>(it) }

        panorama.store = store
        panorama.onPick = { selected = it; refresh() }
        panorama.onFit = { lo, hi ->
            prefs.dbMin = lo.roundToInt()
            prefs.dbMax = hi.roundToInt()
            refresh()
        }

        bandChips = Bands.ALL.map { band ->
            val button = chip(band.letter).apply {
                contentDescription = band.name
                setOnClickListener { selectBand(band.letter) }
            }
            bandRow.addView(button)
            band.letter to button
        }
        bandRow.addView(spacer())
        maxChip = chip(getString(R.string.chip_max)).apply {
            setOnClickListener { prefs.showMaxHold = !prefs.showMaxHold; refresh() }
        }
        avgChip = chip(getString(R.string.chip_avg)).apply {
            setOnClickListener { prefs.showAverage = !prefs.showAverage; refresh() }
        }
        bandRow.addView(maxChip)
        bandRow.addView(avgChip)

        tiles.forEach { it.onPick = { id -> selected = id; refresh() } }
        scanButton.setOnClickListener { toggleScan() }
        holdsButton.text = getString(R.string.holds)
        holdsButton.setOnClickListener { store.resetHolds() }
        gainBar.max = Prefs.GAIN_MAX
        gainBar.progress = prefs.gain
        gainValue.text = prefs.gain.toString()
        gainBar.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(bar: SeekBar, value: Int, fromUser: Boolean) {
                draggingGain = fromUser
                gainValue.text = value.toString()
            }

            override fun onStartTrackingTouch(bar: SeekBar) {
                draggingGain = true
            }

            // One command when the finger lifts, not one per pixel of travel.
            override fun onStopTrackingTouch(bar: SeekBar) {
                draggingGain = false
                applyGain(bar.progress)
            }
        })

        statusLine.text = getString(R.string.plug_in)
        // `adb shell am start -n com.onebody.widscan58/.Widscan58Activity --ez sim true`
        showSim = intent.getBooleanExtra("sim", false)
        if (showSim) {
            sim = SimFeed(store, main, ::refresh).also { it.start() }
        }
    }

    override fun onResume() {
        super.onResume()
        main.post(drawLoop)
        main.postDelayed(watchdog, WATCHDOG_PERIOD_MS)
        if (showSim) sim?.start()
        if (link == null) findBoard(usbManager())?.let(::requestAndOpen)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        // The USB_DEVICE_ATTACHED filter launches or raises us with the device in hand.
        (intent.usbExtra(UsbManager.EXTRA_DEVICE, UsbDevice::class.java) ?: findBoard(usbManager()))
            ?.let(::requestAndOpen)
    }

    override fun onPause() {
        super.onPause()
        main.removeCallbacks(drawLoop)
        main.removeCallbacks(watchdog)
        sim?.stop()
        closeLink(getString(R.string.note_paused))
    }

    // ---- drawing: one coalesced refresh per frame, only when the data changed ----

    private val drawLoop = object : Runnable {
        override fun run() {
            if (store.version != drawnVersion) {
                drawnVersion = store.version
                refresh()
            }
            main.postDelayed(this, FRAME_MS)
        }
    }

    private fun refresh() {
        panorama.selected = selected
        panorama.dbMin = prefs.dbMin.toDouble()
        panorama.dbMax = prefs.dbMax.toDouble()
        panorama.showMaxHold = prefs.showMaxHold
        panorama.showAverage = prefs.showAverage
        panorama.invalidate()

        val now = store.now()
        val planned = store.profile != null
        tileGrid.visibility = if (planned) View.VISIBLE else View.GONE
        if (planned) tiles.forEachIndexed { id, view -> view.bind(channelTile(store, id, selected), now) }

        val deviceGain = store.status?.gain
        if (!draggingGain) {
            gainBar.progress = deviceGain ?: prefs.gain
            gainValue.text = gainBar.progress.toString()
        }
        tint(maxChip, prefs.showMaxHold)
        tint(avgChip, prefs.showAverage)
        val band = currentBand()
        bandChips.forEach { (letter, chip) -> tint(chip, letter == band) }

        val live = link != null || sim != null
        val running = sim?.running ?: scanRunning()
        scanButton.setText(if (live && running) R.string.scan_stop else R.string.scan_start)
        scanButton.isEnabled = live
        gainBar.isEnabled = live
        statusLine.text = status()
    }

    private fun scanRunning(): Boolean = store.telemetry?.running ?: (store.status?.running ?: 1) != 0

    private fun status(): String {
        val parts = mutableListOf<String>()
        if (note.isNotEmpty()) parts.add(note)
        val l = link
        when {
            l != null -> parts.add(l.description)
            sim != null -> parts.add(getString(R.string.status_sim))
            else -> return note.ifEmpty { getString(R.string.plug_in) }
        }
        parts.add(store.profile?.name ?: getString(R.string.status_connecting))
        store.telemetry?.let { parts.add(getString(R.string.status_rot, it.rotationHz)) }
        store.status?.let { parts.add(getString(R.string.status_hops, it.plan.size)) }
        parts.add(getString(R.string.status_gain, store.status?.gain ?: prefs.gain))
        store.telemetry?.let { parts.add(getString(R.string.status_link, it.linkFill * 100)) }
        return parts.joinToString(" · ")
    }

    /** The band the device's current plan belongs to, for the chip highlight. */
    private fun currentBand(): String? {
        val names = store.profile?.channels?.map { it.name }.orEmpty().ifEmpty { prefs.channels }
        return names.firstOrNull()?.take(1)?.let { letter -> Bands.ALL.find { it.letter == letter }?.letter }
    }

    // ---- controls ----

    private fun selectBand(letter: String) {
        prefs.setBand(letter)
        note = ""
        command(Bands.channelsCommand(prefs.deviceSettings().channels))
        command("PROFILE?")
        command("STATUS?")
        refresh()
    }

    private fun applyGain(value: Int) {
        prefs.setGain(value)
        command("GAIN MANUAL $value")
        command("STATUS?")
        refresh()
    }

    private fun toggleScan() {
        command(if (scanRunning()) "SCAN STOP" else "SCAN START")
        command("STATUS?")
        refresh()
    }

    // ---- link ----

    private fun usbManager() = getSystemService(UsbManager::class.java)!!

    private fun requestAndOpen(device: UsbDevice) {
        if (link != null) return
        UsbAccess.withPermission(this, device, ::openLink) { note = it }
    }

    private fun openLink(device: UsbDevice) {
        if (link != null) return
        val layout: CdcLayout = cdcLayoutOf(device)
            ?: run {
                note = getString(R.string.note_no_cdc, device.deviceName)
                return
            }
        val opened = runCatching { UsbSerialLink.open(this, device, layout) }
            .onFailure { note = getString(R.string.note_open_failed, it.message.orEmpty()) }
            .getOrNull() ?: return
        link = opened
        parser = StreamParser()
        runner = CommandRunner(write = { opened.write(it) }, scheduler = scheduler)
        opened.onBytes = ::onBytes
        opened.onClose = { why -> main.post { closeLink(why) } }
        lastFrameMs = SystemClock.elapsedRealtime()
        note = ""
        // The firmware boots silent in RotorHazard mode; ask for our stream first.
        command("PROTOCOL MONITOR") { handshake() }
    }

    /** Put the board in the stored state, then run the monitor scan. */
    private fun handshake() {
        command("SCAN STOP")
        prefs.deviceSettings().restoreCommands().forEach { command(it) }
        // The C5 monitor-mode spectrum: 4 hops per frame, 256 bins across 40 MHz.
        command("SPEC 4 256 40000000 MAX")
        command("PROFILE?")
        command("STATUS?")
        command("SPURS?")
        command("SCAN START")
        command("INFO")
    }

    /** Bytes arrive on the reader thread; every state change is marshalled to the UI. */
    private fun onBytes(chunk: ByteArray) {
        val items = parser?.push(chunk) ?: return
        if (items.isEmpty()) return
        val sawFrame = items.any { it is StreamItem.Frame }
        main.post {
            if (sawFrame) lastFrameMs = SystemClock.elapsedRealtime()
            for (item in items) {
                runner?.onItem(item)
                store.ingest(item)
            }
        }
    }

    private fun closeLink(why: String) {
        val l = link ?: return
        link = null
        parser = null
        runner?.failAll("link closed")
        runner = null
        l.onBytes = null
        l.onClose = null
        l.close()
        note = getString(R.string.note_disconnected, why)
    }

    private fun command(line: String, reply: (CommandResult) -> Unit = {}) {
        sim?.let {
            it.onCommand(line)
            return
        }
        val r = runner ?: run {
            note = getString(R.string.note_not_connected, line)
            return
        }
        r.send(line) { result ->
            if (result is CommandResult.Failed) note = result.why
            reply(result)
        }
    }

    /** Telemetry arrives at 4 Hz even when stopped, so a quiet link means the board restarted. */
    private val watchdog = object : Runnable {
        override fun run() {
            if (link != null && SystemClock.elapsedRealtime() - lastFrameMs > NO_DATA_MS) {
                lastFrameMs = SystemClock.elapsedRealtime()
                note = getString(R.string.note_no_data)
                command("PROTOCOL MONITOR") { handshake() }
            }
            main.postDelayed(this, WATCHDOG_PERIOD_MS)
        }
    }

    /** A band or trace toggle: single letters get a fixed square so the row reads as a keypad. */
    private fun chip(label: String) = Button(this).apply {
        text = label
        isAllCaps = false
        textSize = 11f
        minWidth = 0
        minimumWidth = 0
        minHeight = 0
        setPadding(theme.dp(6f).roundToInt(), 0, theme.dp(6f).roundToInt(), 0)
        layoutParams = LinearLayout.LayoutParams(
            if (label.length == 1) theme.dp(40f).roundToInt() else LinearLayout.LayoutParams.WRAP_CONTENT,
            theme.dp(34f).roundToInt(),
        ).apply { marginEnd = theme.dp(2f).roundToInt() }
    }

    private fun tint(button: Button, on: Boolean) = button.setTextColor(if (on) theme.accent else theme.muted)

    private fun spacer() = View(this).apply {
        layoutParams = LinearLayout.LayoutParams(theme.dp(10f).roundToInt(), 1)
    }

    companion object {
        private const val FRAME_MS = 33L
        private const val WATCHDOG_PERIOD_MS = 1000L
        private const val NO_DATA_MS = 3000L
        private val TILE_IDS = intArrayOf(
            R.id.tile0, R.id.tile1, R.id.tile2, R.id.tile3, R.id.tile4, R.id.tile5, R.id.tile6, R.id.tile7,
        )
    }
}
