// RotorHazard race data over the server's JSON API: the current heat's pilots,
// matched to channels by frequency, so the views can show names on the
// channels. Read-only polling; the server keeps ownership of its timing.
// GPL-3.0-or-later.
import { t } from "../i18n";
import type { Store } from "./store";

const POLL_MS = 2000;
const TIMEOUT_MS = 4000;
const KEY = "widscan58.rhserver";

export interface RhPilot {
  mhz: number;
  label: string; // callsign, or the name when there is no callsign
  full: string; // callsign and name, for a tooltip
}

export interface RhSnapshot {
  heatId: number;
  heatName: string;
  practice: boolean;
  byMhz: Map<number, RhPilot>;
}

export type RhServerStatus = "off" | "waiting" | "loading" | "ok" | "error";

interface RhPilotJson {
  id?: number;
  pilot_id?: number;
  callsign?: string | null;
  name?: string | null;
}

interface RhStatusJson {
  state?: { current_heat?: number | null; currentProfile?: number | null };
}

interface RhHeatJson {
  heat?: {
    setup?: { heat_id?: number; displayname?: string | null; nodes_pilots?: Record<string, number | null> } | null;
  } | null;
}

interface RhProfileJson {
  profile?: { frequencies?: unknown[] } | null;
}

/** The server address as a base URL (http(s) only), without a trailing slash. */
export function normalizeRhUrl(input: string): string {
  const s = input.trim();
  if (!s) return "";
  const withScheme = /^[a-z][a-z0-9+.-]*:\/\//i.test(s) ? s : `http://${s}`;
  try {
    const u = new URL(withScheme);
    if (u.protocol !== "http:" && u.protocol !== "https:") return "";
    u.hash = "";
    u.search = "";
    return u.href.replace(/\/+$/, "");
  } catch {
    return "";
  }
}

/** The frequency (MHz) or 0 for a node the profile has switched off. */
export function rhFrequency(v: unknown): number {
  const n = typeof v === "number" ? v : typeof v === "string" ? Number(v) : NaN;
  return Number.isFinite(n) && n > 0 ? Math.round(n) : 0;
}

/**
 * The current heat's pilots by channel frequency. The heat's node->pilot table
 * gives each pilot's seat; the profile gives that seat's frequency; this page's
 * channel plan is matched by MHz, so a different node order still lines up.
 */
export function buildSnapshot(
  status: RhStatusJson | null,
  heat: RhHeatJson | null,
  pilots: RhPilotJson[] | null,
  profile: RhProfileJson | null,
): RhSnapshot {
  const heatId = Number(status?.state?.current_heat) || 0;
  const setup = heat?.heat?.setup;
  // The current heat can change between the status and the heat reads.
  const forThisHeat = setup?.heat_id === heatId;
  const snapshot: RhSnapshot = {
    heatId,
    heatName: forThisHeat && setup ? setup.displayname?.trim() || "" : "",
    practice: heatId === 0,
    byMhz: new Map(),
  };
  if (!heatId || !forThisHeat || !setup || !pilots) return snapshot;
  const freqs = profile?.profile?.frequencies ?? [];
  const byId = new Map<number, RhPilotJson>();
  for (const p of pilots) {
    const id = Number(p.pilot_id ?? p.id);
    if (id > 0) byId.set(id, p);
  }
  for (const [node, pilotId] of Object.entries(setup.nodes_pilots ?? {})) {
    const id = Number(pilotId);
    if (!(id > 0)) continue;
    const mhz = rhFrequency(freqs[Number(node)]);
    if (!mhz) continue;
    const p = byId.get(id);
    const callsign = p?.callsign?.trim() ?? "";
    const name = p?.name?.trim() ?? "";
    const label = callsign || name || `#${id}`;
    const full = callsign && name && callsign !== name ? `${callsign} · ${name}` : label;
    const old = snapshot.byMhz.get(mhz);
    snapshot.byMhz.set(mhz, old ? { mhz, label: `${old.label} / ${label}`, full: `${old.full} / ${full}` } : { mhz, label, full });
  }
  return snapshot;
}

async function getJson<T>(url: string, signal: AbortSignal): Promise<T> {
  const r = await fetch(url, { signal, cache: "no-store", headers: { Accept: "application/json" } });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return (await r.json()) as T;
}

export class RhServer {
  url = ""; // as entered
  on = false;
  status: RhServerStatus = "off";
  error: { key: string; params?: Record<string, string | number> } | null = null;
  snapshot: RhSnapshot | null = null;
  lastOk = 0; // performance.now() of the last full read
  private base = "";
  private timer: number | null = null;
  private controller: AbortController | null = null;
  private gen = 0;
  private lastKey = "";

