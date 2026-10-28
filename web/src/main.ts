// Widscan58 web UI entry point. GPL-3.0-or-later.
import "./styles.css";
import { App, type View } from "./model/app";
import { Store, parseInfo } from "./model/store";
import {
  Device,
  WebSerialTransport,
  WebSocketTransport,
  grantedEspressifPort,
  requestEspressifPort,
  type Transport,
} from "./serial/device";
import { Recorder, ReplayTransport } from "./serial/recorder";
import { MockTransport, type SimTarget } from "./mock/mockDevice";
import { applyLang, getLang, setLang, t, type Lang } from "./i18n";
import { el, invalidateTheme } from "./util/canvas";
import { ChannelGrid } from "./views/channelGrid";
import { Panorama } from "./views/panorama";
import { Zoom } from "./views/zoom";
import { History } from "./views/history";
import { Telemetry, Console } from "./views/debug";
import { Controls } from "./views/controls";
import { RawIqView } from "./views/rawiq";
import { FlashDialog } from "./views/flashDialog";
import { GpioDialog } from "./views/gpioDialog";
import { RhServerDialog } from "./views/rhServerDialog";
import { ScreenWake } from "./util/wakeLock";
import { registerServiceWorker } from "./util/pwa";
import { lapCallout, speak } from "./util/speech";
import { voiceSettings } from "./views/timerEventsDialog";
import { TimerEvents, TimerGraph, TimerSeats, TimerSettings } from "./views/timer";
import { PowerChannels, PowerWarning } from "./views/power";
import { MONITOR_DEFAULTS, POWER_WIN_HZ, powerCommands } from "./model/power";

const store = new Store();
const app = new App(store);
const recorder = new Recorder();

// ---- Theme ------------------------------------------------------------------
// The installed app's title bar follows the theme (--page).
function setThemeColor(t: string) {
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", t === "light" ? "#f9f9f7" : "#0d0d0d");
}
function applyTheme(t: string) {
  document.documentElement.dataset.theme = t;
  setThemeColor(t);
  darkSwitch.checked = t !== "light";
  invalidateTheme();
  try {
    localStorage.setItem("widscan58.theme", t);
  } catch {
    /* storage unavailable */
  }
  store.touch();
}
let themeName = "dark";
try {
  themeName = localStorage.getItem("widscan58.theme") ?? "dark";
} catch {
  /* storage unavailable */
}
document.documentElement.dataset.theme = themeName;
setThemeColor(themeName);

registerServiceWorker(); // installable, works offline

