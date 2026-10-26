// The language the app's own words are read in: a stored tag, put on an Activity's base
// before a single view is built. GPL-3.0-or-later.
package com.onebody.widscan58

import android.content.Context
import android.content.res.Configuration
import java.util.Locale

/**
 * Chinese sits in unqualified `res/values` and English in `res/values-en`, so a chosen tag needs
 * no resource directory of its own — and `system`, which wraps nothing, leaves the phone's own
 * choice (including Android 13+'s per-app language setting) in charge.
 */
object AppLanguage {
    /** The context a fresh Activity should attach to: the one its stored language resolves in. */
    fun wrap(base: Context): Context {
        val tag = Prefs(base).language
        if (tag == Prefs.LANGUAGE_SYSTEM) return base
        val config = Configuration(base.resources.configuration)
        config.setLocale(Locale.forLanguageTag(tag))
        return base.createConfigurationContext(config)
    }
}
