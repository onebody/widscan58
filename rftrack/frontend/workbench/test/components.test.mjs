/**
 * 组件渲染单测。**不开浏览器**：无构建形态下 Vue 是由 `index.html` 注入的全局量，
 * 所以组件工厂的 `rt` 是从参数拿的——这里传一个假 `h`，树形与文案就能直接断言。
 *
 * S6 起这里的数据源换了两样：
 *
 * - 四格实时组件读的是**真的 `createLive`**，喂进去的是 `test/support/live-fixture.mjs`
 *   那套假 socket/假时钟。于是"页面画的这一档"与"后端环里的那一格"走的是同一条合并路，
 *   组件测试里改形状会立刻红，而不是等浏览器那一轮才发现读错了键。
 * - 趋势与事件的历史读的是 `GET /api/samples/summary` 与 `GET /api/events` 的桩，
 *   响应形状照抄后端 `routers/samples.py` / `routers/events.py`。
 *
 * 钉的都是规范里那几条「看得见才算做到」的判据：MOCK 角标在不在、四态各说什么、
 * 不支持的话有没有说、有没有冒出距离/坐标、联动是不是只传频道号。
 *
 * 为什么值得为文案写测试：这些句子是唯一能让运营者分辨「录的 vs 实时」「有阈值 vs 没阈值」
 * 的东西。改结构的人看不见这句话的分量，改着改着就没了；只有断言能把它留住。
 */

import assert from "node:assert/strict";
import test from "node:test";

import * as registry from "../registry.js";
import { IMPLEMENTED, build, declaredWithoutImpl, implWithoutDeclared } from "../components/index.js";
import { connected, eventFrame, eventRow, point, samplesFrame } from "./support/live-fixture.mjs";

// --- 假 Vue -------------------------------------------------------------------

function fakeRt() {
  const h = (tag, props, children) => {
    if (Array.isArray(props) || typeof props === "string" || typeof props === "number") {
      children = props;
      props = {};
    }
    return { tag, props: props || {}, children: children === undefined ? null : children };
  };
  const hooks = { mounted: [], updated: [] };
  const refs = [];
  return {
    h,
    /** 记下每个 `ref`：绘制路径要能把画布"挂"上去，否则 `paint()` 永远走 early return，测不到。 */
    ref: (value) => { const r = { value }; refs.push(r); return r; },
    computed: (fn) => ({ get value() { return fn(); } }),
    onMounted: (fn) => hooks.mounted.push(fn),
    onUpdated: (fn) => hooks.updated.push(fn),
    nextTick: (fn) => { if (fn) fn(); return Promise.resolve(); },
    watchEffect: (fn) => fn(),
    hooks,
    refs,
  };
}

/** 只记录调用序列的 2D 上下文：够 `chart.draw()` 跑完，不需要真画像素。 */
function fakeCanvas({ w = 240, h = 120 } = {}) {
  const ops = [];
  const ctx = new Proxy({ ops }, {
    get(target, key) {
      if (key in target) return target[key];
      if (key === "canvas") return { width: w, height: h };
      return (...args) => { ops.push([key, ...args]); };
    },
    set(target, key, value) { target[key] = value; return true; },
  });
  return { clientWidth: w, clientHeight: h, style: {}, getContext: () => ctx, ops };
}

/** `paint()` 会从样式表取色；这是浏览器才有的那一格，Node 里给一个空实现即可（取色有 fallback）。 */
if (!globalThis.getComputedStyle) {
  globalThis.getComputedStyle = () => ({ getPropertyValue: () => "" });
}

const walk = (node) => {
  if (node === null || node === undefined) return [];
  if (Array.isArray(node)) return node.flatMap(walk);
  if (typeof node !== "object" || !node.tag) return [];
  return [node, ...walk(node.children)];
};

/** 把树上所有文字拼起来：文案判据（不是频谱、系统不提供）就在这里断。 */
const textOf = (node) => {
  const parts = [];
  const visit = (value) => {
    if (typeof value === "string" || typeof value === "number") parts.push(String(value));
    else if (Array.isArray(value)) value.forEach(visit);
    else if (value && typeof value === "object") visit(value.children);
  };
  visit(node);
  return parts.join(" ");
};

const hasClass = (node, name) => {
  const value = node.props.class;
  if (typeof value === "string") return value.split(/\s+/).includes(name);
  if (Array.isArray(value)) return value.filter(Boolean).some((part) => part.split(/\s+/).includes(name));
  return false;
};

const findByClass = (root, name) => walk(root).filter((node) => hasClass(node, name));

function clickNode(root, matcher) {
  const target = walk(root).find((node) => node.props && matcher(node));
  assert.ok(target, "要点的节点没找到：结构变了，判据也就断了");
  assert.equal(typeof target.props.onClick, "function", "这个节点没有 onClick，等于点不动");
  target.props.onClick({ target: { checked: true, value: "" }, stopPropagation() {}, preventDefault() {} });
  return target;
}

/**
 * 「不许出现 X」这类判据不能整棵树一刀切：规范要求的**否定句本身**就带着那些字
 * （距离/方向/坐标：RSSI 推不出这些）。所以先把那句合法的否定话挖掉，再断剩下的里面
 * 一个都不许有——这样既放得过诚实的声明，也拦得住哪天冒出来的「约 12 米」。
 */
const withNegationsRemoved = (text) => {
  const allowed = ["距离/方向/坐标：RSSI 推不出这些，系统不提供"];
  return allowed.reduce((acc, sentence) => acc.split(sentence).join("〔已声明不提供〕"), text);
};

/** 扫一遍整棵树：本系统能给的是电平与时间，任何距离/坐标/轨迹的字眼都是假话。 */
function assertNoInference(node) {
  const stripped = withNegationsRemoved(textOf(node));
  for (const banned of ["距离", "坐标", "方位", "米", "轨迹", "纬度", "经度"]) {
    assert.equal(stripped.includes(banned), false, `不该出现「${banned}」：${stripped}`);
  }
}

// --- 假依赖 -------------------------------------------------------------------

function fakeLease() {
  const timers = [];
  const hooks = [];
  const subs = [];
  return {
    timers,
    hooks,
    setTimeout(fn, ms) { timers.push({ fn, ms, kind: "timeout" }); return timers.length; },
    setInterval(fn, ms) { timers.push({ fn, ms, kind: "interval" }); return timers.length; },
    clearTimeout() {},
    subscribe(topic, handler) { subs.push({ topic, handler }); return () => subs.splice(subs.findIndex((row) => row.handler === handler), 1); },
    onRelease(fn) { hooks.push(fn); return () => hooks.splice(hooks.indexOf(fn), 1); },
    live: () => timers.length + subs.length,
    release() { hooks.forEach((fn) => fn()); timers.length = 0; subs.length = 0; },
  };
}