  constructor(private store: Store) {
    try {
      const saved = JSON.parse(localStorage.getItem(KEY) ?? "null");
      if (typeof saved?.url === "string") this.url = saved.url;
      this.on = saved?.on === true;
    } catch {
      /* storage unavailable or corrupt */
    }
    this.base = normalizeRhUrl(this.url);
    if (this.on) this.restart();
    else this.status = "off";
  }

  configure(patch: { url?: string; on?: boolean }) {
    if (patch.url !== undefined) this.url = patch.url;
    if (patch.on !== undefined) this.on = patch.on;
    try {
      localStorage.setItem(KEY, JSON.stringify({ url: this.url, on: this.on }));
    } catch {
      /* storage unavailable */
    }
    this.restart();
  }

  /** The error in the current language (it can outlive a language switch). */
  errorText(): string | null {
    return this.error ? t(this.error.key, this.error.params) : null;
  }

  /** The pilot on a channel frequency (MHz), or null. */
  pilotAt(mhz: number): RhPilot | null {
    return this.snapshot && !this.snapshot.practice ? this.snapshot.byMhz.get(Math.round(mhz)) ?? null : null;
  }

  /** Drop the race data (the setting is kept). */
  stop() {
    this.gen++;
    this.controller?.abort();
    this.controller = null;
    if (this.timer !== null) window.clearTimeout(this.timer);
    this.timer = null;
    this.snapshot = null;
    this.status = "off";
    this.lastKey = "";
    this.store.touch();
  }

  /** Read once now (the dialog's Check now). */
  checkNow() {
    this.restart(0);
  }

  private restart(delay = 0) {
    this.gen++;
    this.controller?.abort();
    this.controller = null;
    if (this.timer !== null) window.clearTimeout(this.timer);
    this.timer = null;
    this.base = normalizeRhUrl(this.url);
    this.snapshot = null;
    this.error = null;
    if (!this.on) {
      this.status = "off";
      this.touch();
      return;
    }
    if (!this.base) {
      this.status = "waiting";
      this.touch();
      return;
    }
    if (location.protocol === "https:" && this.base.startsWith("http:")) {
      this.status = "error";
      this.error = {
        key: "The page is https, so the browser blocks an http:// RotorHazard address. Open this page locally or serve RotorHazard over https.",
      };
      this.touch();
      return; // no use trying: the browser blocks every request
    }
    this.status = "loading";
    this.touch();
    this.schedule(delay);
  }

  private schedule(delay = POLL_MS) {
    if (this.timer !== null) window.clearTimeout(this.timer);
    this.timer = window.setTimeout(() => void this.poll(), delay);
  }

  private async poll() {
    this.timer = null;
    if (!this.on || !this.base) return;
    const gen = this.gen;
    const c = (this.controller = new AbortController());
    const timer = window.setTimeout(() => c.abort(), TIMEOUT_MS);
    try {
      const status = await getJson<RhStatusJson>(`${this.base}/api/status`, c.signal);
      const heatId = Number(status?.state?.current_heat) || 0;
      const profileId = Number(status?.state?.currentProfile) || 0;
      const [heat, pilots, profile] = await Promise.all([
        heatId ? getJson<RhHeatJson>(`${this.base}/api/heat/${heatId}`, c.signal) : Promise.resolve(null),
        getJson<{ pilots?: RhPilotJson[] }>(`${this.base}/api/pilot/all`, c.signal),
        profileId ? getJson<RhProfileJson>(`${this.base}/api/profile/${profileId}`, c.signal) : Promise.resolve(null),
      ]);
      if (gen !== this.gen) return;
      this.snapshot = buildSnapshot(status, heat, pilots?.pilots ?? null, profile);
      this.error = null;
      this.status = "ok";
      this.lastOk = performance.now();
    } catch (e) {
      if (gen !== this.gen) return;
      this.status = "error";
      this.error = {
        key: "Cannot reach {url}: {msg}",
        params: { url: this.base, msg: (e as Error).name === "AbortError" ? "timeout" : (e as Error).message },
      };
    } finally {
      window.clearTimeout(timer);
      if (gen === this.gen) {
        this.touch();
        this.schedule();
      }
    }
  }

  /** Tell the views when something visible changed (not on every poll). */
  private touch() {
    const key = `${this.status}|${this.error?.key ?? ""}|${JSON.stringify(this.error?.params ?? null)}|${
      this.snapshot?.heatId ?? ""
    }|${this.snapshot?.heatName ?? ""}|${
      this.snapshot?.practice ? "p" : [...(this.snapshot?.byMhz ?? [])].map(([m, p]) => `${m}:${p.label}`).join(",")
    }`;
    if (key === this.lastKey) return;
    this.lastKey = key;
    this.store.touch();
  }
}
