// One seat's level bar: the live level under the EnterAt and ExitAt marks, as the web's seat
// tile. Mirrors the .rh-bar div in repo/web/src/styles.css. GPL-3.0-or-later.
package com.onebody.widscan58.view

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.RectF
import android.util.AttributeSet
import android.view.View
import kotlin.math.max
import kotlin.math.min

class RhSeatBar @JvmOverloads constructor(context: Context, attrs: AttributeSet? = null) :
    View(context, attrs) {

    private val theme = Style(context)
    private val paint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val box = RectF()

    /** The bar's own range: the web takes a little more than the low of nadir/ExitAt. */
    var lo = 0
        set(value) {
            field = value.coerceAtLeast(0)
            invalidate()
        }
    var hi = 255
        set(value) {
            field = value.coerceAtMost(255)
            invalidate()
        }
    var level: Int? = null
        set(value) {
            field = value
            invalidate()
        }
    var enter = 0
        set(value) {
            field = value
            invalidate()
        }
    var exit = 0
        set(value) {
            field = value
            invalidate()
        }
    var color = theme.ink
        set(value) {
            field = value
            invalidate()
        }
    var crossing = false
        set(value) {
            field = value
            invalidate()
        }

    private fun x(v: Int) = ((v - lo) / max(1.0, (hi - lo).toDouble()).toFloat()) * width

    override fun onDraw(canvas: Canvas) {
        val h = height.toFloat()
        box.set(0f, 0f, width.toFloat(), h)
        paint.color = theme.surface2
        canvas.drawRoundRect(box, h / 3f, h / 3f, paint)
        level?.let {
            paint.color = if (crossing) theme.good else color
            canvas.drawRoundRect(0f, 0f, x(it), h, h / 3f, h / 3f, paint)
        }
        val mark = theme.dp(2f)
        for ((v, c) in listOf(exit to theme.warning, enter to theme.good)) {
            paint.color = c
            val left = min(x(v) - mark / 2, width - mark)
            canvas.drawRect(max(0f, left), 0f, max(0f, left) + mark, h, paint)
        }
    }
}
