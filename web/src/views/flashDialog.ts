// "Flash device" dialog: a short walkthrough and the flash progress. GPL-3.0-or-later.
import { el } from "../util/canvas";
import { t } from "../i18n";
import { OfflineError, flashFirmware, loadVersions, type FirmwareVersion, type VersionIndex } from "../serial/flasher";
import { isInstalledApp, offlineStatus, setOfflineFirmware, type OfflineStatus } from "../util/pwa";
import type { Port } from "../serial/device";
import type { DeviceInfo } from "../model/store";

export interface FlashHost {
  /** The port to flash: the connected board (closed first) or one the user picks. */
  takePort(): Promise<Port>;
  /** Called when flashing ends; ok means the board is booting the new firmware. */
  finished(ok: boolean): void;
  /** The connected board's INFO, or null when no board is connected over Web Serial. */
  device(): DeviceInfo | null;
}

export class FlashDialog {
  readonly el: HTMLDialogElement;
  private boardVersion = el("td", {}, "…");
  private boardDate = el("td", {}, "");
  private newVersion = el("td", {}, "…");
  private newDate = el("td", {}, "");
  private bar = el("progress", { max: "1", value: "0" }) as HTMLProgressElement;
  private status = el("div", { class: "flash-status" });
  private logPre = el("pre", { class: "flash-log" });
  private flashBtn = el("button", { class: "primary", autofocus: "" }, "Flash");
  private closeBtn = el("button", {}, "Close");
  // The firmware version to install (firmware/versions.json, via index.json).
  private versionSelect = el("select", {}) as HTMLSelectElement;
  private versionNote = el("small", {});
  private versionRow = el(
    "label",
    { class: "flash-firmware", hidden: "" },
    el("span", {}, t("Firmware")),
    this.versionSelect,
    this.versionNote,
  );
  // "Make firmware available offline": installed app only (util/pwa.ts).
  private offlineBox = el("input", { type: "checkbox" }) as HTMLInputElement;
  private offlineHint = el("small", {});
  private offlineRow = el(
    "label",
    { class: "option", hidden: "" },
    this.offlineBox,
    el("span", {}, "Make firmware available offline", this.offlineHint),
  );
  private versions: Promise<VersionIndex> | null = null;
  private index: VersionIndex | null = null;
  /** The version the user picked; kept across opens. */
  private selectedId = "";
  private current: FirmwareVersion | null = null;
  private busy = false;
  /** Offline, and the selected firmware is not on this device. */
  private noFirmware = false;
  /** Ignore a slow offline status for a version that is no longer selected. */
  private offlineCheck = 0;

  constructor(private host: FlashHost) {
    this.el = el(
      "dialog",
      { class: "flash-dialog" },
      el("h2", {}, "Flash firmware"),
      this.versionRow,
      el(
        "table",
        { class: "flash-versions" },
        el("thead", {}, el("tr", {}, el("th", {}), el("th", {}, "Version"), el("th", {}, "Committed"))),
        el(
          "tbody",
          {},
          el("tr", {}, el("th", {}, "On the board"), this.boardVersion, this.boardDate),
          el("tr", {}, el("th", {}, "To install"), this.newVersion, this.newDate),
        ),
      ),
      el(
        "ol",
        {},
        el("li", {}, "Use Chrome or Edge on a computer, or Chrome on Android with a USB OTG adapter."),
        el("li", {}, "Plug the board in with a USB data cable (some cables only carry power)."),
        el(
          "li",
          {},
          el("strong", {}, "New board, or one that will not connect: "),
          "hold BOOT, press and release RESET, then release BOOT. The board is now waiting to be flashed. " +
            "A board that already runs this firmware is switched over automatically.",
        ),
        el("li", {}, "Click Flash and pick the board (“USB JTAG/serial debug unit”) in the browser’s list."),
      ),
      this.offlineRow,
      el("div", { class: "flash-progress" }, this.bar, this.status),
      el("details", {}, el("summary", {}, "Details"), this.logPre),
      el("div", { class: "flash-buttons" }, this.closeBtn, this.flashBtn),
    );
    this.flashBtn.onclick = () => void this.flash();
    this.closeBtn.onclick = () => this.el.close();
    this.versionSelect.onchange = () => this.selectVersion();
    this.offlineBox.onchange = () => void this.toggleOffline();
    const recheck = () => {
      if (this.el.open && !this.busy) void this.refreshOffline();
    };
    window.addEventListener("online", recheck);
    window.addEventListener("offline", recheck);
    // Esc must not hide a flash in progress.
    this.el.addEventListener("cancel", (e) => {
      if (this.busy) e.preventDefault();
    });
  }

