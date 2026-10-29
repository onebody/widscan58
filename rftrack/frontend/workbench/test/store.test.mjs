/**
 * 工作台状态机单测：规范 §10.1 的交互清单在这里逐条判定。
 *
 * 假服务端只实现 store 真的会调的那几个端点，并且**把收到的请求体留下来**——
 * "整份替换语义""保存后不残留脏标记""删除要等服务端点头"这些判据，
 * 光看内存状态是断不出来的，必须看发出去的那一笔。
 */

import assert from "node:assert/strict";
import test from "node:test";

import { createStore } from "../store.js";
import * as registry from "../registry.js";

const SCHEMA = {
  layers: ["default", "global", "site", "layout", "instance"],
  keys: [
    { key: "theme", kind: "choice", layers: ["global", "site", "layout", "instance"], choices: ["dark", "light"], locked: false, unit: null },
    { key: "refresh_ms", kind: "int", layers: ["global", "site", "layout", "instance"], minimum: 100, maximum: 5000, default: 200, unit: "ms", locked: false },
    { key: "threshold_db", kind: "float", layers: ["global", "site", "layout", "instance"], minimum: -120, maximum: 0, default: -85, unit: "dBFS", locked: false },
    { key: "slots", kind: "slots", layers: ["global", "site", "layout", "instance"], minimum: 8, maximum: 8, locked: false },
    { key: "current_site_id", kind: "int", layers: ["global"], locked: false, requires_restart: true },
    { key: "instance_role", kind: "string", layers: ["global"], locked: true },
  ],
};

const LAYOUT = {
  layout_id: 7, site_id: 1, name: "默认视图", is_active: true, degraded: false,
  version: 1, widget_count: 2, defaults: { refresh_ms: 200 },
};

const WIDGET_ROWS = [
  {
    instance_id: "channel-matrix-1", plugin_id: "channel-matrix",
    pos_x: 0, pos_y: 0, pos_w: 6, pos_h: 4, sort: 0,
    config: { threshold_db: -80 },
    declaration_error: null,
    effective: {
      threshold_db: { key: "threshold_db", value: -80, source: "instance", unit: "dBFS", chain: [{ layer: "instance", value: -80 }] },
      refresh_ms: { key: "refresh_ms", value: 200, source: "layout", unit: "ms", chain: [{ layer: "layout", value: 200 }] },
    },
  },
  {
    instance_id: "node-status-1", plugin_id: "node-status",
    pos_x: 6, pos_y: 0, pos_w: 4, pos_h: 4, sort: 1,
    config: {}, declaration_error: null,
    effective: { refresh_ms: { key: "refresh_ms", value: 200, source: "layout", unit: "ms", chain: [] } },
  },
];

function fakeTimers() {
  const state = { intervals: 0, timeouts: 0, live: new Map(), next: 1 };
  return {
    state,
    setInterval(fn, ms) { const id = state.next++; state.intervals++; state.live.set(id, "interval"); return id; },
    setTimeout(fn, ms) { const id = state.next++; state.timeouts++; state.live.set(id, "timeout"); return id; },
    clearInterval(id) { if (state.live.delete(id)) state.intervals--; },
    clearTimeout(id) { if (state.live.delete(id)) state.timeouts--; },
  };
}

