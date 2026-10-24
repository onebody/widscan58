# Widscan58 for Android

A handheld spectrum analyzer for the [Widscan58](../repo) ESP32-C5 5.8 GHz FPV
band scanner. Plug the board into the phone over USB-C and the app talks to it
directly as a serial device — no Wi-Fi, no bridge, no root.

Built as a companion to the browser UI in `repo/web`; the wire protocol and the
display engine are ports of it, so the numbers mean the same thing in both.

## Requirements

- Android 8.0 (API 26) or newer, with USB OTG.
- A Widscan58 board (ESP32-C5) running the widscan58 firmware. Its native USB is
  the Espressif USB-Serial/JTAG port (VID `0x303A`), which enumerates as a CDC-ACM
  device with one bulk pair. Flash it first with `repo/tools`, or from any
  computer — the app itself does not flash.
- A cable that carries data, not charge-only.

## Build and install

```sh
cd app
./gradlew :core:test :mobile:assembleDebug
adb install -r mobile/build/outputs/apk/debug/mobile-debug.apk
```

The toolchain is Gradle 8.14 + AGP 8.11 + Kotlin 2.2, JDK 17. `local.properties`
must point at your Android SDK (`sdk.dir=…`).

Then: connect the board (OTG adapter or a direct USB-C phone), open **Widscan58**,
grant the USB permission dialog. The app appears in the attach filter, so plugging
the board in can also open it by itself.

## What the screen shows

| Part | Content |
| --- | --- |
| status line | profile, rotations/s, hops, gain, link fill, link name |
| band chips | one tap puts a whole band (R/A/B/E/F/L) on the device; `Max`/`Avg` toggle the hold traces |
| panorama | every LO segment stitched on a nearest-LO basis: live trace, max hold, average, per-bin noise floor, RSSI/search windows per channel, plan LO markers, board spurs |
| waterfall | one row per completed scan rotation, newest at the top, scaled against the smoothed noise floor; 2 dp per row, so about 45 rotations of history on a phone |
| channel tiles | level (EMA when reported), bar over floor−10…floor+40 dB, SNR, floor, badges, 10 s sparkline |
| bottom bar | Start/Stop, manual gain 0…63, Reset holds |

Tap the chart or a tile to select a channel. Long-press the chart fits the dB
range to what is on screen (it is remembered). Until a plan arrives the tiles are
hidden and the chart reads *Waiting for spectrum frames…* rather than showing a
false empty sweep.

## Without the board

`SimFeed` is a device simulator behind one intent extra, so the instrument can be
laid out and demoed with no hardware (the web UI has the same thing in
`repo/web/src/mock`):

```sh
adb shell am start -n com.onebody.widscan58/.Widscan58Activity --ez sim true
```

It emits the same SPECTRUM / METRICS / TELEMETRY payloads the firmware does for a
band of VTXs plus the board spurs, answers the band, gain and Start/Stop
controls, and never touches the USB stack. Nothing else starts it.

## Language

Chinese is the default and English is the one translation: `res/values/` carries
Chinese, `res/values-en/` English, so the app follows the system language and
falls back to Chinese on a device set to anything else. On Android 13+ the
system's per-app language picker offers both (`res/xml/locale_config.xml`).

Unit symbols (`dBFS`, `MHz`, `SNR`), the band letters and the raw USB
diagnostics — the probe screen and `UsbLinkException` messages — read the same in
both, because they are what you paste into a bug report.

The screen is portrait-only for now: there is no landscape layout, and rotating
would crush the panorama into a strip.

## How it talks to the board

`PROTOCOL MONITOR` first — the firmware boots silent in RotorHazard mode. Then
`SCAN STOP`, the stored plan (`CHANNELS …`, `MODE …`, `GAIN MANUAL …`),
`SPEC 4 256 40000000 MAX`, `PROFILE?`, `STATUS?`, `SPURS?`, `SCAN START`.
Telemetry is 4 Hz even when stopped, so no frames for 3 s means the board
restarted; the app re-runs the handshake by itself.

