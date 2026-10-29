// Golden 帧夹具导出：字节 + **TypeScript 解析器的实际输出**一起落盘，供 Python 侧对拍。
//
// 为什么不由 Python 侧自己造期望值：那样对拍只证明"我写的东西自洽"，证明不了跟
// 线上真正在用的解析器一致。权威是 web/src/proto/frames.ts 与 web/src/serial/parser.ts，
// 所以这里直接 import 它们（node 跑 TypeScript 用 --experimental-strip-types，
// 已在 Mac 上实测可加载这两个文件；不引 vite/tsx，避免给仓库添构建依赖）。
//
// 这些是**合成**帧（字节布局按 firmware/src/proto/frames.h 的 _Static_assert 尺寸构造），
// 不是真机录制的字节流。所以它证明的是"两份解析器口径一致"，不证明"固件确实这么发"
// —— 后者要等 M0-3 的真机 60 s 录制，届时把录制文件再喂一遍同一份对拍测试。
//
// 用法：node --experimental-strip-types rftrack/tools/export_golden_frames.mjs

import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { StreamParser, crc32, encodeFrame } from "../../web/src/serial/parser.ts";
import {
  FRAME_LEVELS,
  FRAME_LOG,
  FRAME_METRICS,
  FRAME_RAWIQ,
  FRAME_SPECTRUM,
  FRAME_TELEMETRY,
  RawIqAssembler,
  parseLevels,
  parseMetrics,
  parseRawIqChunk,
  parseSpectrum,
  parseTelemetry,
  rawIqChunkIsLast,
} from "../../web/src/proto/frames.ts";

const here = dirname(fileURLToPath(import.meta.url));
const OUT = resolve(here, "../backend/tests/golden/frames-v1.json");

const bytes = (...parts) => {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let o = 0;
  for (const p of parts) {
    out.set(p, o);
    o += p.length;
  }
  return out;
};

const hex = (u8) => Array.from(u8, (b) => b.toString(16).padStart(2, "0")).join("");
const text = (s) => new TextEncoder().encode(s);

// 帧头的 version 字节：parser.ts 从不读它，只靠 CRC 认帧。夹具里固定写 1，
// 另有一条 version=2 的用例，用来把"TS 根本不校验版本"这个事实钉进对拍里。
function frame(type, payload, version = 1) {
  const out = encodeFrame(type, payload);
  out[3] = version;
  // 改了版本字节必须重算 CRC，否则这条用例会以 crcErrors 失败——那是对拍的正确答案吗？
  // 不是：TS 不校验版本，所以重算 CRC 后它应当被正常解出，这才是我们要镜像的行为。
  const c = crc32(out, 0, 6 + payload.length);
  new DataView(out.buffer).setUint32(6 + payload.length, c, true);
  return out;
}

// --- payload 构造器：偏移量照抄 firmware/src/proto/frames.h ---------------

function metricsPayload(nch = 3) {
  const H = 34;
  const dv = new DataView(new ArrayBuffer(H + 26 * nch));
  dv.setUint32(0, 7, true);
  dv.setUint32(4, 123456, true);
  dv.setUint32(8, 2410437, true);
  dv.setUint32(12, 1050, true);
  dv.setUint32(16, 160, true);
  dv.setUint32(20, 67602, true); // 超出旧 u16 量程的 dsp_us
  dv.setUint16(24, 2, true);
  dv.setInt16(26, -6860, true);
  dv.setInt16(28, -3800, true);
  dv.setUint8(30, 60);
  dv.setUint8(31, 1);
  dv.setUint8(32, 4);
  dv.setUint8(33, nch);
  const chans = [
    { id: 3, flags: 1, rssi: -3600, ema: -3550, floor: -5650, snr: 2050, clear: -32768, peak: -3300, peakOff: -1234, centroid: 885000, obwLo: -4000, obwHi: 4400 },
    { id: 5, flags: 0, rssi: -7000, ema: -7100, floor: -8000, snr: 1000, clear: -8100, peak: -6900, peakOff: 0, centroid: -25000, obwLo: -1000, obwHi: 1200 },
    { id: 8, flags: 2 | 32, rssi: -150, ema: -140, floor: -6000, snr: 5850, clear: -6200, peak: -100, peakOff: 220000, centroid: 219500, obwLo: -5600, obwHi: 5700 },
  ];
  for (let j = 0; j < nch; j++) {
    const o = H + 26 * j;
    const c = chans[j % chans.length];
    dv.setUint8(o, c.id);
    dv.setUint8(o + 1, c.flags);
    dv.setInt16(o + 2, c.rssi, true);
    dv.setInt16(o + 4, c.ema, true);
    dv.setInt16(o + 6, c.floor, true);
    dv.setInt16(o + 8, c.snr, true);
    dv.setInt16(o + 10, c.clear, true);
    dv.setInt16(o + 12, c.peak, true);
    dv.setInt32(o + 14, c.peakOff, true);
    dv.setInt32(o + 18, c.centroid, true);
    dv.setInt16(o + 22, c.obwLo, true);
    dv.setInt16(o + 24, c.obwHi, true);
  }
  return new Uint8Array(dv.buffer);
}