function makeStore(options = {}) {
  const sent = { put: [], delete: [], patch: [], activate: [], create: [], clone: [] };
  // 假服务端**带状态**：store 保存完会重新 `loadLayouts` 读回事实，如果这里回的还是写死的那两份，
  // "保存后新格子还在""另存为之后当前布局换成副本"这两条判据就永远断不出来（而且断的是假服务的态，不是 store 的）。
  const layoutsTable = (options.layouts ?? [LAYOUT]).map((row) => ({ ...row }));
  const widgetTable = (options.rows ?? WIDGET_ROWS).map((row) => ({
    ...row, layout_id: row.layout_id ?? LAYOUT.layout_id, config: { ...row.config }, effective: { ...row.effective },
  }));
  let nextLayoutId = 100;
  const countWidgets = (layoutId) => {
    const row = layoutsTable.find((item) => item.layout_id === layoutId);
    if (row) row.widget_count = widgetTable.filter((item) => item.layout_id === layoutId).length;
  };

  const api = {
    schema: async () => SCHEMA,
    layouts: async () => layoutsTable.map((row) => ({ ...row })),
    widgets: async (layoutId) => widgetTable
      .filter((row) => row.layout_id === layoutId)
      .map((row) => ({ ...row, config: { ...row.config } })),
    putWidget: async (layoutId, instanceId, payload) => {
      sent.put.push({ layoutId, instanceId, payload });
      if (options.rejectPut) {
        const error = new Error("rejected");
        error.reason = options.rejectPut;
        error.detail = options.rejectDetail || { points: 4000, ring_depth_per_slot: 900 };
        throw error;
      }
      let row = widgetTable.find((item) => item.instance_id === instanceId);
      if (!row) {
        row = { instance_id: instanceId, layout_id: layoutId, declaration_error: null, effective: {} };
        widgetTable.push(row);
      }
      Object.assign(row, {
        layout_id: layoutId,
        plugin_id: payload.plugin_id || row.plugin_id,
        pos_x: payload.pos_x, pos_y: payload.pos_y, pos_w: payload.pos_w, pos_h: payload.pos_h,
        sort: payload.sort,
        config: { ...payload.config },
      });
      row.effective = Object.fromEntries(
        Object.entries(payload.config).map(([key, value]) => [key, { key, value, source: "instance", unit: null, chain: [{ layer: "instance", value }] }])
      );
      countWidgets(layoutId);
      return { ...row, config: { ...row.config } };
    },
    deleteWidget: async (layoutId, instanceId) => {
      sent.delete.push({ layoutId, instanceId });
      const index = widgetTable.findIndex((row) => row.instance_id === instanceId);
      if (index >= 0) widgetTable.splice(index, 1);
      countWidgets(layoutId);
      return null;
    },
    patchLayout: async (layoutId, payload) => {
      sent.patch.push({ layoutId, payload });
      const row = layoutsTable.find((item) => item.layout_id === layoutId);
      Object.assign(row, payload);
      return { ...row };
    },
    activateLayout: async (layoutId) => {
      sent.activate.push(layoutId);
      layoutsTable.forEach((row) => { row.is_active = row.layout_id === layoutId; });
      return { ...layoutsTable.find((row) => row.layout_id === layoutId) };
    },
    createLayout: async (payload) => {
      sent.create.push(payload);
      const row = { ...LAYOUT, layout_id: nextLayoutId++, name: payload.name, is_active: true, widget_count: 0, defaults: {} };
      layoutsTable.push(row);
      return { ...row };
    },
    cloneLayout: async (layoutId, payload) => {
      sent.clone.push({ layoutId, payload });
      const source = layoutsTable.find((row) => row.layout_id === layoutId);
      const row = { ...source, layout_id: nextLayoutId++, name: payload.name || "副本", is_active: true };
      layoutsTable.push(row);
      layoutsTable.forEach((item) => { if (item !== row) item.is_active = false; });
      return { ...row };
    },
  };
  const context = {
    state: { siteId: options.siteId ?? 1, instanceRole: options.role ?? "site", globals: options.globals ?? { settings: {} } },
  };
  const timers = fakeTimers();
  const store = createStore({ api, context, registry, timers, columns: 12 });
  return { store, api, sent, context, timers, layoutsTable, widgetTable };
}

async function booted(options = {}) {
  const harness = makeStore(options);
  await harness.store.loadSchema();
  await harness.store.loadLayouts(1);
  return harness;
}

test("schema 驱动面板：只列这一层可写的键，locked 的一律不出现", async () => {
  const { store } = await booted();
  assert.deepEqual(store.editableKeys("instance").map((entry) => entry.key),
    ["theme", "refresh_ms", "threshold_db", "slots"]);
  assert.deepEqual(store.editableKeys("global").map((entry) => entry.key),
    ["theme", "refresh_ms", "threshold_db", "slots", "current_site_id"]);
  assert.equal(store.editableKeys("layout").some((entry) => entry.key === "instance_role"), false);
});