Frames are the binary `0xA5 'R'` packets with a CRC32; `PROFILE`/`STATUS` replies
are JSON. All of it is decoded in `:core`, off the USB read thread, and applied
on the main thread.

## Modules

- `core/` — pure Kotlin/JVM: frame parser, CRC, stream resync, command runner,
  band plans, the store (segments, max hold, average, per-LO floor), panorama
  compositing, waterfall rows, channel tiles. No `android.*` imports, covered by
  49 JVM tests.
- `mobile/` — Android: USB CDC host (`usb/`), the JSON reply codec, preferences,
  the instrument screen and its canvas views.

## Not in this version

Channel zoom, RSSI history pages, the channel-plan editor, RotorHazard mode,
power-meter mode, RAW IQ capture, the diagnostics console, and record/replay.
The log/probe screen is still reachable as the **USB probe** launcher entry, for
when the board shows up but not as a CDC port.

## Verification

`:core:test` — 49 JVM tests over the frame codec, stream resync, command runner,
store, panorama compositing, waterfall rows and channel tiles.

The UI has been driven on an `arm64-v8a` emulator (Android 11, 1080x1920) against
the simulated feed at 411x731 dp and, with `adb shell wm density 480`, at 360x640
dp: empty state, band switching, dB fit, hold toggles, gain and Start/Stop all
behave, and the waterfall fills one row per rotation. Both languages were
screenshotted at both widths — the axis gutter is sized for the wider of
`-29.7s` and `-44.2秒`.

The language switch itself was checked the way a device does it, not by renaming
resource directories: `settings put system system_locales zh-CN` plus a reboot
puts `[zh_CN]` into the activity's `mCurrentConfig`, and the same path back to
`en-US` renders English again.

On real hardware — a Xiaomi 25113PN0EC running Android 16 at 1220x2656 and 520 dpi,
about 375x817 dp — the same simulated feed renders correctly in both languages, and
`cmd locale set-app-locales com.onebody.widscan58 --locales en` flips the app to
English while the phone itself stays Chinese, which is the Android 13+ per-app
picker taking `res/xml/locale_config.xml` at its word. That pass found two things
the emulator could not: Android 15+ lays content under the status and gesture bars,
so the content view now pads itself by the system-bar insets, and vendor
`DeviceDefault` themes disagree about button case, so the two buttons pin theirs.

The wire format and the command set were then checked against the board itself, over
its CDC-ACM port on the Mac, rather than only against the simulator. A 128 KiB capture
decoded to 150 SPECTRUM, 902 METRICS and 22 TELEMETRY frames with zero CRC errors, at
the ~370 KB/s the board sustains in monitor mode, and every line the connect handshake
sends was answered acceptingly: `PROTOCOL MONITOR`, `CHANNELS R1=5658 … R8=5917`,
`MODE GROUP`, `GAIN MANUAL 45`, `SPEC 4 256 40000000 MAX`, `PROFILE?`, `STATUS?`,
`SPURS?`, `SCAN START` and `INFO` (`INFO widscan58|esp32c5|1|78d1c81|…`). Three things
came out of that which the firmware source alone does not make obvious: the board's
USB-Serial-JTAG exposes a vendor class `0xFF` interface with a bulk pair of its own
right beside the CDC one, `CHANNELS?`/`MODE?`/`SPEC?` are not commands at all (the
device answers `ERR command`), and a scan that is stopped keeps sending `TELEMETRY`
frames at about 4 Hz, which is what lets the watchdog treat a truly silent line as a
dead link instead of an idle one.

Not yet exercised is the last part of the real USB path — enumeration on a phone, the
permission dialog, and the C5 DTR/RTS erratum — because that needs the board on a
phone. `MainActivity` is the diagnostic for exactly that: it prints each attached
device's VID/PID, permission state and per-interface endpoint table.

## License

GPL-3.0-or-later, same as the upstream firmware and web UI, whose authors and
the ESP32-SDR / C5VRX work it builds on are credited in `repo/README.md`.