const fakeWidget = (pluginId, overrides = {}) => ({
  instance_id: `${pluginId}-1`,
  plugin_id: pluginId,
  x: 0, y: 0, w: 6, h: 4,
  sort: 0,
  config: {},
  server_config: {},
  effective: {},
  declaration_error: null,
  missing: false,
  isNew: false,
  lease: fakeLease(),
  ...overrides,
});

const emptyContext = {
  sites: [], relays: [], gateways: [], nodes: [], scopeNodes: [], scopeGateways: [],
  siteId: null, relayId: null, gatewayId: null, nodeId: null,
};

const ONLINE_BOARD = {
  node_id: 30, gateway_id: 20, site_id: 1, display_name: "板 R1", status: "active", stops_rh: true,
  transport: { kind: "uart", addr: "/dev/ttyAMA0" }, fw_version: "0.6-dev", protocol_version: 6,
  last_seen: "2026-10-29T04:00:00", declaration_error: null,
  // 形状照抄后端 `services/capabilities.py`：上限是**平铺**在能力对象上的
  // （`rendered[name] = {"supported": True, **limits}`），根本没有 `limits` 子对象。
  // 上一版这里凭空造了嵌套形状，组件读错也照样绿——浏览器那一轮才把"上限丢失"抓出来。
  capabilities: {
    levels: { supported: true },
    spectrum: { supported: true, max_bins: 512, paced_rotation_hz: 3.5 },
    rawiq: { supported: false, reason: "undeclared" },
  },
};

/** 一块没声明电平帧的板：四态里的 `unsupported` 只有它配得上。 */
const NO_LEVELS_BOARD = { ...ONLINE_BOARD, node_id: 34, display_name: "板 R9", capabilities: { levels: { supported: false, reason: "undeclared" } } };

const withBoards = (nodes, gateway) => ({
  contextState: { ...emptyContext, nodes, scopeNodes: nodes, gatewayId: gateway ? gateway.gateway_id : null, nodeId: nodes.length ? nodes[0].node_id : null },
  gateway,
});

// --- REST 桩：形状照抄 routers/{samples,events}.py ----------------------------

/** 摘要桶的一行：键与 `services/history.py::summary_row` 一一对。 */
const summaryRow = (overrides = {}) => ({
  ts_bucket: 1_764_172_800,
  node_id: 30,
  slot: 1,
  gateway_id: 20,
  bucket_s: 1,
  sample_count: 5,
  ema_count: 5,
  rssi_mean: -80.5,
  rssi_min: -88.0,
  rssi_max: -75.0,
  rssi_last: -79.0,
  ema_mean: -81.2,
  ema_min: null,
  ema_max: null,
  ema_last: null,
  source: "real",
  ...overrides,
});

const summaryAnswer = (rows, overrides = {}) => ({
  site_id: 1,
  bucket: 1,
  bucket_widths: [1],
  allowed_buckets: [1],
  range: { from: "2026-10-29T03:59:30+00:00", to: "2026-10-29T04:00:00+00:00", span_s: 30 },
  filters: { node: 30, slot: 1 },
  limit: 150,
  truncated: false,
  sources: [...new Set(rows.map((row) => row.source))].sort(),
  rows,
  ...overrides,
});

/** 一条 `GET /api/events/{id}` 的详情：`as_out(with_limitations=True)`。 */
const detailAnswer = (id, overrides = {}) => ({
  ...eventRow(id, overrides),
  limitations: [
    "这是采集层写下的观测事实，不是判决：它只说这一段时间里进过什么。",
    "安静只意味着这段时间里没有帧进来：页面关掉、桥重启都会这样，不等于板子断电",
  ],
});

function fakeApi(options) {
  const calls = [];
  const settle = (reply) => (reply && reply.__error ? Promise.reject({ reason: reply.__error, message: "桩：读数失败" }) : Promise.resolve(reply));
  const pick = (value, params, index) => (typeof value === "function" ? value(params, index) : value);
  return {
    calls,
    samplesSummary: (params) => {
      calls.push({ name: "samplesSummary", args: [params] });
      return settle(pick(options.summary ?? summaryAnswer([]), params, calls.length));
    },
    eventsFeed: (params) => {
      calls.push({ name: "eventsFeed", args: [params] });
      const rows = typeof options.events === "function" ? options.events(params) : options.events ?? [];
      return settle({
        site_id: params.site,
        range: { from: "2026-10-29T03:00:00+00:00", to: "2026-10-29T04:00:00+00:00", span_s: 3600 },
        filters: { node: null, kind: null },
        limit: rows.length,
        truncated: false,
        sources: [...new Set(rows.map((row) => row.source))].sort(),
        rows,
      });
    },
    eventDetail: (eventId, site = null) => {
      calls.push({ name: "eventDetail", args: [eventId, site] });
      return settle(pick(options.detail ?? detailAnswer(eventId), { eventId, site }, calls.length));
    },
    audit: () => ({ calls: [], dropped: 0 }),
  };
}

/**
 * 一套依赖：`live` 是真的 `createLive`（假 socket/假时钟），REST 是桩，动作是记录器。
 *
 * `frames` 在连接打开之后按顺序喂进去，`drop` 在那之后把连接打掉——四态里的
 * `offline`/`stale` 就是靠这两步在同一个夹具里切出来的。
 */