// ---- Header -----------------------------------------------------------------
const dot = el("span", { class: "dot" });
const connText = el("span", {}, "Disconnected");
const connectBtn = el("button", { class: "primary", "aria-haspopup": "menu", "aria-expanded": "false" }, "Connect"); // becomes Disconnect while connected
// Flashing needs the page itself to own the board's USB port, which browsers
// only allow on https or localhost. A LAN http page (the Pi one) can never do
// it, so the button is shown disabled rather than failing on click.
const flashAvailable = WebSerialTransport.supported();
const flashBtn = el("button", {}, "Flash device");
function updateFlashBtn() {
  flashBtn.disabled = !flashAvailable;
  flashBtn.title = flashAvailable
    ? t("Install the scanner firmware on a board")
    : t(
        "This device cannot flash: open the page at localhost or over https in Chrome/Edge, with the board plugged into this computer.",
      );
}
updateFlashBtn();
const simBtn = el("button", {}, "Simulate");
const replayInput = el("input", { type: "file", accept: ".widscan58", style: "display:none" }) as HTMLInputElement;
const replayBtn = el("button", {}, "Replay…");
const recordBtn = el("button", { title: "Record the raw device stream to a .widscan58 file" }, "● Record");
// Server endpoints: boards bridged by the server this page came from
// (serve.mjs /api/endpoints) plus relay nodes that connected in to it — the
// wireless/remote way to scan, and the only way from a plain http LAN address
// without Web Serial. The first entry means "not connected": picking one
// connects, picking it back disconnects.
const endpointSelect = el("select", {
  hidden: "",
  class: "ep-select",
  title: "Connect to a board bridged by the server this page came from",
  "aria-label": "Server endpoint",
}) as HTMLSelectElement;
endpointSelect.append(new Option("Select a board…", ""));
// Connect opens this list of boards the server can bridge; picking one connects.
const boardMenu = el("div", { class: "menu board-menu", hidden: "", role: "menu" }) as HTMLDivElement;
// Board link switch (serve.mjs /api/mode): starts/stops the RotorHazard server
// to free the board's UART for scanning, or hands the radio back to RH.
const boardBtn = el("button", { hidden: "" });
// Menu: the page / device protocol (monitor, lap timer, or power meter on
// the monitor protocol), GPIO Configure,
// the theme, and about: the project link and the connected device's firmware
// version.
const menuBtn = el("button", { class: "menu-btn", title: "Menu", "aria-haspopup": "true", "aria-expanded": "false" }, "☰");
const modeItems = {
  monitor: el("button", { class: "menu-item", role: "menuitemradio" }, "Monitor mode"),
  timer: el("button", { class: "menu-item", role: "menuitemradio" }, "Lap timer mode"),
  power: el("button", { class: "menu-item", role: "menuitemradio" }, "Power meter mode"),
};
const timerNote = el("div", { class: "menu-note" });
const gpioItem = el("button", { class: "menu-item", role: "menuitem", title: "Pins the device sets at startup" }, "GPIO Configure");
const rhDataItem = el("button", { class: "menu-item", role: "menuitem" }, "RotorHazard race data…");
const rhDataNote = el("div", { class: "menu-note", hidden: "" });
const darkSwitch = el("input", { type: "checkbox", role: "switch" }) as HTMLInputElement;
darkSwitch.checked = themeName !== "light";
const langSelect = el("select", { class: "menu-select", "aria-label": "Language" }) as HTMLSelectElement;
langSelect.append(new Option("English", "en"), new Option("中文", "zh"));
langSelect.value = getLang();
const fwValue = el("span", { class: "menu-value" }, "not connected");
const fwNote = el("div", { class: "menu-note", hidden: "" }); // a newer firmware this page can flash
const menu = el(
  "div",
  { class: "menu", role: "menu", hidden: "" },
  el("div", { class: "menu-label" }, "Mode"),
  modeItems.monitor,
  modeItems.timer,
  modeItems.power,
  timerNote,
  el("div", { class: "menu-sep" }),
  gpioItem,
  rhDataItem,
  rhDataNote,
  el("label", { class: "menu-item menu-switch" }, el("span", {}, "Dark mode"), darkSwitch),
  el("label", { class: "menu-item menu-language" }, el("span", {}, "Language"), langSelect),
  el("div", { class: "menu-sep" }),
  el("div", { class: "menu-label" }, "About"),
  el("a", { class: "menu-item", role: "menuitem", href: "https://github.com/onebody/widscan58", target: "_blank", rel: "noopener" }, "GitHub project ↗"),
  el("div", { class: "menu-info" }, el("span", {}, "Firmware"), fwValue),
  fwNote,
);
const header = el(
  "header",
  { class: "top" },
  el("span", { class: "brand" }, el("span", { class: "menu-wrap" }, menuBtn, menu), el("h1", {}, "Widscan58")),
  el("span", { class: "conn-state" }, dot, connText),
  el("span", { class: "actions" }, boardBtn, endpointSelect, el("span", { class: "menu-wrap" }, connectBtn, boardMenu), flashBtn),
);
// Session sources other than a real device, shown in the protocol console.
const sessionRow = el("div", { class: "session-row" }, simBtn, replayBtn, replayInput, recordBtn);
const banner = el("div", { class: "banner", style: "display:none" });

type Page = "monitor" | "timer" | "power" | "both";
const views: { view: View; page: Page; minMs: number; last: number; drawn: number }[] = [];
function addView(page: Page, view: View, minMs = 0) {
  views.push({ view, page, minMs, last: 0, drawn: -1 });
  return view.el;
}
const monitorMain = el(
  "main",
  {},
  addView("monitor", new ChannelGrid(app), 100),
  addView("monitor", new Panorama(app), 33),
  addView("monitor", new Zoom(app), 33),
  addView("monitor", new History(app), 50),
  addView("monitor", new Telemetry(app), 200),
  addView("monitor", new Controls(app), 100),
  addView("monitor", new RawIqView(app), 100),
);
const timerMain = el(
  "main",
  { class: "timer-page" },
  addView("timer", new TimerSeats(app), 100),
  addView("timer", new TimerGraph(app), 50),
  addView("timer", new TimerEvents(app), 200),
  addView("timer", new TimerSettings(app), 300),
);
const powerMain = el(
  "main",
  { class: "power-page" },
  addView("power", new PowerWarning()),
  addView("power", new PowerChannels(app), 100),
);
const sharedMain = el("main", { class: "shared" }, addView("both", new Console(app, sessionRow), 200));
document.getElementById("app")!.append(header, banner, monitorMain, timerMain, powerMain, sharedMain);

// ---- Pages and device protocol ---------------------------------------------------
// Firmware without the PROTOCOL command is always in monitor mode.
let legacyFirmware = false;
function showPage() {
  const mode = app.settings.uiMode;
  monitorMain.style.display = mode === "monitor" ? "" : "none";
  timerMain.style.display = mode === "timer" ? "" : "none";
  powerMain.style.display = mode === "power" ? "" : "none";
  for (const [m, item] of Object.entries(modeItems)) item.setAttribute("aria-checked", String(m === mode));
  const replay = app.readOnly && !!app.device;
  modeItems.timer.disabled = legacyFirmware || replay;
  timerNote.textContent = legacyFirmware
    ? t("This firmware has no lap timer mode: flash a newer one.")
    : replay
      ? t("Replays are monitor mode only.")
      : "";
  timerNote.hidden = !timerNote.textContent;
  for (const v of views) v.drawn = -1;
  updateBanner();
}

