/**
 * 绘图模型与落笔的单测。这里钉的是三件事：
 * **像素坐标是纯数据**（可以在 Node 里断），**阈值线不许被画成一条数据曲线**，以及
 * **每一笔的来源跟着点走**——折线里混进回放段时，页面上的 MOCK/真实角标才不是一句空话。
 */

import assert from "node:assert/strict";
import test from "node:test";

import { PAD, colorsFrom, draw, frame, model, niceStep, resizeCanvas } from "../chart.js";

const samples = (values) => values.map((rssi_db, tick) => ({ source: "real", tick, at: tick * 200, rssi_db }));

test("niceStep 只给 1/2/5×10ⁿ：沿用原 web/src/util/canvas.ts，两处刻度必须一致", () => {
  assert.equal(niceStep(40, 4), 10);
  assert.equal(niceStep(30, 4), 10);
  assert.equal(niceStep(100, 4), 20);
  assert.equal(niceStep(7, 4), 2);
  assert.equal(niceStep(1, 4), 0.2);
});

test("frame：值域 → 像素，且 y 轴是反的（dBFS 越大越靠上）", () => {
  const axis = frame({ w: 480, h: 180 }, { x: [0, 10], y: [-100, -50] });
  assert.equal(axis.left, PAD.left);
  assert.equal(axis.right, 470);
  assert.equal(axis.sy(-50), axis.top);
  assert.equal(axis.sy(-100), axis.bottom);
  assert.equal(axis.sx(0), axis.left);
  assert.equal(axis.sx(10), axis.right);
  assert.equal(axis.ix(axis.right), 10, "反解要能回去，否则鼠标定位是猜的");
});

test("frame：退化值域不许除零", () => {
  const axis = frame({ w: 200, h: 100 }, { x: [5, 5], y: [-80, -80] });
  assert.equal(Number.isFinite(axis.sx(5)), true);
  assert.equal(Number.isFinite(axis.sy(-80)), true);
  // 画布比内边距还窄时也要给出合法区间，不许出现 right < left。
  const tiny = frame({ w: 20, h: 12 }, { x: [0, 1], y: [0, 1] });
  assert.ok(tiny.right > tiny.left);
  assert.ok(tiny.bottom > tiny.top);
});

test("model：点序跟着节拍走，不是跟着到达顺序", () => {
  const built = model({ samples: samples([-90, -85, -70, -88]), size: { w: 480, h: 180 } });
  assert.deepEqual(built.points.map((point) => point.tick), [0, 1, 2, 3]);
  const xs = built.points.map((point) => point.x);
  for (let i = 1; i < xs.length; i += 1) assert.ok(xs[i] > xs[i - 1]);
  // −70 是最高值，它的 y 必须最小（画布坐标向下）。
  const ys = built.points.map((point) => point.y);
  assert.equal(ys.indexOf(Math.min(...ys)), 2);
});

test("model：值域把阈值也包进去，线贴边的问题由此避免", () => {
  const built = model({ samples: samples([-60, -60]), threshold: -50, size: { w: 480, h: 180 } });
  assert.ok(built.domain.y[1] >= -50, "阈值在数据之上时轴要抬上去，否则阈值线画在画布外");
  const low = model({ samples: samples([-120, -120]), threshold: -125, size: { w: 480, h: 180 } });
  assert.ok(low.domain.y[0] <= -125);
});

test("model：阈值是「判据」，不在 points 里", () => {
  const built = model({ samples: samples([-90, -80]), threshold: -85 });
  assert.equal(built.threshold.value, -85);
  assert.equal(built.points.some((point) => point.rssi_db === -85), false);
  const none = model({ samples: samples([-90]) });
  assert.equal(none.threshold, null, "没设阈值就不画那条线，也不拿 −85 兜底");
});

test("model：来源逐点跟着走，不在这里塌成一个值", () => {
  const rows = [-90, -88, -86].map((rssi_db, tick) => ({ source: tick === 0 ? "mock" : "real", tick, rssi_db }));
  const built = model({ samples: rows, size: { w: 480, h: 180 } });
  assert.deepEqual(built.points.map((point) => point.source), ["mock", "real", "real"]);
  assert.equal(
    built.points.every((point) => point.source === "real"),
    false,
    "折线里真有回放段：把三点统一成 real 就等于让 MOCK 冒充实时（S6 出口判据）"
  );
});

test("model：空序列不崩，gridY 仍有刻度", () => {
  const built = model({ samples: [] });
  assert.deepEqual(built.points, []);
  assert.ok(built.gridY.length >= 2);
  assert.equal(Number.isFinite(built.axis.sx(0)), true);
});

test("draw：阈值走虚线、数据走实线，两者笔色不同", () => {
  const calls = [];
  const ctx = new Proxy({}, {
    get(_target, key) {
      if (key === "setTransform" || typeof key === "symbol") return () => {};
      return (...args) => calls.push([String(key), args]);
    },
    set() { return true; },
  });
  const built = model({ samples: samples([-90, -80, -70]), threshold: -85, size: { w: 480, h: 180 } });
  const result = draw(ctx, built, { grid: "#000", muted: "#888", series: "#3987e5", threshold: "#c98500", font: "11px" });
  assert.equal(result.points, 3);
  const dashed = calls.filter(([name]) => name === "setLineDash");
  assert.equal(dashed.length, 2, "先设虚线画阈值，再清掉画数据");
  assert.deepEqual(dashed[0][1], [[5, 4]]);
  assert.deepEqual(dashed[1][1], [[]]);
  // 每条网格线都配一个刻度文字，否则轴上的数字是装饰。
  const labels = calls.filter(([name]) => name === "fillText");
  assert.equal(labels.length, built.gridY.length);
});

test("draw：只有一个点时不画折线以外的东西，但末点标记要有", () => {
  const calls = [];
  const ctx = new Proxy({}, { get: (_t, key) => (typeof key === "symbol" ? () => {} : (...args) => calls.push([String(key), args])), set: () => true });
  const built = model({ samples: samples([-80]), size: { w: 480, h: 180 } });
  const result = draw(ctx, built, { grid: "#000", muted: "#888", series: "#3987e5", threshold: "#c98500", font: "11px" });
  assert.equal(result.points, 1);
  assert.ok(calls.some(([name]) => name === "arc"));
  assert.ok(!calls.some(([name, args]) => name === "setLineDash"), "没设阈值就不该动虚线");
});

test("resizeCanvas：按 DPR 设后备缓冲，尺寸没变就不重设", () => {
  let resized = 0;
  const canvas = {
    clientWidth: 300,
    clientHeight: 120,
    width: 0,
    height: 0,
    getContext: () => ({ setTransform: () => {}, clearRect: () => {}, set width(v) { resized += 1; }, get width() { return 0; } }),
  };
  const surface = resizeCanvas(canvas, 2);
  assert.equal(canvas.width, 600);
  assert.equal(canvas.height, 240);
  assert.equal(surface.w, 300);
  assert.equal(surface.h, 120);
  assert.equal(resized, 0, "clearRect 不该被算成一次尺寸变更");
});

test("colorsFrom：只认 CSS 变量，取不到才用后备", () => {
  const colors = colorsFrom((name) => ({ "--grid": " #2c2c2a ", "--s1": "#3987e5" })[name]);
  assert.equal(colors.grid, "#2c2c2a");
  assert.equal(colors.series, "#3987e5");
  assert.equal(colors.threshold, "#c98500", "取不到的那条必须说清是后备值");
  const fromFallback = colorsFrom(() => "", { fallback: { threshold: "#abcdef" } });
  assert.equal(fromFallback.threshold, "#abcdef");
});
