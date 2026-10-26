# Widscan58 for Android

A handheld spectrum analyzer for the Widscan58 board. The ESP32-C5 goes into the
phone's USB-C port and the app reads it directly as a serial device: panorama,
waterfall, eight channel tiles, band and gain controls, no Wi-Fi, no bridge
computer, no root.

It is a companion to the browser UI in [`../web`](../web), not a replacement for
it. The wire codec, the resyncing stream parser and the display engine here are
ports of `../web/src/serial/parser.ts` and `../web/src/model/store.ts`, so a
reading means the same thing in both. What the native app adds is that the
instrument opens itself when you plug the board in, needs no browser and no
network at the field, and keeps a full-size layout that a phone browser tab does
not give you. If your phone's browser has no Web Serial, this is the other way
in.

## Requirements

- Android 8.0 (API 26) or newer, with USB host / OTG on the phone.
- A board running the widscan58 firmware. Its native USB is the Espressif
  USB-Serial/JTAG port (VID `0x303A`, PID `0x1001`), which enumerates as CDC-ACM.
  Flash the firmware first from a computer or the web UI's flasher — the app does
  not flash.
- A cable that carries data, not charge-only.

## Build and install

```sh
cd app
./gradlew :core:test :mobile:assembleDebug
adb install -r mobile/build/outputs/apk/debug/mobile-debug.apk
```

Gradle 8.14, AGP 8.11, Kotlin 2.2, JDK 17. `local.properties` must point at your
Android SDK (`sdk.dir=…`). `:core:test` is pure JVM and needs no device.

A release build is `./gradlew :mobile:assembleRelease`, which on its own produces an
**unsigned APK that no phone will install**. To sign it, drop a git-ignored
`keystore.properties` in the project root — the key itself stays outside the repository:

```ini
storeFile=/absolute/path/to/release.jks
storePassword=…
keyPassword=…
keyAlias=…
```

With that file present the release build is signed; without it the build still succeeds
and just stays unsigned, so a clean checkout never fails. Once the signing key differs
from the debug key, older debug installs cannot be upgraded in place — uninstall first.

Then connect the board and grant the USB permission dialog. The app is in the
`USB_DEVICE_ATTACHED` filter, so plugging the board in can open it by itself.

## Documentation

- **The illustrated tutorial ships inside the app**: `⋯ → Help`. It is a local page
  under `mobile/src/main/assets/help/` — no network access, no permission for it,
  and it works with the board unplugged. Chinese only for now.
- **The reference manual** is [`docs/MANUAL.zh-CN.md`](docs/MANUAL.zh-CN.md): every
  control with the values it accepts and the firmware command behind it, plus the
  wire frame format and the `.widscan58` / CSV file layouts. Also Chinese only.

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

The portrait-only layout is deliberate: there is no landscape layout, and
rotating would crush the panorama into a strip.

## How it talks to the board

`PROTOCOL MONITOR` first, because the firmware boots silent in RotorHazard mode
([`../docs/RotorHazard.md`](../docs/RotorHazard.md)). Then `SCAN STOP`, the
stored plan (`CHANNELS R1=5658 … R8=5917`, `MODE GROUP`, `GAIN MANUAL 45`),
`SPEC 4 256 40000000 MAX`, `PROFILE?`, `STATUS?`, `SPURS?`, `SCAN START`.

Frames are the binary `0xA5 'R' type ver | u16 len | payload | u32 crc32` packets;
`PROFILE` and `STATUS` answer with JSON. All of it is decoded in `:core`, off the
USB read thread, and applied on the main thread. The read is DTR-on, RTS-off:
on a C5 at rev 1.0 or earlier an RTS reset can leave USB dead until power cycle.

A stopped scan still emits `TELEMETRY` at about 4 Hz, which is what lets the
watchdog tell a dead link from an idle one — no frames for 3 s means the board
restarted, and the app runs the handshake again by itself.

## Modules