test("加一块格子：落到空位、标脏、未保存角标的数据来源是 isNew", async () => {
  const { store } = await booted();
  const before = store.state.widgets.length;
  const added = store.add("rssi-trend");
  assert.equal(store.state.widgets.length, before + 1);
  assert.equal(added.isNew, true);
  assert.equal(added.instance_id, "rssi-trend-1");
  assert.deepEqual({ x: added.x, y: added.y, w: added.w, h: added.h }, { x: 0, y: 4, w: 6, h: 4 });
  assert.ok(store.state.draft.has("rssi-trend-1"));
  // 再加一块同类的：实例号必须续上，不能撞车（撞了 lease 就会两份并一份）。
  assert.equal(store.add("rssi-trend").instance_id, "rssi-trend-2");
});

test("删除：新建的直接忘掉，已存的进待删清单，点了保存才真的发 DELETE", async () => {
  const { store, sent } = await booted();
  const fresh = store.add("channel-detail");
  store.remove(fresh.instance_id);
  assert.deepEqual(sent.delete, [], "没落过库的格子不该去删服务端");

  store.remove("node-status-1");
  assert.equal(store.state.widgets.some((row) => row.instance_id === "node-status-1"), false);
  assert.deepEqual(store.audit().pendingDelete, ["node-status-1"]);
  await store.save();
  assert.deepEqual(sent.delete, [{ layoutId: 7, instanceId: "node-status-1" }]);
  assert.deepEqual(store.audit().pendingDelete, []);
});

test("复制：副本是新实例号、带走配置、但不继承源格子的生效值与声明错误", async () => {
  const { store } = await booted();
  const source = store.state.widgets.find((row) => row.instance_id === "channel-matrix-1");
  const copy = store.duplicate("channel-matrix-1");
  assert.equal(copy.instance_id, "channel-matrix-2");
  assert.deepEqual(copy.config, { threshold_db: -80 });
  assert.equal(copy.isNew, true);
  assert.deepEqual(copy.effective, {}, "副本还没回过服务端，生效值没有事实依据");
  assert.equal(copy.declaration_error, null);
  assert.deepEqual(source.declaration_error, null);
  assert.ok(store.state.draft.has("channel-matrix-2"));
});

test("拖动：越界夹紧、被压的格子一起标脏、两块谁也不盖谁", async () => {
  const { store } = await booted();
  store.dragBy("channel-matrix-1", { x: 20, y: 20 });
  const moved = store.state.widgets.find((row) => row.instance_id === "channel-matrix-1");
  assert.equal(moved.x, 6, "12 列宽、6 格宽最多到 x=6");
  store.dragBy("channel-matrix-1", { x: -20, y: -20 });
  assert.equal(store.state.widgets.find((row) => row.instance_id === "channel-matrix-1").y, 0);

  store.dragBy("channel-matrix-1", { x: 0, y: 0 });
  store.dragBy("node-status-1", { x: -6, y: 0 });
  const rows = store.rects();
  for (let i = 0; i < rows.length; i += 1) {
    for (let j = i + 1; j < rows.length; j += 1) {
      assert.equal(rows[i].x < rows[j].x + rows[j].w && rows[j].x < rows[i].x + rows[i].w
        && rows[i].y < rows[j].y + rows[j].h && rows[j].y < rows[i].y + rows[i].h, false);
    }
  }
  assert.ok(store.state.draft.size >= 1, "推动别人也要把别人标脏，否则保存会漏掉它");
});

test("缩放：最小尺寸来自组件声明，不是页面自己抄一份", async () => {
  const { store } = await booted();
  const resized = store.resizeTo("channel-matrix-1", { x: 0, y: 0 });
  assert.deepEqual({ w: resized.w, h: resized.h }, { w: registry.component("channel-matrix").min.w, h: registry.component("channel-matrix").min.h });
  assert.equal(store.resizeTo("channel-matrix-1", { x: 11, y: 9 }).w, 12);
});