function harness(options = {}) {
  const calls = [];
  const actions = new Proxy({}, {
    get(_target, name) {
      if (typeof name !== "string") return undefined;
      return (...args) => { calls.push({ name, args }); };
    },
  });
  const boards = options.boards ?? [ONLINE_BOARD];
  const store = {
    state: { link: options.link ?? null, widgets: [], selection: null },
    effectiveOf: (widget, key) => (options.effective || {})[key] ?? null,
  };
  const context = {
    state: {
      ...emptyContext,
      nodes: boards,
      scopeNodes: boards,
      siteId: 1,
      gatewayId: boards.length ? boards[0].gateway_id ?? null : null,
      nodeId: boards.length ? boards[0].node_id : null,
      ...(options.contextState || {}),
    },
    gateway: () => options.gateway ?? null,
    node: () => (options.node === undefined ? (boards.length ? boards[0] : null) : options.node),
    lag: () => options.lag ?? null,
  };
  const env = connected({ site: options.site ?? 1, staleAfterS: options.staleAfterS ?? 15 });
  for (const frame of options.frames ?? []) env.net.sockets[0].sent(frame);
  if (options.drop) env.net.sockets[0].dropped(options.drop, options.dropReason ?? "");
  const api = fakeApi(options);
  const deps = { store, context, live: env.live, actions, signal: { tick: { value: 0 } }, registry, api };
  const rt = fakeRt();
  return {
    rt, deps, actions, calls, api, store, context,
    live: env.live, time: env.time, timers: env.timers, socket: env.net.sockets[0], sockets: env.net.sockets,
    kit: build(rt, deps),
  };
}

/** 跑一次渲染：`setup()` 拿 props，回出来的 render 函数再调一次才是树。 */
function render(id, options = {}, widgetOverrides = {}) {
  const env = harness(options);
  const widget = fakeWidget(id, widgetOverrides);
  const component = env.kit.components[id];
  assert.ok(component, `组件 ${id} 没注册`);
  const tree = component.setup({ widget });
  return { ...env, env, widget, tree, node: tree() };
}

/** 挂上之后等一次异步读数落地再渲染：趋势与事件流的正文都由 `onMounted` 那一次取数决定。 */
async function renderLoaded(id, options = {}, widgetOverrides = {}) {
  const view = render(id, options, widgetOverrides);
  view.rt.hooks.mounted.forEach((fn) => fn());
  await new Promise((resolve) => setImmediate(resolve));
  return { ...view, node: view.tree() };
}

// --- 对账 ---------------------------------------------------------------------

test('注册表与实现一一对：少装文件或多出死代码都会被这条钉住', () => {
  assert.deepEqual(declaredWithoutImpl(), []);
  assert.deepEqual(implWithoutDeclared(), []);
  assert.deepEqual(Object.keys(IMPLEMENTED).sort(), registry.COMPONENTS.map((entry) => entry.id).sort());
});

test('认不出的 plugin_id 落到未注册占位件，而不是崩掉整页', () => {
  const { kit } = harness();
  assert.equal(kit.resolve({ plugin_id: "from-a-newer-build" }), kit.components["*unknown*"]);
  assert.equal(kit.resolve({ plugin_id: "channel-matrix" }), kit.components["channel-matrix"]);
  assert.equal(typeof kit.components["*unknown*"].setup, "function", "占位件必须也是建好的组件，否则第一次降级就崩");
});

// --- 多频道矩阵：档来自帧 -----------------------------------------------------

test('多频道矩阵：画的是帧里真报的那几档，来源角标是「真实」', () => {
  const { node } = render("channel-matrix", { frames: [samplesFrame()], effective: { refresh_ms: 200, threshold_db: -85 } });
  assert.equal(findByClass(node, "badge--real").length, 1);
  assert.equal(findByClass(node, "badge--demo").length, 0, "S5 那枚演示角标不该在任何地方复活");
  assert.equal(findByClass(node, "pill").length, 0, "读数正常时不挂状态牌子：有数就别占窄屏那一格");
  assert.match(textOf(node), /离散频道电平，不是频谱/);
  assert.match(textOf(node), /板 R1 · 模式 FULL/);
  assert.equal(walk(node).filter((row) => row.tag === "tr").length, 1 + 2, "表头 + 帧里那两档");
  assertNoInference(node);
});

test('多频道矩阵：回放帧进来自带 MOCK 角标，不用页面知道"此刻在放录影"', () => {
  const { node } = render("channel-matrix", { frames: [samplesFrame({ source: "mock" })], effective: { refresh_ms: 200 } });
  assert.equal(findByClass(node, "badge--mock").length, 1);
  assert.equal(textOf(findByClass(node, "badge--mock")[0]), "MOCK");
  assert.equal(findByClass(node, "badge--real").length, 0);
});

test('多频道矩阵：LO 是整帧的，写在元信息里；没有频道方案表就不逐行报频率', () => {
  const { node } = render("channel-matrix", { frames: [samplesFrame({ channels: [point(0), point(1)] })], effective: { refresh_ms: 200 } });
  assert.match(textOf(node), /帧 LO 5658\.0 MHz（5658000 kHz）/);
  const rows = walk(node).filter((row) => row.tag === "tr").slice(1);
  assert.equal(rows.every((row) => textOf(row).includes("MHz")), false, "档位行里不许长出一个 MHz：那一列要的是频道方案表，后端还没有");
});

test('多频道矩阵：配置里的 slots 只能筛掉帧里的档，不能凭空加行', () => {
  const { node } = render("channel-matrix", {
    frames: [samplesFrame({ channels: [point(0)] })],
    effective: { refresh_ms: 200, slots: [0, 1] },
  });
  assert.equal(walk(node).filter((row) => row.tag === "tr").length, 1 + 1, "只有第 1 档真进过帧，第二行不许出现");
  assert.match(textOf(node), /配置里的 2 档还没进帧/);
});

test('多频道矩阵：没设阈值就直说不判，不拿 −85 兜底也不报假的活动态', () => {
  const { node } = render("channel-matrix", { frames: [samplesFrame()], effective: { refresh_ms: 200 } });
  const text = textOf(node);
  assert.match(text, /未设阈值/);
  assert.equal(/阈值\s*-?\d/.test(text), false, "页面里不许凭空长出一个阈值数");
  assert.match(text, /未判/, "活动列要能说「这一格没判」");
  assert.equal(findByClass(node, "dot--idle").length, 0, "没判过 ≠ 判过且静默");
});

test('多频道矩阵：活动列按帧里的 RSSI 与阈值比出来的结果画，两个态各一个点', () => {
  const { node } = render("channel-matrix", {
    frames: [samplesFrame({ channels: [point(0, { rssi: -88.4 }), point(1, { rssi: -70 })] })],
    effective: { refresh_ms: 200, threshold_db: -85 },
  });
  assert.equal(findByClass(node, "dot--idle").length, 1);
  assert.equal(findByClass(node, "dot--active").length, 1);
  assert.equal(findByClass(node, "num--active").length, 1, "超阈值那一档的数字要加重，扫一眼就找得到");
});