  open() {
    // A fresh version index on every open (and again at Flash): a page left
    // open across a deploy would otherwise offer the old build, whose files
    // and hashes are gone.
    if (!this.busy || !this.versions) this.versions = loadVersions();
    this.versions.then(
      (idx) => {
        this.showIndex(idx);
        this.flashBtn.disabled = this.busy || this.noFirmware;
        void this.refreshOffline();
      },
      (e: Error) => {
        this.index = this.current = null;
        this.noFirmware = false;
        this.versionRow.hidden = true;
        this.offlineRow.hidden = true;
        setText(this.newVersion, t("(unavailable)"));
        setText(this.newDate, "");
        this.setStatus(e instanceof OfflineError ? this.offlineText() : t("Cannot flash: {msg}.", { msg: e.message }), "err");
        this.flashBtn.disabled = true;
      },
    );
    if (!this.busy) {
      this.bar.value = 0;
      this.setStatus("");
      this.logPre.textContent = "";
    }
    this.el.showModal();
    this.refreshDevice(); // after showModal: it skips a closed dialog
  }

  private offlineText(): string {
    return this.offlineRow.hidden
      ? t("No internet connection, and the firmware is not on this device. Connect to the internet to flash.")
      : t(
          "No internet connection, and the firmware is not on this device. Connect to the internet to flash. To flash without internet, turn on Make firmware available offline while connected.",
        );
  }

  /** The offline option for the selected version, and whether flashing can
   *  work without a network. */
  private async refreshOffline() {
    const v = this.current;
    if (!v || !this.el.open) {
      this.offlineRow.hidden = true;
      return;
    }
    const seq = ++this.offlineCheck;
    const st = await offlineStatus(v.id).catch(() => null);
    if (seq !== this.offlineCheck) return; // the selection changed meanwhile
    this.offlineRow.hidden = !st || !isInstalledApp();
    if (st) this.showOffline(st, v);
    const was = this.noFirmware;
    this.noFirmware = !navigator.onLine && !!st && !st.firmware;
    if (this.noFirmware) {
      this.setStatus(this.offlineText(), "err");
      if (!this.busy) this.flashBtn.disabled = true;
    } else if (was) {
      this.setStatus("");
      if (!this.busy) this.flashBtn.disabled = false;
    }
  }

  private showOffline(st: OfflineStatus, v: FirmwareVersion) {
    if (v !== this.current) return; // an answer for a version the user left
    this.offlineBox.checked = st.wanted;
    setText(
      this.offlineHint,
      !st.wanted
        ? t("Downloads the firmware (about 1 MB) now and with every app update, so you can flash without internet.")
        : st.saved
          ? t("Saved on this device ({version}); every app update brings the new firmware.", {
              version: v.manifest.version,
            })
          : t("Not downloaded yet; it is tried again with the next app update."),
    );
  }

  private async toggleOffline() {
    const v = this.current;
    if (!v) return;
    const on = this.offlineBox.checked;
    this.offlineBox.disabled = true;
    if (on) setText(this.offlineHint, t("Downloading the firmware…"));
    try {
      const st = await setOfflineFirmware(on, v.id);
      if (st) this.showOffline(st, v);
    } catch (e) {
      setText(
        this.offlineHint,
        t("Download failed ({msg}); it is tried again with the next app update.", { msg: (e as Error).message }),
      );
    } finally {
      this.offlineBox.disabled = false;
      void this.refreshOffline();
    }
  }

