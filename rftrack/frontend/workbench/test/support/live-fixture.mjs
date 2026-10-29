/**
 * 实时通路的测试夹具：**帧的形状只在这里定义一次**。
 *
 * 为什么单独一个文件而不是各测各的：`point()` 的键是照着后端
 * `services/capture.py::_point()` 抄的，环里换了字段，`live.test.mjs`（合并逻辑）和
 * `components.test.mjs`（渲染逻辑）必须一起改。两份副本的坏处是其中一份先过期——
 * 过期那一份还会一直绿，因为它只是"没跟着改"。
 *
 * 三个假件都**故意笨**：socket 不模拟 readyState，定时器不模拟时间流逝，时钟只有一个
 * 可以拧的指针。要钉的是"我们对着那些事件写了什么判据"，不是重演 WebSocket 协议。
 */

import {
  EVENT_CAP,
  NODE_CAP,
  createLive,
} from "../../live.js";


/** 夹具里的"现在"：2026-10-29 04:00:00 UTC，与录制基线同一时刻。 */
export const T0 = Date.UTC(2026, 9, 29, 4, 0, 0);

/** 可变时钟：判新鲜度的话只有把时间拧动才验得出来。 */
export function clock(start = T0) {
  const holder = { now: start };
  return {
    holder,
    now: () => holder.now,
    advance: (ms) => {
      holder.now += ms;
    },
  };
}

/** 只记账的定时器：退避序列与"到底有没有残留定时器"因此可以逐条断言。 */
export function fakeTimers() {
  const due = [];
  return {
    due,
    setTimeout(fn, ms) {
      due.push({ fn, ms, id: due.length + 1 });
      return due.length;
    },
    clearTimeout(id) {
      const at = Number(id) - 1;
      if (due[at]) due[at].cancelled = true;
    },
    /** 跑掉最早的那一条（其余留着）：重连测试要的就是"这一次到底排了几毫秒"。 */
    fire() {
      const next = due.find((row) => !row.cancelled);
      if (!next) return null;
      next.cancelled = true;
      next.fn();
      return next.ms;
    },
    pending: () => due.filter((row) => !row.cancelled).length,
  };
}

export function fakeSocket() {
  const sockets = [];
  const factory = (url) => {
    const socket = {
      url,
      closed: 0,
      handlers: 0,
      onopen: null,
      onmessage: null,
      onerror: null,
      onclose: null,
      close() {
        socket.closed += 1;
      },
      /** 模拟服务端把连接打开。 */
      opened() {
        socket.onopen && socket.onopen({});
      },
      /** 模拟服务端推一帧（文本，跟真实协议一致：WS 上走的是 JSON 串）。 */
      sent(message) {
        socket.onmessage && socket.onmessage({ data: typeof message === "string" ? message : JSON.stringify(message) });
      },
      /** 模拟对端关闭：`code` 与 `reason` 就是服务端 `WebSocketDisconnect` 里那两样。 */
      dropped(code = 1006, reason = "") {
        socket.onclose && socket.onclose({ code, reason });
      },
    };
    sockets.push(socket);
    return socket;
  };
  return { factory, sockets };
}

/** 环里的一格：键与 `services/capture.py::_point` 一一对（解析器那一份 + 时刻/来源/`ema` 别名）。 */
export const point = (id, extra = {}) => ({
  id,
  flags: 0,
  rssi: -88.4,
  rssiEma: -89.1,
  ema: -89.1,
  floor: -101.2,
  snr: 12.8,
  clearFloor: null,
  peak: null,
  peakOffHz: null,
  centroidOffHz: null,
  obwLoHz: null,
  obwHiHz: null,
  ts: "2026-10-29T04:00:00+00:00",
  seq: 12,
  tMs: 4321,
  mode: "FULL",
  loKhz: 5658000,
  saturated: 0,
  source: "real",
  ...extra,
});

/** `samples` 帧：字段照抄 `services/capture.py` 的发布口，不自造形状。 */
export const samplesFrame = (overrides = {}) => ({
  kind: "samples",
  ts: "2026-10-29T04:00:00+00:00",
  site_id: 1,
  gateway_id: 20,
  node_id: 30,
  transport_addr: "/dev/ttyAMA0",
  display_name: "板 R1",
  ep: 1,
  source: "real",
  seq: 12,
  mode: "FULL",
  loKhz: 5658000,
  tMs: 4321,
  channels: [point(0), point(1)],
  ...overrides,
});

export const eventRow = (id, overrides = {}) => ({
  event_id: id,
  ts: "2026-10-29T04:00:00+00:00",
  site_id: 1,
  gateway_id: 20,
  node_id: 30,
  slot: null,
  freq_khz: null,
  kind: "node_offline",
  metric: null,
  value: null,
  quality: null,
  source: "real",
  payload: { silence_s: 5.2 },
  payload_error: null,
  ...overrides,
});

export const eventFrame = (id, overrides = {}) => {
  const row = eventRow(id, overrides);
  // 信封的 `site_id`/`ts` 跟着行走：后端就是这么拼的（`services/capture.py:876` 的
  // `{"kind": "event", "site_id": row["site_id"], "ts": event["ts"], "event": event}`）。
  return { kind: "event", site_id: row.site_id, ts: row.ts, event: row };
};

/** 一套完整的假件 + 真的 `createLive`：组件测试要的正是"帧走真的合并路，别处全是假的"。 */
export function harness(options = {}) {
  const time = clock(options.now ?? T0);
  const timers = fakeTimers();
  const net = fakeSocket();
  const live = createLive({
    base: options.base ?? "",
    location: options.location ?? { protocol: "http:", host: "192.168.99.111:8000" },
    wsFactory: net.factory,
    timers,
    now: time.now,
    staleAfterS: options.staleAfterS ?? 15,
    nodeCap: options.nodeCap ?? NODE_CAP,
    eventCap: options.eventCap ?? EVENT_CAP,
  });
  return { live, time, timers, net };
}

/** 连上场地 1 并让它处于 open：绝大多数用例都要的第一步，单独抽出来免得每处重复三行。 */
export function connected(options = {}) {
  const env = harness(options);
  env.live.attach(options.site ?? 1);
  env.net.sockets[0].opened();
  return env;
}
