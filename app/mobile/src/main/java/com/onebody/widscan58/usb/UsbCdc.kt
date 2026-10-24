// Finding the board's CDC-ACM endpoints in the Android USB host stack.
// GPL-3.0-or-later.
package com.onebody.widscan58.usb

import android.content.Intent
import android.hardware.usb.UsbConstants
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbEndpoint
import android.hardware.usb.UsbInterface
import android.hardware.usb.UsbManager
import android.os.Build
import android.os.Parcelable

const val ESPRESSIF_VID = 0x303A

/** The Class-typed overload only exists from API 33; the deprecated one still works everywhere. */
fun <T : Parcelable> Intent.usbExtra(key: String, type: Class<T>): T? =
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) getParcelableExtra(key, type)
    else @Suppress("DEPRECATION") getParcelableExtra(key)

private const val CDC_COMM = 0x02
private const val CDC_DATA = 0x0A

/** The two bulk pipes plus the CDC control interface that carries SET_CONTROL_LINE_STATE. */
class CdcLayout(
    val control: UsbInterface,
    val data: UsbInterface,
    val inEndpoint: UsbEndpoint,
    val outEndpoint: UsbEndpoint,
) {
    fun describe(): String = buildString {
        append("control iface ").append(control.id).append('(').append(control.interfaceClass).append(')')
        append("  data iface ").append(data.id).append('(').append(data.interfaceClass).append(')')
        append("  in=0x").append(inEndpoint.address.toString(16))
        append(" out=0x").append(outEndpoint.address.toString(16))
    }
}

private fun UsbDevice.interfaces() = (0 until interfaceCount).map { getInterface(it) }

/**
 * [UsbInterface.getEndpoint] indexes the interface's endpoint array rather than
 * encoding a direction and number, so the bulk pair has to be found by scanning it.
 */
private fun bulkPair(iface: UsbInterface): Pair<UsbEndpoint, UsbEndpoint>? {
    var inbound: UsbEndpoint? = null
    var outbound: UsbEndpoint? = null
    for (i in 0 until iface.endpointCount) {
        val endpoint = iface.getEndpoint(i)
        if (endpoint.type != UsbConstants.USB_ENDPOINT_XFER_BULK) continue
        if (endpoint.direction == UsbConstants.USB_DIR_IN) inbound = endpoint else outbound = endpoint
    }
    return inbound?.let { inEp -> outbound?.let { inEp to it } }
}

/**
 * The board's native USB is the CDC-ACM view of the C5's Serial/JTAG unit: a class 2
 * control interface and a class 10 data interface holding the bulk pair. The class is
 * what matters here, because the same descriptor carries a vendor JTAG interface with a
 * bulk pair of its own right next to it.
 */
fun cdcLayoutOf(device: UsbDevice): CdcLayout? {
    val data = device.interfaces()
        .filter { it.interfaceClass == CDC_DATA }
        .firstNotNullOfOrNull { iface -> bulkPair(iface)?.let { iface to it } }
        ?: return null
    val control = device.interfaces().firstOrNull { it != data.first && it.interfaceClass == CDC_COMM }
        ?: return null
    return CdcLayout(control, data.first, data.second.first, data.second.second)
}

fun espressifDevices(manager: UsbManager): List<UsbDevice> =
    manager.deviceList.values.filter { it.vendorId == ESPRESSIF_VID }.sortedBy { it.deviceName }

/** The first Espressif device that speaks CDC, i.e. our board. */
fun findBoard(manager: UsbManager): UsbDevice? = espressifDevices(manager).firstOrNull { cdcLayoutOf(it) != null }
