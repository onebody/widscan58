// The math behind the raw I/Q snapshot: what a capture says about its signal, and the
// averaged Hann FFT its spectrum is drawn from. Mirrors web/src/views/rawiq.ts — the board
// only ships samples, so every dB on this view is computed here.
// GPL-3.0-or-later.
package com.onebody.widscan58.core

import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.ceil
import kotlin.math.cos
import kotlin.math.floor
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.pow
import kotlin.math.sin
import kotlin.math.sqrt

/** A snapshot's DC, its loudest single count and its AC power, all in transferred counts. */
class IqStats(val meanI: Double, val meanQ: Double, val peak: Int, val rms: Double) {
    /** 10-bit ADC counts per transferred count, for the info line and the dBFS scale. */
    fun scale(shift: Int) = 2.0.pow(shift)
}

object RawIqAnalysis {
    const val FFT_POINTS = 1024

    /** The trace shows this many leading samples; a whole capture would be solid ink. */
    const val TRACE_SAMPLES = 400

    /** The constellation's half-width, rounded up so the points never sit on the frame. */
    fun range(peak: Int) = max(8.0, ceil(peak * 1.15))

    fun stats(r: RawIq): IqStats {
        var mi = 0.0
        var mq = 0.0
        var peak = 0
        for (j in 0 until r.n) {
            val i = r.i[j].toInt()
            val q = r.q[j].toInt()
            mi += i
            mq += q
            peak = max(peak, max(abs(i), abs(q)))
        }
        mi /= r.n
        mq /= r.n
        var power = 0.0
        for (j in 0 until r.n) {
            val di = r.i[j] - mi
            val dq = r.q[j] - mq
            power += di * di + dq * dq
        }
        return IqStats(mi, mq, peak, sqrt(power / r.n))
    }

    /**
     * Hann-windowed [nfft]-point transforms of the capture's whole segments, averaged and
     * shifted so bin 0 is the low edge of the span, in dBFS. [dc] comes from [stats]: the
     * same DC the constellation removes, or the trace's offset lands in the centre bin.
     */
    fun spectrum(r: RawIq, dc: IqStats, nfft: Int = FFT_POINTS): DoubleArray {
        val segments = r.n / nfft
        val acc = DoubleArray(nfft)
        val re = DoubleArray(nfft)
        val im = DoubleArray(nfft)
        for (sg in 0 until segments) {
            for (j in 0 until nfft) {
                val w = 0.5 - 0.5 * cos(2 * PI * j / nfft)
                re[j] = (r.i[sg * nfft + j] - dc.meanI) * w
                im[j] = (r.q[sg * nfft + j] - dc.meanQ) * w
            }
            fft(re, im)
            for (k in 0 until nfft) acc[(k + nfft / 2) % nfft] += re[k] * re[k] + im[k] * im[k]
        }
        // A full-scale 10-bit complex tone reads 0 dB in its bin, because the Hann sum is nfft/2.
        val full = 512 / 2.0.pow(r.shift)
        val norm = 1.0 / (max(1, segments) * ((full * nfft) / 2).pow(2.0))
        return DoubleArray(nfft) { 10 * log10(acc[it] * norm + 1e-20) }
    }

    /** In-place iterative radix-2; the caller keeps [re] and [im] the same power-of-two length. */
    fun fft(re: DoubleArray, im: DoubleArray) {
        val n = re.size
        var j = 0
        for (i in 1 until n) {
            var bit = n shr 1
            while (j and bit != 0) {
                j = j xor bit
                bit = bit shr 1
            }
            j = j xor bit
            if (i < j) {
                val tr = re[i]; re[i] = re[j]; re[j] = tr
                val ti = im[i]; im[i] = im[j]; im[j] = ti
            }
        }
        var len = 2
        while (len <= n) {
            val ang = -2 * PI / len
            for (i in 0 until n step len) {
                for (k in 0 until len / 2) {
                    val wr = cos(ang * k)
                    val wi = sin(ang * k)
                    val a = i + k
                    val b = a + len / 2
                    val tr = re[b] * wr - im[b] * wi
                    val ti = re[b] * wi + im[b] * wr
                    re[b] = re[a] - tr
                    im[b] = im[a] - ti
                    re[a] += tr
                    im[a] += ti
                }
            }
            len = len shl 1
        }
    }

    /** The spectrum's axis bounds, rounded to the decades the grid is labelled with. */
    fun bounds(dbs: DoubleArray): Pair<Double, Double> =
        floor(dbs.min() / 10) * 10 to ceil(dbs.max() / 10) * 10 + 5
}