function levelsPayload(mode, nch = 2) {
  const H = 32;
  const dv = new DataView(new ArrayBuffer(H + 4 * nch));
  dv.setUint32(0, 9, true);
  dv.setUint32(4, 222333, true);
  dv.setUint32(8, 5677000, true);
  dv.setUint32(12, 1050, true);
  dv.setUint32(16, 60, true);
  dv.setUint32(20, 1150, true);
  dv.setUint16(24, 0, true);
  dv.setUint8(26, 60);
  dv.setUint8(27, 0);
  dv.setUint8(28, 4);
  dv.setUint8(29, mode);
  dv.setUint8(30, mode === 2 ? 118 : 0);
  dv.setUint8(31, nch);
  // 0.25 dB 步进：code = (dBFS + 63.75) * 4；snr 码 = dB * 4
  const codes = [
    [0, 1, 111, 82],
    [1, 0, 25, 40],
    [2, 32, 200, 96],
    [3, 4, 60, 12],
  ];
  for (let j = 0; j < nch; j++) {
    const o = H + 4 * j;
    const c = codes[j % codes.length];
    dv.setUint8(o, c[0]);
    dv.setUint8(o + 1, c[1]);
    dv.setUint8(o + 2, c[2]);
    dv.setUint8(o + 3, mode === 2 ? c[3] : 0);
  }
  return new Uint8Array(dv.buffer);
}

function spectrumPayload(n = 8) {
  const out = new Uint8Array(20 + n);
  const dv = new DataView(out.buffer);
  dv.setUint32(0, 41, true);
  dv.setUint32(4, 2431312, true);
  dv.setInt32(8, -11300000, true);
  dv.setUint32(12, 88281, true);
  dv.setUint16(16, n, true);
  dv.setUint8(18, 1);
  dv.setUint8(19, 0); // reserved：TS 不读，Python 也不该读
  for (let j = 0; j < n; j++) out[20 + j] = [0, 115, 255, 128, 1, 254, 64, 190][j % 8];
  return out;
}

function telemetryPayload() {
  const dv = new DataView(new ArrayBuffer(59));
  dv.setUint32(0, 987654, true);
  dv.setUint32(4, 1200, true);
  dv.setUint32(8, 34567, true);
  dv.setUint32(12, 3456, true); // rotation_mhz -> 3.456 Hz
  dv.setUint32(16, 69382, true);
  dv.setUint32(20, 76000, true);
  dv.setUint32(24, 1050, true);
  dv.setUint32(28, 3700, true);
  dv.setUint32(32, 165, true);
  dv.setUint32(36, 3500, true);
  dv.setUint32(40, 66713, true);
  dv.setUint16(44, 250, true); // link_fill_pm -> 0.25
  dv.setUint32(46, 29872, true);
  dv.setUint32(50, 7, true);
  dv.setUint32(54, 3, true);
  dv.setUint8(58, 1);
  return new Uint8Array(dv.buffer);
}