/** Put a connected device in the page's protocol and start the page on it. */
let starting: Promise<void> | null = null;
function startMode(device: Device): Promise<void> {
  const run = async () => {
    if (!device.connected) return; // the link went away before we asked
    let proto: string | null = null;
    try {
      proto = await device.text("PROTOCOL?", 1500);
    } catch (e) {
      store.addLog("info", `no protocol switch (${(e as Error).message}); monitor mode only`);
    }
    // A relay node or bridge can drop mid-handshake; do not keep talking to it.
    if (!device.connected) return;
    legacyFirmware = proto === null;
    if (legacyFirmware && app.settings.uiMode === "timer") app.settings.uiMode = "monitor";
    showPage();
    if (app.settings.uiMode === "timer") {
      await app.send("PROTOCOL RH");
      device.enterRh();
      await app.send("INFO"); // the firmware version (menu, flash dialog); syncDevice has it in monitor mode
      await app.timer.start(device, app.deviceSettings.channels);
    } else {
      await app.timer.stop();
      if (device.rh) {
        await app.send("PROTOCOL MONITOR");
        device.exitRh();
      } else if (!legacyFirmware) {
        const got = await app.send("PROTOCOL MONITOR");
        // A bridge link cannot take the radio while the RH server polls the
        // board's UART (ERR uart_rh in the log); the watchdog keeps retrying.
        if (!got && device.transport instanceof WebSocketTransport && !uartHinted) {
          uartHinted = true;
          store.addLog("info", t("Hint: RotorHazard may hold the radio — the board button in the header frees it."));
        }
      }
      await syncDevice(device);
      if (app.settings.uiMode === "power") await enterPower(device);
      else await leavePower(device);
    }
    lastData = performance.now();
  };
  // One at a time: a mode change while a start runs waits for it.
  const p = (starting ?? Promise.resolve()).then(run, run);
  const tracked: Promise<void> = p
    .catch(() => {})
    .then(() => {
      if (starting === tracked) starting = null;
    });
  starting = tracked;
  return p;
}

function setMode(mode: "monitor" | "timer" | "power") {
  closeMenu();
  if (mode === app.settings.uiMode) return;
  app.settings.uiMode = mode;
  app.changed();
  showPage();
  if (app.device && !app.readOnly) void startMode(app.device);
}
modeItems.monitor.onclick = () => setMode("monitor");
modeItems.timer.onclick = () => setMode("timer");
modeItems.power.onclick = () => setMode("power");
const gpioDialog = new GpioDialog(app);
document.body.append(gpioDialog.el);
gpioItem.onclick = () => {
  closeMenu();
  gpioDialog.open();
};
const rhServerDialog = new RhServerDialog(app);
document.body.append(rhServerDialog.el);
rhDataItem.onclick = () => {
  closeMenu();
  rhServerDialog.open();
};
langSelect.onchange = () => {
  setLang(langSelect.value as Lang);
  relabelChrome();
  store.touch();
};

function closeMenu() {
  menu.hidden = true;
  menuBtn.setAttribute("aria-expanded", "false");
}
menuBtn.onclick = (e) => {
  e.stopPropagation();
  menu.hidden = !menu.hidden;
  menuBtn.setAttribute("aria-expanded", String(!menu.hidden));
};
menu.onclick = (e) => e.stopPropagation();
document.addEventListener("click", closeMenu);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeMenu();
});
darkSwitch.onchange = () => applyTheme(darkSwitch.checked ? "dark" : "light");

// ---- Connection ---------------------------------------------------------------
// Keep screen on (detection events options): only while a device is connected.
const screenWake = new ScreenWake((msg) => store.addLog("error", msg));
const syncWake = () => screenWake.set(app.settings.keepScreenOn && !!app.device);
store.subscribe(syncWake); // the option changing (app.changed touches the store)
// Voice callouts (detection events options): each lap's channel and time.
app.timer.onLap = (seat, lapS) => {
  if (app.settings.voiceOn && seat.channel) speak(lapCallout(seat.channel, lapS), voiceSettings(app.settings));
};

let connState: "off" | "ok" | "sim" | "err" = "off";
let connKey = "Disconnected";
let connParams: Record<string, string | number> | undefined;
function setState(state: typeof connState, key: string, params?: Record<string, string | number>) {
  connState = state;
  connKey = key;
  connParams = params;
  dot.className = `dot ${state === "off" ? "" : state}`;
  connText.textContent = connText.title = t(key, params); // the title shows it in full when cut short
  const connected = state === "ok" || state === "sim";
  connectBtn.textContent = connected ? t("Disconnect") : t("Connect");
  connectBtn.classList.toggle("primary", !connected);
  simBtn.disabled = connected;
  replayBtn.disabled = connected;
  recordBtn.disabled = !connected || app.readOnly;
  syncWake();
}

/** Re-apply the texts that main.ts writes outside a view's render. */
function relabelChrome() {
  setState(connState, connKey, connParams);
  showPage(); // also updates the banner and firmware text
  syncRhDataNote();
  if (boardLinkApi) {
    renderEndpoints();
    updateBoardBtn();
  }
  updateFlashBtn();
  if (recorder.active) recordBtn.textContent = t("■ Stop recording");
}

