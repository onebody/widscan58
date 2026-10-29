/**
 * `live.js` 的账。**不连服务端**：这里传进去的是假 socket、假定时器、假时钟，
 * 于是重连退避、快照合并、事件去重、换场地清账这些判据都能在 Node 里被逐条断言。
 *
 * 为什么值得为这一层单独写测试：它管的正是"页面上那句关于连接的话到底是不是真的"。
 * 状态栏说"已连接 · 2 块板"、格子里说"推送断了"，两句话都出自这里的一份状态机；
 * 而浏览器那一轮（`e2e/workbench.mjs`）只能看到"此刻显示什么"，看不到
 * "被拒之后有没有偷偷重连""换场地有没有把旧场地的最后一帧留下"。
 *
 * 假 socket 是**故意笨**的：它只记录 handler 与 close 调用，不模拟 readyState。
 * 真实 WebSocket 的那些竞态（半开、心跳、TLS 断开码）由服务端那一侧的测试与人工观察负责，
 * 这一层要钉的是"我们对着那些事件写了什么判据"。
 *
 * 帧的形状与三个假件都在 `test/support/live-fixture.mjs`——`components.test.mjs` 用的是同一份，
 * 环里的键改了就得两边一起改，不该靠其中一边"碰巧没测到"。
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  EVENT_CAP,
  NODE_CAP,
  RECONNECT_STEPS_MS,
  STATUS,
  liveUrl,
} from "../live.js";
import {
  T0,
  clock,
  connected,
  eventFrame,
  eventRow,
  fakeSocket,
  fakeTimers,
  harness,
  point,
  samplesFrame,
} from "./support/live-fixture.mjs";

// --- 拨号地址 -----------------------------------------------------------------

test("同源部署：推送地址跟着页面的 origin，路径前缀一个字符都不丢", () => {
  assert.equal(
    liveUrl({ base: "", location: { protocol: "http:", host: "192.168.99.111:8000" }, site: 3 }),
    "ws://192.168.99.111:8000/ws/live?site=3"
  );
  assert.equal(
    liveUrl({ base: "/rftrack", location: { protocol: "https:", host: "track.local" }, site: 3 }),
    "wss://track.local/rftrack/ws/live?site=3"
  );
});

test("?api= 覆盖成绝对地址时：推送跟着 API 走，不跟着静态宿主走", () => {
  // 页面从 file:// 或另一台静态服务器打开、API 在局域网另一头 —— 纯 API 部署形态。
  // 这时 WS 连到页面的 origin 上，那里根本没有 /ws/live，页面会永远停在"重连中"。
  assert.equal(
    liveUrl({ base: "http://10.0.0.5:8000", location: { protocol: "http:", host: "127.0.0.1:5500" }, site: 2 }),
    "ws://10.0.0.5:8000/ws/live?site=2"
  );
  assert.equal(
    liveUrl({ base: "https://track.example.com/api", location: { protocol: "http:", host: "localhost" }, site: 7 }),
    "wss://track.example.com/api/ws/live?site=7"
  );
});

test("场地号进查询串要过编码：这里只允许数字，但钉子得钉在编码这一层", () => {
  assert.match(liveUrl({ base: "", location: { protocol: "http:", host: "h" }, site: "1/2" }), /site=1%2F2$/);
});

// --- 连接状态机 ---------------------------------------------------------------

test("attach 就拨号：状态先是 connecting，onopen 之后才是 open", () => {
  const env = harness();
  env.live.attach(1);
  assert.equal(env.live.status().status, STATUS.CONNECTING);
  assert.equal(env.net.sockets.length, 1);
  assert.equal(env.net.sockets[0].url, "ws://192.168.99.111:8000/ws/live?site=1");
  env.net.sockets[0].opened();
  assert.equal(env.live.status().status, STATUS.OPEN);
  assert.equal(env.live.status().opened, 1);
});

test("没有场地就是「未选场地」而不是「断线」：状态回 idle，一条连接都不拨", () => {
  const env = harness();
  env.live.attach(1);
  env.net.sockets[0].opened();
  env.live.attach(null);
  assert.equal(env.live.status().status, STATUS.IDLE, "把「没选场地」显示成断线，状态栏就在说谎");
  assert.equal(env.net.sockets.length, 1);
  env.live.attach(undefined);
  env.live.attach("");
  assert.equal(env.net.sockets.length, 1);
});

test("同一场地重复 attach 不再拨第二条：换布局、重算上下文都不该多占一个订阅者", () => {
  const env = connected();
  env.live.attach(1);
  env.live.attach(1);
  assert.equal(env.net.sockets.length, 1);
  assert.equal(env.live.status().opened, 1);
});

test("策略性拒绝（1008）不重连：理由原样留着", () => {
  const env = harness();
  env.live.attach(1);
  env.net.sockets[0].dropped(1008, "site_not_found");
  assert.equal(env.live.status().status, STATUS.REFUSED);
  assert.equal(env.live.status().reason, "site_not_found");
  assert.equal(env.timers.pending(), 0, "被拒之后排队重连就是把服务端的连接日志刷满");
  assert.equal(env.net.sockets.length, 1);
});

test("意外断开走退避：1/2/4/8/10 之后一直 10 s，到顶不许再涨", () => {
  const env = connected();
  env.net.sockets[0].dropped(1006, "");
  const waits = [];
  for (let i = 0; i < 6; i += 1) {
    waits.push(env.timers.fire());
    // 刚排出去的那条连接还没打开就再断一次：只有连着失败才走得完这一串退避
    // （一旦 onopen，尝试次数就归零，那是下一条测试要验的事）。
    env.net.sockets[env.net.sockets.length - 1].dropped(1006, "");
  }
  assert.deepEqual(waits, [...RECONNECT_STEPS_MS, RECONNECT_STEPS_MS[RECONNECT_STEPS_MS.length - 1]]);
  assert.equal(env.live.status().status, STATUS.RETRYING);
  // 7 = 第一次断开排的那一轮 + 后面 6 轮各自又断一次排的那一轮。
  // 数字本身不重要，重要的是它跟着"排出去几轮"走，而不是在到顶之后停住不动（停在 5 就骗人说还没重试）。
  assert.equal(env.live.status().attempts, 7);
  assert.equal(env.net.sockets.length, 7, "每一轮退避只拨一条，不多占");
});

test("退避期间连上了就把尝试次数归零：否则下一次断线会从第 7 档等起", () => {
  const env = connected();
  env.net.sockets[0].dropped(1006, "");
  env.timers.fire();
  env.net.sockets[1].dropped(1006, "");
  assert.ok(env.live.status().attempts > 1);
  env.timers.fire();
  env.net.sockets[2].opened();
  assert.equal(env.live.status().attempts, 0);
  assert.equal(env.live.status().status, STATUS.OPEN);
});

test("close() 收尾干净：socket 摘掉 handler、定时器一条不留、数据清空", () => {
  const env = connected();
  env.net.sockets[0].sent(samplesFrame());
  env.live.close();
  assert.equal(env.live.status().status, STATUS.CLOSED);
  assert.equal(env.net.sockets[0].closed, 1);
  assert.equal(env.timers.pending(), 0);
  assert.equal(env.live.status().nodes, 0);
  assert.equal(env.live.node(30), null);
  // handler 摘掉了：这条已经作废的连接再晚到一帧也改不了页面上的任何一句话。
  assert.equal(env.net.sockets[0].onmessage, null);
  assert.equal(env.net.sockets[0].onclose, null);
  env.net.sockets[0].sent(samplesFrame());
  assert.equal(env.live.status().nodes, 0);
});

test("自己关的连接不会再变成「故障重连」：handler 已经摘掉，晚到的 close 事件落不到状态机上", () => {
  const env = connected();
  const socket = env.net.sockets[0];
  env.live.close();
  socket.dropped(1000, "正常收场");
  assert.equal(env.timers.pending(), 0, "自己关的连接，重连定时器属于幽灵账");
  assert.equal(env.live.status().status, STATUS.CLOSED);
});

// --- 快照与增量 ---------------------------------------------------------------

test("快照把 last 帧与事件窗一起端上来：连上之后不用等下一帧就有数", () => {
  const env = connected();
  env.net.sockets[0].sent({
    kind: "snapshot",
    site_id: 1,
    ts: "2026-10-29T04:00:00+00:00",
    samples: [samplesFrame(), samplesFrame({ node_id: 31, display_name: "板 R2", source: "mock" })],
    events: [eventFrame(7), eventFrame(8, { kind: "node_online" })],
  });
  const status = env.live.status();
  assert.equal(status.snapshots, 1);
  assert.equal(status.nodes, 2);
  assert.equal(status.events, 2);
  assert.deepEqual(env.live.sources(), ["mock", "real"]);
  assert.equal(env.live.node(31).source, "mock", "回放帧的来历必须跟着数据，不是跟着页面上的开关");
});

test("快照是「现在」而不是「追加」：两次快照之间消失的板子必须一起消失", () => {
  const env = connected();
  const socket = env.net.sockets[0];
  socket.sent({ kind: "snapshot", site_id: 1, samples: [samplesFrame(), samplesFrame({ node_id: 31 })], events: [] });
  socket.sent({ kind: "snapshot", site_id: 1, samples: [samplesFrame()], events: [] });
  assert.equal(env.live.status().nodes, 1);
  assert.equal(env.live.node(31), null, "旧快照里的板子留在页面上，就是替已经不存在的板子报数");
});

test("增量帧按档位合并，每一档留自己的时刻：跳频下两帧各报几个档", () => {
  const env = connected();
  const socket = env.net.sockets[0];
  socket.sent(samplesFrame({ seq: 20, channels: [point(0, { rssi: -80 }), point(1, { rssi: -95 })] }));
  socket.sent(
    samplesFrame({
      seq: 21,
      ts: "2026-10-29T04:00:02+00:00",
      channels: [point(2, { rssi: -70, ts: "2026-10-29T04:00:02+00:00" })],
    })
  );
  const row = env.live.node(30);
  assert.deepEqual(env.live.slotsOf(30), [0, 1, 2], "档位来自帧，不补 0..7");
  assert.equal(row.seq, 21);
  assert.equal(row.frames, 2);
  assert.equal(env.live.pointOf(30, 0).rssi, -80, "这一帧没报的第 0 档要留着上一帧的值");
  assert.equal(env.live.pointOf(30, 0).at, T0, "每档自己的时刻：不能全被帧头时刻抹平");
  assert.equal(env.live.pointOf(30, 2).at, T0 + 2000);
});

test("node_id 缺失的帧不进表：没有主体就没有读数", () => {
  const env = connected();
  env.net.sockets[0].sent(samplesFrame({ node_id: null }));
  assert.equal(env.live.status().nodes, 0);
});

test("板子表有顶：挤掉的是最久没来的那一块，不是随机一块", () => {
  const env = harness({ nodeCap: 2 });
  env.live.attach(1);
  const socket = env.net.sockets[0];
  socket.opened();
  socket.sent(samplesFrame({ node_id: 30, ts: "2026-10-29T04:00:00+00:00" }));
  socket.sent(samplesFrame({ node_id: 31, ts: "2026-10-29T04:00:01+00:00" }));
  socket.sent(samplesFrame({ node_id: 32, ts: "2026-10-29T04:00:02+00:00" }));
  assert.equal(env.live.status().nodes, 2);
  assert.equal(env.live.node(30), null, "最久没来的那块该被挤掉");
  assert.ok(env.live.node(31) && env.live.node(32));
});

test("事件窗有顶，并且挤掉的最旧：200 条是这一窗的全部真相，页面上要能看见这个数", () => {
  const env = harness({ eventCap: 3 });
  env.live.attach(1);
  const socket = env.net.sockets[0];
  socket.opened();
  for (let id = 1; id <= 5; id += 1) {
    socket.sent(eventFrame(id, { ts: `2026-10-29T04:00:0${id}+00:00` }));
  }
  assert.deepEqual(env.live.state.feed.map((row) => row.event_id), [5, 4, 3]);
  // 两个数不是一件事：`status().events` 是窗里现在躺着几条（页面上"实时窗 N 条"读它），
  // `state.events` 是这一页一共收下过几条（挤出去的也算），出窗的那两条归 REST，不归这里。
  assert.equal(env.live.status().events, 3);
  assert.equal(env.live.state.events, 5, "窗里剩 3 条，但「收到过 5 条」是另一笔账");
  // 比窗里最旧一条还旧的记录回不来：它排到末尾就立刻被挤出。出窗的历史归 REST，不归这里。
  socket.sent(eventFrame(1, { ts: "2026-10-29T04:00:01+00:00" }));
  assert.deepEqual(env.live.state.feed.map((row) => row.event_id), [5, 4, 3]);
});

// --- 事件窗：两条到达路撞同一条记录 ------------------------------------------

test("REST 回填与 WS 推送撞到同一条记录：按 event_id 去重，不加第二条", () => {
  const env = connected();
  env.live.backfill([eventRow(9, { ts: "2026-10-29T04:00:00+00:00" })]);
  env.net.sockets[0].sent(eventFrame(9, { ts: "2026-10-29T04:00:00+00:00" }));
  assert.equal(env.live.state.feed.length, 1);
  assert.equal(env.live.status().events, 1);
});

test("回填的一整批更老的记录不许压在最前面：列表唯一的读法是自上而下时间递减", () => {
  const env = connected();
  env.net.sockets[0].sent(eventFrame(10, { ts: "2026-10-29T04:05:00+00:00" }));
  env.live.backfill([
    eventRow(11, { ts: "2026-10-29T04:03:00+00:00" }),
    eventRow(12, { ts: "2026-10-29T04:01:00+00:00" }),
  ]);
  assert.deepEqual(env.live.state.feed.map((row) => row.event_id), [10, 11, 12]);
});

test("回填里别在场地的行当场丢掉，并且单独记一笔：服务端那道分桶是判据，这是第二道", () => {
  const env = connected();
  const added = env.live.backfill([eventRow(13, { site_id: 2 }), eventRow(14, { site_id: 1 })]);
  assert.equal(added, 1);
  assert.equal(env.live.state.feed.map((row) => row.event_id).join(), "14");
  assert.equal(env.live.status().cross_site_rows, 1);
  assert.equal(env.live.status().unknown_frames, 0, "跨场地行是 REST 的账，不该混进「WS 里认不出的帧」");
});

test("换场地时那一窗脏账一起清：B 场地的页面里不许留着 A 场地的最后一条", () => {
  const env = connected();
  const first = env.net.sockets[0];
  first.sent(samplesFrame());
  first.sent(eventFrame(15));
  env.live.backfill([eventRow(16, { site_id: 2 })]);
  assert.equal(env.live.status().cross_site_rows, 1);

  env.live.attach(2);
  assert.equal(env.net.sockets.length, 2);
  assert.equal(env.net.sockets[1].url, "ws://192.168.99.111:8000/ws/live?site=2");
  const status = env.live.status();
  assert.equal(status.nodes, 0, "快照还在路上，这段时间页面是空的，不是「还是刚才那些」");
  assert.equal(status.events, 0);
  assert.equal(status.frames, 0);
  assert.equal(status.cross_site_rows, 0);
  assert.equal(status.last_frame_at, null);
  assert.equal(status.status, STATUS.CONNECTING);
  // 连接级的账跟着整页走，不跟着场地清：它说的是"这一页一共发生过几次连接"。
  assert.equal(status.opened, 1);
});

test("同一场地重订不清场地级的账：只有真换了场地才清", () => {
  const env = connected();
  env.net.sockets[0].sent(samplesFrame());
  env.net.sockets[0].dropped(1006, "");
  env.timers.fire();
  env.live.attach(1);
  assert.equal(env.live.status().nodes, 1);
});

// --- 新鲜度 -------------------------------------------------------------------

test("判过期用注入的时钟：帧本身没坏，只是不新鲜了，页面要说「不新鲜」而不是「没数据」", () => {
  const env = connected();
  env.net.sockets[0].sent(samplesFrame({ ts: "2026-10-29T04:00:00+00:00" }));
  assert.equal(env.live.stale(env.live.node(30)), false);
  assert.equal(env.live.ageS(env.live.node(30)), 0);
  env.time.advance(20_000);
  assert.equal(env.live.stale(env.live.node(30)), true);
  assert.equal(env.live.ageS(env.live.node(30)), 20);
  assert.equal(env.live.status().stale_after_s, 15);
});

test("没有读数就是 null，不是 0：「0 秒前」在页面上是一句假话", () => {
  const env = connected();
  assert.equal(env.live.ageS(null), null);
  assert.equal(env.live.ageS({ ts: null }), null);
  assert.equal(env.live.stale(null), true, "没数就当不新鲜，而不是当新鲜");
});

test("不带偏移的 ISO 串按 UTC 折：naive 串交给 Date.parse 会被本机时区解释", () => {
  const env = connected();
  env.net.sockets[0].sent(samplesFrame({ ts: "2026-10-29T04:00:00" }));
  assert.equal(env.live.node(30).ts, T0, "历史行读回来是 naive 的：这一格差几个小时就全错了");
});

// --- 认不出的帧 ---------------------------------------------------------------

test("认不出的帧类型只记账不崩页：服务端将来加一种帧，这一页继续画它认得的", () => {
  const env = connected();
  env.net.sockets[0].sent(samplesFrame());
  env.net.sockets[0].sent({ kind: "spectrum", site_id: 1 });
  env.net.sockets[0].sent("这不是 JSON");
  env.net.sockets[0].sent("null");
  assert.equal(env.live.status().unknown_frames, 1);
  assert.equal(env.live.status().bad_frames, 2);
  assert.equal(env.live.status().nodes, 1, "坏帧不许把已经进来的数冲掉");
  assert.equal(env.live.status().status, STATUS.OPEN);
});

test("串场地的 WS 帧当场丢弃：分桶在服务端做，但这一页只该看到订的那个场地", () => {
  const env = connected();
  env.net.sockets[0].sent(samplesFrame({ site_id: 2 }));
  assert.equal(env.live.status().nodes, 0);
  assert.equal(env.live.status().unknown_frames, 1);
});

// --- 状态栏那一段 -------------------------------------------------------------

test("dataLabel：来源与连接一起说，因为它们要一起看才有意义", () => {
  const env = connected();
  assert.equal(env.live.dataLabel(), "无来源 · 已连接 · 还没有帧");
  env.net.sockets[0].sent(samplesFrame());
  env.net.sockets[0].sent(samplesFrame({ node_id: 31, source: "mock" }));
  assert.equal(env.live.dataLabel(), "MOCK+真实 · 已连接 · 2 块板");
  assert.equal(env.live.dataTone(), "ok");
});

test("dataLabel 认不出的来源原样带出来：显示成「真实」就是替没见过的东西作保", () => {
  const env = connected();
  env.net.sockets[0].sent(samplesFrame({ source: "telemetry" }));
  assert.match(env.live.dataLabel(), /未知\(telemetry\)/);
});

test("断线那一段说的是重连到第几次，不是「未连接」了事", () => {
  const env = connected();
  env.net.sockets[0].dropped(1006, "");
  assert.equal(env.live.statusLine(), "重连中（第 1 次）");
  assert.equal(env.live.dataTone(), "warn");
  env.timers.fire();
  assert.equal(env.live.statusLine(), "连接中", "退避已经排完、正在拨的那一次要单独说，不能停在「重连中（第 1 次）」");
  assert.equal(env.live.dataTone(), "muted");
});

test("未选场地时状态栏说的是「未选场地」，不是「已断开」", () => {
  const env = harness();
  assert.equal(env.live.statusLine(), "未选场地");
  assert.equal(env.live.dataTone(), "muted");
});