test('多频道矩阵：帧里一个档都没有时说清为什么，而不是画一张空表糊过去', () => {
  const { node } = render("channel-matrix", { frames: [samplesFrame({ channels: [] })], effective: { refresh_ms: 200 } });
  assert.equal(findByClass(node, "empty").length, 1);
  assert.match(textOf(node), /这一帧里没有档位/);
  assert.match(textOf(node), /帧头报的通道数是 0/);
});

test('多频道矩阵：点一行就是联动，传的是频道号而不是结论', () => {
  const { env, node } = render("channel-matrix", { frames: [samplesFrame()], effective: { refresh_ms: 200 } });
  clickNode(node, (rowNode) => hasClass(rowNode, "clickable"));
  assert.deepEqual(env.calls, [{ name: "linkChannel", args: [0] }]);
});

// --- 四态：无数据 / 过期 / 断线 / unsupported --------------------------------

test('四态·没有主体：四级里没选中板时说的是「没有选中的板」', () => {
  const { node } = render("channel-matrix", { frames: [samplesFrame()], contextState: { nodeId: null } });
  assert.equal(findByClass(node, "pill--no_board").length, 1);
  assert.match(textOf(node), /这一级里没有选中的板/);
  assert.equal(walk(node).filter((row) => row.tag === "tr").length, 0, "没有主体就不摆一张有列头的表");
});

test('四态·unsupported：连接是好的、板子没声明电平帧，就直说板端原话', () => {
  const { node } = render("channel-matrix", { boards: [NO_LEVELS_BOARD], effective: { refresh_ms: 200 } });
  assert.equal(findByClass(node, "pill--unsupported").length, 1);
  assert.match(textOf(node), /这块板没声明电平帧能力（板端未声明）/);
  assert.equal(findByClass(node, "badge--real").length, 0, "没有帧就没有来源，角标不许凭空亮");
});

test('四态·空：声明了电平、连接正常，但这一拍确实没有这块板的帧', () => {
  const { node } = render("channel-matrix", { effective: { refresh_ms: 200 } });
  assert.equal(findByClass(node, "pill--empty").length, 1);
  assert.match(textOf(node), /还没有这块板的帧/);
});

test('没有帧时来源角标说「未取数」：来源未知是"有一份来路不明的数"，这里根本没有数', () => {
  for (const plugin of ["channel-matrix", "channel-detail"]) {
    const { node } = render(plugin, { effective: { refresh_ms: 200 } });
    const badge = findByClass(node, "badge--none");
    assert.equal(badge.length, 1, `${plugin} 没有帧时该有一枚「未取数」角标`);
    assert.equal(textOf(badge[0]), "未取数", `${plugin} 那枚角标说的是「${textOf(badge[0])}」`);
    assert.equal(findByClass(node, "badge--unknown").length, 0, `${plugin} 把没有数渲染成了来路不明`);
  }
});

test('四态·牌子与正文各司其职：牌子上是短标签，整句在一块格子里只出现一次', () => {
  const { node } = render("channel-matrix", { boards: [NO_LEVELS_BOARD], effective: { refresh_ms: 200 } });
  assert.equal(textOf(findByClass(node, "pill--unsupported")[0]), "不支持");
  const text = textOf(node);
  assert.equal(text.split("这块板没声明电平帧能力").length - 1, 1, `整句被印了两遍（牌子一遍、正文一遍）：${text}`);
  assert.match(text, /这块板没声明电平帧能力（板端未声明），这一格没有可画的数据/);
});

test('四态·过期的判据那句在正文里，不在牌子里：数还在画，也得说清距今几秒', () => {
  const { tree, time } = render("channel-matrix", { frames: [samplesFrame()], effective: { refresh_ms: 200 } });
  time.advance(20_000);
  const again = tree();
  assert.equal(textOf(findByClass(again, "pill--stale")[0]), "过期");
  const note = findByClass(again, "note--stale");
  assert.equal(note.length, 1, "过期时正文里要有那句判据：没有它，牌子上那两个字不足以判断是板子安静了还是页面读得慢");
  assert.match(textOf(note[0]), /最后一帧距今 20 s，判据是 15 s/);
});

test('四态·断线：没帧且推送断了，那句"断了"要跟着状态机的那句话', () => {
  const { node } = render("channel-matrix", { drop: 1006, effective: { refresh_ms: 200 } });
  assert.equal(findByClass(node, "pill--offline").length, 1);
  assert.match(textOf(node), /推送断了：重连中（第 1 次）/);
});

test('四态·过期：最后一帧还在画，但牌子要说它距今几秒、判据是多少秒', () => {
  const { tree, time } = render("channel-matrix", { frames: [samplesFrame()], effective: { refresh_ms: 200 } });
  time.advance(20_000);
  const again = tree();
  assert.equal(findByClass(again, "pill--stale").length, 1);
  assert.match(textOf(again), /最后一帧已经不新鲜：最后一帧距今 20 s，判据是 15 s/);
  assert.equal(walk(again).filter((row) => row.tag === "tr").length, 1 + 2, "过期不等于把数擦掉");
});

test('有数就不说断线：帧留着、连接刚断，这一格仍画最后一帧并说它不新鲜还是说连接', () => {
  const { tree, socket } = render("channel-matrix", { frames: [samplesFrame()], effective: { refresh_ms: 200 } });
  socket.dropped(1006, "");
  const again = tree();
  assert.equal(findByClass(again, "pill").length, 0, "读数还新鲜时不挂牌子：连接那句话由状态栏那一行常驻说");
  assert.match(textOf(again), /重连中（第 1 次）/, "元信息里仍要看得见连接态");
});

// --- 单频道详情：帧里报了什么就摆什么 ------------------------------------------

test('单频道详情：把这一档真报的量摆开，报不出的写成看得见的三条说明', () => {
  const { node } = render("channel-detail", {
    frames: [samplesFrame({ channels: [point(1, { peakOffHz: 123, centroidOffHz: -45, obwLoHz: 5650000, obwHiHz: 5665000 })] })],
    link: { kind: "channel", id: 1 },
    effective: { refresh_ms: 200, threshold_db: -90 },
  });
  const text = textOf(node);
  assert.match(text, /2 档/);
  assert.match(text, /-88\.4 dBFS/);
  assert.match(text, /\+123 Hz/);
  assert.match(text, /−45 Hz/);
  assert.match(text, /高于阈值/, "RSSI 高于阈值；不高于的时候下面那条说的是低于");
  assert.match(text, /阈值 -90\.0 dBFS/);
  assert.match(text, /04:00:00/, "更新时间来自这一档自己的时刻");
  const absent = findByClass(node, "absent")[0];
  assert.equal(walk(absent).filter((row) => row.tag === "li").length, 3);
  assert.match(textOf(absent), /这一档的中心频率/);
  assert.match(textOf(absent), /连续频谱/);
  assert.match(textOf(absent), /距离\/方向\/坐标：RSSI 推不出这些，系统不提供/);
  assertNoInference(node);
});

