/**
 * 场地上下文单测。这里钉的是规范里那两条最容易嘴上说说判据：
 * **切场地只换视图、不碰后台**，以及**"我现在看着谁"必须逐段说得出口**（不许空字符串）。
 */

import assert from "node:assert/strict";
import test from "node:test";

import { createContext, readViewMemory, VIEW_MEMORY_KEY } from "../context.js";

const SITE_A = { site_id: 1, name: "A 号赛段", origin: "local", status: "active", timezone: "Asia/Shanghai" };
const SITE_B = { site_id: 2, name: "外部赛段", origin: "aggregated", status: "active", lag_s: 300, last_sync_at: "2026-10-29T04:00:00", timezone: "UTC" };
const SITE_STALE = { site_id: 3, name: "没同步过的赛段", origin: "aggregated", status: "active", lag_s: null, last_sync_at: null, timezone: "UTC" };

function fakeApi(options = {}) {
  const calls = [];
  const record = (name) => (...args) => { calls.push({ name, args }); return Promise.resolve(null); };
  const api = {
    calls,
    health: record("health"),
    sites: record("sites"),
    globalSettings: record("globalSettings"),
    relays: record("relays"),
    gateways: record("gateways"),
    nodes: record("nodes"),
  };
  api.health = async () => { calls.push({ name: "health", args: [] }); return { instance_role: options.role || "site", gateways: [] }; };
  api.sites = async () => { calls.push({ name: "sites", args: [] }); return options.sites || [SITE_A]; };
  api.globalSettings = async () => {
    calls.push({ name: "globalSettings", args: [] });
    return { settings: options.globals || {}, version: 0 };
  };
  api.relays = async (siteId) => {
    calls.push({ name: "relays", args: [siteId] });
    return (options.relays || []) .filter((row) => row.site_id === siteId);
  };
  api.gateways = async (siteId) => {
    calls.push({ name: "gateways", args: [siteId] });
    return (options.gateways || []).filter((row) => row.site_id === siteId);
  };
  api.nodes = async ({ site } = {}) => {
    calls.push({ name: "nodes", args: [site] });
    return (options.nodes || []).filter((row) => row.site_id === site);
  };
  return api;
}

const RELAY_1 = { relay_id: 10, site_id: 1, name: "起点" };
const RELAY_2 = { relay_id: 11, site_id: 1, name: "终点" };
const GW_ONLINE = { gateway_id: 20, relay_id: 10, site_id: 1, name: "接入机甲", dial_state: "online", status: "enabled" };
const GW_DEAD = { gateway_id: 21, relay_id: 11, site_id: 1, name: "接入机乙", dial_state: "unreachable", status: "enabled" };
const NODE_1 = { node_id: 30, gateway_id: 20, site_id: 1, display_name: "板 R1" };
const NODE_2 = { node_id: 31, gateway_id: 20, site_id: 1, display_name: "板 R2" };

test("boot：健康/场地/全局三路并发，实例角色来自健康而不是页面自己认", async () => {
  const api = fakeApi({ role: "aggregator" });
  const context = createContext({ api });
  await context.boot({});
  assert.equal(context.state.instanceRole, "aggregator");
  assert.equal(context.state.loading, false);
  // 选定场地之后紧接着把这一路层级的三张表也拉了；页面第一眼就该有层级，不是空下拉。
  assert.deepEqual(
    api.calls.map((call) => call.name).slice(0, 3).sort(),
    ["globalSettings", "health", "sites"]
  );
  assert.deepEqual(api.calls.slice(3).map((call) => call.name).sort(), ["gateways", "nodes", "relays"]);
});

test("首屏看哪个场地：库里配置 → 只有一个 → 不选（多个场地时不猜）", async () => {
  const configured = createContext({
    api: fakeApi({ sites: [SITE_A, SITE_B], globals: { current_site_id: { key: "current_site_id", value: 2, source: "global" } } }),
  });
  await configured.boot({});
  assert.equal(configured.state.siteId, 2);

  const single = createContext({ api: fakeApi({ sites: [SITE_A] }) });
  await single.boot({});
  assert.equal(single.state.siteId, 1);

  const ambiguous = createContext({ api: fakeApi({ sites: [SITE_A, SITE_B] }) });
  await ambiguous.boot({});
  assert.equal(ambiguous.state.siteId, null, "两个场地且没有配置时不能随机进一个");

  const missing = createContext({
    api: fakeApi({ sites: [SITE_A], globals: { current_site_id: { key: "current_site_id", value: 99, source: "global" } } }),
  });
  await missing.boot({});
  assert.equal(missing.state.error.reason, "configured_site_missing");
});

