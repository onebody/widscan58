// RotorHazard race data settings: the server address, the pilot-name overlay,
// and the poll result. GPL-3.0-or-later.
import { t } from "../i18n";
import type { App } from "../model/app";
import { normalizeRhUrl } from "../model/rhServer";
import { el } from "../util/canvas";

export class RhServerDialog {
  readonly el: HTMLDialogElement;
  private onBox = el("input", { type: "checkbox" }) as HTMLInputElement;
  private urlInput = el("input", {
    type: "text",
    placeholder: "http://192.168.1.50:5000",
    spellcheck: "false",
    autocapitalize: "off",
    autocomplete: "off",
  }) as HTMLInputElement;
  private urlError = el("div", { class: "rh-server-error", hidden: "" }, "Enter a full http:// or https:// address.");
  private state = el("div", { class: "rh-server-state" });
  private checkBtn = el("button", {}, "Check now") as HTMLButtonElement;
  private closeBtn = el("button", { class: "primary", autofocus: "" }, "Close");

  constructor(private app: App) {
    this.closeBtn.onclick = () => this.el.close();
    this.onBox.onchange = () => this.save();
    this.urlInput.onchange = () => this.save();
    this.urlInput.oninput = () => this.syncState();
    this.urlInput.onkeydown = (e) => {
      if (e.key !== "Enter") return;
      e.preventDefault();
      this.save(true);
    };
    this.checkBtn.onclick = () => this.save(true);
    this.el = el(
      "dialog",
      { class: "options-dialog rh-server-dialog" },
      el("h2", {}, "RotorHazard race data"),
      el(
        "p",
        {},
        "Read the current heat from the RotorHazard server and show each pilot on the matching channel, by frequency.",
      ),
      el("label", { class: "option" }, this.onBox, el("span", {}, "Show pilot names")),
      el("label", { class: "rh-server-url" }, el("span", {}, "Server address"), this.urlInput),
      this.urlError,
      this.state,
      el("div", { class: "flash-buttons" }, this.checkBtn, this.closeBtn),
    );
    this.app.store.subscribe(() => {
      if (this.el.open) this.syncState();
    });
  }

  open() {
    const s = this.app.rhServer;
    this.onBox.checked = s.on;
    this.urlInput.value = s.url;
    this.urlError.hidden = true;
    this.syncState();
    this.el.showModal();
  }

  /** Store the address and switch, then (re)start polling. */
  private save(check = false) {
    const s = this.app.rhServer;
    const raw = this.urlInput.value.trim();
    const url = raw ? normalizeRhUrl(raw) : "";
    this.urlError.hidden = !raw || !!url;
    if (raw && !url) {
      if (this.onBox.checked || check) {
        this.onBox.checked = s.on; // the switch did not change
        this.syncState();
        return;
      }
      // Turning the overlay off does not need a valid address.
      this.urlError.hidden = true;
      if (s.on) s.configure({ on: false });
      this.syncState();
      return;
    }
    const on = check || this.onBox.checked;
    this.onBox.checked = on;
    if (check || url !== s.url || on !== s.on) s.configure({ url, on });
    this.syncState();
  }

  private syncState() {
    const s = this.app.rhServer;
    const lines: string[] = [];
    if (!s.on) lines.push(t("Off"));
    else if (s.status === "waiting") lines.push(t("Set the server address."));
    else if (s.status === "loading") lines.push(t("Reading…"));
    else if (s.status === "error") {
      lines.push(s.errorText() ?? t("Off"));
    } else if (s.snapshot) {
      const snap = s.snapshot;
      if (snap.practice) lines.push(t("Practice (no heat): no pilot names"));
      else {
        lines.push(
          t("{name} · {n} pilots", {
            name: snap.heatName || t("Heat {n}", { n: snap.heatId }),
            n: snap.byMhz.size,
          }),
        );
        if (!snap.byMhz.size) lines.push(t("No pilots in this heat."));
      }
    }
    this.state.className = `rh-server-state${s.status === "error" ? " err" : ""}`;
    this.state.replaceChildren(...lines.map((line) => el("div", {}, line)));
    this.checkBtn.disabled = !s.on && !this.urlInput.value.trim();
  }
}