test("保存发的是整份 config：缺键等于取消这一层，服务端那条替换语义不能猜", async () => {
  const { store, sent } = await booted();
  store.setConfig("channel-matrix-1", "threshold_db", null);
  await store.save();
  const put = sent.put.find((row) => row.instanceId === "channel-matrix-1");
  assert.deepEqual(put.payload.config, {}, "取消一把键要靠不再带它，而不是带 null");
  assert.equal(put.payload.plugin_id, undefined, "已存在的格子不该再报 plugin_id");
});

test("新建的格子保存时带 plugin_id——服务端那条 plugin_id_required 就是这么来的", async () => {
  const { store, sent } = await booted();
  store.add("channel-detail");
  await store.save();
  const put = sent.put.find((row) => row.instanceId === "channel-detail-1");
  assert.equal(put.payload.plugin_id, "channel-detail");
  assert.equal(store.state.widgets.find((row) => row.instance_id === "channel-detail-1").isNew, false);
  assert.equal(store.state.draft.size, 0, "保存成功之后不该残留脏标记");
  assert.ok(store.state.savedAt, "保存时间要有，状态栏那句「已保存」不是猜的");
});

test("一格被拒：脏标记留在原处并说清原因，不装作保存过了", async () => {
  const { store } = await booted({ rejectPut: "ring_capacity_exceeded" });
  store.setConfig("channel-matrix-1", "refresh_ms", 100);
  const result = await store.save();
  assert.equal(result.ok, false);
  assert.equal(store.state.draft.has("channel-matrix-1"), true);
  assert.match(store.state.errors[0].message, /环形缓冲每槽只有 900/);
});

test("布局默认值是结构层草稿：保存走 PATCH，不发格子 PUT", async () => {
  const { store, sent } = await booted();
  store.setLayoutDefault("refresh_ms", 300);
  assert.deepEqual(store.audit().draft, ["layout"]);
  await store.save();
  assert.deepEqual(sent.patch, [{ layoutId: 7, payload: { defaults: { refresh_ms: 300 } } }]);
  assert.deepEqual(sent.put, []);
});

test("重命名立刻打服务端，与「我刚才挪了个格子」不是一条路", async () => {
  const { store, sent } = await booted();
  await store.rename("比赛日视图");
  assert.deepEqual(sent.patch, [{ layoutId: 7, payload: { name: "比赛日视图" } }]);
  assert.equal(store.state.layout.name, "比赛日视图");
});

test("另存为：先保存草稿再克隆，克隆出来的那份成为当前", async () => {
  const { store, sent } = await booted();
  store.setConfig("channel-matrix-1", "threshold_db", -70);
  const copy = await store.saveAs("副本视图");
  assert.equal(sent.put.length, 1, "没保存的改动不该在克隆里凭空出现");
  assert.deepEqual(sent.clone, [{ layoutId: 7, payload: { name: "副本视图" } }]);
  assert.equal(store.state.layout.layout_id, copy.layout_id);
  assert.deepEqual(sent.activate, [copy.layout_id]);
  assert.equal(store.state.layout.name, "副本视图");
});

test("切换布局：先让服务端认 is_active，再重开格子；脏集合清空", async () => {
  const { store, sent } = await booted();
  const created = await store.createLayout("另一个视图");
  store.add("channel-detail");
  assert.equal(store.state.draft.size, 1, "新布局里加了块没保存的格子");
  await store.switchLayout(LAYOUT.layout_id);
  assert.equal(store.state.draft.size, 0, "切走就是把这份草稿丢掉——它属于另一个布局");
  assert.equal(store.state.layout.layout_id, LAYOUT.layout_id);
  assert.ok(sent.activate.includes(created.layout_id), "切换必须先 activate，页面不能自己记一个「当前」");
  assert.equal(store.state.layouts.filter((row) => row.is_active).length, 1);
});

test("切换布局被并发抢了：给的是人话，不是把 reason 甩在屏幕上", async () => {
  const { store, api } = await booted();
  api.activateLayout = async () => {
    const error = new Error("POST /api/layouts/8/activate → 409");
    Object.assign(error, { name: "ApiError", status: 409, reason: "active_layout_conflict", detail: { site_id: 1 } });
    throw error;
  };
  const opened = await store.switchLayout(8);
  assert.equal(opened, null, "被拒的切换不该把布局换过去");
  assert.match(store.state.errors[0].message, /重新选一次/);
  assert.notEqual(store.state.errors[0].message, store.state.errors[0].reason, "消息等于 reason 就是没翻译，运营者只能去猜英文标识符");
});

