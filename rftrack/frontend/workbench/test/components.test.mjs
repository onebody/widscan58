/**
 * 组件渲染单测。**不开浏览器**：无构建形态下 Vue 是由 `index.html` 注入的全局量，
 * 所以组件工厂的 `rt` 是从参数拿的——这里传一个假 `h`，树形与文案就能直接断言。
 *
 * 钉的都是规范里那几条「看得见才算做到」的判据：DEMO 角标在不在、不支持的话有没有说、
 * 有没有冒出距离/坐标、联动是不是只传频道号。
 *
 * 为什么值得为文案写测试：这些句子是唯一能让运营者分辨「演示 vs 实时」「有阈值 vs 没阈值」
 * 的东西。改结构的人看不见这句话的分量，改着改着就没了；只有断言能把它留住。
 */

import assert from "node:assert/strict";
import test from "node:test";

import * as registry from "../registry.js";
import { createDemoSource } from "../demo.js";
import { IMPLEMENTED, build, declaredWithoutImpl, implWithoutDeclared } from "../components/index.js";

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
  const allowed = [
    "距离/方向/坐标：RSSI 推不出这些，系统不提供",
    "连续频谱：本阶段无数据源（spectrum-view 标在组件库里）",
  ];
  return allowed.reduce((acc, sentence) => acc.split(sentence).join("〔已声明不提供〕"), text);
};

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

