// The lap callout, spoken: the web's speech.ts on Android's own voices. GPL-3.0-or-later.
package com.onebody.widscan58

import android.content.Context
import android.os.Bundle
import android.speech.tts.TextToSpeech
import java.util.Locale

/** A lap callout: the channel ("R4") and the lap to 0.1 s, the web's speech.ts wording. */
fun lapCallout(channel: String, lapS: Double): String =
    String.format(Locale.US, "%s %.1f", channel, lapS)

class VoiceOut(context: Context, private val prefs: Prefs) {
    private var tts: TextToSpeech? = null
    private var ready = false

    init {
        tts = TextToSpeech(context) { status -> ready = status == TextToSpeech.SUCCESS }
    }

    /** Queue, never interrupt: a photo finish is two laps, and the pilot wants to hear both. */
    fun speak(text: String) {
        val t = tts ?: return
        if (!ready || !prefs.voiceOn) return
        t.setSpeechRate(prefs.voiceRate / 10f)
        t.setPitch(prefs.voicePitch / 10f)
        val volume = Bundle().apply { putFloat(TextToSpeech.Engine.KEY_PARAM_VOLUME, prefs.voiceVolume / 100f) }
        t.speak(text, TextToSpeech.QUEUE_ADD, volume, "lap")
    }

    fun release() {
        tts?.stop()
        tts?.shutdown()
        tts = null
        ready = false
    }
}