test("刷新恢复：丢掉草稿、回到库里那份、租约归还干净", async () => {
  const { store, timers } = await booted();
  store.add("rssi-trend");
  store.lease("channel-matrix-1").setInterval(() => {}, 200);
  store.setLayoutDefault("refresh_ms", 500);
  assert.ok(store.audit().intervals >= 1);
  await store.restore();
  assert.equal(store.state.draft.size, 0);
  assert.equal(store.state.widgets.some((row) => row.instance_id === "rssi-trend-1"), false);
  assert.equal(store.audit().intervals, 0, "恢复之后定时器必须全停");
  assert.equal(timers.state.intervals, 0);
  assert.equal(store.state.layout.defaults.refresh_ms, 200, "回到库里那一份，不是回到我改过的那一份");
});

test("租约按格子记账：删一块不该把同类那块的定时器也带走", async () => {
  const { store } = await booted();
  const a = store.add("channel-detail");
  const b = store.add("channel-detail");
  const handleA = store.lease(a.instance_id).setInterval(() => {}, 500);
  store.lease(b.instance_id).setInterval(() => {}, 500);
  store.remove(a.instance_id);
  assert.equal(store.audit().intervals, 1, "只剩 b 那一份");
  assert.equal(store.release(a.instance_id), false, "已经归还过的租约不该还在账上");
  assert.equal(store.lease(b.instance_id).live(), 1);
  assert.ok(handleA);
});

test("取消订阅要真的删掉：拿字面量数组当键的那版写法会被这里钉住", async () => {
  const { store } = await booted();
  const lease = store.lease("channel-matrix-1");
  const unsubscribe = lease.subscribe("tick", () => {});
  assert.equal(lease.live(), 1);
  unsubscribe();
  assert.equal(lease.live(), 0);
  unsubscribe();
  assert.equal(lease.live(), 0);
});

test("切场地/切布局把上一份租约全还掉：widgets 与 leases 必须同步", async () => {
  const { store } = await booted();
  store.lease("channel-matrix-1").setInterval(() => {}, 200);
  store.lease("node-status-1").setTimeout(() => {}, 200);
  assert.equal(store.audit().leases, 2);
  await store.openLayout(7);
  assert.equal(store.audit().leases, store.state.widgets.length, "一份格子对应一份租约，不多也不少");
  assert.equal(store.audit().intervals + store.audit().timeouts, 0, "重开布局之后旧定时器不该还在跑");
  store.releaseAll();
  assert.equal(store.audit().leases, 0);
});

test("坏声明的格子不崩：注册表里没有的 plugin_id 保留位置与配置", async () => {
  const rows = [{
    instance_id: "ghost-1", plugin_id: "not-installed", pos_x: 0, pos_y: 0, pos_w: 3, pos_h: 3, sort: 0,
    config: { theme: "light" }, declaration_error: "unknown plugin", effective: {},
  }];
  const { store } = await booted({ rows });
  const ghost = store.state.widgets[0];
  assert.equal(ghost.missing, true);
  assert.deepEqual(ghost.config, { theme: "light" });
  assert.equal(store.state.widgets.length, 1, "认不到的组件不该被丢掉，否则用户的格子就没了");
});

test("降级布局只读：改内容不发写请求，但换看别的布局还走得动", async () => {
  const degraded = { ...LAYOUT, degraded: true, version: 99, degrade_reason: "layout_version_too_new" };
  const other = { ...LAYOUT, layout_id: 8, name: "还能用的那份", is_active: false, widget_count: 0 };
  const { store, sent } = await booted({ layouts: [degraded, other] });
  assert.equal(store.readOnlyReason().reason, "layout_version_too_new");
  assert.equal(store.add("rssi-trend"), null);
  assert.equal(store.setConfig("channel-matrix-1", "threshold_db", -60), null);
  await store.save();
  assert.deepEqual(sent.put, [], "降级期间一个写请求都不许发出去");
  assert.equal(store.state.layout.degraded, true);
  // 出不去才是真事故：降级只该挡住"按我们看不懂的语义重写这一份"，不该把页面钉死在这一屏。
  const opened = await store.switchLayout(8);
  assert.ok(opened, "从降级布局切走必须成功");
  assert.deepEqual(sent.activate, [8]);
  assert.equal(store.state.layout.layout_id, 8);
  assert.equal(store.readOnlyReason(), null);
  assert.ok(store.add("rssi-trend"), "切到好布局之后编辑权要回来");
});