function rawiqPayload(n, offset, count) {
  const dv = new DataView(new ArrayBuffer(14 + 2 * count));
  dv.setUint32(0, 5677000, true);
  dv.setUint32(4, 80000000, true);
  dv.setUint16(8, n, true);
  dv.setUint16(10, offset, true);
  dv.setUint8(12, 1);
  dv.setUint8(13, 2);
  for (let j = 0; j < count; j++) {
    dv.setInt8(14 + 2 * j, (offset + j) % 100);
    dv.setInt8(15 + 2 * j, -((offset + j) % 100));
  }
  return new Uint8Array(dv.buffer);
}

// --- 用例：单帧字段解码 ---------------------------------------------------

const cases = [];
const streamCases = [];

function fieldCase(name, type, payload, parsed) {
  cases.push({ name, type, hex: hex(payload), parsed: JSON.parse(JSON.stringify(parsed, replacer)) });
}
function replacer(_k, v) {
  if (v instanceof Float32Array || v instanceof Int8Array || v instanceof Uint8Array) return Array.from(v);
  return v;
}

const metricsDv = new DataView(metricsPayload().buffer);
fieldCase("metrics_full_3ch", FRAME_METRICS, metricsPayload(), parseMetrics(metricsDv));

for (const mode of [1, 2]) {
  const p = levelsPayload(mode);
  fieldCase(`levels_mode_${mode}`, FRAME_LEVELS, p, parseLevels(new DataView(p.buffer)));
}
// mode 字节 0：LEVELS_MODES[0] === "FULL"，既成事实，Python 必须一样处理
const m0 = levelsPayload(0, 1);
fieldCase("levels_mode_0_is_full", FRAME_LEVELS, m0, parseLevels(new DataView(m0.buffer)));
// nch 超出实际字节数：TS 在 `o + SIZE > byteLength` 处 break，只解出放得下的那几个
const truncated = new Uint8Array(levelsPayload(2, 4).subarray(0, 32 + 8));
fieldCase("levels_truncated_nch", FRAME_LEVELS, truncated, parseLevels(new DataView(truncated.buffer)));

for (const n of [3, 8, 512]) {
  const p = spectrumPayload(n);
  fieldCase(`spectrum_n_${n}`, FRAME_SPECTRUM, p, parseSpectrum(new DataView(p.buffer)));
}
fieldCase("telemetry", FRAME_TELEMETRY, telemetryPayload(), parseTelemetry(new DataView(telemetryPayload().buffer)));

// LOG 帧：TS 没有 parseLog，落到"未知类型"这一分支——Python 侧必须同样不崩。
const logPayload = text("LOG hello\n");
cases.push({ name: "log_frame_no_decoder", type: FRAME_LOG, hex: hex(logPayload), parsed: null });

// 版本字节 = 2：TS 不校验版本，照常解出。这条是"版本护栏在 TS 里不存在"的证据。
// hex 一律是 **payload**（与其余用例同构）；整帧（含 version=2 那个字节）另存 whole_hex。
const v2 = frame(FRAME_SPECTRUM, spectrumPayload(3), 2);
fieldCase("version_byte_2_still_parses", FRAME_SPECTRUM, spectrumPayload(3), parseSpectrum(new DataView(spectrumPayload(3).buffer)));
cases.at(-1).whole_hex = hex(v2);

// --- 用例：RAWIQ 组装（跨帧状态机） --------------------------------------