function harness(options = {}) {
  const calls = [];
  const actions = new Proxy({}, {
    get(_target, name) {
      if (typeof name !== "string") return undefined;
      return (...args) => { calls.push({ name, args }); };
    },
  });
  const store = {
    state: { link: options.link ?? null, widgets: [], selection: null },
    effectiveOf: (widget, key) => (options.effective || {})[key] ?? null,
  };
  const context = {
    state: { ...emptyContext, ...(options.contextState || {}) },
    gateway: () => options.gateway ?? null,
    node: () => options.node ?? null,
    lag: () => options.lag ?? null,
  };
  const demo = createDemoSource({ now: () => 1_730_000_000_000 });
  const deps = {
    store, context, demo, actions, signal: { tick: { value: 0 } }, registry,
    api: { audit: () => ({ calls: [], dropped: 0 }) },
  };
  const rt = fakeRt();
  return { rt, deps, actions, calls, kit: build(rt, deps), store, context, demo };
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

// --- 演示组件 -----------------------------------------------------------------

test('多频道矩阵：DEMO 角标 + 离散频道 RSSI 不是频谱那句话必须在场', () => {
  const { node } = render("channel-matrix", { effective: { slots: [0, 1, 2], threshold_db: -85, refresh_ms: 200 } });
  assert.equal(findByClass(node, "badge--demo").length, 1);
  assert.equal(textOf(findByClass(node, "badge--demo")[0]), "DEMO");
  assert.match(textOf(node), /离散频道 RSSI，不是频谱/);
  assert.equal(walk(node).filter((row) => row.tag === "tr").length, 3 + 1, "表头 + 三档");
  assert.match(textOf(node), /5658 MHz/);
  assert.match(textOf(node), /阈值 -85\.0 dBFS/);
});

test('多频道矩阵：没设阈值就直说不判，不拿 −85 兜底也不报假的活动态', () => {
  const { node } = render("channel-matrix", { effective: { slots: [0], refresh_ms: 200 } });
  const text = textOf(node);
  assert.match(text, /未设阈值/);
  assert.equal(/阈值\s*-?\d/.test(text), false, "页面里不许凭空长出一个阈值数");
  assert.match(text, /未判/, "活动列要能说「这一格没判」");
  assert.equal(findByClass(node, "dot--idle").length, 0, "没判过 ≠ 判过且静默");
});

test('多频道矩阵：slots 是空表时说清为什么空，而不是画一张空表糊过去', () => {
  const { node } = render("channel-matrix", { effective: { slots: [], refresh_ms: 200 } });
  assert.equal(findByClass(node, "empty").length, 1);
  assert.match(textOf(node), /没有可显示的档位/);
  assert.match(textOf(node), /slots 是空表/);
});

test('多频道矩阵：点一行就是联动，传的是频道号而不是结论', () => {
  const { env, node } = render("channel-matrix", { effective: { slots: [3, 4], threshold_db: -85, refresh_ms: 200 } });
  clickNode(node, (rowNode) => hasClass(rowNode, "clickable"));
  assert.deepEqual(env.calls, [{ name: "linkChannel", args: [3] }]);
});

test('单频道详情：明说这里没有连续频谱、没有距离/方向/坐标', () => {
  const { node } = render("channel-detail", { effective: { refresh_ms: 200, threshold_db: -90 }, link: { kind: "channel", id: 2 } });
  const text = textOf(node);
  assert.match(text, /连续频谱：本阶段无数据源/);
  assert.match(text, /距离\/方向\/坐标：RSSI 推不出这些，系统不提供/);
  assert.match(text, /3 档 · 5732 MHz/);
  assert.match(text, /阈值 -90\.0 dBFS/);
  assert.equal(findByClass(node, "badge--demo").length, 1);
  const stripped = withNegationsRemoved(text);
  for (const banned of ["距离", "坐标", "方位", "米", "轨迹", "纬度", "经度"]) {
    assert.equal(stripped.includes(banned), false, `不该出现「${banned}」：${stripped}`);
  }
});

test('单频道详情：跟着联动的那一档走，没有联动时按 slots 第一档', () => {
  const linked = render("channel-detail", { effective: { refresh_ms: 200 }, link: { kind: "channel", id: 5 } });
  assert.match(textOf(linked.node), /6 档 · 5843 MHz/);
  const alone = render("channel-detail", { effective: { refresh_ms: 200, slots: [7] } });
  assert.match(textOf(alone.node), /8 档 · 5917 MHz/);
});

test('RSSI 趋势：轴是采样节拍，虚线是阈值不是数据', () => {
  const { node } = render("rssi-trend", { effective: { refresh_ms: 200, time_range_s: 30, threshold_db: -80, slots: [6] } });
  const canvas = walk(node).find((row) => row.tag === "canvas");
  assert.ok(canvas, "趋势图得有画布");
  assert.match(canvas.props["aria-label"], /演示数据/);
  assert.match(textOf(node), /横轴：采样节拍/);
  assert.match(textOf(node), /虚线是阈值，不是数据/);
  assert.match(textOf(node), /150 点/, "30 s ÷ 200 ms = 150 拍");
});

test('RSSI 趋势：点数受栅格容量封顶，长区间不会把手绘循环变成刷屏', () => {
  const { node } = render("rssi-trend", { effective: { refresh_ms: 200, time_range_s: 86_400, slots: [0] } });
  assert.match(textOf(node), /900 点/, "一天 × 5 Hz = 43 200 拍，取上限 900");
});

test('事件流：只有选择与联动，没有确认/忽略/指派这类假装能处置的按钮', () => {
  const { env, node } = render("signal-events", { effective: { refresh_ms: 200 } });
  assert.deepEqual(walk(node).filter((row) => row.tag === "button"), [], "本阶段事件是演示数据，一个处置按钮都不该出现");
  assert.match(textOf(node), /演示事件 · 事件表属 S6，这里不接后端/);
  assert.equal(findByClass(node, "badge--demo").length, 1);
  assert.equal(findByClass(node, "badge--api").length, 0);
  clickNode(node, (row) => hasClass(row, "event"));
  assert.equal(env.calls[0].name, "linkChannel");
  assert.equal(env.calls.length, 1, "点一条事件只做联动这一件事");
});

// --- 实时组件 -----------------------------------------------------------------

const ONLINE_BOARD = {
  node_id: 30, display_name: "板 R1", status: "active", stops_rh: true,
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

const withBoards = (nodes, gateway) => ({
  contextState: { ...emptyContext, nodes, scopeNodes: nodes, gatewayId: gateway ? gateway.gateway_id : null, nodeId: nodes.length ? nodes[0].node_id : null },
  gateway,
});

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

  const emptyTree = render("site-tree", {});
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

test('事件流的心跳不快于 500 ms：演示事件刷太快只会糊成一片', () => {
  assert.equal(render("signal-events", { effective: { refresh_ms: 100 } }).widget.lease.timers[0].ms, 500);
});

test('节点状态自己按 2 s 节奏排：这块表跟着接入机心跳，不跟着演示节拍', () => {
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

test('趋势组件：有画布就真画一次，释放后补跑的钩子什么都不画', () => {
  const { widget, rt, tree } = render("rssi-trend", { effective: { refresh_ms: 200, time_range_s: 4, threshold_db: -80, slots: [0] } });
  const surface = fakeCanvas();
  rt.refs.find((row) => row.value === null).value = surface; // `beat` 也是 ref，按初值认画布那一个
  tree();
  assert.equal(widget.lease.hooks.length, 1, "没登记释放钩子就等于没摘数据");
  const strokesWithData = surface.ops.filter(([op]) => op === "stroke").length;
  assert.ok(strokesWithData >= 3, `网格线 + 阈值线 + 折线，实际 ${strokesWithData} 次 stroke`);
  assert.equal(surface.ops.some(([op, ...args]) => op === "setLineDash" && args[0].length === 2), true, "阈值线必须是虚线：它是判据不是第 N 条曲线");

  widget.lease.release();
  surface.ops.length = 0;
  rt.hooks.updated[0]();
  rt.hooks.mounted[0]();
  assert.deepEqual(surface.ops, [], "格子已经没了，晚一步跑的绘制不许再往画布上落笔");
});