test('单频道详情：空值一律画 —，不画成 0', () => {
  const { node } = render("channel-detail", {
    frames: [samplesFrame({ channels: [point(0, { rssiEma: null, ema: null, snr: null, peak: null, clearFloor: null })] })],
    effective: { refresh_ms: 200 },
  });
  const values = findByClass(node, "field").map((field) => textOf(field));
  assert.equal(values.filter((row) => /EMA —/.test(row)).length, 1);
  assert.equal(values.filter((row) => /SNR —/.test(row)).length, 1);
  assert.equal(values.some((row) => /0\.0 dBFS/.test(row)), false, "把没测写成 0 dBFS 是这一层最贵的假话");
});

test('单频道详情：没有联动时先回落配置的第一档，再回落帧里的第一档', () => {
  const fieldText = (node, label) => findByClass(node, "field").map(textOf).find((text) => text.startsWith(label));
  const byConfig = render("channel-detail", { frames: [samplesFrame()], effective: { refresh_ms: 200, slots: [1] } });
  assert.equal(fieldText(byConfig.node, "频道"), "频道 2 档");
  const byFrame = render("channel-detail", { frames: [samplesFrame()], effective: { refresh_ms: 200 } });
  assert.equal(fieldText(byFrame.node, "频道"), "频道 1 档");
});

test('单频道详情：联动跳到没进帧的那一档时说要的那一档没进来，不拿别的档顶', () => {
  const { node } = render("channel-detail", { frames: [samplesFrame()], link: { kind: "channel", id: 7 }, effective: { refresh_ms: 200 } });
  assert.match(textOf(node), /这一帧里没有第 8 档/);
  assert.equal(findByClass(node, "field").length, 0);
});

test('单频道详情：来源角标跟着帧走，回放帧就是 MOCK', () => {
  const { node } = render("channel-detail", { frames: [samplesFrame({ source: "mock", channels: [point(0)] })], effective: { refresh_ms: 200 } });
  assert.equal(findByClass(node, "badge--mock").length, 1);
});

// --- RSSI 趋势：读的是摘要桶 --------------------------------------------------

test('RSSI 趋势：取数打的是 /api/samples/summary，参数带场地/节点/档与节拍换算来的上限', async () => {
  const { node, api } = await renderLoaded("rssi-trend", {
    link: { kind: "channel", id: 1 },
    effective: { refresh_ms: 200, time_range_s: 30 },
    summary: summaryAnswer([summaryRow()]),
  });
  const params = api.calls[0].args[0];
  assert.equal(api.calls[0].name, "samplesSummary");
  assert.deepEqual([params.site, params.node, params.slot], [1, 30, 1]);
  assert.equal(params.limit, 150, "30 s ÷ 200 ms = 150 拍");
  assert.equal(typeof params.from, "string");
  assert.match(textOf(node), /桶宽 1 s · 1 点/);
  assert.match(textOf(node), /横轴：桶起点（unix 秒）/, "x 是时间，不是频率——这是规范 §2.3 的那条");
  assert.match(textOf(node), /虚线是阈值，不是数据/);
  assertNoInference(node);
});

test('RSSI 趋势：一天的区间被点数上限封顶，库不会被这一格当节拍器', async () => {
  const { api } = await renderLoaded("rssi-trend", {
    effective: { refresh_ms: 200, time_range_s: 86_400, slots: [1] },
    summary: summaryAnswer([summaryRow()]),
  });
  assert.equal(api.calls[0].args[0].limit, 900, "一天 × 5 Hz = 43 200 拍，取上限 900");
});

test('RSSI 趋势：既没联动也没配置档位时不发请求——没有 slot 就不知道该问哪一档', async () => {
  const { api, node } = await renderLoaded("rssi-trend", { effective: { refresh_ms: 200 } });
  assert.deepEqual(api.calls, []);
  assert.match(textOf(node), /没有联动的档位/);
});

test('RSSI 趋势：轮询不快于 1 s，节拍配得再小也按上限定', () => {
  assert.equal(render("rssi-trend", { effective: { refresh_ms: 200 } }).widget.lease.timers[0].ms, 1000);
  assert.equal(render("rssi-trend", { effective: { refresh_ms: 5000 } }).widget.lease.timers[0].ms, 5000);
});

test('RSSI 趋势：空均值的桶不进折线，并且数出来', async () => {
  const { node } = await renderLoaded("rssi-trend", {
    effective: { refresh_ms: 200, slots: [1] },
    summary: summaryAnswer([summaryRow({ rssi_mean: null, sample_count: 0 }), summaryRow({ rssi_mean: -80 })]),
  });
  assert.match(textOf(node), /1 个桶没有均值（那些桶里一个点都没有），已从折线里跳过/);
  assert.match(textOf(node), /· 1 点/);
});

test('RSSI 趋势：截断要说，因为"这一屏就是全部"是读曲线的人默认相信的事', async () => {
  const { node } = await renderLoaded("rssi-trend", {
    effective: { refresh_ms: 200, slots: [1] },
    summary: summaryAnswer([summaryRow()], { truncated: true, limit: 900 }),
  });
  assert.match(textOf(node), /行数已达上限 900，只画最近这一段/);
});

test('RSSI 趋势：混合来源的桶各挂一枚角标，回放段不冒充实时段', async () => {
  const { node } = await renderLoaded("rssi-trend", {
    effective: { refresh_ms: 200, slots: [1] },
    summary: summaryAnswer([summaryRow({ source: "mock" }), summaryRow({ source: "real", ts_bucket: 1_764_172_801 })]),
  });
  assert.equal(findByClass(node, "badge--mock").length, 1);
  assert.equal(findByClass(node, "badge--real").length, 1);
});