  /** Shows the connected board's version; kept while flashing, when the board
   *  is disconnected, until it reconnects with the new firmware. */
  refreshDevice() {
    if (!this.el.open || this.busy) return;
    const d = this.host.device();
    setText(this.boardVersion, d ? d.version : t("not connected"));
    setText(this.boardDate, d ? (d.date ?? t("unknown")) : "");
  }

  /** Fills the version selector and shows the version that will be installed:
   *  the one the user picked before, if the page still offers it. */
  private showIndex(idx: VersionIndex) {
    this.index = idx;
    const v =
      idx.versions.find((x) => x.id === this.selectedId) ??
      idx.versions.find((x) => x.id === idx.default) ??
      idx.versions[0];
    this.versionSelect.replaceChildren(
      ...idx.versions.map((x) => el("option", { value: x.id }, `${x.label} — ${x.manifest.version}`)),
    );
    this.versionRow.hidden = idx.versions.length < 2;
    this.showVersion(v);
  }

  private showVersion(v: FirmwareVersion) {
    this.selectedId = v.id;
    this.current = v;
    this.versionSelect.value = v.id;
    setText(this.newVersion, v.manifest.version);
    setText(this.newDate, v.manifest.date ?? t("unknown"));
    setText(this.versionNote, v.note ?? "");
  }

  private selectVersion() {
    const v = this.index?.versions.find((x) => x.id === this.versionSelect.value);
    if (!v || this.busy) return;
    this.showVersion(v);
    this.noFirmware = false;
    this.flashBtn.disabled = false;
    this.setStatus("");
    void this.refreshOffline();
  }

  /** The firmware versions this page offers, once known. */
  async offeredVersions(): Promise<string[]> {
    this.versions ??= loadVersions();
    return this.versions.then(
      (idx) => idx.versions.map((v) => v.manifest.version),
      () => [],
    );
  }

  private setStatus(text: string, kind: "" | "ok" | "err" = "") {
    this.status.textContent = text;
    this.status.className = `flash-status ${kind}`;
  }

  private log(line: string) {
    this.logPre.textContent += line + "\n";
    this.logPre.scrollTop = this.logPre.scrollHeight;
  }

  private async flash() {
    // Fetched now but awaited after takePort: the browser's port chooser
    // needs this click's user activation, which a slow fetch could outlast.
    const fresh = (this.versions = loadVersions());
    fresh.catch(() => {});
    let port: Port;
    try {
      port = await this.host.takePort();
    } catch (e) {
      // Dismissing the browser's chooser lands here too.
      this.setStatus(t("No board selected ({msg}).", { msg: (e as Error).message }), "err");
      return;
    }
    this.busy = true;
    this.flashBtn.disabled = this.closeBtn.disabled = this.versionSelect.disabled = true;
    this.bar.value = 0;
    this.logPre.textContent = "";
    let ok = false;
    try {
      this.showIndex(await fresh);
      const version = this.current;
      if (!version) throw new Error(t("no firmware is published with this page"));
      await flashFirmware(port, version, {
        stage: (text) => {
          this.setStatus(text);
          this.log(`-- ${text}`);
        },
        progress: (f) => (this.bar.value = f),
        log: (l) => this.log(l),
      });
      ok = true;
      this.setStatus(
        t("Done: {version} is installed and the board is restarting. The scanner reconnects on its own.", {
          version: version.manifest.version,
        }),
        "ok",
      );
    } catch (e) {
      const msg = (e as Error).message;
      this.log(`!! ${msg}`);
      // esptool-js loads its chip code when it connects, which fails offline too.
      if (e instanceof OfflineError || (e instanceof TypeError && /fetch|import/i.test(msg))) {
        this.setStatus(
          t(
            "Flashing failed: the firmware could not be downloaded (no internet connection?). Connect to the internet and click Flash again; press RESET on the board to start the firmware it has.",
          ),
          "err",
        );
      } else {
        this.setStatus(t("Flashing failed: {msg}. Do step 3, then click Flash again.", { msg }), "err");
      }
    } finally {
      this.busy = false;
      this.flashBtn.disabled = this.closeBtn.disabled = this.versionSelect.disabled = false;
      this.host.finished(ok);
    }
  }
}

function setText(e: HTMLElement, text: string) {
  if (e.textContent !== text) e.textContent = text;
}