/** The RotorHazard server's state in the menu, when the name overlay is on. */
function syncRhDataNote() {
  const s = app.rhServer;
  let text = "";
  if (s.on) {
    if (s.status === "error") text = t("RH: {msg}", { msg: s.errorText() ?? "" });
    else if (s.status === "loading") text = t("Reading…");
    else if (s.status === "waiting") text = t("RH: {msg}", { msg: t("Set the server address.") });
    else if (s.status === "ok" && s.snapshot) {
      const snap = s.snapshot;
      if (snap.practice) text = t("RH: practice · no pilots");
      else {
        const heat = snap.heatName || t("Heat {n}", { n: snap.heatId });
        text = snap.byMhz.size
          ? t("RH: {heat} · {n} pilots", { heat, n: snap.byMhz.size })
          : t("RH: {heat} · no pilots", { heat });
      }
    }
  }
  rhDataNote.textContent = text;
  rhDataNote.hidden = !text;
  rhDataNote.classList.toggle("err", s.status === "error");
}

function updateBanner() {
  const p = store.profile;
  const parts: (Node | string)[] = [];
  const add = (strong: string, rest: string) => parts.push(el("strong", {}, strong), ` ${rest} `);
  if (app.device?.transport instanceof MockTransport)
    add(t("Simulated device."), t("Signals, spurs and IMD products are synthetic."));
  if (app.settings.uiMode === "timer" && app.device)
    add(
      t("Lap timer mode."),
      t(
        "This page polls the device as a RotorHazard server would. Disconnect before a RotorHazard server opens the port.",
      ),
    );
  if (app.readOnly) add(t("Replay."), t("Controls are disabled."));
  if (p && p.scale !== 1)
    add(
      `${p.name}:`,
      t("Raceband scaled into {lo}–{hi} MHz (k = {k}). Widths and windows are scaled by k.", {
        lo: p.band_khz[0] / 1000,
        hi: p.band_khz[1] / 1000,
        k: p.scale.toFixed(4),
      }),
    );
  banner.replaceChildren(...parts);
  banner.style.display = parts.length ? "block" : "none";
  updateFirmwareText();
}

// The firmware version (INFO, parseInfo) is shown in the menu for any
// connected device; the offer of another version only over Web Serial, where
// the page can flash it. The page offers several versions
// (firmware/versions.json); a board running any of them is up to date.
let offeredFws: string[] = [];
function updateFirmwareText() {
  const fw = app.device ? parseInfo(store.info)?.version : undefined;
  const value = !app.device ? t("not connected") : fw || t("unknown");
  if (fwValue.textContent !== value) fwValue.textContent = value;
  const offer =
    fw && offeredFws.length && !offeredFws.includes(fw) && app.device?.transport instanceof WebSerialTransport
      ? offeredFws[0]
      : null;
  const note = offer ? t("{v} available", { v: offer }) : "";
  if (fwNote.textContent !== note) fwNote.textContent = note;
  fwNote.hidden = !note;
}

async function syncDevice(device: Device) {
  if (device.rh) return; /* lap timer mode: the page does its own setup */
  await app.restoreDevice(device);
  for (const cmd of ["INFO", "PROFILE?", "STATUS?", "SPURS?"]) {
    if (!device.connected) return;
    try {
      await device.command(cmd);
    } catch (e) {
      if (!device.connected) return; // the close reason is already logged
      store.addLog("error", (e as Error).message);
    }
  }
}

// Power meter mode runs the monitor protocol with the scan its calibration
// was made with (model/power.ts). Monitor mode's own windows and spectrum are
// kept here and put back on leaving it; the channel plan, LO mode and gain
// come back with restoreDevice.
let monitorScan: { win: string; spec: string } | null = null;
const powerScan = (st: typeof store.status) => !!st && st.rssi_hz === POWER_WIN_HZ && st.spec_every === 0;
async function sendAll(device: Device, cmds: string[]) {
  for (const cmd of cmds) {
    if (!device.connected) return;
    store.addLog("tx", cmd);
    try {
      await device.command(cmd);
    } catch (e) {
      if (!device.connected) return; // the close reason is already logged
      store.addLog("error", (e as Error).message);
    }
  }
}
async function enterPower(device: Device) {
  const st = store.status;
  if (st && !powerScan(st))
    monitorScan = {
      win: `WIN ${st.rssi_hz} ${st.search_hz}`,
      spec: `SPEC ${st.spec_every} ${st.spec_bins} ${st.spec_span_hz} ${st.spec_detector ? "MAX" : "MEAN"}`,
    };
  await sendAll(device, [...powerCommands(store.profile?.usable_hz ?? 40000000), "PROFILE?", "STATUS?"]);
}
async function leavePower(device: Device) {
  if (!powerScan(store.status)) return;
  const d = store.profile && MONITOR_DEFAULTS[store.profile.name];
  const scan = monitorScan ?? (d && { win: `WIN ${d.rssiHz} ${d.searchHz}`, spec: d.spec });
  if (scan) await sendAll(device, [scan.win, scan.spec, "PROFILE?", "STATUS?"]);
}