test("层级回落：选了位置就只看它名下的接入机，接入机没了就如实回落到未选", async () => {
  const api = fakeApi({ relays: [RELAY_1, RELAY_2], gateways: [GW_ONLINE, GW_DEAD], nodes: [NODE_1, NODE_2] });
  const context = createContext({ api });
  await context.boot({});
  assert.equal(context.state.relayId, 10);
  assert.equal(context.state.gatewayId, 20);
  assert.equal(context.state.nodeId, 30);
  assert.deepEqual(context.state.scopeNodes.map((row) => row.node_id), [30, 31]);

  context.selectRelay(11);
  assert.deepEqual(context.state.scopeGateways.map((row) => row.gateway_id), [21]);
  assert.equal(context.state.gatewayId, 21);
  assert.equal(context.state.nodeId, null, "乙接入机名下没有板，就不该还指着某块板");

  // 别人把唯一的接入机删了：回落 + 说出来（状态栏那段变成"未选接入机"）。
  api.gateways = async () => [];
  await context.selectSite(1);
  context.reconcile();
  assert.equal(context.state.gatewayId, null);
  assert.equal(context.statusBar().parts.find((part) => part.label === "接入机").value, "未选接入机");
});

test("状态栏逐段有话：场地/位置/接入机/板都要说清「现在是谁」，且没有空段", async () => {
  const api = fakeApi({ relays: [RELAY_1], gateways: [GW_ONLINE], nodes: [NODE_1] });
  const context = createContext({ api });
  await context.boot({});
  // "数据"那一段的正文由 `live.js` 算好交进来（来源 + 连接态 + 板数），`context` 只负责排在最后：
  // 场地上下文不知道 WebSocket 的事，让它去拼那句话就是让两层共用一套词汇表。
  const data = { value: "MOCK+真实 · 已连接 · 2 块板", tone: "warn" };
  const bar = context.statusBar({ layout: { name: "默认视图", degraded: false }, dirty: true, data });
  const values = Object.fromEntries(bar.parts.map((part) => [part.label, part.value]));
  assert.equal(values["场地"], "A 号赛段");
  assert.equal(values["位置"], "起点");
  assert.equal(values["接入机"], "接入机甲 · online");
  assert.equal(values["板"], "板 R1");
  assert.equal(values["布局"], "默认视图");
  assert.equal(values["改动"], "未保存");
  assert.equal(values["数据"], "MOCK+真实 · 已连接 · 2 块板");
  assert.equal(bar.parts.find((part) => part.label === "数据").tone, "warn");
  assert.equal(bar.parts.some((part) => part.value === ""), false);
  assert.match(bar.text, /场地 A 号赛段 · 位置 起点 · 接入机 接入机甲 · online · 板 板 R1/);

  // 没给这一段就不出现那一段：空字符串在状态栏里是一句"数据：（没有）"，比不写更难读。
  const noData = context.statusBar({ layout: { name: "默认视图", degraded: false } });
  assert.equal(noData.parts.some((part) => part.label === "数据"), false);
});

test("未选/没有是两句话：空库里说「还没有场地」，有库没选说「未选」", async () => {
  const empty = createContext({ api: fakeApi({ sites: [] }) });
  await empty.boot({});
  assert.equal(empty.statusBar().parts[0].value, "还没有场地");

  const notPicked = createContext({ api: fakeApi({ sites: [SITE_A, SITE_B] }) });
  await notPicked.boot({});
  assert.equal(notPicked.statusBar().parts[0].value, "未选");
});

test("汇聚场地必须带滞后说明；从未同步比滞后更大更该置灰", async () => {
  const context = createContext({ api: fakeApi({ sites: [SITE_B], staleAfterS: 120 }) });
  await context.boot({});
  const extra = context.lag(SITE_B);
  assert.equal(extra.stale, true);
  assert.match(extra.label, /滞后 300 秒/);
  assert.equal(context.statusBar().parts.find((part) => part.label === "汇聚").tone, "stale");

  const fresh = createContext({ api: fakeApi({ sites: [] }) });
  assert.equal(fresh.lag({ ...SITE_B, lag_s: 5 }).stale, false);
  assert.equal(fresh.lag(SITE_A), null, "本地场地不该挂汇聚注脚");

  const never = createContext({ api: fakeApi({ sites: [] }) });
  const unknown = never.lag(SITE_STALE);
  assert.equal(unknown.stale, true);
  assert.equal(unknown.label, "从未同步");
});