- `core/` — pure Kotlin/JVM: frame parser, CRC, stream resync, command runner,
  band plans, the store (segments, max hold, average, per-LO floor), panorama
  compositing, waterfall rows, channel tiles. No `android.*` imports, 49 tests.
- `mobile/` — Android: USB CDC host (`usb/`), the JSON reply codec, preferences,
  the instrument screen and its canvas views.

Nothing in `core/` knows what a USB endpoint is, which is why the interesting
bugs in this app were all found in `mobile/usb/`.

## Language

Chinese is the default and English is the one translation: `res/values/` carries
Chinese, `res/values-en/` English, so the app follows the system language and
falls back to Chinese on a device set to anything else. On Android 13+ the
system's per-app language picker offers both (`res/xml/locale_config.xml`).
There is a Chinese edition of this document at
[`README.zh-CN.md`](README.zh-CN.md).

Unit symbols (`dBFS`, `MHz`, `SNR`), the band letters and the raw USB
diagnostics — the probe screen and `UsbLinkException` messages — read the same in
both languages, because they are what you paste into a bug report.

## Without the board

`SimFeed` is a device simulator behind one intent extra, so the instrument can be
laid out and demoed with no hardware, the way `../web/src/mock` does for the web
UI:

```sh
adb shell am start -n com.onebody.widscan58/.Widscan58Activity --ez sim true
```

It emits the same SPECTRUM / METRICS / TELEMETRY payloads the firmware does for a
band of VTXs plus the board spurs, answers the band, gain and Start/Stop controls,
and never touches the USB stack. Nothing else starts it.

## Not in this version

Channel zoom, RSSI history pages, the channel-plan editor, RotorHazard mode,
power-meter mode, RAW IQ capture, the diagnostics console, and record/replay. The
log/probe screen is reachable as the **USB probe** launcher entry, for when the
board shows up but not as a CDC port.

## Verification

`:core:test` — 49 JVM tests over the frame codec, stream resync, command runner,
store, panorama compositing, waterfall rows and channel tiles.

The UI was driven on an `arm64-v8a` emulator (Android 11, 1080x1920) against the
simulated feed at 411x731 dp and, with `adb shell wm density 480`, at 360x640 dp.
The language switch was checked the way a device does it — `settings put system
system_locales` plus a reboot puts `[zh_CN]` in the activity's `mCurrentConfig` —
not by renaming resource directories.

Against the board itself, over its CDC-ACM port: a 128 KiB capture decoded to 150
SPECTRUM, 902 METRICS and 22 TELEMETRY frames with **zero CRC errors**, at the
~370 KB/s the board sustains in monitor mode; and every line of the handshake was
accepted, `INFO` answering `widscan58|esp32c5|1|78d1c81|…`. Three things only the
hardware showed: the USB-Serial/JTAG descriptor carries a vendor class `0xFF`
interface with a bulk pair of its own right beside the CDC data interface, so
"the first interface with a bulk pair" is the wrong test; `CHANNELS?`, `MODE?` and
`SPEC?` are not commands at all; and a stopped scan keeps sending telemetry.

On a phone — Xiaomi 25113PN0EC, Android 16, 1220x2656 at 520 dpi — with the board
on the phone's own port, the system log shows the attach arriving as
`/dev/bus/usb/001/010, mVendorId=12346, mProductId=4097, mClass=239` and the
activity being started from `USB_DEVICE_ATTACHED`, and the instrument scans. That
pass found three things the emulator structurally could not: Android 15+ lays
content under the status and gesture bars, so the content view pads itself by the
system-bar insets; vendor `DeviceDefault` themes disagree about button case; and
`UsbInterface.getEndpoint(int)` takes an array index, not a direction and number
packed together, so the endpoint lookup had to become a scan of the interface's
endpoints.

## License

GPL-3.0-or-later, the same license as the firmware and web UI it ports, whose
authors and the ESP32-SDR / C5VRX work they build on are credited in the
top-level [`README.md`](../README.md).
