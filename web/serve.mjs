// Serves the built page (dist/) over http on the LAN, so a phone or laptop can
// open it from a plain http address. This is what lets the page read an
// http:// RotorHazard server: an https page (GitHub Pages) is blocked by the
// browser's mixed-content rules before the request leaves the tab.
// Zero dependencies: node serve.mjs [--port 8080] [--host 0.0.0.0] [--dir dist]
import { createReadStream, existsSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { networkInterfaces } from "node:os";
import { dirname, extname, join, normalize, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const arg = (name, fallback) => {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
};

const port = Number(arg("port", "8080"));
const host = arg("host", "0.0.0.0");
const root = resolve(here, arg("dir", "dist"));

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

const server = createServer((req, res) => {
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
});
