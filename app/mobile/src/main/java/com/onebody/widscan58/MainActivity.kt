// GPL-3.0-or-later.
package com.onebody.widscan58

import android.app.Activity
import android.hardware.usb.UsbConstants
import android.hardware.usb.UsbManager
import android.os.Bundle
import android.view.WindowManager
import android.widget.ScrollView
import android.widget.TextView
import com.onebody.widscan58.core.Bands

/**
 * Bring-up screen: enumerate what the phone's USB host stack actually exposes for the
 * board, before the transport and views are wired.
 */
class MainActivity : Activity() {
    private lateinit var text: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        text = TextView(this).apply {
            textSize = 13f
            setTextColor(0xFFC3C2B7.toInt())
            setPadding(32, 32, 32, 32)
            typeface = android.graphics.Typeface.MONOSPACE
        }
        setContentView(ScrollView(this).apply { addView(text) })
        padContentForSystemBars()
    }

    override fun onResume() {
        super.onResume()
        text.text = describe()
    }

    private fun describe(): String {
        val manager = getSystemService(USB_SERVICE) as UsbManager
        val devices = manager.deviceList.values.sortedBy { it.deviceName }
        if (devices.isEmpty()) return "No USB device.\nPlug the ESP32-C5 into the phone's USB-C port."
        return buildString {
            appendLine("Widscan58 ${BuildConfig.VERSION_NAME}")
            appendLine("Channels: ${Bands.channelsCommand(Bands.names("R"))}")
            appendLine()
            for (d in devices) {
                appendLine("${d.deviceName}  VID=%04X PID=%04X".format(d.vendorId, d.productId))
                appendLine("  ${d.manufacturerName ?: "-"} / ${d.productName ?: "-"}")
                appendLine("  permission: ${manager.hasPermission(d)}")
                for (i in 0 until d.interfaceCount) {
                    val iface = d.getInterface(i)
                    appendLine(
                        "  iface $i class=%d sub=%d proto=%d endpoints=%s".format(
                            iface.interfaceClass, iface.interfaceSubclass, iface.interfaceProtocol,
                            (0 until iface.endpointCount).joinToString { e ->
                                val ep = iface.getEndpoint(e)
                                "${ep.address}/${ep.type}/${if (ep.direction == UsbConstants.USB_DIR_IN) "IN" else "OUT"}/${ep.maxPacketSize}"
                            },
                        )
                    )
                }
                appendLine()
            }
        }
    }
}