test('RSSI 趋势：取数失败时曲线沿用上一次读数，但那句话必须挂在图上', async () => {
  let round = 0;
  const view = await renderLoaded("rssi-trend", {
    effective: { refresh_ms: 1000, slots: [1] },
    summary: () => {
      round += 1;
      return round === 1 ? summaryAnswer([summaryRow()]) : { __error: "db_locked" };
    },
  });
  view.widget.lease.timers[0].fn(); // 下一次心跳：这一次桩报错了
  await new Promise((resolve) => setImmediate(resolve));
  const text = textOf(view.tree());
  assert.match(text, /取数失败：db_locked · 曲线沿用上一次的成功读数/);
  assert.match(text, /· 1 点/, "上一答的读数没有被静默擦掉");
});

test('RSSI 趋势：取数失败且从来没有成功过时，只说失败与"第一次读数还在路上"', async () => {
  const { node } = await renderLoaded("rssi-trend", {
    effective: { refresh_ms: 200, slots: [1] },
    summary: { __error: "site_not_found" },
  });
  assert.match(textOf(node), /取数失败：site_not_found/);
  assert.equal(textOf(node).includes("沿用"), false);
  assert.match(textOf(node), /摘要桶里还没有这一档的行/);
});

test('RSSI 趋势：没有选中的板就不发请求——node_id 是这一格的先决条件', async () => {
  const { api, node } = await renderLoaded("rssi-trend", { contextState: { nodeId: null }, effective: { refresh_ms: 200, slots: [1] } });
  assert.deepEqual(api.calls, []);
  assert.match(textOf(node), /这一级里没有选中的板/);
});

test('RSSI 趋势：迟到的一答不采纳——换了场地再回来不能看见上一场地的曲线', async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const view = render("rssi-trend", {
    effective: { refresh_ms: 200, slots: [1] },
    summary: () => gate.then(() => summaryAnswer([summaryRow()], { rows: [summaryRow({ rssi_mean: -66.6 })] })),
  });
  view.rt.hooks.mounted.forEach((fn) => fn());
  view.context.state.siteId = 2; // 答案还在路上的时候切了场地
  release(null);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(textOf(view.tree()).includes("-66.6"), false);
});

// --- 事件信息流 ---------------------------------------------------------------

test('事件流：推送来的两条各说各的类型，档位为空不假装是某一档', () => {
  const { node } = render("signal-events", {
    frames: [samplesFrame(), eventFrame(7), eventFrame(8, { kind: "node_online", ts: "2026-10-29T04:00:05+00:00" })],
    effective: { refresh_ms: 200 },
  });
  const rows = findByClass(node, "event");
  assert.equal(rows.length, 2);
  assert.match(textOf(rows[0]), /节点恢复/);
  assert.match(textOf(rows[1]), /节点失联/);
  assert.equal(findByClass(node, "event-slot").length, 0, "`slot` 为空就不该出现「N 档」");
  assert.match(textOf(node), /实时窗 2 条（上限 200）/);
});

test('事件流：回放写下的那条带 MOCK 角标，与真实那条一眼分开', () => {
  const { node } = render("signal-events", {
    frames: [samplesFrame(), eventFrame(7, { source: "mock" })],
    effective: { refresh_ms: 200 },
  });
  // 两枚：元信息那一行的汇总角标 + 这一行自己的。少画任何一枚都会丢一件事——
  // 前者说"这一窗混着两种来源"，后者说"就是这一条是录的"。
  const row = findByClass(node, "event")[0];
  assert.equal(findByClass(row, "badge--mock").length, 1);
  assert.equal(findByClass(node, "badge--real").length, 0);
});

test('事件流：B 场地的推送与回填都进不了 A 场地这一格', async () => {
  const { node, live } = await renderLoaded("signal-events", {
    frames: [samplesFrame(), eventFrame(9, { site_id: 2 })],
    events: [eventRow(10, { site_id: 2 })],
    effective: { refresh_ms: 200 },
  });
  assert.equal(findByClass(node, "event").length, 0);
  assert.equal(live.status().unknown_frames, 1, "信封上写着别在场地的帧：当场丢弃并记一笔");
  assert.equal(live.status().cross_site_rows, 1, "REST 回填里的别场地行：第二道拦截");
});

test('事件流：REST 回填与推送撞到同一条记录时只画一条', async () => {
  const { node } = await renderLoaded("signal-events", {
    frames: [samplesFrame(), eventFrame(11, { ts: "2026-10-29T04:00:00+00:00" })],
    events: [eventRow(11, { ts: "2026-10-29T04:00:00+00:00" })],
    effective: { refresh_ms: 200 },
  });
  assert.equal(findByClass(node, "event").length, 1);
});

test('事件流：回填的一批更老记录排在下面，列表自上而下时间递减', async () => {
  const { node } = await renderLoaded("signal-events", {
    frames: [samplesFrame(), eventFrame(20, { ts: "2026-10-29T04:05:00+00:00" })],
    events: [eventRow(21, { ts: "2026-10-29T04:03:00+00:00" }), eventRow(22, { ts: "2026-10-29T04:01:00+00:00" })],
    effective: { refresh_ms: 200 },
  });
  assert.deepEqual(findByClass(node, "event").map(textOf).map((row) => (row.includes("04:05") ? "新" : row.includes("04:03") ? "中" : "老")), ["新", "中", "老"]);
});

test('事件流：认不出的类型原样报出来，不并进「其他」也不藏起来', () => {
  const { node } = render("signal-events", { frames: [samplesFrame(), eventFrame(30, { kind: "gear_changed" })], effective: { refresh_ms: 200 } });
  assert.match(textOf(node), /未知类型 gear_changed/);
});

test('事件流：这一格不是归档——窗顶、只画前 N 条与"更早的走 REST"三句都在', () => {
  const frames = [samplesFrame()];
  for (let id = 1; id <= 55; id += 1) {
    frames.push(eventFrame(id, { ts: `2026-10-29T04:00:${String(id % 60).padStart(2, "0")}+00:00` }));
  }
  const { node } = render("signal-events", { frames, effective: { refresh_ms: 200 } });
  const text = textOf(node);
  assert.match(text, /实时窗 55 条（上限 200）/);
  assert.match(text, /这一格只画最近 50 条/);
  assert.match(text, /其余在窗里没画/);
  assert.match(text, /更早的走 GET \/api\/events/);
  assert.equal(findByClass(node, "event").length, 50);
});

test('事件流：一条都没有时说的是"这一窗里没有事件"，安静不等于没发生', () => {
  const { node } = render("signal-events", { frames: [samplesFrame()], effective: { refresh_ms: 200 } });
  assert.match(textOf(node), /这一窗里没有事件/);
  assert.match(textOf(node), /断流与恢复才会写事件/);
});

