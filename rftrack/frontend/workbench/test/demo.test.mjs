/**
 * 演示数据源单测。钉的是规范 §2.3 那三条：**可区分、可复现、不越界**。
 * "越界"在这里是可断言的具体形状：没有距离/方向/坐标字段，也没有节点身份。
 */

import assert from "node:assert/strict";
import test from "node:test";

import { DEMO_SLOTS, createDemoSource, rssi } from "../demo.js";

const NOW = () => 1_730_000_000_000;
const demo = createDemoSource({ now: NOW });

test("每一条读数都带 source=demo——角标只是可见形式，凭据在数据对象上", () => {
  const matrix = demo.channelMatrix({ slots: [0, 1, 2], threshold: -85, refreshMs: 200 });
  assert.equal(matrix.source, "demo");
  assert.equal(matrix.channels.length, 3);
  for (const channel of matrix.channels) assert.equal(channel.source, "demo");

  const series = demo.series({ slot: 2, points: 10, refreshMs: 200 });
  assert.equal(series.source, "demo");
  for (const sample of series.samples) assert.equal(sample.source, "demo");

  const events = demo.events({ limit: 5, refreshMs: 200 });
  assert.equal(events.source, "demo");
  for (const item of events.items) assert.equal(item.source, "demo");
});

test("同一个 (seed, slot, tick) 永远给同一个数：演示要能复现，测试要能钉住", () => {
  assert.equal(rssi(3, 100), rssi(3, 100));
  assert.notEqual(rssi(3, 100), rssi(4, 100));
  const again = createDemoSource({ now: NOW });
  assert.deepEqual(again.channelMatrix({ slots: [0, 1] }), demo.channelMatrix({ slots: [0, 1] }));
});

test("RSSI 落在协议量纲内：dBFS 不会给出正数，也不会低于 −128", () => {
  for (let slot = 0; slot < DEMO_SLOTS; slot += 1) {
    for (let tick = 0; tick < 200; tick += 1) {
      const value = rssi(slot, tick, 20261029);
      assert.ok(value <= 0 && value >= -128, `slot ${slot} tick ${tick} → ${value}`);
      assert.equal(value, Math.round(value * 10) / 10, "一位小数：int16/100 的精度就够，多出来的位数是假的");
    }
  }
});

test("频率是 MHz 且用仓库里那套 Raceband 八档，不自己编第二套频道", () => {
  const matrix = demo.channelMatrix({ slots: [0, 7], refreshMs: 200 });
  assert.deepEqual(matrix.channels.map((row) => row.mhz), [5658, 5917]);
  for (const channel of matrix.channels) {
    assert.ok(Number.isInteger(channel.mhz) && channel.mhz > 5000 && channel.mhz < 6000);
  }
});

test("活动态就是「超过阈值没有」这一个判据，不掺别的结论", () => {
  const matrix = demo.channelMatrix({ slots: [2, 3], threshold: -85, refreshMs: 200 });
  for (const channel of matrix.channels) {
    assert.equal(channel.active, channel.rssi_db > -85);
  }
  const loose = demo.channelMatrix({ slots: [2], threshold: -120, refreshMs: 200 });
  assert.equal(loose.channels[0].active, true);
});

test("没给阈值时活动态是 null，而不是 false：没判过 ≠ 判过且静默", () => {
  const matrix = demo.channelMatrix({ slots: [0, 6], refreshMs: 200 });
  assert.equal(matrix.threshold_db, null);
  assert.deepEqual(matrix.channels.map((row) => row.active), [null, null]);
  assert.equal(matrix.channels[0].rssi_db <= 0, true, "读数本身照旧给，只是不替用户下结论");
});

test("节拍跟着生效的 refresh_ms 走：同一个 now 换周期就是不同的一拍", () => {
  assert.equal(demo.tickAt(200), Math.floor(NOW() / 200));
  assert.equal(demo.tickAt(1000), Math.floor(NOW() / 1000));
  assert.equal(demo.tickAt(0), Math.floor(NOW() / 1), "refresh_ms 为 0 时不许除零");
});

test("序列长度就是请求的那一段，且时间戳单调递增", () => {
  const series = demo.series({ slot: 1, points: 30, refreshMs: 200 });
  assert.equal(series.samples.length, 30);
  assert.equal(series.samples.at(-1).tick, demo.tickAt(200));
  for (let i = 1; i < series.samples.length; i += 1) {
    assert.ok(series.samples[i].at > series.samples[i - 1].at);
  }
});

test("事件流不编造节点身份、坐标、距离或轨迹：这些字段必须压根不存在", () => {
  const { items } = demo.events({ limit: 20, refreshMs: 200 });
  assert.equal(items.length, 20);
  const banned = ["node_id", "node", "display_name", "distance_m", "range_m", "lat", "lon", "x", "y", "track", "bearing", "rssi_percent"];
  for (const item of items) {
    for (const key of banned) assert.equal(item[key], undefined, `事件里出现了 ${key}，那是按 RSSI 猜出来的东西`);
    assert.ok(["info", "warning", "alert"].includes(item.level), `level 只有三档：${item.level}`);
    assert.ok(["activity", "node_state", "threshold"].includes(item.kind));
    assert.equal(item.source, "demo");
  }
});

test("档位数与后端同一个事实：8，不是页面自己多画几列", () => {
  assert.equal(DEMO_SLOTS, 8);
  assert.equal(demo.DEMO_SLOTS, 8);
});
