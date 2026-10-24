// The board as a serial port: claim the CDC interfaces, read bulk IN on its own
// thread, write bulk OUT. GPL-3.0-or-later.
package com.onebody.widscan58.usb

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.hardware.usb.UsbConstants
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbDeviceConnection
import android.hardware.usb.UsbInterface
import android.hardware.usb.UsbManager

private const val SET_CONTROL_LINE_STATE = 0x22

/** UsbConstants has no recipient bits. */
private const val USB_RECIP_INTERFACE = 0x01
private const val REQ_OUT_CLASS_INTERFACE =
    UsbConstants.USB_DIR_OUT or UsbConstants.USB_TYPE_CLASS or USB_RECIP_INTERFACE

class UsbLinkException(message: String) : Exception(message)

/**
 * Owns the [UsbDeviceConnection] and one reader thread. Callbacks arrive on that thread;
 * the caller decides how to get to the UI.
 */
class UsbSerialLink private constructor(
    private val manager: UsbManager,
    private val device: UsbDevice,
    private val connection: UsbDeviceConnection,
    private val layout: CdcLayout,
) {
    var onBytes: ((ByteArray) -> Unit)? = null
    var onClose: ((String) -> Unit)? = null

    @Volatile private var active = false
    private var reader: Thread? = null

    val description: String get() = "${device.deviceName} · ${layout.describe()}"

    private fun start() {
        active = true
        reader = Thread({ readLoop() }, "widscan58-usb").apply {
            isDaemon = true
            priority = Thread.NORM_PRIORITY + 2
            start()
        }
    }

    private fun readLoop() {
        val endpoint = layout.inEndpoint
        // Android's USB host stack caps a bulk transfer at 16 KiB.
        val buffer = ByteArray(MAX_TRANSFER)
        var idle = 0
        while (active) {
            val n = connection.bulkTransfer(endpoint, buffer, buffer.size, READ_TIMEOUT_MS)
            if (n > 0) {
                idle = 0
                onBytes?.invoke(buffer.copyOf(n))
                continue
            }
            // A timeout is normal while the scan is idle; a gone device is not.
            if (++idle > IDLE_RETRY_LIMIT) {
                idle = 0
                if (!stillPresent()) {
                    finish("device unplugged")
                    return
                }
            }
        }
        finish("closed")
    }

    private fun stillPresent() = manager.deviceList[device.deviceName] != null

    /** Commands are a few dozen bytes; the CDC write path takes them in one shot. */
    fun write(data: ByteArray): Boolean =
        connection.bulkTransfer(layout.outEndpoint, data, data.size, WRITE_TIMEOUT_MS) >= 0

    fun close() {
        active = false
        reader?.interrupt()
        reader = null
        finishQuiet()
    }

    private fun finish(why: String) {
        finishQuiet()
        onClose?.invoke(why)
    }

    private fun finishQuiet() {
        runCatching { connection.releaseInterface(layout.data) }
        runCatching { connection.releaseInterface(layout.control) }
        runCatching { connection.close() }
    }

    companion object {
        private const val MAX_TRANSFER = 16384
        private const val READ_TIMEOUT_MS = 200
        private const val WRITE_TIMEOUT_MS = 1000
        private const val IDLE_RETRY_LIMIT = 15 // ~3 s of silent line before asking if it is gone

        /**
         * DTR on, RTS off. A CDC host is expected to assert DTR, and on the C5
         * (rev <= 1.0) an RTS reset can leave USB dead until power cycle —
         * firmware/src/main.c says the same. Never assert RTS here.
         */
        fun open(context: Context, device: UsbDevice, layout: CdcLayout): UsbSerialLink {
            val manager = context.getSystemService(UsbManager::class.java)
            if (!manager.hasPermission(device)) throw UsbLinkException("no USB permission yet")
            val connection = manager.openDevice(device)
                ?: throw UsbLinkException("openDevice failed: another app or a serial tool holds the port")
            fun claim(which: UsbInterface, name: String) {
                if (!connection.claimInterface(which, true)) {
                    connection.close()
                    throw UsbLinkException("cannot claim the CDC $name interface")
                }
            }
            return try {
                claim(layout.control, "control")
                claim(layout.data, "data")
                connection.controlTransfer(
                    REQ_OUT_CLASS_INTERFACE, SET_CONTROL_LINE_STATE, 1, layout.control.id, null, 0, 200,
                )
                UsbSerialLink(manager, device, connection, layout).also { it.start() }
            } catch (e: UsbLinkException) {
                connection.close()
                throw e
            }
        }
    }
}

/** One-shot permission request; the grant survives a reboot only if the app was told to remember it. */
object UsbAccess {
    private const val ACTION = "com.onebody.widscan58.USB_PERMISSION"

    fun withPermission(
        context: Context,
        device: UsbDevice,
        onGranted: (UsbDevice) -> Unit,
        onDenied: (String) -> Unit,
    ) {
        val manager = context.getSystemService(UsbManager::class.java)
        if (manager.hasPermission(device)) {
            onGranted(device)
            return
        }
        val receiver = object : BroadcastReceiver() {
            override fun onReceive(receiverContext: Context, intent: Intent) {
                context.unregisterReceiver(this)
                val granted = intent.getBooleanExtra(UsbManager.EXTRA_PERMISSION_GRANTED, false)
                val which = intent.usbExtra(UsbManager.EXTRA_DEVICE, UsbDevice::class.java)
                when {
                    granted && which != null -> onGranted(which)
                    else -> onDenied("permission denied for ${device.deviceName}")
                }
            }
        }
        context.registerReceiver(receiver, IntentFilter(ACTION), Context.RECEIVER_NOT_EXPORTED)
        val flags = if (android.os.Build.VERSION.SDK_INT >= 31) PendingIntent.FLAG_IMMUTABLE else 0
        manager.requestPermission(
            device,
            PendingIntent.getBroadcast(context.applicationContext, device.deviceId, Intent(ACTION), flags),
        )
    }
}