test('事件流：点开一条才取详情，证据链与后端那句局限说明原样端上来', async () => {
  const view = await renderLoaded("signal-events", {
    frames: [samplesFrame(), eventFrame(40, { payload: { silence_s: 5.2 } })],
    events: [],
    effective: { refresh_ms: 200 },
  });
  clickNode(view.node, (row) => hasClass(row, "event"));
  await new Promise((resolve) => setImmediate(resolve));
  const text = textOf(view.tree());
  assert.deepEqual(
    view.api.calls.map((call) => call.name),
    ["eventsFeed", "eventDetail"]
  );
  assert.match(text, /"silence_s": 5\.2/, "证据链是原样贴的 JSON，不是组件的形容词");
  assert.match(text, /安静只意味着这段时间里没有帧进来/, "'能讲到哪一步'是后端说的");
  assert.match(text, /记录 40 · 场地 1/);
});

test('事件流：处置类的按钮一个都不画，唯一的动作是把那一档设为联动', async () => {
  const view = await renderLoaded("signal-events", {
    frames: [samplesFrame(), eventFrame(41, { slot: 2 })],
    // 联动按钮看的是**详情那一条**的 `slot`：详情端点才是完整记录，列表行只是摘要。
    detail: detailAnswer(41, { slot: 2 }),
    effective: { refresh_ms: 200 },
  });
  clickNode(view.node, (row) => hasClass(row, "event"));
  await new Promise((resolve) => setImmediate(resolve));
  const tree = view.tree();
  const buttons = walk(tree).filter((row) => row.tag === "button");
  assert.equal(buttons.length, 1, `本阶段事件只允许一个动作，实际 ${buttons.length} 个`);
  assert.equal(textOf(buttons[0]), "把这一档设为联动频道");
  clickNode(tree, (row) => row.tag === "button");
  assert.deepEqual(view.calls, [{ name: "linkChannel", args: [2] }]);
  const text = textOf(tree);
  for (const word of ["确认", "忽略", "指派", "清除"]) {
    assert.equal(text.includes(word), false, `出现了「${word}」：这一阶段事件是不可处置的观测`);
  }
});

test('事件流：详情取不到时把失败说出来，不把面板留成一片空白', async () => {
  const view = await renderLoaded("signal-events", {
    frames: [samplesFrame(), eventFrame(42)],
    detail: { __error: "event_not_found" },
    effective: { refresh_ms: 200 },
  });
  clickNode(view.node, (row) => hasClass(row, "event"));
  await new Promise((resolve) => setImmediate(resolve));
  assert.match(textOf(view.tree()), /详情取不到：event_not_found/);
});

// --- 实时组件（REST 那两路）---------------------------------------------------

test('节点状态：读的是 /api/nodes 的事实，角标是实时而不是 DEMO', () => {
  const gateway = { gateway_id: 20, name: "接入机甲", dial_state: "online" };
  const { node, env } = render("node-status", withBoards([ONLINE_BOARD], gateway));
  assert.equal(findByClass(node, "badge--demo").length, 0);
  assert.equal(textOf(findByClass(node, "badge--api")[0]), "实时");
  const text = textOf(node);
  assert.match(text, /板 R1/);
  assert.match(text, /在活动/);
  assert.match(text, /会停 RH/, "stops_rh 是介质属性，必须显示出来（R21）");
  assert.match(text, /支持 · 512 bins/, "有界能力要带上限，只说支持等于把有界画成无界");
  assert.match(text, /3\.5 Hz 转速/, "声明里有两个上限就得都带出来，挑一个显示等于藏起另一个界");
  assert.match(text, /不支持（板端未声明）/);
  assert.equal(text.includes("DEMO"), false);
  clickNode(node, (row) => hasClass(row, "node"));
  assert.deepEqual(env.calls, [{ name: "selectNode", args: [30] }]);
});

test('节点状态：能力声明坏了就整行按不支持渲染，并说清是哪一块板', () => {
  const broken = {
    node_id: 31, display_name: "板 R2", status: "lost", stops_rh: false,
    transport: { kind: "usb" }, fw_version: null, protocol_version: null, last_seen: null,
    declaration_error: "malformed capability",
    capabilities: { levels: { supported: false, reason: "malformed" } },
  };
  const text = textOf(render("node-status", withBoards([broken], { gateway_id: 20, name: "接入机甲", dial_state: "unreachable" })).node);
  assert.match(text, /能力声明有问题（malformed capability）/);
  assert.match(text, /已失联/);
  assert.match(text, /固件未上报/);
  assert.match(text, /协议未上报/);
  assert.match(text, /不支持（声明格式非法）/);
  assert.equal(text.includes("DEMO"), false, "实时组件不许出现演示角标");
});

test('节点状态：板端声明了本系统不认识的项，原样报出来而不是并进「不支持」', () => {
  const odd = {
    ...ONLINE_BOARD,
    node_id: 33,
    capabilities: { spectrum: { supported: null, reason: "unknown_capability", declared: { max_bins: 1024 } } },
  };
  const text = textOf(render("node-status", withBoards([odd], { gateway_id: 20, name: "接入机甲", dial_state: "online" })).node);
  assert.match(text, /本系统不认识这一项/);
  assert.equal(/不支持/.test(text), false, "supported 为 null 是「说了但不认识」，翻成「不支持」等于把板端报过的一句话改成没说过");
});

test('节点状态：未知状态原样报出来，不翻译成「在活动」', () => {
  const odd = { ...ONLINE_BOARD, node_id: 32, status: "degraded", stops_rh: false };
  const text = textOf(render("node-status", withBoards([odd], null)).node);
  assert.match(text, /degraded/);
  assert.equal(text.includes("在活动"), false);
});

test('节点状态：没有板子时空状态按接入机与拨号态分别说', () => {
  const shown = (gateway) => textOf(render("node-status", withBoards([], gateway)).node);
  assert.match(shown({ gateway_id: 20, name: "接入机甲", dial_state: "dialing" }), /拨号态：dialing/);
  assert.match(shown({ gateway_id: 20, name: "接入机甲", dial_state: "online" }), /板子插好后由后台拨号循环登记/);
  assert.match(shown(null), /当前位置里没有选中的接入机/);
});