test("每场地记住自己的当前布局：切走再切回不该看到别人的布局", async () => {
  const api = fakeApi({ sites: [SITE_A, SITE_B] });
  const context = createContext({ api });
  await context.boot({});
  context.memoryFor(1).layoutId = 100;
  context.memoryFor(2).layoutId = 200;
  assert.equal(context.memoryFor(1).layoutId, 100);
  assert.equal(context.memoryFor(2).layoutId, 200);
  assert.notEqual(context.memoryFor(1), context.memoryFor(2));
});

test("切场地/切位置/切接入机一次都不碰写端点：这条判据靠调用记录断言", async () => {
  const api = fakeApi({ sites: [SITE_A, SITE_B], relays: [RELAY_1, RELAY_2], gateways: [GW_ONLINE, GW_DEAD], nodes: [NODE_1] });
  const context = createContext({ api });
  await context.boot({});
  await context.selectSite(2);
  context.selectRelay(1);
  context.selectGateway(20);
  context.selectNode(30);
  const writes = api.calls.filter((call) => ["POST", "PUT", "PATCH", "DELETE"].includes(call.name));
  assert.deepEqual(writes, []);
  // 两个场地且没有配置时 boot 不选（见上一测），所以第一次层级查询是 selectSite(2) 发出的：
  // 带的是新场地的 site 参数，不是复用上一次的范围查询。
  assert.deepEqual(api.calls.filter((call) => call.name === "relays").map((call) => call.args[0]), [2]);
});

test("能力只认「答过话的那一路」：没加载过层级就不声称有节点", async () => {
  const context = createContext({ api: fakeApi() });
  assert.deepEqual(context.capabilities(), [], "boot 之前一条通路都没答过话，库里就不该有任何一格是可点的");
  await context.boot({});
  assert.deepEqual(context.capabilities().sort(), ["hierarchy", "live", "nodes"]);
  // S5 那一路 `demo` 不在这里：演示数据整份删了，能力词表还留着它就是在给一个不存在的通路作保。
  assert.equal(context.capabilities().includes("demo"), false);

  // 只有场地、层级还没回话：`live` 能订上推送，但节点格要说"缺少能力：nodes"。
  const bare = createContext({ api: fakeApi() });
  bare.state.siteId = 1;
  assert.deepEqual(bare.capabilities(), ["live"]);
});

// --- 视图记忆：刷新回来还是这一屏（规范 §10.1 第三条）------------------------------

/** 假存储：只认 `getItem/setItem`，形状照浏览器 localStorage 的那两条最小接口来。 */
const memoryStorage = (initial = {}) => ({
  data: { ...initial },
  getItem(key) {
    return Object.prototype.hasOwnProperty.call(this.data, key) ? this.data[key] : null;
  },
  setItem(key, value) {
    this.data[key] = String(value);
  },
  /** 读回浏览器里那份记忆（测试用它来验"写出去的形状"）。 */
  read() {
    return JSON.parse(this.data[VIEW_MEMORY_KEY]);
  },
});

const remembered = (payload) => memoryStorage({ [VIEW_MEMORY_KEY]: JSON.stringify(payload) });

test("readViewMemory：认不出来就当没有，绝不把一次坏数据变成一个错误页", () => {
  const empty = { siteId: null, bySite: {} };
  assert.deepEqual(readViewMemory(null), empty, "没有存储（Node、无痕首启）");
  assert.deepEqual(readViewMemory({ getItem: () => null }), empty, "第一次打开，键还不存在");
  assert.deepEqual(readViewMemory({ getItem: () => "{" }), empty, "半截 JSON");
  assert.deepEqual(readViewMemory({ getItem: () => "null" }), empty, "存进去的是 null");
  assert.deepEqual(readViewMemory({ getItem: () => JSON.stringify({ siteId: 1, bySite: "不是对象" }) }), empty, "bySite 形状不对");
  assert.deepEqual(readViewMemory({ getItem: () => { throw new Error("SecurityError"); } }), empty, "存储本身抛异常");
  assert.deepEqual(readViewMemory({ getItem: () => JSON.stringify({ siteId: 3, bySite: { 3: { relayId: 7 } } }) }),
    { siteId: 3, bySite: { 3: { relayId: 7 } } }, "认得出来的那份要原样交回去");
});