async function connect(transport: Transport, readOnly = false) {
  const device = new Device(transport);
  store.info = "";
  device.onItem((item) => {
    store.ingest(item);
    if (item.kind === "frame") lastData = performance.now();
  });
  device.onRaw((chunk) => recorder.add(chunk));
  if (transport instanceof WebSerialTransport) transport.onWarning = (msg) => store.addLog("info", `serial: ${msg}`);
  device.onClose = (why) => {
    // serve.mjs sends short codes for the reasons worth explaining.
    const BRIDGE_WHY: Record<string, string> = {
      busy: "the board is already bridged to another page",
      busyport: "the board's serial port is held by another program — with one board shared with RotorHazard, click the board button first",
      "no-serial": "the server has no board to bridge",
      "node offline": "the relay node went offline",
      "uart-only": "this board is on the host's UART link: RotorHazard only, the firmware refuses to scan there",
    };
    const whyText = BRIDGE_WHY[why] ? t(BRIDGE_WHY[why]) : why;
    store.addLog("info", `connection closed: ${whyText}`);
    if (app.device === device) {
      app.device = null;
      if (why === "replay finished") setState("off", "Replay finished");
      else setState("err", "Disconnected ({why})", { why: whyText });
      stopRecording();
    }
    if (transport instanceof WebSocketTransport && transport === bridgeTransport) {
      bridgeTransport = null;
      bridgedEp = "";
      endpointSelect.value = "";
      void refreshEndpoints();
    }
  };
  try {
    await device.open();
  } catch (e) {
    store.addLog("error", `open failed: ${(e as Error).message}`);
    setState("err", "Connection failed ({why})", { why: (e as Error).message });
    return;
  }
  app.device = device;
  app.readOnly = readOnly;
  setState(transport instanceof MockTransport ? "sim" : "ok", transport.name);
  (window as unknown as { widscan58: unknown }).widscan58 = { app, store }; // test/debug hook
  store.addLog("info", `connected via ${transport.name}`);
  if (readOnly) {
    legacyFirmware = false;
    if (app.settings.uiMode === "timer") app.settings.uiMode = "monitor";
    showPage();
  } else await startMode(device);
  updateBanner();
}

async function disconnect() {
  userDisconnected = true;
  const d = app.device!;
  await app.timer.stop();
  // Leave the device silent, as a RotorHazard server expects to find it.
  if (!d.rh && !legacyFirmware && !app.readOnly && d.connected) await d.command("PROTOCOL RH", 500).catch(() => {});
  app.device = null;
  stopRecording();
  await d.close();
  setState("off", "Disconnected");
}

connectBtn.onclick = async () => {
  if (app.device) return disconnect();
  if (!WebSerialTransport.supported()) {
    // Served by serve.mjs: the page's own server bridges the board over a
    // WebSocket, which works from a plain http LAN address without Web Serial.
    if (boardLinkApi) {
      if (!boardMenu.hidden) return closeBoardMenu();
      await pickBridge();
      return;
    }
    store.addLog("error", "Web Serial is not available: use Chrome, Edge or Opera over https or localhost.");
    setState("err", "Web Serial unsupported in this browser");
    return;
  }
  userDisconnected = false;
  void connect(new WebSerialTransport());
};

