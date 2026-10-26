// RotorHazard mode as one full-screen sheet: the seats and their thresholds, the level chart,
// the detection events with their rules and the lap callout, and the receiver settings this mode
// allows. Mirrors the four cards of web/src/views/rh.ts plus rhEventsDialog.ts's options.
// GPL-3.0-or-later.
package com.onebody.widscan58

import android.app.Activity
import android.app.AlertDialog
import android.app.Dialog
import android.graphics.Typeface
import android.text.SpannableStringBuilder
import android.text.Spanned
import android.text.style.ForegroundColorSpan
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.HorizontalScrollView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.SeekBar
import android.widget.TextView
import com.onebody.widscan58.core.RH_LAP_COUNT_MAX
import com.onebody.widscan58.core.RH_LAP_COUNT_MIN
import com.onebody.widscan58.core.RH_LEVEL_MAX
import com.onebody.widscan58.core.RH_MIN_LAP_MAX_S
import com.onebody.widscan58.core.RH_SEATS
import com.onebody.widscan58.core.RhEvent
import com.onebody.widscan58.core.RhEventKind
import com.onebody.widscan58.core.RhLapRules
import com.onebody.widscan58.core.RhLinkStats
import com.onebody.widscan58.core.RhMetric
import com.onebody.widscan58.core.RhModel
import com.onebody.widscan58.core.RhSeat
import com.onebody.widscan58.core.RhThreshold
import com.onebody.widscan58.core.rhLevelDb
import com.onebody.widscan58.view.RhLevelsView
import com.onebody.widscan58.view.RhSeatBar
import com.onebody.widscan58.view.Style
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.math.roundToInt
import kotlin.random.Random

/**
 * Every RotorHazard write goes through [RhFeed.submit], so none of it waits on the board while the
 * screen is drawing. Reads are free: the model's rings are the poller's, copied here as we walk them.
 */