test("降级布局是场地里唯一那一份时，新建还得能用——否则连逃生口都没有", async () => {
  const degraded = { ...LAYOUT, degraded: true, version: 99, degrade_reason: "layout_version_too_new" };
  const { store, sent } = await booted({ layouts: [degraded] });
  const created = await store.createLayout("逃生口");
  assert.ok(created, "新建不动那份太老的布局的字节，服务端也没按来源布局判版本");
  assert.equal(sent.create.length, 1);
  assert.equal(store.state.layout.layout_id, created.layout_id);
  assert.equal(store.readOnlyReason(), null);
});

test("汇聚实例连切换与新建都不许：那是在写 is_active，服务端同一条口子（405）", async () => {
  const other = { ...LAYOUT, layout_id: 8, is_active: false };
  const { store, sent } = await booted({ layouts: [LAYOUT, other], role: "aggregator" });
  assert.equal(await store.switchLayout(8), null);
  assert.deepEqual(sent.activate, []);
  assert.equal(await store.createLayout("试试"), null);
  assert.deepEqual(sent.create, []);
  assert.equal(store.readOnlyReason().reason, "aggregator_read_only");
});

test("版本超前被服务端拒时回的是人话：页面上不许只剩一个英文标识符", async () => {
  const { store } = await booted();
  const described = store.describe({
    reason: "layout_version_too_new",
    detail: { layout_id: 9, version: 99, supported: 1 },
  });
  assert.match(described.message, /结构版本是 99/);
  assert.match(described.message, /只认到 1/);
  assert.equal(/^[a-z_]+$/.test(described.message), false);
});

test("汇聚实例只读是另一件事：理由与修法都不同，页面要分开显示", async () => {
  const { store } = await booted({ role: "aggregator" });
  assert.equal(store.readOnlyReason().reason, "aggregator_read_only");
});

test("生效值读取：已保存的格子听服务端（null 就是没设），新格子退到全局那一份", async () => {
  const globals = { settings: { refresh_ms: { key: "refresh_ms", value: 250, source: "global", unit: "ms", chain: [] } } };
  const { store } = await booted({ globals });
  const saved = store.state.widgets.find((row) => row.instance_id === "node-status-1");
  assert.equal(store.effectiveOf(saved, "threshold_db"), null, "库里没算出来的值不该被页面猜一个");
  assert.equal(store.effectiveOf(saved, "refresh_ms"), 200);
  const fresh = store.add("rssi-trend");
  assert.equal(store.effectiveOf(fresh, "refresh_ms"), 250, "新格子退到全局生效值，而不是页面自己的偏好");
});

test("联动只带 id 与种类：结论属 S8，这里不越权", async () => {
  const { store } = await booted();
  const seen = [];
  const off = store.onLink((link) => seen.push(link));
  store.linkTo("channel", 3);
  assert.deepEqual(store.state.link, { kind: "channel", id: 3 });
  assert.deepEqual(seen, [{ kind: "channel", id: 3 }]);
  off();
  store.linkTo("node", 30);
  assert.equal(seen.length, 1, "取消订阅之后不该再收到");
});

test("读布局失败要说得出口：error 带 reason，页面才有的分支", async () => {
  const harness = makeStore();
  harness.api.layouts = async () => { const error = new Error("boom"); error.reason = "site_required"; throw error; };
  await harness.store.loadSchema();
  const layout = await harness.store.loadLayouts(1);
  assert.equal(layout, null);
  assert.equal(harness.store.state.error.reason, "site_required");
  assert.match(harness.store.state.error.message, /缺少 \?site=/);
});