// ---- Board link (single board shared by scanning and RotorHazard) ----------------
// serve.mjs answers /api/mode only when started with the RH start/stop
// commands; GitHub Pages (and any other host) never does, so the button and
// the bridge fallback stay hidden there.
let boardLinkApi = false;
let boardLink: "rh" | "scan" | "unknown" = "unknown";
let uartHinted = false;
let uartPort = ""; // the host's UART board (serve.mjs --rh-serial), once the list says so
function updateBoardBtn() {
  // Name the port: "scanning / RotorHazard" alone reads like a board picker,
  // which is the endpoint list's job, and never says which board RH drives.
  boardBtn.textContent =
    boardLink === "rh"
      ? uartPort
        ? t("RotorHazard on ({port}) — click to stop", { port: uartPort })
        : t("RotorHazard on — click to stop")
      : t("RotorHazard off — click to start");
  boardBtn.title = t("Starts or stops the RotorHazard server on the host to free the board's radio.");
}
boardBtn.onclick = async () => {
  if (boardBtn.disabled) return;
  boardBtn.disabled = true;
  const to = boardLink === "scan" ? "rh" : "scan";
  // Bridging the UART board is what stopped RH: leaving the bridge starts it
  // again on the server, so handing the radio back is just a disconnect.
  if (to === "rh" && bridgedEp.startsWith("uart:") && app.device) {
    await disconnect().catch(() => {});
    boardBtn.disabled = false;
    return;
  }
  try {
    // Only one board on the server means RH and the page share its tty (the
    // legacy single-board switch): leave it silent so RH can open the port.
    // With several boards RH has its own (the UART one) and scanning goes on.
    const sharedBoard =
      bridgedEp.startsWith("serial:") && endpoints.filter((e) => e.kind === "serial").length === 1;
    if (to === "rh" && app.device && sharedBoard) await disconnect();
    const r = await fetch("/api/mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: to }),
    });
    const j = (await r.json().catch(() => ({}))) as { state?: typeof boardLink; error?: string };
    if (!r.ok) throw new Error(j.error ?? `HTTP ${r.status}`);
    boardLink = j.state ?? to;
    uartHinted = false;
    updateBoardBtn();
    store.addLog(
      "info",
      to === "scan"
        ? t("RH server stopped; the radio is free in ~2 s (the page retries on its own).")
        : t("RH server started; it takes the radio back on its first byte."),
    );
    if (to === "scan") {
      if (app.device) void startMode(app.device);
      // One board shared with RH: the server lets go of the tty as it stops,
      // so wait a moment before the bridge opens it. An explicit stop is not
      // undone by this connect, so RH stays down until the next user connect.
      // Scanning is the point of this state: pick up a board rather than
      // parking on an empty page — the endpoint list still switches boards.
      else setTimeout(() => void resumeScan(), 1500);
    }
  } catch (e) {
    setState("err", "Switching the board failed ({why})", { why: (e as Error).message });
  } finally {
    boardBtn.disabled = false;
  }
};
void (async () => {
  try {
    const r = await fetch("/api/mode", { signal: AbortSignal.timeout(3000) });
    if (!r.ok) return;
    const j = (await r.json()) as { state?: "rh" | "scan" | "unknown"; stop?: boolean; start?: boolean };
    boardLinkApi = true;
    boardLink = j.state ?? "unknown";
    const inScan = j.state === "scan";
    // Without both commands the switch cannot do anything: this is a computer
    // running serve.mjs for its own USB boards, where RotorHazard is not the
    // page's business. Offer only what works.
    if (j.stop && j.start) {
      boardBtn.hidden = false;
      updateBoardBtn();
    }
    endpointSelect.hidden = false;
    void refreshEndpoints();
    setInterval(refreshEndpoints, 5000); // boards plug in, relay nodes come and go
    // The host is already in scan mode: this page exists to show the stream,
    // so pick up a board instead of parking on an empty window.
    if (inScan) setTimeout(() => void resumeScan(), 800);
  } catch {
    /* GitHub Pages or another static host: no board link control */
  }
})();
// ---- Server endpoints (serve.mjs) ------------------------------------------------
// A server lists what it can bridge: its own boards (/dev/widscan58*) and the
// relay nodes that connected in. The page picks one at connect time; the
// placeholder option means "not connected".
// "uart" is the board on the host's own UART pins. With a stop command
// configured the server frees it (stops RotorHazard) and it scans like a USB
// board; otherwise it is shown so the list is complete, but only for RH.
type Endpoint = {
  id: string;
  kind: "serial" | "remote" | "uart";
  label: string;
  path?: string;
  online: boolean;
  busy: boolean;
  scannable?: boolean;
};
const canScan = (e: Endpoint) => e.online && (e.kind !== "uart" || !!e.scannable);
let endpoints: Endpoint[] = [];
let bridgedEp = ""; // the endpoint this page is bridged to, if any
let bridgeTransport: WebSocketTransport | null = null; // guards the onClose reset when switching endpoints

function endpointName(e: Endpoint): string {
  if (e.kind === "remote") return t("Wireless node {name}", { name: e.label });
  if (e.kind === "uart" && !e.scannable) return t("{name} (RotorHazard only)", { name: e.label });
  return e.label;
}

function renderEndpoints() {
  const keep = endpointSelect.value;
  endpointSelect.replaceChildren(new Option(t("Select a board…"), ""));
  for (const e of endpoints) {
    const name = endpointName(e);
    const opt = new Option(e.busy && !(e.kind === "uart" && !e.scannable) ? t("{name} (in use)", { name }) : name, e.id);
    opt.disabled = e.kind === "uart" && !e.scannable;
    endpointSelect.append(opt);
  }
  endpointSelect.value = [...endpointSelect.options].some((o) => o.value === keep) ? keep : bridgedEp;
  endpointSelect.disabled = endpoints.length === 0;
  endpointSelect.title = endpoints.length
    ? t("Connect to a board bridged by the server this page came from")
    : t("The server reports no board and no relay node.");
}

let drawnEndpoints = "\u0000"; // what the select currently shows
async function refreshEndpoints() {
  try {
    const r = await fetch("/api/endpoints", { signal: AbortSignal.timeout(3000) });
    if (!r.ok) return;
    const j = (await r.json()) as { endpoints?: Endpoint[] };
    endpoints = j.endpoints ?? [];
    const port = endpoints.find((e) => e.kind === "uart")?.label ?? "";
    if (port !== uartPort) {
      uartPort = port;
      if (!boardBtn.hidden) updateBoardBtn();
    }
    const key = endpoints
      .map((e) => `${e.id}:${e.online ? 1 : 0}:${e.busy ? 1 : 0}:${e.scannable ? 1 : 0}`)
      .join("|");
    // Rebuilding the <option>s closes the browser's own picker and drops the
    // tap that was about to select a board, so leave the list alone unless it
    // really changed — and never while the picker is open.
    if (key === drawnEndpoints || document.activeElement === endpointSelect) return;
    drawnEndpoints = key;
    renderEndpoints();
  } catch {
    /* server went away; the next poll tries again */
  }
}