test('场地层级树：点一层只换视图，一次写请求都不发', () => {
  const site = { site_id: 1, name: "A 号赛段", status: "active", origin: "local" };
  const relay = { relay_id: 10, name: "起点" };
  const gateway = { gateway_id: 20, relay_id: 10, name: "接入机甲", dial_state: "online" };
  const board = { node_id: 30, gateway_id: 20, display_name: "板 R1", status: "active" };
  const { node, env } = render("site-tree", {
    contextState: {
      ...emptyContext, sites: [site], relays: [relay], gateways: [gateway], nodes: [board],
      scopeGateways: [gateway], scopeNodes: [board], siteId: 1, relayId: 10, gatewayId: 20, nodeId: 30,
    },
  });
  const text = textOf(node);
  assert.match(text, /点一层就切视图；后台拨号与采集不受影响/);
  assert.match(text, /在线/);
  assert.match(text, /1 台接入机/);
  assert.match(text, /1 块板/);
  clickNode(node, (row) => hasClass(row, "tree-node--relay"));
  clickNode(node, (row) => hasClass(row, "tree-node--gateway"));
  assert.deepEqual(env.calls.map((call) => call.name), ["selectRelay", "selectGateway"]);
  const WRITE_ACTIONS = ["setConfig", "setLayoutDefault", "save", "rename", "saveAs", "add", "remove", "applyRect", "createLayout"];
  assert.deepEqual(env.calls.filter((call) => WRITE_ACTIONS.includes(call.name)), [], "切层级不该产生任何一次写");
});

test('场地层级树：汇聚场地把滞后写在树上，空库说还没有场地', () => {
  const agg = { site_id: 2, name: "外部赛段", status: "active", origin: "aggregated" };
  const { node } = render("site-tree", {
    contextState: { ...emptyContext, sites: [agg], siteId: 2 },
    lag: { label: "滞后 300 秒 · 最近同步 12:00", stale: true },
  });
  assert.match(textOf(node), /聚合视图：滞后 300 秒/);
  assert.equal(findByClass(node, "lag--stale").length, 1);
  assert.equal(findByClass(node, "chip--agg").length, 1);

  const emptyTree = render("site-tree", { contextState: emptyContext });
  assert.match(textOf(emptyTree.node), /库里还没有场地/);
});

// --- 未注册占位件 -------------------------------------------------------------

test('未注册组件：说出认不到谁，并保住位置与配置', () => {
  const { node } = render("*unknown*", {}, {
    plugin_id: "from-a-newer-build",
    x: 2, y: 3, w: 4, h: 5,
    config: { theme: "light", slots: [0, 1] },
    declaration_error: "config row broken",
    missing: true,
  });
  const text = textOf(node);
  assert.match(text, /组件未注册/);
  assert.match(text, /from-a-newer-build/);
  assert.match(text, /位置 2,3 · 4×5 与 2 项配置都保留着/);
  assert.match(text, /装上对应组件后会自动回来/);
  assert.match(text, /配置行本身也报问题：config row broken/);
});

// --- 心跳与租约 ---------------------------------------------------------------

test('心跳走 lease.setTimeout 自排：不直接碰全局定时器，否则清理无法记账', () => {
  for (const id of ["channel-matrix", "channel-detail", "signal-events", "node-status", "rssi-trend"]) {
    const { widget } = render(id, { effective: { refresh_ms: 250 } });
    assert.ok(widget.lease.timers.length >= 1, `${id} 没登记心跳`);
    assert.equal(widget.lease.timers.every((timer) => timer.kind === "timeout"), true, `${id} 用了 setInterval，节拍改了就不会跟着变`);
  }
});

test('心跳跟着生效的 refresh_ms 走：改了周期不必重建组件', () => {
  assert.equal(render("channel-matrix", { effective: { refresh_ms: 900 } }).widget.lease.timers[0].ms, 900);
  assert.equal(render("channel-matrix", { effective: {} }).widget.lease.timers[0].ms, 200, "服务端没给值时才用声明的默认节拍");
});

test('事件流的心跳不快于 500 ms：一屏事件刷太快只会糊成一片', () => {
  assert.equal(render("signal-events", { effective: { refresh_ms: 100 } }).widget.lease.timers[0].ms, 500);
});

test('节点状态自己按 2 s 节奏排：这块表跟着接入机心跳，不跟着数据节拍', () => {
  assert.equal(render("node-status", { effective: { refresh_ms: 100 } }).widget.lease.timers[0].ms, 2000);
});

test('节拍回调跑一次就多排一次：组件活着期间心跳不会自己断', () => {
  const { widget } = render("channel-matrix", { effective: { refresh_ms: 200 } });
  assert.equal(widget.lease.timers.length, 1);
  widget.lease.timers[0].fn();
  assert.equal(widget.lease.timers.length, 2);
  widget.lease.release();
  assert.equal(widget.lease.timers.length, 0, "释放之后账上不许留定时器");
});

test('趋势组件：有画布就真画一次，释放后补跑的钩子什么都不画', async () => {
  const view = render("rssi-trend", { effective: { refresh_ms: 200, time_range_s: 4, threshold_db: -80, slots: [0] }, summary: summaryAnswer([summaryRow({ slot: 0, ts_bucket: 1_764_172_800 }), summaryRow({ slot: 0, ts_bucket: 1_764_172_801, rssi_mean: -77 })]) });
  view.rt.hooks.mounted.forEach((fn) => fn());
  await new Promise((resolve) => setImmediate(resolve));
  const surface = fakeCanvas();
  view.rt.refs.find((row) => row.value === null).value = surface; // `beat` 也是 ref，按初值认画布那一个
  view.tree();
  assert.equal(view.widget.lease.hooks.length >= 1, true, "没登记释放钩子就等于没摘数据");
  const strokesWithData = surface.ops.filter(([op]) => op === "stroke").length;
  assert.ok(strokesWithData >= 3, `网格线 + 阈值线 + 折线，实际 ${strokesWithData} 次 stroke`);
  assert.equal(surface.ops.some(([op, ...args]) => op === "setLineDash" && args[0].length === 2), true, "阈值线必须是虚线：它是判据不是第 N 条曲线");

  view.widget.lease.release();
  surface.ops.length = 0;
  view.rt.hooks.updated.forEach((fn) => fn());
  view.rt.hooks.mounted.forEach((fn) => fn());
  assert.deepEqual(surface.ops, [], "格子已经没了，晚一步跑的绘制不许再往画布上落笔");
});
