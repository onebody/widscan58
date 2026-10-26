// The installed app and its service worker (web/sw.js). GPL-3.0-or-later.

/** Running as the installed app, rather than in a browser tab. */
export function isInstalledApp(): boolean {
  const modes = ["standalone", "minimal-ui", "fullscreen", "window-controls-overlay"];
  return (
    modes.some((m) => matchMedia(`(display-mode: ${m})`).matches) ||
    (navigator as Navigator & { standalone?: boolean }).standalone === true
  );
}

/** Registers sw.js (built page only: the dev server has none). Page loads
 *  come from the network first and the cache offline; the worker's own
 *  updates install in the background, silently. */
export function registerServiceWorker() {
  if (!import.meta.env.PROD || !("serviceWorker" in navigator)) return;
  navigator.serviceWorker.register("./sw.js").catch((e) => console.warn("service worker:", e));
}

export interface OfflineStatus {
  /** "Make firmware available offline" is on for this firmware version. */
  wanted: boolean;
  /** This version's firmware and the flasher's code are saved with this version of the app. */
  saved: boolean;
  /** This version's firmware files are on this device (saved, or kept from flashing). */
  firmware: boolean;
}

/** Asks the service worker; null when there is none (dev server, first visit). */
async function ask(msg: object): Promise<OfflineStatus | null> {
  const sw = navigator.serviceWorker?.controller;
  if (!sw) return null;
  const ch = new MessageChannel();
  const answer = new Promise<OfflineStatus & { error?: string }>((resolve) => (ch.port1.onmessage = (e) => resolve(e.data)));
  sw.postMessage(msg, [ch.port2]);
  const r = await answer;
  if (r.error) {
    // Most likely a deploy since this worker installed: fetch the new one.
    void navigator.serviceWorker.getRegistration().then((reg) => reg?.update());
    throw new Error(r.error);
  }
  return r;
}

export const offlineStatus = (id: string) => ask({ cmd: "status", id });

/** Turns "Make firmware available offline" on for a version (downloads it now)
 *  or off (deletes it). */
export const setOfflineFirmware = (on: boolean, id: string) => ask({ cmd: "offline", on, id });