function closeBoardMenu() {
  boardMenu.hidden = true;
  connectBtn.setAttribute("aria-expanded", "false");
}
boardMenu.onclick = (e) => e.stopPropagation();
// Capture phase: some views stop the click on their controls (channel
// checkboxes), and those clicks must still dismiss the list.
document.addEventListener(
  "pointerdown",
  (e) => {
    if (boardMenu.hidden) return;
    const target = e.target as Node;
    if (boardMenu.contains(target) || connectBtn.contains(target)) return;
    closeBoardMenu();
  },
  true,
);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeBoardMenu();
});

/** Connect asks which board to scan when the server offers a choice. */
async function pickBridge(opts: { restoreRh?: boolean } = {}) {
  await refreshEndpoints(); // boards may have been plugged or picked up since the last poll
  const usable = endpoints.filter(canScan);
  if (!usable.length) {
    store.addLog("error", t("No board or relay node is available on the server."));
    return;
  }
  if (usable.length === 1) return connectBridge(usable[0].id, opts);
  boardMenu.replaceChildren(el("div", { class: "menu-label" }, t("Select a board…")));
  for (const e of endpoints) {
    if (!e.online) continue;
    if (e.kind === "uart" && !e.scannable) {
      // Never a scan source — the firmware refuses PROTOCOL MONITOR on that
      // link — but it is the board the user can "switch to": tapping it hands
      // the radio back to RH timing.
      const on = boardLink === "rh";
      const item = el(
        "button",
        { class: "menu-item", role: "menuitemradio", "aria-checked": on ? "true" : "false" },
        on ? t("{name} — RotorHazard timing", { name: e.label }) : t("{name} — start RotorHazard timing", { name: e.label }),
      ) as HTMLButtonElement;
      item.title = t("This board is wired to the host's UART pins: RotorHazard owns it, and the firmware refuses to scan there.");
      item.onclick = () => {
        closeBoardMenu();
        if (boardLink !== "rh") boardBtn.click(); // same path as the header button (shared-board case included)
      };
      boardMenu.append(item);
      continue;
    }
    const name = endpointName(e);
    const item = el("button", { class: "menu-item", role: "menuitem" }, e.busy ? t("{name} (in use)", { name }) : name) as HTMLButtonElement;
    item.disabled = e.busy;
    item.onclick = () => {
      closeBoardMenu();
      connectBridge(e.id, opts);
    };
    boardMenu.append(item);
  }
  boardMenu.hidden = false;
  connectBtn.setAttribute("aria-expanded", "true");
}

function connectBridge(id?: string, opts: { restoreRh?: boolean } = {}) {
  if (app.device) return;
  // The auto-pick never takes the UART board: selecting it means stopping
  // RotorHazard, which only an explicit choice should do.
  const auto = endpoints.filter((e) => canScan(e) && e.kind !== "uart");
  const ep = id ?? (auto.find((e) => !e.busy) ?? auto[0])?.id;
  if (!ep) {
    store.addLog("error", t("No board or relay node is available on the server."));
    return;
  }
  userDisconnected = false;
  bridgedEp = ep;
  endpointSelect.value = ep;
  const info = endpoints.find((e) => e.id === ep);
  bridgeTransport = new WebSocketTransport(
    `ws://${location.host}/serial?ep=${encodeURIComponent(ep)}`,
    info ? endpointName(info) : ep,
  );
  void connect(bridgeTransport).then(() => {
    if (opts.restoreRh !== false && !ep.startsWith("uart:")) void restoreRhIfIdle();
  });
}

/**
 * The host is in scan mode and the page has no stream: take a free board on
 * its own. RH stays down, exactly as the switch intended; the endpoint list
 * still lets the user move to another board afterwards.
 */
async function resumeScan() {
  if (app.device) return;
  await refreshEndpoints();
  if (endpoints.some(canScan)) connectBridge(undefined, { restoreRh: false });
}

/**
 * Scanning a USB board never needs RH's own (UART) board, so a stopped RH
 * there just means lap timing is down: bring it back. The legacy single-board
 * setup is the exception — RH and the page want the same tty.
 */
async function restoreRhIfIdle() {
  if (!boardLinkApi || boardLink !== "scan" || !app.device) return;
  if (bridgedEp.startsWith("uart:")) return; // the bridge owns it: RH comes back when the page leaves
  if (bridgedEp.startsWith("serial:") && endpoints.filter((e) => e.kind === "serial").length === 1) return;
  try {
    const r = await fetch("/api/mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: "rh" }),
    });
    const j = (await r.json().catch(() => ({}))) as { state?: typeof boardLink; error?: string };
    if (!r.ok) throw new Error(j.error ?? `HTTP ${r.status}`);
    boardLink = j.state ?? "rh";
    updateBoardBtn();
    store.addLog("info", t("RH server started; it takes the radio back on its first byte."));
  } catch (e) {
    store.addLog("error", t("Restoring RotorHazard failed ({why})", { why: (e as Error).message }));
  }
}