test("视图记忆写出去的形状：siteId + 每场地一份位置/接入机/板；布局不在这里", async () => {
  const storage = memoryStorage();
  const context = createContext({
    api: fakeApi({ sites: [SITE_A, SITE_B], relays: [RELAY_1, RELAY_2], gateways: [GW_ONLINE, GW_DEAD], nodes: [NODE_1, NODE_2] }),
    storage,
  });
  await context.boot({ siteId: 1 });
  context.selectRelay(11);
  context.selectGateway(21);
  const saved = storage.read();
  assert.equal(saved.siteId, 1);
  assert.deepEqual(saved.bySite["1"], { layoutId: null, instanceId: null, relayId: 11, gatewayId: 21, nodeId: null });
});

test("刷新回到上一屏那个场地，位置/接入机/板一起接着用；库里那行配置仍然压在它之上", async () => {
  const levelOptions = { sites: [SITE_A, SITE_B], relays: [RELAY_1, RELAY_2], gateways: [GW_ONLINE, GW_DEAD], nodes: [NODE_1, NODE_2] };
  const storage = remembered({ siteId: 1, bySite: { 1: { relayId: 11, gatewayId: 21, nodeId: null } } });
  const context = createContext({ api: fakeApi(levelOptions), storage });
  await context.boot({});
  assert.equal(context.state.siteId, 1, "两个场地且库里没配：浏览器那份「上次看的」要接住，否则刷新回到未选");
  assert.equal(context.state.relayId, 11);
  assert.equal(context.state.gatewayId, 21);

  // 顺序是 配置 → 上一屏 → 单场地 → 不选：实例级事实必须压得过浏览器。
  const configured = createContext({
    api: fakeApi({ ...levelOptions, globals: { current_site_id: { key: "current_site_id", value: 2, source: "global" } } }),
    storage,
  });
  await configured.boot({});
  assert.equal(configured.state.siteId, 2, "库里配了 2，浏览器记着 1：听库里的");
});

test("上一屏指向已经不存在的场地：回落成「未选」，不猜也不报错", async () => {
  const context = createContext({
    api: fakeApi({ sites: [SITE_A, SITE_B] }),
    storage: remembered({ siteId: 99, bySite: { 99: { relayId: 1 } } }),
  });
  await context.boot({});
  assert.equal(context.state.siteId, null);
  assert.equal(context.state.error, null, "视图记忆只是提示，指向被删的场地不构成一次失败");
});

test("旧记忆指向的场地被删了：单场地规则照样选上剩下那个，但不许抹掉历史场地那一条", async () => {
  const storage = remembered({ siteId: 2, bySite: { 2: { relayId: 11, instanceId: "w-detail" } } });
  // 库现在只剩场地 1（2 被别人删了）：单场地规则会把它选上，写回时不能顺手把 2 那一条抹掉。
  const context = createContext({
    api: fakeApi({ sites: [SITE_A], relays: [RELAY_1, RELAY_2], gateways: [GW_ONLINE, GW_DEAD], nodes: [NODE_1, NODE_2] }),
    storage,
  });
  await context.boot({});
  assert.equal(context.state.siteId, 1);
  const saved = storage.read();
  // 旧条目会被补齐成完整形状（缺的字段是 null，不是"没这个键"）：读的人不必再判两种写法。
  assert.deepEqual(saved.bySite["2"], { layoutId: null, instanceId: "w-detail", relayId: 11, gatewayId: null, nodeId: null },
    "persist 按整张表重写：没进过 Map 的旧条目会被抹掉");
  assert.equal(saved.bySite["1"].relayId, 10, "场地 2 的位置不许漏到场地 1 上");
});

test("存储不可用（无痕、配额满）时静默降级：页面照常加载，persist 如实说没写成", async () => {
  const full = { getItem: () => null, setItem: () => { throw new Error("QuotaExceededError"); } };
  const context = createContext({ api: fakeApi(), storage: full });
  await context.boot({});
  assert.equal(context.state.siteId, 1, "写不进存储不该影响这一屏能不能加载");
  assert.equal(context.state.error, null);
  assert.equal(context.persist(), false, "没写成就要说得出口：状态栏那句「已保存」不能靠它");

  const blocked = createContext({ api: fakeApi(), storage: { getItem: () => { throw new Error("SecurityError"); }, setItem: () => {} } });
  await blocked.boot({});
  assert.equal(blocked.state.siteId, 1, "读存储抛异常也不能把页面变成错误页");
});