{
  const chunks = [rawiqPayload(2500, 0, 1024), rawiqPayload(2500, 1024, 1024), rawiqPayload(2500, 2048, 452)];
  const a = new RawIqAssembler();
  cases.push({
    name: "rawiq_snapshot_assembled",
    type: FRAME_RAWIQ,
    hex: chunks.map(hex).join(":"),
    parsed: JSON.parse(JSON.stringify(chunks.map((c) => a.add(new DataView(c.buffer))), replacer)),
  });
  const gapChunks = [rawiqPayload(3000, 0, 1024), rawiqPayload(3000, 2048, 952), rawiqPayload(100, 0, 100)];
  const b = new RawIqAssembler();
  cases.push({
    name: "rawiq_missing_chunk_drops_snapshot",
    type: FRAME_RAWIQ,
    hex: gapChunks.map(hex).join(":"),
    parsed: JSON.parse(JSON.stringify(gapChunks.map((c) => b.add(new DataView(c.buffer))), replacer)),
  });
  const chunk = rawiqPayload(2500, 1024, 1024);
  const chunkHeader = JSON.parse(JSON.stringify(parseRawIqChunk(new DataView(chunk.buffer)), replacer));
  delete chunkHeader.p; // TS 把原始 DataView 也带在返回对象里，夹具只留字段
  cases.push({
    name: "rawiq_chunk_header_fields",
    type: FRAME_RAWIQ,
    hex: hex(chunk),
    parsed: chunkHeader,
    is_last: rawIqChunkIsLast(new DataView(chunk.buffer)),
  });
}

// --- 用例：字节流分包（含交错文本、坏帧重同步、跨块分片） ------------------

function streamCase(name, feed, expectItems) {
  const p = new StreamParser();
  const out = [];
  for (const chunk of feed) out.push(...p.push(chunk));
  streamCases.push({
    name,
    feed: feed.map(hex),
    items: expectItems(out),
    counters: { crcErrors: p.crcErrors, resyncBytes: p.resyncBytes, bytes: p.bytes },
  });
}
const describeItems = (out) =>
  out.map((i) =>
    i.kind === "text"
      ? { kind: "text", line: i.line }
      // parser.ts 交出来的是 DataView：Array.from(DataView) 得到空数组，
      // 必须先看穿到它的 buffer，否则 golden 里的 payload_hex 会是空串（我第一版就踩了）。
      : { kind: "frame", type: i.type, payload_hex: hex(new Uint8Array(i.payload.buffer)), payload_len: i.payload.byteLength }
  );

const metricsFrame = encodeFrame(FRAME_METRICS, metricsPayload());
const splitMetrics = (() => {
  const feed = [];
  for (let j = 0; j < metricsFrame.length; j += 5) feed.push(metricsFrame.subarray(j, j + 5));
  return feed;
})();
const corruptThenGood = (() => {
  const bad = encodeFrame(FRAME_METRICS, metricsPayload());
  bad[10] ^= 0xff;
  return [bytes(bad, metricsFrame, text("OK\n"))];
})();
const joinedMidFrame = [bytes(metricsFrame.subarray(9), text("\n"), metricsFrame, text("ERR busy\n"))];

streamCase("text_frame_text_interleaved", [bytes(text("OK\n"), metricsFrame, text('STATUS {"a":1}\n'))], describeItems);
streamCase("frame_split_across_chunks", splitMetrics, describeItems);
streamCase("corrupted_frame_then_good_one", corruptThenGood, describeItems);
streamCase("joined_mid_frame_tail_ignored", joinedMidFrame, describeItems);
streamCase("leading_garbage_needs_resync", [bytes(new Uint8Array([0x00, 0xff, 0x55]), encodeFrame(FRAME_SPECTRUM, spectrumPayload(3)), text("R\n"))], describeItems);
streamCase("sync0_without_sync1", [bytes(new Uint8Array([0xa5, 0x00, 0x01]), encodeFrame(FRAME_LEVELS, levelsPayload(1)))], describeItems);
streamCase("empty_stream", [new Uint8Array(0)], describeItems);
streamCase("spectrum_on_uart_cap_512", [encodeFrame(FRAME_SPECTRUM, spectrumPayload(512))], describeItems);

const doc = {
  generated_by: "rftrack/tools/export_golden_frames.mjs",
  authority: ["web/src/serial/parser.ts", "web/src/proto/frames.ts"],
  layout_source: "firmware/src/proto/frames.h",
  caveat: "合成帧：证明两份解析器口径一致；不证明固件实际字节（那要 M0-3 真机录制）",
  cases,
  streams: streamCases,
};
mkdirSync(dirname(OUT), { recursive: true });
writeFileSync(OUT, JSON.stringify(doc, replacer, 2) + "\n");
console.log(`golden -> ${OUT}`);
console.log(`  cases=${cases.length} streams=${streamCases.length}`);