endpointSelect.onchange = async () => {
  const ep = endpointSelect.value;
  closeBoardMenu(); // the board button's own list may be open behind the picker
  if (app.device) await disconnect().catch(() => {}); // one radio per page
  if (ep) connectBridge(ep);
};
// ?sim=s3 simulates the ESP32-S3's scaled 2.4 GHz band instead of the C5's.
const simTarget = (): SimTarget => (new URLSearchParams(location.search).get("sim") === "s3" ? "s3" : "c5");
simBtn.onclick = () => void connect(new MockTransport(simTarget()));
replayBtn.onclick = () => replayInput.click();
replayInput.onchange = async () => {
  const f = replayInput.files?.[0];
  if (!f) return;
  void connect(new ReplayTransport(await f.arrayBuffer(), f.name), true);
  replayInput.value = "";
};
// ---- Flashing -------------------------------------------------------------------
let flashing = false; // keeps auto-reconnect off the port meanwhile
const flashDialog = new FlashDialog({
  async takePort() {
    const t = app.device?.transport;
    if (t instanceof WebSerialTransport && t.currentPort) {
      // Flash the connected board. Quiet its stream first, so the reset into
      // download mode does not race it (tools/flash.sh does the same).
      const port = t.currentPort;
      if (!app.device!.rh) await app.device!.command("SCAN STOP", 500).catch(() => {});
      flashing = true;
      await disconnect();
      return port;
    }
    if (app.device) await disconnect(); // simulator or replay
    const port = await requestEspressifPort();
    flashing = true;
    return port;
  },
  device() {
    return app.device?.transport instanceof WebSerialTransport ? parseInfo(store.info) : null;
  },
  finished(ok) {
    flashing = false;
    if (!ok) return;
    // The board re-enumerates after its reset; autoConnect also runs on that event.
    userDisconnected = false;
    setTimeout(() => void autoConnect(), 1500);
  },
});
document.body.append(flashDialog.el);
store.subscribe(() => flashDialog.refreshDevice()); // INFO after a (re)connect
flashBtn.onclick = () => flashDialog.open();
void flashDialog.offeredVersions().then((vs) => {
  offeredFws = vs;
  updateFirmwareText();
});


function stopRecording() {
  if (!recorder.active) return;
  const blob = recorder.end();
  const a = el("a", { href: URL.createObjectURL(blob), download: `session-${new Date().toISOString().replace(/[:.]/g, "-")}.widscan58` });
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 10000);
  recordBtn.textContent = t("● Record");
  recordBtn.classList.remove("on");
  store.addLog("info", "recording saved");
}
recordBtn.onclick = () => {
  if (recorder.active) return stopRecording();
  const pre: string[] = [];
  if (store.profile) pre.push(`PROFILE ${JSON.stringify(store.profile)}`);
  if (store.status) pre.push(`STATUS ${JSON.stringify(store.status)}`);
  if (store.spursKhz.length) pre.push(`SPURS ${store.spursKhz.length} ${store.spursKhz.join(" ")}`);
  recorder.begin(pre);
  recordBtn.textContent = t("■ Stop recording");
  recordBtn.classList.add("on");
};

// Auto-reconnect: once this origin has been granted the board, reconnect
// without the chooser on page load and whenever the board re-enumerates
// (reset, reflash, replug).
let userDisconnected = false;
async function autoConnect() {
  if (app.device || userDisconnected || flashing) return;
  const port = await grantedEspressifPort();
  if (port && !app.device) {
    store.addLog("info", "reconnecting to previously granted port");
    await connect(new WebSerialTransport(port));
  }
}
// ?bridge=ws://host:port connects through tools/serial_bridge.py instead of
// Web Serial (headless tests, remote viewing); ?sim starts the simulator
// (?sim=s3 for the S3 band).
const params = new URLSearchParams(location.search);
const bridge = params.get("bridge");
if (bridge) void connect(new WebSocketTransport(bridge));
else if (params.has("sim")) void connect(new MockTransport(simTarget()));
else if (WebSerialTransport.supported()) {
  WebSerialTransport.onConnectEvent(() => setTimeout(() => void autoConnect(), 500));
  void autoConnect();
}

store.subscribe(() => updateBanner());
store.subscribe(syncRhDataNote);
applyLang();
setState("off", "Disconnected");
showPage();
syncRhDataNote();

// The firmware boots silent (lap timer mode). In monitor mode telemetry
// arrives at 4 Hz even when scanning is stopped, so a quiet link means the
// board restarted: switch it back.
let lastData = performance.now();
setInterval(() => {
  const d = app.device;
  if (!d || d.rh || app.readOnly || legacyFirmware || starting || flashing || app.settings.uiMode === "timer") return;
  if (performance.now() - lastData > 3000) {
    store.addLog("info", "no data for 3 s: switching the device to monitor mode again");
    lastData = performance.now();
    void startMode(d);
  }
}, 1000);

// ---- Render loop: each view redraws when data changed, at its own max rate ----
function frame(now: number) {
  const page = app.settings.uiMode;
  for (const v of views) {
    if (v.page !== "both" && v.page !== page) continue;
    if (v.drawn === store.version || now - v.last < v.minMs) continue;
    v.last = now;
    v.drawn = store.version;
    try {
      v.view.render();
    } catch (e) {
      console.error(e);
    }
  }
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
window.addEventListener("resize", () => store.touch());
