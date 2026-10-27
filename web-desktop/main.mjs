// Desktop shell for the widscan58 web page. Loads web/dist over a private
// app:// protocol (secure context, so Web Serial / Web USB work) and provides
// the serial / USB device chooser UI Electron does not ship by default.
// webSecurity is off because the page must talk to a plain-http RotorHazard
// server on the LAN — blocked as mixed content otherwise.
import { app, BrowserWindow, Menu, dialog, protocol, session, shell } from "electron";
import { readFileSync, existsSync, statSync } from "node:fs";
import { dirname, extname, join, normalize, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const DIST = resolve(here, "dist");
const HOST = "widscan58";

const TYPES = new Map(
  Object.entries({
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".bin": "application/octet-stream",
    ".wasm": "application/wasm",
    ".txt": "text/plain; charset=utf-8",
  }),
);

protocol.registerSchemesAsPrivileged([
  {
    scheme: "app",
    privileges: { standard: true, secure: true, supportFetchAPI: true, stream: true, codeCache: true },
  },
]);

let win = null;

function createWindow() {
  win = new BrowserWindow({
    width: 1280,
    height: 880,
    minWidth: 720,
    minHeight: 540,
    title: "widscan58",
    autoHideMenuBar: true,
    icon: join(DIST, "icon-512.png"),
    webPreferences: {
      webSecurity: false,
    },
  });
  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });
  void win.loadURL(`app://${HOST}/index.html`);
  win.on("closed", () => {
    win = null;
  });
}

async function pickDevice(kind, list, label) {
  if (!win || !list.length) return "";
  const buttons = list.map((item, i) => {
    if (kind === "serial") return [item.displayName, item.portName].filter(Boolean).join(" — ") || `串口 ${i + 1}`;
    return [item.productName, item.manufacturerName, `VID ${item.vendorId} PID ${item.productId}`]
      .filter(Boolean)
      .join(" — ");
  });
  const { response } = await dialog.showMessageBox(win, {
    type: "question",
    title: `选择${label}`,
    message: `选择要连接的${label}（取消则不连接）`,
    buttons: [...buttons, "取消"],
    cancelId: buttons.length,
    defaultId: 0,
  });
  return response < buttons.length ? list[response][kind === "serial" ? "portId" : "deviceId"] : "";
}

app.whenReady().then(() => {
  protocol.handle("app", (req) => {
    let path;
    try {
      path = decodeURIComponent(new URL(req.url).pathname);
    } catch {
      return new Response("Bad request", { status: 400 });
    }
    if (path.endsWith("/")) path += "index.html";
    const file = normalize(join(DIST, path));
    if (!file.startsWith(DIST + sep) || !existsSync(file) || !statSync(file).isFile()) {
      return new Response("Not found", { status: 404 });
    }
    const type = TYPES.get(extname(file).toLowerCase()) ?? "application/octet-stream";
    return new Response(readFileSync(file), { headers: { "Content-Type": type } });
  });

  const ses = session.defaultSession;
  // WIDSCAN58_AUTO_SERIAL=<substring> auto-picks the first matching port
  // instead of showing the chooser (used for automated end-to-end tests).
  const autoSerial = process.env.WIDSCAN58_AUTO_SERIAL;
  ses.on("select-serial-port", (event, portList, _contents, callback) => {
    console.log("[serial] chooser event, ports:", portList.map((p) => `${p.portName} (${p.displayName ?? "?"}) vid=0x${p.usbVendorId?.toString(16)}`));
    event.preventDefault();
    if (autoSerial) {
      const hit = portList.find((p) => `${p.portName} ${p.displayName ?? ""}`.includes(autoSerial));
      if (hit) return callback(hit.portId);
      console.warn("[serial] no port matches WIDSCAN58_AUTO_SERIAL=", autoSerial);
    }
    void pickDevice("serial", portList, "串口").then((portId) => callback(portId));
  });
  ses.on("select-usb-device", (event, details, callback) => {
    event.preventDefault();
    void pickDevice("usb", details.deviceList, "USB 设备").then((deviceId) => callback(deviceId));
  });

  if (process.platform !== "darwin") Menu.setApplicationMenu(null);

  createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("child-process-gone", (_event, details) => {
  console.error("[process] gone:", details.type, details.reason, details.exitCode);
  if (details.type === "Renderer") {
    console.error("[process] renderer died — reloading window instead of quitting");
    if (win && !win.isDestroyed()) {
      win.destroy();
      win = null;
    }
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  }
});

app.on("window-all-closed", () => {
  app.quit();
});
