// Serves the built page (dist/) over http on the LAN, so a phone or laptop can
// open it from a plain http address. This is what lets the page read an
// http:// RotorHazard server: an https page (GitHub Pages) is blocked by the
// browser's mixed-content rules before the request leaves the tab.
// It can also bridge boards to connected pages over a WebSocket, for browsers
// without Web Serial, and switch the board between scanning and RotorHazard by
// starting/stopping the RH server (the firmware gives the radio back to
// whoever speaks first; the RH server's first UART byte always reclaims it).
//
// Endpoints: every tty whose name starts with --serial (/dev/widscan58 and the
// per-board udev aliases /dev/widscan58-<serial>), plus remote nodes that
// connect in over ws://host/node?id=<name> (an ESP32-S3 relaying a C5). A page
// lists them at /api/endpoints and connects to one at ws://host/serial?ep=<id>.
// Zero dependencies for the http+WS serving: node serve.mjs [--port 8080]
//   [--host 0.0.0.0] [--dir dist] [--serial /dev/widscan58] [--baud 115200]
//   [--rh-stop-cmd "sudo systemctl stop rotorhazard"]
//   [--rh-start-cmd "sudo systemctl start rotorhazard"]
//   [--rh-serial /dev/serial0]  (RotorHazard's own board, listed but not bridged)
// (the serial bridge additionally needs the serialport package installed).
import { createReadStream, existsSync, readdirSync, realpathSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { exec } from "node:child_process";
import { createHash } from "node:crypto";
import { networkInterfaces } from "node:os";
import { basename, dirname, extname, join, normalize, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const arg = (name, fallback) => {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
};

const port = Number(arg("port", "8080"));
const host = arg("host", "0.0.0.0");
const root = resolve(here, arg("dir", "dist"));
// A prefix, not one device: the udev rule also creates widscan58-<serial>
// aliases so several boards can be bridged at once.
const serialPrefix = arg("serial", null);
const baud = Number(arg("baud", "115200"));
const rhStopCmd = arg("rh-stop-cmd", null);
const rhStartCmd = arg("rh-start-cmd", null);
// Optional: prints the RH service state ("active" / "inactive"), so a restarted
// server reports the truth instead of guessing until the first switch.
const rhStateCmd = arg("rh-state-cmd", null);
// The board on the host's own UART pins (the one RotorHazard drives). It can
// be bridged like a USB board: the firmware carries monitor frames over the
// UART too, as long as the RotorHazard server releases the port first.
const rhSerial = arg("rh-serial", null);
// Who the radio currently belongs to, as far as we last made it so. The board
// itself cannot be asked (it answers in whatever protocol it is in); the RH
// server taking it back on its own keeps this honest in the RH direction, and
// the page switches only through this API.
let boardLink = "unknown"; // "rh" | "scan" | "unknown"
if (rhStateCmd) {
  runCmd(rhStateCmd).then(
    (out) => (boardLink = String(out ?? "").trim() === "active" ? "rh" : "scan"),
    (e) => console.log(`board link: cannot read the RH state (${e.message})`),
  );
}

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

if (!existsSync(join(root, "index.html"))) {
  console.error(`No ${join(root, "index.html")} — build the page first: npm run build`);
  process.exit(1);
}

/** Runs one of the configured shell commands, with a hard timeout. */
function runCmd(cmd) {
  return new Promise((resolve, reject) => {
    exec(cmd, { timeout: 15000 }, (e, stdout, stderr) => (e ? reject(new Error(stderr.trim() || e.message)) : resolve(stdout)));
  });
}

function sendJson(res, code, body) {
  res.writeHead(code, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-cache" });
  res.end(JSON.stringify(body));
}

const server = createServer((req, res) => {
  // Board link switch: the page starts/stops the RotorHazard server, which
  // frees the UART for 2 s so its PROTOCOL MONITOR can take the radio, and
  // hands it back (the RH server's first byte reclaims the radio itself).
  // What the page can connect to: local boards bridged by this server plus
  // relay nodes that connected in. Anonymous LAN callers get no more than this.
  if (req.url === "/api/endpoints") return sendJson(res, 200, { endpoints: listEndpoints() });
  if (req.url === "/api/mode") {
    if (req.method === "GET")
      return sendJson(res, 200, { state: boardLink, stop: !!rhStopCmd, start: !!rhStartCmd, bridged: pages.size > 0 });
    if (req.method === "POST") {
      let body = "";
      req.on("data", (c) => {
        body += c;
        if (body.length > 1024) req.destroy();
      });
      req.on("end", async () => {
        let mode;
        try {
          mode = JSON.parse(body).mode;
        } catch {
          return sendJson(res, 400, { error: "bad JSON" });
        }
        if (mode !== "scan" && mode !== "rh") return sendJson(res, 400, { error: "mode must be scan or rh" });
        const cmd = mode === "scan" ? rhStopCmd : rhStartCmd;
        if (!cmd) return sendJson(res, 409, { error: `no --rh-${mode === "scan" ? "stop" : "start"}-cmd configured` });
        try {
          await runCmd(cmd);
        } catch (e) {
          return sendJson(res, 500, { error: String(e.message).slice(0, 300) });
        }
        boardLink = mode;
        console.log(`board link: ${mode === "scan" ? "RH stopped" : "RH started"} (${cmd})`);
        sendJson(res, 200, { state: boardLink });
      });
      return;
    }
    res.writeHead(405).end();
    return;
  }
  if (req.method !== "GET" && req.method !== "HEAD") {
    res.writeHead(405).end();
    return;
  }
  let path;
  try {
    path = decodeURIComponent(new URL(req.url ?? "/", "http://localhost").pathname);
  } catch {
    res.writeHead(400).end();
    return;
  }
  if (path.endsWith("/")) path += "index.html";
  const file = normalize(join(root, path));
  if (!file.startsWith(root + sep) || !existsSync(file) || !statSync(file).isFile()) {
    res.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" }).end("Not found");
    return;
  }
  res.writeHead(200, {
    "Content-Type": TYPES.get(extname(file)) ?? "application/octet-stream",
    // Every reload should see the build that is on disk; the service worker
    // updates its own cache when the file list changes.
    "Cache-Control": "no-cache",
  });
  if (req.method === "HEAD") {
    res.end();
    return;
  }
  createReadStream(file).pipe(res);
});

server.on("error", (e) => {
  console.error(`Cannot listen on ${host}:${port}: ${e.message}`);
  process.exit(1);
});

// ---- Endpoints: local serial ports and remote relay nodes -----------------------
// One page per endpoint: the radio has a single owner, so a second page asking
// for the same endpoint is refused with "busy" instead of interleaving frames.
const pages = new Map(); // endpoint id -> page socket
const nodes = new Map(); // node id -> { socket, id, page }

/** The ttys this server may bridge: /dev/widscan58 plus widscan58-<serial>. */
function listSerials() {
  if (!serialPrefix) return [];
  const dir = dirname(serialPrefix);
  const base = basename(serialPrefix);
  let names;
  try {
    names = readdirSync(dir).filter((name) => name.startsWith(base)).sort();
  } catch {
    return [];
  }
  // udev gives every board a serial-suffixed alias and the last one the bare
  // name too; both are the same tty, so keep the specific alias only.
  const byTty = new Map();
  for (const name of names) {
    const path = join(dir, name);
    let tty = path;
    try {
      tty = realpathSync(path);
    } catch {
      /* a stale alias: keep the name as its own key */
    }
    const known = byTty.get(tty);
    if (!known || name.length > basename(known).length) byTty.set(tty, path);
  }
  return [...byTty.values()].sort();
}

function listEndpoints() {
  const out = [];
  for (const path of listSerials()) {
    const id = `serial:${path}`;
    out.push({ id, kind: "serial", label: basename(path), path, online: true, busy: pages.has(id) });
  }
  if (rhSerial) {
    let path = rhSerial;
    try {
      path = realpathSync(rhSerial); // /dev/serial0 -> ttyAMA0
    } catch {
      /* not there right now */
    }
    out.push({
      id: `uart:${rhSerial}`,
      kind: "uart",
      label: basename(path),
      path,
      online: existsSync(rhSerial),
      busy: pages.has(`uart:${rhSerial}`),
      scannable: !!rhStopCmd, // without a stop command RotorHazard keeps the tty
    });
  }
  for (const node of [...nodes.values()].sort((a, b) => a.id.localeCompare(b.id)))
    out.push({
      id: `node:${node.id}`,
      kind: "remote",
      label: node.id,
      online: true,
      busy: pages.has(`node:${node.id}`),
    });
  return out;
}

/** The endpoint a bare /serial should use: first board, else first remote node. */
function defaultEndpoint() {
  const all = listEndpoints().filter((e) => e.kind !== "uart");
  return (all.find((e) => e.kind === "serial") ?? all[0])?.id ?? null;
}

// ---- WebSocket bridge (ws://host/serial?ep=<id>, ws://host/node?id=<name>) ------
// A hand-rolled RFC6455 server keeps this file dependency-free: pages and nodes
// only ever exchange binary frames, so a small subset is enough.
const WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";

function wsFrame(opcode, payload) {
  const len = payload.length;
  let header;
  if (len < 126) header = Buffer.from([0x80 | opcode, len]);
  else if (len < 65536) {
    header = Buffer.alloc(4);
    header[0] = 0x80 | opcode;
    header[1] = 126;
    header.writeUInt16BE(len, 2);
  } else {
    header = Buffer.alloc(10);
    header[0] = 0x80 | opcode;
    header[1] = 127;
    header.writeBigUInt64BE(BigInt(len), 2);
  }
  return Buffer.concat([header, payload]);
}

function wsCloseFrame(reason) {
  const why = Buffer.from(reason.slice(0, 100), "utf8");
  return wsFrame(8, Buffer.concat([Buffer.from([0x10, 0x00]), why])); // 1008: policy
}

const sendBinary = (socket, chunk) => socket.write(wsFrame(2, chunk));

/** Reads RFC6455 frames from a client; ping/close are answered here. */
function readFrames(socket, head, onBinary, onClose) {
  let buf = head.length ? Buffer.from(head) : Buffer.alloc(0);
  let frag = null; // {chunks} while a binary message spans several frames
  let done = false;
  // A vanished client (killed process, pulled cable) ends the stream without a
  // close frame: the socket then sits in CLOSE_WAIT and only "end" fires, so
  // the endpoint would stay locked forever if this waited for "close".
  let beat = null;
  const finish = () => {
    if (done) return;
    done = true;
    clearInterval(beat);
    socket.destroy();
    onClose();
  };
  // A client that vanishes without FIN (laptop asleep, Wi-Fi gone) leaves the
  // connection established and the board locked. Browsers answer protocol
  // pings on their own, so silence is the signal that nobody is there.
  let lastSeen = Date.now();
  beat = setInterval(() => {
    if (done) return;
    if (Date.now() - lastSeen > 45000) return finish();
    if (socket.writable) socket.write(wsFrame(9, Buffer.alloc(0)));
  }, 15000);
  beat.unref?.();
  const feed = (opcode, fin, payload) => {
    if (opcode === 8) {
      socket.write(wsFrame(8, payload.subarray(0, 2)));
      socket.end();
      return;
    }
    if (opcode === 9) return void socket.write(wsFrame(10, payload));
    if (opcode === 2) {
      if (fin) return void onBinary(payload);
      frag = { chunks: [payload] };
      return;
    }
    if (opcode === 0 && frag) {
      frag.chunks.push(payload);
      if (fin) {
        const full = Buffer.concat(frag.chunks);
        frag = null;
        onBinary(full);
      }
    }
    // 1 text / 10 pong: unused
  };
  socket.on("data", (chunk) => {
    if (done) return;
    lastSeen = Date.now();
    buf = Buffer.concat([buf, chunk]);
    for (;;) {
      if (buf.length < 2) return;
      const opcode = buf[0] & 0x0f;
      const fin = (buf[0] & 0x80) !== 0;
      let len = buf[1] & 0x7f;
      let off = 2;
      if (len === 126) {
        if (buf.length < 4) return;
        len = buf.readUInt16BE(2);
        off = 4;
      } else if (len === 127) {
        if (buf.length < 10) return;
        const big = buf.readBigUInt64BE(2);
        if (big > 64n * 1024n * 1024n) return socket.destroy(); // absurd frame
        len = Number(big);
        off = 10;
      }
      if (buf[1] & 0x80) off += 4; // clients must mask; the mask is skipped, not verified
      if (buf.length < off + len) return;
      const payload = Buffer.from(buf.subarray(off, off + len));
      if (buf[1] & 0x80) {
        const mask = buf.subarray(off - 4, off);
        for (let i = 0; i < payload.length; i++) payload[i] ^= mask[i & 3];
      }
      buf = buf.subarray(off + len);
      feed(opcode, fin, payload);
      if (socket.destroyed) return;
    }
  });
  socket.on("close", finish);
  socket.on("end", finish);
  socket.on("error", finish);
}

server.on("upgrade", (req, socket, head) => {
  const url = new URL(req.url ?? "/", "http://localhost");
  const path = url.pathname;
  const deny = (code, msg) => {
    socket.write(`HTTP/1.1 ${code}\r\nConnection: close\r\nContent-Length: ${msg.length}\r\n\r\n${msg}`);
    socket.destroy();
  };
  if (path !== "/serial" && path !== "/node") return deny("404 Not Found", "no such socket");
  if (req.headers.upgrade?.toLowerCase() !== "websocket" || !req.headers["sec-websocket-key"])
    return deny("400 Bad Request", "not a websocket");
  // Accept the handshake even when the bridge cannot be served, then close
  // with the reason: a refused handshake reaches the page only as a generic
  // "cannot reach bridge", while a close reason is shown as-is.
  socket.write(
    "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n" +
      `Sec-WebSocket-Accept: ${createHash("sha1").update(req.headers["sec-websocket-key"] + WS_MAGIC).digest("base64")}\r\n\r\n`,
  );
  socket.setNoDelay(true);
  if (path === "/node") return attachNode(socket, head, url.searchParams.get("id"));
  const ep = url.searchParams.get("ep") ?? defaultEndpoint();
  if (!ep) return void socket.end(wsCloseFrame("no-serial"));
  if (pages.has(ep)) return void socket.end(wsCloseFrame("busy"));
  const remote = /^node:(.+)$/.exec(ep);
  if (remote) return bridgeRemote(socket, head, ep, remote[1]);
  const local = /^serial:(.+)$/.exec(ep);
  if (local) return void bridgeSerial(socket, head, ep, local[1]);
  const uart = /^uart:(.+)$/.exec(ep);
  if (uart) return void bridgeUart(socket, head, ep, uart[1]);
  void socket.end(wsCloseFrame(`unknown endpoint ${ep}`));
});

/** A relay node (ESP32-S3) connects in and waits for a page to attach. */
function attachNode(socket, head, rawId) {
  const id = (rawId ?? "").trim();
  if (!/^[A-Za-z0-9_.:-]{1,64}$/.test(id)) return void socket.end(wsCloseFrame("bad-id"));
  const old = nodes.get(id);
  if (old) {
    nodes.delete(id);
    old.socket.end(wsCloseFrame("replaced"));
    if (old.page) {
      old.page.end(wsCloseFrame("node reconnected"));
      pages.delete(`node:${id}`);
      old.page = null;
    }
  }
  const node = { id, socket, page: null };
  nodes.set(id, node);
  console.log(`node: ${id} connected`);
  readFrames(
    socket,
    head,
    (payload) => {
      if (node.page?.writable) sendBinary(node.page, payload);
    },
    () => {
      if (nodes.get(id) === node) nodes.delete(id);
      if (node.page) {
        node.page.end(wsCloseFrame("node offline"));
        pages.delete(`node:${id}`);
        node.page = null;
      }
      console.log(`node: ${id} gone`);
    },
  );
}

function bridgeRemote(socket, head, ep, id) {
  const node = nodes.get(id);
  if (!node) return void socket.end(wsCloseFrame("node offline"));
  node.page = socket;
  pages.set(ep, socket);
  console.log(`node: bridging ${id} to a page`);
  readFrames(socket, head, (payload) => {
    if (node.socket.writable) sendBinary(node.socket, payload);
  }, () => {
    if (node.page === socket) node.page = null;
    if (pages.get(ep) === socket) pages.delete(ep);
  });
}

async function bridgeSerial(socket, head, ep, path, afterClose) {
  let serial;
  try {
    const { SerialPort } = await import("serialport");
    serial = new SerialPort({ path, baudRate: baud });
    await new Promise((resolve, reject) => {
      serial.once("open", resolve);
      serial.once("error", reject);
    });
  } catch (e) {
    console.error(`serial: cannot open ${path}: ${e.message}`);
    // The RH server owning the same tty (single-board setups) is a common
    // cause; the page turns this into "RotorHazard is holding the port".
    const busy = /resource temporarily unavailable|cannot lock|access denied|permission denied/i.test(e.message);
    socket.end(wsCloseFrame(busy ? "busyport" : `cannot open ${path}: ${e.message}`));
    return;
  }
  console.log(`serial: bridging ${path} to a page`);
  pages.set(ep, socket);

  const onData = (chunk) => socket.write(wsFrame(2, chunk));
  const onErr = (e) => {
    console.error(`serial: ${path}: ${e.message}`);
    socket.end(wsCloseFrame(`serial error: ${e.message}`));
  };
  serial.on("data", onData);
  serial.on("error", onErr);

  readFrames(
    socket,
    head,
    (payload) => serial.write(payload),
    () => {
      if (pages.get(ep) !== socket) return;
      pages.delete(ep);
      serial.off("data", onData);
      serial.off("error", onErr);
      serial.close(() => console.log(`serial: ${path} released`));
      afterClose?.();
    },
  );
}

/** Bridging the UART board: the RotorHazard server owns the tty, so stop it
 * first and start it again once the page leaves. */
async function bridgeUart(socket, head, ep, path) {
  if (!rhStopCmd) return void bridgeSerial(socket, head, ep, path); // open fails with busyport while RH runs
  let stopped = false;
  if (boardLink !== "scan") {
    try {
      await runCmd(rhStopCmd);
      boardLink = "scan";
      stopped = true;
      console.log(`serial: RH stopped for ${path}`);
      // The firmware refuses PROTOCOL MONITOR until 2 s have passed without
      // RotorHazard traffic; wait that out before the page starts talking.
      await new Promise((r) => setTimeout(r, 2500));
    } catch (e) {
      socket.end(wsCloseFrame(`cannot stop RotorHazard: ${e.message}`));
      return;
    }
  }
  bridgeSerial(socket, head, ep, path, stopped ? resumeRh : undefined);
}

function resumeRh() {
  if (!rhStartCmd) return;
  runCmd(rhStartCmd)
    .then(() => {
      boardLink = "rh";
      console.log("serial: RH restarted");
    })
    .catch((e) => console.error(`serial: RH restart failed: ${e.message}`));
}

server.listen(port, host, () => {
  console.log(`Serving ${root}`);
  // http://localhost is a secure context (the board link needs one); a LAN
  // address over http is not, but it can still read an http RotorHazard.
  console.log(`  board and RotorHazard:  http://localhost:${port}/`);
  if (host === "0.0.0.0") {
    for (const addresses of Object.values(networkInterfaces())) {
      for (const a of addresses ?? []) {
        if (a.family === "IPv4" && !a.internal) console.log(`  other devices (no board link):  http://${a.address}:${port}/`);
      }
    }
  }
  console.log("Enter the RotorHazard address as http://<its-ip>:5000.");
  if (serialPrefix) {
    console.log(`Serial bridge: ws://<this-host>:${port}/serial?ep=serial:<path>  (${serialPrefix}* @ ${baud})`);
    const found = listSerials();
    console.log(found.length ? `  boards now: ${found.join(", ")}` : `  no ${serialPrefix}* device right now`);
  } else console.log("Serial bridge: off (start with --serial <port> to enable)");
  console.log(`Relay nodes: ws://<this-host>:${port}/node?id=<name>`);
  if (rhStopCmd || rhStartCmd) console.log("Board link switch: on (POST /api/mode {mode:scan|rh})");
  else console.log("Board link switch: off (start with --rh-stop-cmd/--rh-start-cmd to enable)");
});
