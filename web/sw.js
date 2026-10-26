// Widscan58 service worker: makes the page installable and usable offline (at
// a race field without internet). GPL-3.0-or-later.
//
// The build (vite.config.ts, pwaPlugin) prepends CACHE, named after a hash of
// the build's files, FILES, the page itself and firmware/index.json (the
// firmware versions the flash dialog offers), FLASH_CODE, the code only the
// flasher loads, and INDEX, the parsed index.json; it writes the result to
// dist/sw.js. A deploy changes CACHE, so the browser installs the new worker
// in the background; it takes over at once. Page loads go to the network first
// (the fetch handler), so a load after a deploy runs the new version; the
// cached page is the fallback offline. No prompts either way.
//
// The firmware is not installed with the page: those who flash download the
// version they picked then, and it is kept (RUNTIME) for flashing again
// offline, until the next deploy. With "Make firmware available offline" (the
// flash dialog, installed app only) a version is installed with the page too
// (OFFLINE), and every update brings the new one. The PREFS cache remembers
// which versions were asked for.
/* global CACHE, FILES, FLASH_CODE, INDEX */
const RUNTIME = CACHE + "-rt";
const OFFLINE = CACHE + "-fw";
const PREFS = "widscan58-prefs";
const PREF_OFFLINE = "./offline-firmware";

/** The files a firmware version needs, or null for an id this build lacks. */
function versionFiles(id) {
  const v = INDEX?.versions.find((x) => x.id === id);
  return v ? v.manifest.builds.flatMap((b) => b.parts.map((p) => `firmware/${v.dir}/${p.path}`)) : null;
}

/** The versions the user wants offline, oldest first. */
async function wantedIds() {
  if (!INDEX) return [];
  const r = await caches.match(PREF_OFFLINE, { cacheName: PREFS });
  if (!r) return [];
  const text = await r.text();
  if (text === "1") return [INDEX.default]; // the option before there were versions
  try {
    const ids = JSON.parse(text)?.ids;
    return Array.isArray(ids) ? ids.filter((id) => versionFiles(id)) : [];
  } catch {
    return [];
  }
}

async function setWantedIds(ids) {
  const prefs = await caches.open(PREFS);
  if (ids.length) await prefs.put(PREF_OFFLINE, new Response(JSON.stringify({ ids })));
  else await prefs.delete(PREF_OFFLINE);
}

/** The flasher's code and the given versions into OFFLINE, or none of them.
 *  A failed download leaves what arrived: status() then reports the version
 *  as not saved, and the next app update tries again. */
async function saveOffline(reload, ids) {
  const files = new Set(FLASH_CODE);
  for (const id of ids) for (const f of versionFiles(id) ?? []) files.add(f);
  const c = await caches.open(OFFLINE);
  await c.addAll([...files].map((f) => new Request(f, { cache: reload ? "reload" : "no-cache" })));
}

/** Forgets a version's files; the flasher's code stays while another is kept. */
async function dropOffline(id) {
  const c = await caches.open(OFFLINE);
  for (const f of versionFiles(id) ?? []) await c.delete(f);
}

self.addEventListener("install", (e) => {
  e.waitUntil(
    (async () => {
      const c = await caches.open(CACHE);
      await c.addAll(FILES.map((f) => new Request(f, { cache: "reload" })));
      // A failed firmware download does not hold back the update: flashing
      // then fetches it, as without the option.
      const ids = await wantedIds();
      if (ids.length) await saveOffline(true, ids).catch(() => {});
      await self.skipWaiting();
    })(),
  );
});

self.addEventListener("activate", (e) => {
  const keep = [CACHE, RUNTIME, OFFLINE, PREFS];
  e.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith("widscan58-") && !keep.includes(k)).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

/** A page load from the network, revalidated past the HTTP cache (GitHub
 *  Pages: max-age=600); null when it fails or takes over NAV_TIMEOUT_MS. */
const NAV_TIMEOUT_MS = 3000;
async function navigateFromNetwork(req) {
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), NAV_TIMEOUT_MS);
  try {
    const r = await fetch(req.url, { cache: "no-cache", credentials: "same-origin", signal: abort.signal });
    return r.ok ? r : null;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

// Page loads (any query: ?sim, ?bridge=) from the network first, so a deploy
// shows at once; its assets have new hashed names, so they come from the
// network too. Offline (or slow), the cached index.html. The saved firmware
// and the rest of the page from their caches. Everything else from the
// network, kept in RUNTIME for when it is offline: a version's parts are
// fetched in one go when flashing, so the cached ones match.
self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET" || new URL(req.url).origin !== self.location.origin) return;
  const opts = { ignoreSearch: true, ignoreVary: true };
  e.respondWith(
    (async () => {
      if (req.mode === "navigate") {
        const page = (await navigateFromNetwork(req)) ?? (await caches.match("index.html", { cacheName: CACHE }));
        if (page) return page;
      }
      for (const cacheName of [CACHE, OFFLINE]) {
        const hit = await caches.match(req, { ...opts, cacheName });
        if (hit) return hit;
      }
      try {
        const r = await fetch(req);
        if (r.ok) await (await caches.open(RUNTIME)).put(req, r.clone());
        return r;
      } catch (err) {
        return (await caches.match(req, { ...opts, cacheName: RUNTIME })) ?? Promise.reject(err);
      }
    })(),
  );
});

/** What the flash dialog shows for one version: whether it is wanted offline
 *  (and saved with this version of the app) and whether its files are on this
 *  device at all (saved, or kept from flashing). */
async function status(id) {
  const wanted = (await wantedIds()).includes(id);
  const files = versionFiles(id);
  const has = async (cacheName, f) => !!(await caches.match(f, { ignoreVary: true, cacheName }));
  const all = files ? [...files, ...FLASH_CODE] : null;
  const saved = !!all && (await Promise.all(all.map((f) => has(OFFLINE, f)))).every(Boolean);
  const firmware =
    !!files && (await Promise.all(files.map(async (f) => (await has(OFFLINE, f)) || (await has(RUNTIME, f))))).every(Boolean);
  return { wanted, saved, firmware };
}

// Requests from the page (util/pwa.ts), answered on the message's port:
// {cmd: "status", id} and {cmd: "offline", on, id}, both with status(id) or
// {error}.
self.addEventListener("message", (e) => {
  const reply = e.ports[0];
  const { cmd, on, id } = e.data ?? {};
  if (!reply || (cmd !== "status" && cmd !== "offline")) return;
  const vid = id ?? INDEX?.default;
  e.waitUntil(
    (async () => {
      if (cmd === "offline" && versionFiles(vid)) {
        const ids = await wantedIds();
        if (on) {
          await setWantedIds(ids.includes(vid) ? ids : [...ids, vid]);
          await saveOffline(false, [vid]);
        } else {
          const next = ids.filter((x) => x !== vid);
          await setWantedIds(next);
          if (next.length) await dropOffline(vid);
          else await caches.delete(OFFLINE);
        }
      }
      reply.postMessage(await status(vid));
    })().catch((err) => reply.postMessage({ error: String(err?.message ?? err) })),
  );
});
