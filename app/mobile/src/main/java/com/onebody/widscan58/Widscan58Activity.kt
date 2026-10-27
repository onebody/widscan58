// Handheld spectrum instrument: USB CDC link to the board, the shared engine in
// :core, and the chart/tiles/controls this screen draws. GPL-3.0-or-later.
package com.onebody.widscan58

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbManager
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.provider.OpenableColumns
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.LinearLayout
import android.widget.PopupMenu
import android.widget.SeekBar
import android.widget.TextView
import com.onebody.widscan58.core.Bands
import com.onebody.widscan58.core.Cancellable
import com.onebody.widscan58.core.CommandResult
import com.onebody.widscan58.core.CommandRunner
import com.onebody.widscan58.core.DeviceCommands
import com.onebody.widscan58.core.DropWindow
import com.onebody.widscan58.core.GainMode
import com.onebody.widscan58.core.LinkRate
import com.onebody.widscan58.core.LogDir
import com.onebody.widscan58.core.Power
import com.onebody.widscan58.core.RhLink
import com.onebody.widscan58.core.RhLinkError
import com.onebody.widscan58.core.RhSerialLink
import com.onebody.widscan58.core.RhSimLink
import com.onebody.widscan58.core.Scheduler
import com.onebody.widscan58.core.SessionRecorder
import com.onebody.widscan58.core.StreamItem
import com.onebody.widscan58.core.StreamParser
import com.onebody.widscan58.core.Store
import com.onebody.widscan58.core.channelTile
import com.onebody.widscan58.core.channelsCsv
import com.onebody.widscan58.core.readSession
import com.onebody.widscan58.core.restoreCommands
import com.onebody.widscan58.core.spectrumCsv
import com.onebody.widscan58.usb.CdcLayout
import com.onebody.widscan58.usb.UsbAccess
import com.onebody.widscan58.usb.UsbSerialLink
import com.onebody.widscan58.usb.cdcLayoutOf
import com.onebody.widscan58.usb.findBoard
import com.onebody.widscan58.usb.usbExtra
import com.onebody.widscan58.view.ChannelTileView
import com.onebody.widscan58.view.PanoramaView
import com.onebody.widscan58.view.Style
import java.io.OutputStream
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
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

    /** The wire of whichever source is running, so a capture can be saved any time. */
    private val recorder = SessionRecorder({ SystemClock.elapsedRealtime() })
    private val feed = ReplayFeed(main, ::feedBytes, ::replayFinished)

    private lateinit var theme: Style
    private lateinit var panorama: PanoramaView
    private lateinit var tiles: List<ChannelTileView>
    private lateinit var tileGrid: View
    private lateinit var bandRow: LinearLayout
    private lateinit var bandChips: List<Pair<String, Button>>
    private lateinit var statusLine: TextView
    private lateinit var scanButton: Button
    private lateinit var holdsButton: Button
    private lateinit var setupButton: Button
    private lateinit var gainBar: SeekBar
    private lateinit var gainValue: TextView
    private lateinit var maxChip: Button
    private lateinit var avgChip: Button
    private lateinit var moreChip: Button
    private var sheet: SettingsSheet? = null
    private var general: GeneralSettingsSheet? = null
    private var history: HistorySheet? = null
    private var telemetry: TelemetrySheet? = null
    private var console: ConsoleSheet? = null
    private var gpio: GpioSheet? = null
    private var session: SessionSheet? = null

    /** One raw capture at a time: the board holds its aperture while the sheet waits for the chunks. */
    private var rawiq: RawIqSheet? = null

    /** Power meter mode: the board runs the calibration's scan while the page is open. */
    private var power: PowerSheet? = null
    private var monitorScan: Power.MonitorRestore? = null
    /** True while the entry note waits for the board to report the calibration scan running. */
    private var awaitPowerScan = false

    /** The RotorHazard session, if the board is in that mode: while it runs, it owns the wire. */
    private var rh: RhFeed? = null
    private var rhVoice: VoiceOut? = null
    private var rhSheet: RhSheet? = null

    /** The web card derives these from the counters, and does it whether or not it is on screen. */
    private val linkRate = LinkRate()
    private val dropWindow = DropWindow()

    private var link: UsbSerialLink? = null
    private var sim: SimFeed? = null
    private var runner: CommandRunner? = null
    private var parser: StreamParser? = null
    private var selected = -1
    private var zoom: ZoomSheet? = null
    private var draggingGain = false
    private var lastFrameMs = 0L
    private var drawnVersion = -1L
    private var note = ""
    private var pendingName = ""
    private var showSim = false

    override fun attachBaseContext(newBase: Context) {
        // Before the first view is built: the language decides which resources every label below reads.
        super.attachBaseContext(AppLanguage.wrap(newBase))
    }

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
        setupButton = chip(getString(R.string.chip_settings)).apply {
            contentDescription = getString(R.string.settings_title)
            setOnClickListener { openSetup() }
        }
        // The row is full at six bands plus the live controls, so the whole-screen
        // views — history, telemetry, and the ones after them — come from a menu.
        moreChip = chip(getString(R.string.chip_more)).apply {
            contentDescription = getString(R.string.more_views)
            setOnClickListener { openMore() }
        }
        bandRow.addView(maxChip)
        bandRow.addView(avgChip)
        bandRow.addView(setupButton)
        bandRow.addView(moreChip)
        sheet = SettingsSheet(this, prefs, ::applySetting, ::refresh, ::planNames, ::applyPlan)

        // A tile is a button: it opens the channel's own chart. The panorama stays a scrubber.
        tiles.forEach { it.onPick = { id -> selected = id; refresh(); openZoom(id) } }
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
            // The simulator puts wire bytes on the same sink a board does, so it needs the same parser.
            parser = StreamParser()
            sim = SimFeed(main, ::feedBytes, ::refresh).also { it.start() }
            // The same restore a board gets, so the sim comes up in the stored state.
            handshake()
        }
        // …and `--ez rh true` runs a RotorHazard board with no hardware behind it.
        if (intent.getBooleanExtra("rh", false)) startRh(RhSimLink({ SystemClock.elapsedRealtime() }, JsonWrites::rhConfig))
    }

    override fun onResume() {
        super.onResume()
        main.post(drawLoop)
        main.postDelayed(watchdog, WATCHDOG_PERIOD_MS)
        feed.resume()
        if (showSim) sim?.start()
        rh?.resume()
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
        // The meter's page ends with the session, and its scan goes back while the wire is still up.
        leavePower()
        sheet?.dismiss()
        general?.dismiss()
        zoom?.dismiss()
        history?.dismiss()
        telemetry?.dismiss()
        console?.dismiss()
        gpio?.dismiss()
        session?.dismiss()
        rawiq?.dismiss()
        rhSheet?.dismiss()
        main.removeCallbacks(drawLoop)
        main.removeCallbacks(watchdog)
        feed.pause()
        sim?.stop()
        closeLink(getString(R.string.note_paused))
        // A board's session went with that port; the simulator keeps its link, so its cycle has to be
        // stopped here or it polls, and calls laps, from the background.
        rh?.pause()
    }

    override fun onDestroy() {
        super.onDestroy()
        endRh()
        rhVoice?.release()
        rhVoice = null
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
        if (awaitPowerScan && Power.scanIsReady(store.status)) {
            awaitPowerScan = false
            note = ""
        }
        sampleLink()
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
        // The manual-gain ceiling is the PHY table depth the board reports, not our guess.
        store.status?.gainMax?.takeIf { it > 0 }?.let { if (gainBar.max != it) gainBar.max = it }
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
        setupButton.isEnabled = live
        sheet?.update(store.status)
        zoom?.update()
        history?.update()
        telemetry?.update(linkCounters())
        console?.update()
        gpio?.update()
        session?.update()
        rhSheet?.update()
        power?.update()
        rawiq?.update()
        statusLine.text = status()
    }

    private fun scanRunning(): Boolean = store.telemetry?.running ?: (store.status?.running ?: 1) != 0

    private fun status(): String {
        val parts = mutableListOf<String>()
        if (note.isNotEmpty()) parts.add(note)
        val l = link
        when {
            feed.started -> parts.add(getString(R.string.status_replay, feed.name))
            l != null -> parts.add(l.description)
            sim != null -> parts.add(getString(R.string.status_sim))
            else -> return note.ifEmpty { getString(R.string.plug_in) }
        }
        // The board is answering a different protocol than the instrument's own frames.
        if (rh != null) return (parts + getString(R.string.rh_title)).joinToString(" · ")
        // The meter runs the calibration's scan, not this profile's own.
        if (power != null) parts.add(getString(R.string.power_title))
        parts.add(store.profile?.name ?: getString(R.string.status_connecting))
        store.telemetry?.let { parts.add(getString(R.string.status_rot, it.rotationHz)) }
        store.status?.let { parts.add(getString(R.string.status_hops, it.plan.size)) }
        parts.add(getString(R.string.status_gain, store.status?.gain ?: prefs.gain))
        store.telemetry?.let { parts.add(getString(R.string.status_link, it.linkFill * 100)) }
        return parts.joinToString(" · ")
    }

    /** The band the device's current plan belongs to, for the chip highlight. */
    private fun currentBand(): String? =
        planNames().firstOrNull()?.take(1)?.let { letter -> Bands.ALL.find { it.letter == letter }?.letter }

    // ---- controls ----

    /** The plan the board dials now, falling back to what we would send it. */
    private fun planNames(): List<String> =
        store.profile?.channels?.map { it.name }.orEmpty().ifEmpty { prefs.channels }

    /**
     * A new plan is measured whole: CHANNELS switches every channel of the plan it
     * builds back on, and the old mask picked channels by position in the old plan.
     */
    private fun applyPlan(names: List<String>) {
        prefs.setChannels(names)
        prefs.enabledMask = (1 shl prefs.channels.size) - 1
        note = ""
        val settings = prefs.deviceSettings()
        command(Bands.channelsCommand(settings.channels))
        command(DeviceCommands.enable(settings.enabledFor()))
        command("PROFILE?")
        command("STATUS?")
        refresh()
    }

    private fun selectBand(letter: String) = applyPlan(Bands.names(letter))

    private fun applyGain(value: Int) {
        prefs.setGain(value, gainBar.max)
        command(DeviceCommands.gain(GainMode.MANUAL, value, gainBar.max))
        command("STATUS?")
        refresh()
    }

    /** The sweep parameters only mean something against a board that can answer for them. */
    private fun openSetup() {
        if (link == null && sim == null) {
            note = getString(R.string.plug_in)
            refresh()
            return
        }
        sheet?.show(store.status)
    }

    /** The app's own choices, which — unlike the scanner's parameters — are answerable with no board in hand. */
    private fun openGeneral() {
        general?.dismiss()
        general = GeneralSettingsSheet(this, prefs, ::recreate).also { it.show() }
    }

    /** A tile opens that channel's chart, and its stepper moves the rest of the screen with it. */
    private fun openZoom(id: Int) {
        if (store.profile?.channels?.getOrNull(id) == null) return
        zoom?.dismiss()
        zoom = ZoomSheet(this, prefs, store) { picked ->
            selected = picked
            refresh()
        }.also { it.show(id) }
    }

    /** The whole plan across time, which is the one view the panorama cannot give. */
    private fun openHistory() {
        history?.dismiss()
        history = HistorySheet(this, prefs, store).also { it.show(selected) }
    }

    private fun openMore() {
        val menu = PopupMenu(this, moreChip)
        menu.menu.add(0, MENU_GENERAL, 0, R.string.general_title)
        menu.menu.add(0, MENU_SCALE, 0, R.string.auto_scale)
        menu.menu.add(0, MENU_HISTORY, 0, R.string.history_title)
        menu.menu.add(0, MENU_TELEMETRY, 0, R.string.telemetry_title)
        menu.menu.add(0, MENU_CONSOLE, 0, R.string.console_title)
        menu.menu.add(0, MENU_GPIO, 0, R.string.gpio_title)
        menu.menu.add(0, MENU_SESSION, 0, R.string.session_title)
        menu.menu.add(0, MENU_RH, 0, R.string.rh_title)
        menu.menu.add(0, MENU_POWER, 0, R.string.power_title)
        menu.menu.add(0, MENU_RAWIQ, 0, R.string.rawiq_title)
        menu.menu.add(0, MENU_HELP, 0, R.string.help_title)
        menu.menu.add(0, MENU_ABOUT, 0, R.string.about_title)
        menu.setOnMenuItemClickListener { item ->
            when (item.itemId) {
                MENU_GENERAL -> openGeneral()
                MENU_SCALE -> panorama.autoScale()
                MENU_HISTORY -> openHistory()
                MENU_TELEMETRY -> openTelemetry()
                MENU_CONSOLE -> openConsole()
                MENU_GPIO -> openGpio()
                MENU_SESSION -> openSession()
                MENU_RH -> openRh()
                MENU_POWER -> openPower()
                MENU_RAWIQ -> openRawIq()
                MENU_HELP -> startActivity(Intent(this, HelpActivity::class.java))
                MENU_ABOUT -> startActivity(Intent(this, AboutActivity::class.java))
                else -> return@setOnMenuItemClickListener false
            }
            true
        }
        menu.show()
    }

    /** The wire in both directions, which is the only way to see what the board really said. */
    private fun openConsole() {
        console?.dismiss()
        console = ConsoleSheet(this, store, ::applySetting).also { it.show() }
    }

    /** The numbers the board reports about itself, which the instrument layout has no room for. */
    private fun openTelemetry() {
        telemetry?.dismiss()
        telemetry = TelemetrySheet(this, store).also { it.show() }
    }

    /** The samples behind one reading, which is the only way to see what the ADC actually held. */
    private fun openRawIq() {
        rawiq?.dismiss()
        rawiq = RawIqSheet(this, store, ::sendCapture).also { it.show(selected) }
    }

    /** Which pins the board drives at start-up — only a board can say. */
    private fun openGpio() {
        if (link == null && sim == null) {
            note = getString(R.string.plug_in)
            refresh()
            return
        }
        gpio?.dismiss()
        gpio = GpioSheet(this, store, ::applyGpio).also { it.show() }
    }

    // ---- power meter mode ----

    /** The meter is a different scan on the same wire, so it needs a live one. */
    private fun openPower() {
        if (feed.started) {
            note = getString(R.string.note_replay_readonly)
            refresh()
            return
        }
        if (link == null && sim == null) {
            note = getString(R.string.plug_in)
            refresh()
            return
        }
        // Reopening from the menu keeps the mode: dismiss() clears the page's own exit path first.
        power?.dismiss()
        val sheet = PowerSheet(this, prefs, store, ::applyGpio, ::applyPlan)
        // Closing the page is leaving the mode: the board's own scan goes back with it.
        sheet.onDismissed = { leavePower() }
        power = sheet
        sheet.show()
        enterPower()
    }

    /** The scan the calibration was measured with: one LO, manual gain 0, a ±15 MHz window. */
    private fun enterPower() {
        Power.monitorScan(store.status)?.let { monitorScan = it }
        Power.commands(store.profile?.usableHz ?: SPEC_FULL_SPAN_HZ).forEach { command(it) }
        command("PROFILE?")
        command("STATUS?")
        awaitPowerScan = true
        note = getString(R.string.power_entering)
        refresh()
    }

    private fun leavePower() {
        val sheet = power ?: return
        power = null
        sheet.dismiss()
        // Only a board that is still here can be handed its scan back.
        if (link != null || sim != null) restoreMonitorScan()
        monitorScan = null
        awaitPowerScan = false
        note = getString(R.string.power_left)
        refresh()
    }

    /** What the instrument was sweeping before the meter took over, or the profile's own default. */
    private fun restoreMonitorScan() {
        if (!Power.isPowerScan(store.status)) return
        val restore = monitorScan ?: store.profile?.name?.let { Power.MONITOR_DEFAULTS[it] } ?: return
        // The meter wrote MODE and GAIN too; the web gets those back by restoring on reconnect,
        // which a phone that stays plugged in never gets.
        val settings = prefs.deviceSettings()
        command(DeviceCommands.mode(settings.mode))
        command(DeviceCommands.gain(settings.gainMode, settings.gain, gainBar.max))
        restore.commands().forEach { command(it) }
        command("PROFILE?")
        command("STATUS?")
    }

    /** Record the wire, play a capture back, or hand the readings to a spreadsheet. */
    private fun openSession() {
        session?.dismiss()
        session = SessionSheet(
            this,
            recorder,
            feed,
            object : SessionActions {
                override fun recordable() = link != null || sim != null

                // A replay takes the place of a source, so nothing else may be running to start one.
                override fun replayable() = link == null && sim == null && !recorder.active

                override fun startRecording() {
                    recorder.begin(JsonWrites.sessionPreamble(store))
                    note = ""
                    refresh()
                }

                override fun stopAndSave() {
                    recorder.stop()
                    saveRecording()
                }

                override fun saveLast() = saveRecording()

                override fun pickReplay() = pickFile(REQ_OPEN_SESSION, true, "", ANY_TYPE)

                override fun stopReplay() = replayFinished()

                override fun export(kind: ExportKind) = pickFile(
                    if (kind == ExportKind.CHANNELS) REQ_SAVE_CHANNELS else REQ_SAVE_SPECTRUM,
                    false,
                    "widscan58-" + kind.name.lowercase(Locale.US) + "-" + stamp() + ".csv",
                    MIME_CSV,
                )

                override fun note() = this@Widscan58Activity.note
            },
        ).also { it.show() }
    }

    private fun saveRecording() = pickFile(REQ_SAVE_SESSION, false, "widscan58-" + stamp() + ".widscan58", MIME_SESSION)

    /** The storage app runs outside the process, so it answers through onActivityResult. */
    private fun pickFile(request: Int, open: Boolean, name: String, mime: String) {
        pendingName = name
        val intent = Intent(if (open) Intent.ACTION_OPEN_DOCUMENT else Intent.ACTION_CREATE_DOCUMENT).apply {
            addCategory(Intent.CATEGORY_OPENABLE)
            type = mime
            if (!open) putExtra(Intent.EXTRA_TITLE, name)
        }
        runCatching { startActivityForResult(intent, request) }
            .onFailure { note = getString(R.string.note_save_failed, name, it.message.orEmpty()) }
        refresh()
    }

    override fun onActivityResult(request: Int, result: Int, data: Intent?) {
        super.onActivityResult(request, result, data)
        val uri = data?.data
        if (result != RESULT_OK || uri == null) {
            // A cancelled save keeps the capture, so the sheet still offers to save it.
            refresh()
            return
        }
        when (request) {
            REQ_SAVE_SESSION -> {
                val written = formatBytes(recorder.bytes)
                writeFile(uri, pendingName, written, recorder::discard) { recorder.writeTo(it) }
            }
            REQ_SAVE_CHANNELS -> writeText(uri, pendingName, channelsCsv(store))
            REQ_SAVE_SPECTRUM -> writeText(uri, pendingName, spectrumCsv(store))
            else -> readReplay(uri)
        }
    }

    /** A phone's storage provider is slow enough to stay off the drawing thread. */
    private fun writeFile(
        uri: Uri,
        name: String,
        detail: String,
        done: () -> Unit = {},
        body: (OutputStream) -> Unit,
    ) {
        Thread {
            val outcome = runCatching { contentResolver.openOutputStream(uri)?.use(body) ?: error("no stream for " + uri) }
            main.post {
                val ok = outcome.isSuccess
                if (ok) done()
                note = if (ok) {
                    getString(R.string.note_saved, name, detail)
                } else {
                    getString(R.string.note_save_failed, name, outcome.exceptionOrNull()?.message.orEmpty())
                }
                refresh()
            }
        }.start()
    }

    private fun writeText(uri: Uri, name: String, text: String) {
        val bytes = text.toByteArray(Charsets.UTF_8)
        writeFile(uri, name, formatBytes(bytes.size.toLong())) { it.write(bytes) }
    }

    private fun readReplay(uri: Uri) {
        val name = displayName(uri)
        Thread {
            val read = runCatching { contentResolver.openInputStream(uri)?.use { readSession(it) } ?: emptyList() }
            main.post {
                val chunks = read.getOrDefault(emptyList())
                note = when {
                    read.isFailure -> getString(R.string.note_replay_failed, name, read.exceptionOrNull()?.message.orEmpty())
                    chunks.isEmpty() -> getString(R.string.note_replay_empty)
                    else -> ""
                }
                if (chunks.isNotEmpty()) {
                    // A replay owns the parser exactly as a board does: the store cannot tell them apart.
                    parser = StreamParser()
                    lastFrameMs = SystemClock.elapsedRealtime()
                    feed.start(name, chunks)
                }
                refresh()
            }
        }.start()
    }

    /** The session ran out, or the user stopped it: put the instrument back on standby. */
    private fun replayFinished() {
        feed.stop()
        parser = null
        note = getString(R.string.note_replay_finished)
        refresh()
    }

    /** What the storage app calls the file, which is not the last segment of a content: URI. */
    private fun displayName(uri: Uri): String {
        var name = ""
        val column = OpenableColumns.DISPLAY_NAME
        contentResolver.query(uri, arrayOf(column), null, null, null)?.use { c ->
            val at = c.getColumnIndex(column)
            if (at >= 0 && c.moveToFirst()) name = c.getString(at).orEmpty()
        }
        return name.ifEmpty { uri.lastPathSegment?.substringAfterLast('/').orEmpty() }
    }

    private fun stamp(): String = SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(Date())

    /** KiB/s and dropped frames are rates off counters, so they need sampling between the panels. */
    private fun sampleLink() {
        val now = store.now()
        parser?.let { linkRate.sample(now, it.bytes) }
        store.telemetry?.let { dropWindow.add(now, it.drops) }
    }

    private fun linkCounters(): LinkCounters {
        val p = parser ?: return LinkCounters.STANDBY
        return LinkCounters(
            live = true,
            bytes = p.bytes,
            bytesPerSecond = linkRate.perSecond,
            crcErrors = p.crcErrors,
            resyncBytes = p.resyncBytes,
            recentDrops = dropWindow.recent(store.telemetry?.drops ?: 0),
        )
    }

    /** One write, then the reply that lets the form show the device's state instead of our request. */
    private fun applySetting(line: String) {
        command(line)
        // A query already answers for itself; only a change needs the status read back.
        val answers = line.endsWith("?") || line == DeviceCommands.REBOOT || line == DeviceCommands.BOOTLOADER
        if (!answers) command("STATUS?")
        refresh()
    }

    /** The GPIO sheet reads its own state back, so a write needs no STATUS? alongside it. */
    private fun applyGpio(line: String) {
        command(line)
        refresh()
    }

    /** A capture is answered by its own chunks, so there is nothing to read back after it. */
    private fun sendCapture(line: String) {
        command(line)
    }

    private fun toggleScan() {
        command(if (scanRunning()) "SCAN STOP" else "SCAN START")
        command("STATUS?")
        refresh()
    }

    // ---- RotorHazard mode ----

    /**
     * Ask the board to switch, then hand the port over. Only a board can be asked: the line that
     * leaves monitor mode is the one thing our monitor simulator has no reason to answer.
     */
    private fun openRh() {
        if (rh != null) {
            rhSheet?.show()
            return
        }
        val l = link
        if (feed.started || l == null || sim != null) {
            note = getString(if (feed.started) R.string.note_replay_readonly else R.string.rh_need_board)
            refresh()
            return
        }
        command("SCAN STOP")
        command("PROTOCOL RH") { result ->
            if (result is CommandResult.Failed) {
                note = result.why
                refresh()
                return@command
            }
            // From here the wire answers a different protocol: nothing may be left waiting for a reply.
            runner?.failAll("RotorHazard mode")
            startRh(RhSerialLink({ SystemClock.elapsedRealtime() }) { bytes ->
                if (!l.write(bytes)) throw RhLinkError("usb write failed")
            })
        }
        note = getString(R.string.rh_entering)
        refresh()
    }

    /** The poll loop starts here; the sheet is the only thing that reads what it finds. */
    private fun startRh(source: RhLink) {
        val voice = rhVoice ?: VoiceOut(this, prefs).also { rhVoice = it }
        val session = RhFeed(prefs, store, source, main)
        rh = session
        session.model.onLap = { seat, lapS ->
            val channel = seat.channel
            if (channel != null) main.post { voice.speak(lapCallout(channel, lapS)) }
        }
        session.start(planNames())
        rhSheet?.dismiss()
        rhSheet = RhSheet(this, prefs, session, voice, ::leaveRh).also { it.show() }
        note = ""
        refresh()
    }

    /** Leave on the board's own terms: the line goes out while the port is still ours to write. */
    private fun leaveRh() {
        val r = rh ?: return
        rhSheet?.dismiss()
        rhSheet = null
        rh = null
        note = ""
        refresh()
        r.leave {
            // The board is listening to our stream again: the instrument starts from the handshake.
            parser = StreamParser()
            lastFrameMs = SystemClock.elapsedRealtime()
            // Only a real port has a monitor mode to come back to; the simulator just ends the session.
            if (link != null) {
                note = getString(R.string.rh_left_monitor)
                handshake()
            }
            refresh()
        }
    }

    /** The port went away under us: stop polling without trying to talk to the board again. */
    private fun endRh() {
        rhSheet?.dismiss()
        rhSheet = null
        val r = rh ?: return
        rh = null
        r.abandon()
    }

    // ---- link ----

    private fun usbManager() = getSystemService(UsbManager::class.java)!!

    private fun requestAndOpen(device: UsbDevice) {
        if (link != null || sim != null || feed.started) return
        UsbAccess.withPermission(this, device, ::openLink) { note = it }
    }

    private fun openLink(device: UsbDevice) {
        if (link != null || feed.started) return
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
        opened.onBytes = ::feedBytes
        opened.onClose = { why -> main.post { closeLink(why) } }
        lastFrameMs = SystemClock.elapsedRealtime()
        note = ""
        store.addLog(LogDir.INFO, "connected via usb")
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

    /** Bytes arrive on the reader thread, or on the main one from a replay; state changes go to the UI. */
    private fun feedBytes(chunk: ByteArray) {
        val r = rh
        if (r != null) {
            // In RotorHazard mode these are the node's answers, not our frames, and the wire is not
            // a spectrum stream worth recording.
            r.push(chunk)
            return
        }
        recorder.add(chunk)
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
        // The session writes to this port, so it goes before the port does.
        endRh()
        // The link is gone, so this only closes the page; there is no board left to hand a scan back.
        leavePower()
        parser = null
        runner?.failAll("link closed")
        runner = null
        l.onBytes = null
        l.onClose = null
        l.close()
        note = getString(R.string.note_disconnected, why)
        store.addLog(LogDir.INFO, "connection closed: $why")
    }

    private fun command(line: String, reply: (CommandResult) -> Unit = {}) {
        if (feed.started) {
            note = getString(R.string.note_replay_readonly)
            refresh()
            return
        }
        if (rh != null) {
            // The board is not listening to the monitor protocol; its own sheet sends what it needs.
            store.addLog(LogDir.ERROR, "RotorHazard mode: $line")
            return
        }
        store.addLog(LogDir.TX, line)
        sim?.let {
            it.onCommand(line)
            return
        }
        val r = runner ?: run {
            store.addLog(LogDir.ERROR, "not connected: $line")
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
            // A RotorHazard session answers its own reads, so no monitor frame is due and silence
            // is not evidence the board restarted.
            if (link != null && rh == null && SystemClock.elapsedRealtime() - lastFrameMs > NO_DATA_MS) {
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
        // Tight enough that the English words still fit beside the six band letters.
        setPadding(theme.dp(3f).roundToInt(), 0, theme.dp(3f).roundToInt(), 0)
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
        private const val MENU_SCALE = 1
        private const val MENU_HISTORY = 2
        private const val MENU_TELEMETRY = 3
        private const val MENU_CONSOLE = 4
        private const val MENU_GPIO = 5
        private const val MENU_SESSION = 6
        private const val MENU_RH = 7
        private const val MENU_POWER = 8
        private const val MENU_RAWIQ = 9

        /** The menu lists what it adds first, so an id need not sort where the entry sits. */
        private const val MENU_GENERAL = 10
        private const val MENU_HELP = 11
        private const val MENU_ABOUT = 12

        /** The meter's spectrum span before a profile has said what it can tune. */
        private const val SPEC_FULL_SPAN_HZ = 40_000_000L
        private const val REQ_SAVE_SESSION = 20
        private const val REQ_SAVE_CHANNELS = 21
        private const val REQ_SAVE_SPECTRUM = 22
        private const val REQ_OPEN_SESSION = 23
        private const val MIME_SESSION = "application/octet-stream"
        private const val MIME_CSV = "text/csv"

        /** ACTION_OPEN_DOCUMENT wants a wildcard: a capture has no registered type. */
        private const val ANY_TYPE = "*/*"
        private val TILE_IDS = intArrayOf(
            R.id.tile0, R.id.tile1, R.id.tile2, R.id.tile3, R.id.tile4, R.id.tile5, R.id.tile6, R.id.tile7,
        )
    }
}
