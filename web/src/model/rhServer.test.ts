// GPL-3.0-or-later.
import { describe, expect, it } from "vitest";
import { buildSnapshot, normalizeRhUrl, rhFrequency } from "./rhServer";

describe("normalizeRhUrl", () => {
  it("adds http://, drops query/hash and trailing slashes", () => {
    expect(normalizeRhUrl("192.168.1.50:5000/")).toBe("http://192.168.1.50:5000");
    expect(normalizeRhUrl("  http://rh.local:5000/?x=1#y  ")).toBe("http://rh.local:5000");
    expect(normalizeRhUrl("https://race.example.com/rh/")).toBe("https://race.example.com/rh");
  });

  it("rejects other schemes and empty input", () => {
    expect(normalizeRhUrl("")).toBe("");
    expect(normalizeRhUrl("   ")).toBe("");
    expect(normalizeRhUrl("ftp://rh.local")).toBe("");
    expect(normalizeRhUrl("not a url")).toBe("");
  });
});

describe("rhFrequency", () => {
  it("rounds MHz values and treats unset nodes as 0", () => {
    expect(rhFrequency(5658)).toBe(5658);
    expect(rhFrequency("5695.4")).toBe(5695);
    expect(rhFrequency(0)).toBe(0);
    expect(rhFrequency(null)).toBe(0);
    expect(rhFrequency("")).toBe(0);
  });
});

const status = (heat: number) => ({ state: { current_heat: heat, currentProfile: 3 } });
const profile = { profile: { frequencies: [5658, 5695, 0, 5732] } };
const pilots = [
  { id: 7, callsign: "BMS", name: "Bob Smith" },
  { id: 8, callsign: null, name: "Alice" },
  { id: 9, callsign: "CJ", name: "CJ" },
];

function heat(heatId: number, nodes: Record<string, number | null>) {
  return { heat: { setup: { heat_id: heatId, displayname: "Heat 12", nodes_pilots: nodes } } };
}

describe("buildSnapshot", () => {
  it("matches pilots to channels by the seat frequency, callsign first", () => {
    const s = buildSnapshot(status(12), heat(12, { "0": 7, "1": 8, "2": null, "3": 9 }), pilots, profile);
    expect(s.heatId).toBe(12);
    expect(s.heatName).toBe("Heat 12");
    expect(s.practice).toBe(false);
    expect([...s.byMhz.keys()]).toEqual([5658, 5695, 5732]);
    expect(s.byMhz.get(5658)).toEqual({ mhz: 5658, label: "BMS", full: "BMS · Bob Smith" });
    expect(s.byMhz.get(5695)).toEqual({ mhz: 5695, label: "Alice", full: "Alice" });
    expect(s.byMhz.get(5732)).toEqual({ mhz: 5732, label: "CJ", full: "CJ" });
  });

  it("has no names in practice (no current heat)", () => {
    const s = buildSnapshot(status(0), null, pilots, profile);
    expect(s.practice).toBe(true);
    expect(s.byMhz.size).toBe(0);
  });

  it("keeps every pilot when two seats share a frequency", () => {
    const s = buildSnapshot(status(12), heat(12, { "0": 7, "1": 8 }), pilots, { profile: { frequencies: [5658, 5658] } });
    expect(s.byMhz.get(5658)?.label).toBe("BMS / Alice");
    expect(s.byMhz.get(5658)?.full).toBe("BMS · Bob Smith / Alice");
  });

  it("ignores a heat reply for another heat, and pilots without a name", () => {
    const s = buildSnapshot(status(12), heat(11, { "0": 7 }), pilots, profile);
    expect(s.heatName).toBe("");
    expect(s.byMhz.size).toBe(0);
    const unnamed = buildSnapshot(status(12), heat(12, { "0": 99 }), pilots, profile);
    expect(unnamed.byMhz.get(5658)?.label).toBe("#99");
  });
});