class RhSheet(
    private val activity: Activity,
    private val prefs: Prefs,
    private val feed: RhFeed,
    private val voice: VoiceOut,
    private val onExit: () -> Unit,
) {
    private val theme = Style(activity)
    private val store = feed.store
    private val replies = JsonReplies()
    private val model: RhModel get() = feed.model
    private var dialog: Dialog? = null
    private var tab = TAB_SEATS
    private val status = TextView(activity)
    private val cards = (0 until RH_SEATS).map { SeatCard(it) }
    private val eventBody = TextView(activity)
    private var eventKey = ""
    private lateinit var levels: RhLevelsView
    private lateinit var pages: List<View>
    private lateinit var tabChips: List<Button>
    private val spanChips = mutableMapOf<Int, Button>()
    private val seatChips = mutableMapOf<Int, Button>()
    private var gainBar: SeekBar? = null
    private var gainValue: TextView? = null
    private var draggingGain = false
    private var fftRow: LinearLayout? = null
    private val fftChips = mutableMapOf<Int, Button>()
    private var fftFor = -1
    private var statsLine: TextView? = null
    private var voiceBody: View? = null
    private val ruleRows = mutableListOf<RuleRow>()
    private val syncers = mutableListOf<() -> Unit>()
    private val clock = SimpleDateFormat("HH:mm:ss.SSS", Locale.US)
    private var originWallMs = 0L
    private var originNowS = 0.0

    fun show() {
        // The event list is in the store's own seconds; this is the wall clock it started at.
        originWallMs = System.currentTimeMillis()
        originNowS = store.now()
        // The pages are built once for the session, so a closed sheet comes back exactly as it was.
        dialog?.let {
            it.show()
            render()
            return
        }
        val body = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad(10f), 0, pad(10f), pad(4f))
            addView(tabRow(), below(pad(2f)))
            addView(status, below(pad(4f)))
            addView(pageRow(), LinearLayout.LayoutParams(MATCH, 0, 1f))
        }
        val d = AlertDialog.Builder(activity)
            .setTitle(R.string.rh_title)
            .setView(body)
            .setNeutralButton(R.string.rh_plan, null)
            .setNegativeButton(R.string.rh_exit_mode) { _, _ -> onExit() }
            .setPositiveButton(R.string.close, null)
            .create()
        dialog = d
        d.show()
        // The plan editor opens on top of this sheet, so its button must not close the sheet too.
        d.getButton(AlertDialog.BUTTON_NEUTRAL).setOnClickListener { editPlan() }
        render()
    }

    /** Called with the instrument's own frames, so the pages keep up with the poll loop. */
    fun update() {
        if (dialog?.isShowing != true) return
        render()
    }

    /** Hide the sheet, keep it: the session owns these views, and [show] puts them back on screen. */
    fun dismiss() {
        dialog?.dismiss()
    }

    // ---- pages ----

    private fun tabRow(): View {
        val row = chipRow()
        tabChips = TABS.mapIndexed { id, res ->
            chip(str(res)) {
                tab = id
                showPage()
                render()
            }.also { row.addView(it) }
        }
        return scrolled(row)
    }

    private fun pageRow(): View {
        pages = listOf(
            ScrollView(activity).apply { addView(seatsPage(), full()) },
            levelsPage(),
            eventsPage(),
            ScrollView(activity).apply { addView(receiverPage(), full()) },
        )
        val column = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
        pages.forEach { column.addView(it, LinearLayout.LayoutParams(MATCH, 0, 1f)) }
        showPage()
        return column
    }

    private fun showPage() {
        pages.forEachIndexed { id, page -> page.visibility = if (id == tab) View.VISIBLE else View.GONE }
        tabChips.forEachIndexed { id, chip -> tint(chip, id == tab) }
        // The chart is the one page that has to be told its size after the dialog opens.
        if (tab == TAB_LEVELS) levels.requestLayout()
    }

    /** Seats: one card each, with the thresholds under the level it is reading. */
    private fun seatsPage(): View {
        val column = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
        cards.forEach { column.addView(it.root, below(pad(4f))) }
        return column
    }

    private fun levelsPage(): View {
        val column = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
        column.addView(scrolled(spanRow()), below(pad(2f)))
        column.addView(scrolled(seatRow()), below(pad(2f)))
        levels = RhLevelsView(activity).apply {
            minimumHeight = pad(180f)
            store = this@RhSheet.store
            model = this@RhSheet.model
            spanSeconds = prefs.rhSpanSeconds.toDouble()
            seat = prefs.rhSeat
        }
        column.addView(levels, LinearLayout.LayoutParams(MATCH, 0, 1f).apply { topMargin = pad(4f) })
        return column
    }

    private fun spanRow(): LinearLayout {
        val row = chipRow()
        for (seconds in SPANS) {
            spanChips[seconds] = chip(str(R.string.rh_span, seconds)) {
                prefs.rhSpanSeconds = seconds
                render()
            }.also { row.addView(it) }
        }
        row.addView(hint(str(R.string.rh_selected_hint)))
        return row
    }

    /** The chart only draws the selected seat's thresholds and pass lines; this is how to pick it. */
    private fun seatRow(): LinearLayout {
        val row = chipRow()
        for (s in model.seats) {
            seatChips[s.index] = chip(s.channel ?: "${s.index + 1}") {
                prefs.rhSeat = s.index
                render()
            }.also { row.addView(it) }
        }
        return row
    }

    /** Events: the list, and the options the web puts behind a gear. */
    private fun eventsPage(): View {
        eventBody.apply {
            textSize = 11f
            setTextColor(theme.ink)
            typeface = Typeface.MONOSPACE
        }
        val column = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            addView(eventOptions(), below(pad(2f)))
            addView(rulesBlock(), below(pad(2f)))
            addView(voiceBlock(), below(pad(4f)))
            addView(
                ScrollView(activity).apply { addView(eventBody, full()) },
                LinearLayout.LayoutParams(MATCH, 0, 1f),
            )
        }
        return column
    }

    private fun eventOptions(): View {
        val row = chipRow()
        row.addView(chip(str(R.string.rh_events_clear)) { feed.submit { it.clearEvents() } })
        row.addView(chip(str(R.string.rh_show_crossings)) {
            prefs.rhShowCrossings = !prefs.rhShowCrossings
            render()
        }.also { crossingsChip = it })
        row.addView(chip(str(R.string.rh_show_fastest)) {
            prefs.rhShowFastest = !prefs.rhShowFastest
            render()
        }.also { fastestChip = it })
        return scrolled(row)
    }

    private var crossingsChip: Button? = null
    private var fastestChip: Button? = null

    private fun rulesBlock(): View {
        val column = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
        ruleRows += RuleRow(
            str(R.string.rh_min_lap),
            str(R.string.rh_min_lap_hint),
            0,
            RH_MIN_LAP_MAX_S,
            { it.minLapOn to it.minLapS },
            { m, on, v -> m.setLapRules(m.lapRules.of(minLapOn = on, minLapS = v)) },
            { "%.0f s".format(Locale.US, it) },
        )
        ruleRows += RuleRow(
            str(R.string.rh_fastest_count),
            str(R.string.rh_fastest_count_hint),
            RH_LAP_COUNT_MIN,
            RH_LAP_COUNT_MAX,
            { it.fastestOn to it.count.toDouble() },
            { m, on, v -> m.setLapRules(m.lapRules.of(fastestOn = on, count = v.roundToInt())) },
            { "%.0f".format(Locale.US, it) },
        )
        ruleRows.forEach { column.addView(it.root, below(pad(2f))) }
        return column
    }

    /** Voice: the web hides the controls until callouts are on, since they only mean something then. */
    private fun voiceBlock(): View {
        val head = chipRow()
        head.addView(chip(str(R.string.rh_voice)) {
            prefs.voiceOn = !prefs.voiceOn
            render()
        }.also { voiceChip = it })
        val controls = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            val test = chipRow()
            test.addView(chip(str(R.string.rh_voice_test)) { testCallout() })
            addView(scrolled(test))
            addView(slider("volume", R.string.rh_voice_volume, 0, 100, { prefs.voiceVolume = it }, { prefs.voiceVolume }) { "$it" })
            addView(
                slider("rate", R.string.rh_voice_rate, 1, 20, { prefs.voiceRate = it }, { prefs.voiceRate }) {
                    "%.1f×".format(Locale.US, it / 10.0)
                },
            )
            addView(
                slider("pitch", R.string.rh_voice_pitch, 5, 20, { prefs.voicePitch = it }, { prefs.voicePitch }) {
                    "%.1f".format(Locale.US, it / 10.0)
                },
            )
        }
        voiceBody = controls
        return LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            addView(scrolled(head))
            addView(controls)
        }
    }

    private var voiceChip: Button? = null

    /** A short labelled slider: the web's voice controls, one line high. */
    private fun slider(key: String, label: Int, lo: Int, hi: Int, write: (Int) -> Unit, read: () -> Int, fmt: (Int) -> String): View {
        val value = smallLabel()
        val bar = SeekBar(activity).apply {
            max = hi - lo
            setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
                override fun onProgressChanged(b: SeekBar, v: Int, fromUser: Boolean) {
                    if (!fromUser) return
                    write(lo + v)
                    value.text = fmt(lo + v)
                }

                override fun onStartTrackingTouch(b: SeekBar) {}

                override fun onStopTrackingTouch(b: SeekBar) {}
            })
        }
        syncers += {
            bar.progress = read() - lo
            value.text = fmt(read())
        }
        return LinearLayout(activity).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            addView(smallLabel().apply { text = str(label) }, hparams(start = 0f, end = 6f))
            addView(bar, LinearLayout.LayoutParams(0, WRAP, 1f))
            addView(value, hparams(start = 6f))
        }
    }

    /** The selected seat's channel and a made-up lap, as the web's test button says it. */
    private fun testCallout() {
        val seat = model.seats.firstOrNull { it.index == prefs.rhSeat && it.channel != null }
            ?: model.seats.firstOrNull { it.channel != null }
        voice.speak(lapCallout(seat?.channel ?: "R1", Random.nextDouble(10.0, 30.0)))
    }

    private fun receiverPage(): View {
        val column = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
        val value = smallLabel()
        gainValue = value
        val bar = SeekBar(activity).apply {
            max = Prefs.GAIN_MAX
            setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
                override fun onProgressChanged(b: SeekBar, v: Int, fromUser: Boolean) {
                    if (fromUser) value.text = v.toString()
                }

                override fun onStartTrackingTouch(b: SeekBar) {
                    draggingGain = true
                }

                // One command when the finger lifts, not one per pixel of travel.
                override fun onStopTrackingTouch(b: SeekBar) {
                    draggingGain = false
                    feed.submit { it.command("GAIN MANUAL ${b.progress}") }
                }
            })
        }
        gainBar = bar
        column.addView(
            labeled(
                R.string.rh_gain,
                LinearLayout(activity).apply {
                    orientation = LinearLayout.HORIZONTAL
                    gravity = Gravity.CENTER_VERTICAL
                    addView(bar, LinearLayout.LayoutParams(0, WRAP, 1f))
                    addView(value, hparams(start = 6f))
                },
            ),
        )
        column.addView(labeled(R.string.rh_mode, scrolled(chipRowOf("mode", MODES))))
        column.addView(labeled(R.string.rh_metric, scrolled(chipRowOf("metric", METRICS))))
        column.addView(labeled(R.string.rh_filter, scrolled(chipRowOf("filter", FILTERS))))
        val fft = chipRow()
        fftRow = fft
        column.addView(labeled(R.string.rh_fft, scrolled(fft)))
        val saves = chipRow()
        saves.addView(chip(str(R.string.rh_save)) { feed.submit { it.command("CONFIG SAVE") } })
        saves.addView(chip(str(R.string.rh_reset)) { feed.submit { it.command("CONFIG RESET") } })
        column.addView(labeled(R.string.rh_save_hint, scrolled(saves)))
        val stats = TextView(activity).apply {
            textSize = 10f
            setTextColor(theme.muted)
        }
        statsLine = stats
        column.addView(stats, below(pad(6f)))
        return column
    }

    /** The segmented controls: one chip per value, tinted for what the board reports. */
    private fun chipRowOf(key: String, options: List<Pair<String, Int>>): LinearLayout {
        val row = chipRow()
        val byValue = optionChips.getOrPut(key) { mutableMapOf() }
        for ((value, label) in options) {
            val b = chip(str(label)) {
                feed.submit { m ->
                    if (key == "metric") RhMetric.of(value)?.let { m.setMetric(it) } else m.command(commandFor(key, value))
                }
            }
            byValue[value] = b
            row.addView(b)
        }
        return row
    }

    private fun commandFor(key: String, value: String) = when (key) {
        "mode" -> "MODE $value"
        "filter" -> "LEVELFILTER $value"
        else -> "METRICS $value"
    }

    private val optionChips = mutableMapOf<String, MutableMap<String, Button>>()

    // ---- rendering ----

    private fun render() {
        status.text = statusLine()
        when (tab) {
            TAB_SEATS -> cards.forEach { it.render() }
            TAB_LEVELS -> renderLevels()
            TAB_EVENTS -> renderEvents()
            TAB_RECEIVER -> renderReceiver()
        }
    }

    private fun statusLine(): String {
        val m = model
        val error = m.error
        val active = m.seats.count { it.channel != null }
        val loopUs = m.seats.firstOrNull { it.channel != null && it.loopUs > 0 }?.loopUs
        return when {
            error != null -> str(R.string.rh_status_error, error)
            !m.running -> str(R.string.rh_status_not_polling)
            loopUs != null -> str(R.string.rh_status_updates, active, m.cycleMs, 1_000_000 / loopUs)
            else -> str(R.string.rh_status_seats, active, m.cycleMs)
        }
    }

    private fun renderLevels() {
        levels.spanSeconds = prefs.rhSpanSeconds.toDouble()
        levels.seat = prefs.rhSeat
        levels.invalidate()
        spanChips.forEach { (seconds, b) -> tint(b, seconds == prefs.rhSpanSeconds) }
        seatChips.forEach { (index, b) ->
            val s = model.seats[index]
            b.text = s.channel ?: "${index + 1}"
            b.isEnabled = s.channel != null
            b.alpha = if (s.channel == null) 0.4f else 1f
            tint(b, index == prefs.rhSeat)
        }
    }

    private fun renderEvents() {
        val shown = model.events.toList() // the ring belongs to the poll thread
        val key = "${shown.size} ${prefs.rhShowCrossings} ${prefs.rhShowFastest} ${model.lapRules}"
        if (key != eventKey) {
            eventKey = key
            eventBody.text = eventRows(shown.asReversed())
        }
        crossingsChip?.let { tint(it, prefs.rhShowCrossings) }
        val rules = model.lapRules
        fastestChip?.let {
            it.isEnabled = rules.fastestOn
            it.alpha = if (rules.fastestOn) 1f else 0.4f
            tint(it, prefs.rhShowFastest)
        }
        voiceChip?.let { tint(it, prefs.voiceOn) }
        voiceBody?.visibility = if (prefs.voiceOn) View.VISIBLE else View.GONE
        ruleRows.forEach { it.sync(rules) }
        syncers.forEach { it() }
    }

    /** Newest first, the web's list. Crossings and fastest runs are offered, not assumed. */
    private fun eventRows(events: List<RhEvent>): CharSequence {
        val text = SpannableStringBuilder()
        var rows = 0
        for (e in events) {
            if (rows >= RhModel.EVENTS_SHOWN) break
            val line = eventLine(e) ?: continue
            val at = text.length
            text.append("${clockOf(e.t)}  $line\n")
            text.setSpan(ForegroundColorSpan(eventColor(e)), at, text.length, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
            rows++
        }
        if (rows == 0) text.append(str(R.string.rh_no_events))
        return text
    }

    private fun clockOf(t: Double): String = clock.format(Date(originWallMs + ((t - originNowS) * 1000).toLong()))

    private fun eventLine(e: RhEvent): String? {
        val who = "${e.seat + 1} ${model.seats[e.seat].channel.orEmpty()}"
        val peak = e.peak?.let { " · " + str(R.string.rh_event_peak, it) }.orEmpty()
        return when (e.kind) {
            RhEventKind.ENTER -> if (!prefs.rhShowCrossings) null else "$who · " + str(R.string.rh_event_enter, dash(e.level))
            RhEventKind.EXIT -> if (!prefs.rhShowCrossings) null else "$who · " + str(R.string.rh_event_exit, dash(e.level))
            RhEventKind.PASS -> "$who · " + when {
                e.ignored -> str(R.string.rh_event_ignored, sec(e.ignoredS))
                e.lapS != null -> str(R.string.rh_event_pass, sec(e.lapS))
                else -> str(R.string.rh_event_pass_first)
            } + peak
            RhEventKind.FASTEST -> {
                val f = e.fastest
                if (!prefs.rhShowFastest || f == null) null
                else "$who · " + str(R.string.rh_event_fastest, f.laps.size, sec(f.totalS)) +
                    e.previous?.let { " · " + str(R.string.rh_event_was, sec(it.totalS)) }.orEmpty()
            }
        }
    }

    private fun eventColor(e: RhEvent) = when {
        e.kind == RhEventKind.FASTEST -> theme.accent
        e.ignored -> theme.warning
        e.kind == RhEventKind.PASS -> theme.ink
        else -> theme.muted
    }

    private fun renderReceiver() {
        val c = model.config?.let { replies.rhConfig(it) }
        if (c != null) {
            gainBar?.let { if (!draggingGain) it.max = c.gainMax.coerceAtLeast(1) }
            if (!draggingGain) {
                gainBar?.progress = c.gain.coerceAtLeast(0)
                gainValue?.text = c.gain.toString()
            }
            optionChips["mode"]?.forEach { (v, b) -> tint(b, v == c.mode.name) }
            optionChips["filter"]?.forEach { (v, b) -> tint(b, v == c.levelFilter.name) }
            if (c.nfft != fftFor) buildFft(c.nfft)
            fftChips.forEach { (v, b) -> tint(b, v == c.nfft) }
        }
        optionChips["metric"]?.forEach { (v, b) -> tint(b, v == model.metric.name) }
        statsLine?.text = str(R.string.rh_link_stats, reads(), writes(), errors())
    }

    /** The four sizes the web's select offers, plus the board's own if it is not one of them. */
    private fun buildFft(nfft: Int) {
        fftFor = nfft
        val row = fftRow ?: return
        row.removeAllViews()
        fftChips.clear()
        for (v in (FFTS.toList() + nfft).distinct().sorted()) {
            val b = chip(str(R.string.rh_fft_option, v, 4 * v)) { feed.submit { it.command("FFT $v") } }
            fftChips[v] = b
            row.addView(b)
        }
    }

    private fun reads() = (feed.link as? RhLinkStats)?.reads ?: 0

    private fun writes() = (feed.link as? RhLinkStats)?.writes ?: 0

    private fun errors() = (feed.link as? RhLinkStats)?.errors ?: 0

    // ---- one rule ----

    /** A lap rule: the switch that enables it, the slider for its value, and what the model says. */
    private inner class RuleRow(
        label: String,
        hint: String,
        val lo: Int,
        val hi: Int,
        val read: (RhLapRules) -> Pair<Boolean, Double>,
        val write: (RhModel, Boolean, Double) -> Unit,
        val fmt: (Double) -> String,
    ) {
        private var on = read(model.lapRules).first
        private var dragging = false
        private val chip = chip(label) {
            on = !on
            feed.submit { write(it, on, (lo + bar.progress).toDouble()) }
        }
        private val bar = SeekBar(activity).apply {
            max = hi - lo
            setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
                override fun onProgressChanged(b: SeekBar, v: Int, fromUser: Boolean) {
                    if (fromUser) value.text = fmt((lo + v).toDouble())
                }

                override fun onStartTrackingTouch(b: SeekBar) {
                    dragging = true
                }

                override fun onStopTrackingTouch(b: SeekBar) {
                    dragging = false
                    feed.submit { write(it, on, (lo + b.progress).toDouble()) }
                }
            })
        }
        private val value = smallLabel()
        val root = LinearLayout(activity).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            addView(chip, hparams(end = 4f))
            addView(
                LinearLayout(activity).apply {
                    orientation = LinearLayout.VERTICAL
                    addView(smallLabel().apply { text = hint; setTextColor(theme.muted); textSize = 9f }, full())
                    addView(this@RuleRow.bar, LinearLayout.LayoutParams(MATCH, WRAP))
                },
                LinearLayout.LayoutParams(0, WRAP, 1f).apply { marginStart = pad(4f) },
            )
            addView(value, hparams(start = 6f))
        }

        fun sync(rules: RhLapRules) {
            val (enabledNow, at) = read(rules)
            on = enabledNow
            tint(chip, enabledNow)
            bar.isEnabled = enabledNow
            bar.alpha = if (enabledNow) 1f else 0.4f
            if (!dragging) {
                bar.progress = (at.roundToInt() - lo).coerceIn(0, hi - lo)
                value.text = fmt(at)
            }
        }
    }

    private fun editPlan() {
        ChannelPlanSheet(activity) { names ->
            prefs.setChannels(names)
            feed.submit { it.writeSeats(names) }
        }.show(prefs.channels)
    }

    // ---- one seat ----

    private inner class SeatCard(val index: Int) {
        val root = LinearLayout(activity)
        private val name = TextView(activity)
        private val freq = TextView(activity)
        private val big = TextView(activity)
        private val bar = RhSeatBar(activity)
        private val stats = TextView(activity)
        private val enter = thresholdRow(RhThreshold.ENTER)
        private val exit = thresholdRow(RhThreshold.EXIT)

        init {
            name.textSize = 12f
            name.setTextColor(theme.ink)
            name.setTypeface(null, Typeface.BOLD)
            freq.textSize = 11f
            freq.setTextColor(theme.muted)
            big.textSize = 20f
            big.setTextColor(theme.ink)
            stats.textSize = 10f
            stats.setTextColor(theme.muted)
            bar.color = theme.channelColor(index)
            val top = LinearLayout(activity).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                addView(name, LinearLayout.LayoutParams(0, WRAP, 1f))
                addView(freq)
            }
            root.apply {
                orientation = LinearLayout.VERTICAL
                setBackgroundColor(theme.surface)
                setPadding(pad(8f), pad(4f), pad(8f), pad(6f))
                addView(top)
                addView(big)
                addView(bar, LinearLayout.LayoutParams(MATCH, pad(10f)).apply { topMargin = pad(3f) })
                addView(stats, above(pad(3f)))
                addView(enter.row, above(pad(4f)))
                addView(exit.row, above(pad(2f)))
                setOnClickListener {
                    prefs.rhSeat = index
                    render()
                }
            }
        }

        /** One threshold: its name, the slider, its value, and the button that takes the level now. */
        private fun thresholdRow(which: RhThreshold): Row {
            val value = smallLabel()
            var dragging = false
            val bar = SeekBar(activity).apply {
                max = RH_LEVEL_MAX - 1
                setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
                    override fun onProgressChanged(b: SeekBar, v: Int, fromUser: Boolean) {
                        if (fromUser) value.text = (v + 1).toString()
                    }

                    override fun onStartTrackingTouch(b: SeekBar) {
                        dragging = true
                    }

                    override fun onStopTrackingTouch(b: SeekBar) {
                        dragging = false
                        feed.submit { it.setThreshold(index, which, (b.progress + 1).toDouble()) }
                    }
                })
            }
            val row = LinearLayout(activity).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                addView(
                    smallLabel().apply { text = str(if (which == RhThreshold.ENTER) R.string.rh_enter else R.string.rh_exit_at) },
                    hparams(end = 6f),
                )
                addView(bar, LinearLayout.LayoutParams(0, WRAP, 1f))
                addView(value, hparams(start = 6f))
                addView(
                    chip(str(R.string.rh_now)) {
                        feed.submit { m -> m.recentLevel(index)?.let { m.setThreshold(index, which, it) } }
                    },
                    hparams(start = 4f),
                )
            }
            return Row(row, bar, value) { !dragging }
        }

        fun render() {
            val s = model.seats[index]
            val m = model.metric
            // Read each once: the poller owns these and may change any of them between two reads.
            val level = s.level
            val channel = s.channel
            val peak = s.nodePeak
            val nadir = s.nodeNadir
            name.text = str(R.string.rh_seat, index + 1) + (channel?.let { " · $it" } ?: "")
            freq.text = if (channel == null) str(R.string.rh_off) else "${s.mhz} MHz"
            big.text = if (level == null) "—" else
                String.format(Locale.US, "%d  %.1f %s", level, rhLevelDb(level, m), unit(m))
            bar.lo = maxOf(0, minOf(nadir ?: 255, s.exit) - 20)
            bar.hi = minOf(255, maxOf(peak ?: 0, s.enter) + 20)
            bar.level = level
            bar.enter = s.enter
            bar.exit = s.exit
            bar.crossing = s.crossing
            stats.text = statLine(s)
            root.setBackgroundColor(if (index == prefs.rhSeat) theme.surface2 else theme.surface)
            enter.sync(s.enter)
            exit.sync(s.exit)
        }

        private fun statLine(s: RhSeat): String {
            val r = model.lapRules
            val parts = mutableListOf(
                str(R.string.rh_laps, s.laps.passes),
                str(R.string.rh_last_lap, sec(s.laps.lastLapS)),
            )
            if (r.fastestOn) parts.add(str(R.string.rh_fastest, r.count, sec(s.laps.fastest?.totalS)))
            parts.add(str(R.string.rh_pass_peak, dash(s.passPeak)))
            parts.add(str(R.string.rh_peak_nadir, "${dash(s.nodePeak)} / ${dash(s.nodeNadir)}"))
            return parts.joinToString(" · ")
        }
    }

    /** A threshold slider, its value, and whether the finger is off it. */
    private inner class Row(val row: View, val bar: SeekBar, val value: TextView, val idle: () -> Boolean) {
        fun sync(code: Int) {
            if (!idle()) return
            bar.progress = (code - 1).coerceAtLeast(0)
            value.text = code.toString()
        }
    }

    private fun unit(m: RhMetric) = str(if (m == RhMetric.RSSI) R.string.rh_unit_dbfs else R.string.rh_unit_snr)

    private fun dash(v: Int?) = v?.toString() ?: "—"

    private fun sec(v: Double?) = if (v == null) "—" else String.format(Locale.US, "%.3f", v)

    // ---- widgets ----

    private fun labeled(label: Int, body: View): View {
        val column = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            addView(hint(str(label)), above(pad(6f)))
            addView(body, LinearLayout.LayoutParams(MATCH, WRAP).apply { topMargin = pad(2f) })
        }
        return column
    }

    private fun hint(text: String) = TextView(activity).apply {
        this.text = text
        textSize = 10f
        setTextColor(theme.muted)
    }

    private fun smallLabel() = TextView(activity).apply {
        textSize = 11f
        setTextColor(theme.ink)
    }

    private fun chipRow() = LinearLayout(activity).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
    }

    /** The chip rows scroll: eight seats and four spans do not fit a phone's dialog. */
    private fun scrolled(row: LinearLayout) = HorizontalScrollView(activity).apply { addView(row) }

    private fun chip(label: String, click: () -> Unit) = Button(activity).apply {
        text = label
        isAllCaps = false
        textSize = 11f
        minWidth = 0
        minimumWidth = 0
        minHeight = 0
        setTextColor(theme.ink)
        setPadding(pad(6f), 0, pad(6f), 0)
        layoutParams = LinearLayout.LayoutParams(WRAP, pad(32f)).apply { marginEnd = pad(3f) }
        setOnClickListener { click() }
    }

    private fun tint(button: Button, on: Boolean) = button.setTextColor(if (on) theme.accent else theme.muted)

    private fun str(res: Int) = activity.getString(res)

    private fun str(res: Int, vararg args: Any) = activity.getString(res, *args)

    private fun full() = LinearLayout.LayoutParams(MATCH, WRAP)

    private fun above(top: Int) = LinearLayout.LayoutParams(MATCH, WRAP).apply { topMargin = top }

    private fun below(bottom: Int) = LinearLayout.LayoutParams(MATCH, WRAP).apply { bottomMargin = bottom }

    private fun hparams(start: Float = 0f, end: Float = 0f) =
        LinearLayout.LayoutParams(WRAP, WRAP).apply {
            marginStart = pad(start)
            marginEnd = pad(end)
        }

    private fun pad(dp: Float) = theme.dp(dp).roundToInt()

    companion object {
        private const val MATCH = LinearLayout.LayoutParams.MATCH_PARENT
        private const val WRAP = LinearLayout.LayoutParams.WRAP_CONTENT
        private const val TAB_SEATS = 0
        private const val TAB_LEVELS = 1
        private const val TAB_EVENTS = 2
        private const val TAB_RECEIVER = 3
        private val TABS = intArrayOf(R.string.rh_tab_seats, R.string.rh_tab_levels, R.string.rh_tab_events, R.string.rh_tab_receiver)
        private val SPANS = intArrayOf(10, 30, 60, 120)
        private val FFTS = intArrayOf(64, 128, 256, 512)
        private val MODES = listOf("GROUP" to R.string.rh_mode_group, "SINGLE" to R.string.rh_mode_single)
        private val METRICS = listOf("RSSI" to R.string.rh_metric_rssi, "SNR" to R.string.rh_metric_snr)
        private val FILTERS = listOf("MEDIAN" to R.string.rh_filter_median, "EMA" to R.string.rh_filter_ema)
    }
}
