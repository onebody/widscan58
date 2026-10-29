/**
 * 工作台状态机。**不含 Vue、不含 DOM**：`§10.1` 的交互清单（增删/拖缩/复制/保存/另存为/
 * 重命名/切换/刷新恢复/坏布局降级/资源清理）全部在这里判定，`node --test` 直接跑它。
 *
 * 三条口径先说清，因为它们决定了页面上每一个按钮什么时候可点：
 *
 * 1. **两类写入分两条路**。格子级（位置、局部配置）是**草稿**，改完只标脏，按"保存"才落库；
 *    布局级（重命名、另存为、切换、删除）是**结构动作**，点了就立刻打服务端——它们与
 *    "我刚才挪了个格子"不是一回事，混在一起就会出现"改了名字结果把没保存的格子也带丢了"。
 * 2. **谁是当前布局只有一份真相**：服务端的 `is_active`（按场地唯一）。页面不自带"我记住的
 *    那个"，否则切场地回来会出现两个都能解释"当前"的地方。
 * 3. **销毁必须成对归还**。每个格子有一份 lease（定时器 + 订阅 + 归还钩子），删格子、切布局、
 *    换场地都走 `release()`；`audit()` 把"还有几个没还"说出口，这条判据才有断言的对象。
 */

import * as grid from "./grid.js";

const LAYOUT_ONLY = "layout";

function pos_from(row) {
  return { x: row.pos_x, y: row.pos_y, w: row.pos_w, h: row.pos_h };
}

export function createStore({ api, context, registry, demo, timers = globalThis, columns = grid.COLUMNS }) {
  const state = {
    loading: false,
    error: null,
    schema: [],
    layers: [],
    byKey: new Map(),
    layouts: [],
    layout: null,
    widgets: [],
    selection: null,
    /** 组件间的联动：`{kind:"channel"|"node"|"event", id}`。S8 的目标选择走同一条口子。 */
    link: null,
    linkSubscribers: new Set(),
    draft: new Set(),
    removed: new Map(),
    errors: [],
    savedAt: null,
  };

  const leases = new Map();
  let released = 0;

  // --- lease：定时器与订阅的记账 ------------------------------------------------

  /**
   * 给一个格子发一份租约。组件只能用它来登记要回收的东西；
   * `setTimeout`/`setInterval` 直接自己调就等于绕过记账，"清理有断言"会变成空话。
   */
  function lease(instanceId) {
    if (!leases.has(instanceId)) {
      leases.set(instanceId, { instanceId, intervals: new Set(), timeouts: new Set(), subs: new Set(), hooks: new Set() });
    }
    const entry = leases.get(instanceId);
    return {
      setInterval(fn, ms) {
        const handle = timers.setInterval(fn, ms);
        entry.intervals.add(handle);
        return handle;
      },
      setTimeout(fn, ms) {
        const handle = timers.setTimeout(fn, ms);
        entry.timeouts.add(handle);
        return handle;
      },
      clearTimeout(handle) {
        timers.clearTimeout(handle);
        entry.timeouts.delete(handle);
      },
      subscribe(topic, handler) {
        // 取消订阅要能真的删掉：拿 `[topic, handler]` 字面量当键，每次都是新数组，删除永远落空。
        const record = { topic, handler };
        entry.subs.add(record);
        return () => entry.subs.delete(record);
      },
      onRelease(fn) {
        entry.hooks.add(fn);
        return () => entry.hooks.delete(fn);
      },
      live: () => entry.intervals.size + entry.timeouts.size + entry.subs.size,
    };
  }

  function release(instanceId) {
    const entry = leases.get(instanceId);
    if (!entry) return false;
    entry.intervals.forEach((handle) => timers.clearInterval(handle));
    entry.timeouts.forEach((handle) => timers.clearTimeout(handle));
    entry.hooks.forEach((fn) => fn());
    leases.delete(entry.instanceId);
    released += 1;
    return true;
  }

  function audit() {
    let intervals = 0;
    let timeouts = 0;
    let subs = 0;
    leases.forEach((entry) => {
      intervals += entry.intervals.size;
      timeouts += entry.timeouts.size;
      subs += entry.subs.size;
    });
    return {
      widgets: state.widgets.length,
      leases: leases.size,
      intervals,
      timeouts,
      subscriptions: subs,
      released,
      draft: [...state.draft],
      pendingDelete: [...state.removed.keys()],
    };
  }

  // --- 生效值声明 ---------------------------------------------------------------

  async function loadSchema() {
    const document = await api.schema();
    state.schema = document.keys;
    state.byKey = new Map(document.keys.map((entry) => [entry.key, entry]));
    state.layers = document.layers;
    return document;
  }

  const spec = (key) => state.byKey.get(key) || null;

  /** 属性面板该出现哪些键：只认服务端声明里"这一层可写"的那些，不自己抄一遍。 */
  function editableKeys(layer) {
    return state.schema.filter((entry) => !entry.locked && entry.layers.includes(layer));
  }

  // --- 布局 --------------------------------------------------------------------

  async function loadLayouts(siteId) {
    state.error = null;
    state.loading = true;
    try {
      state.layouts = await api.layouts(siteId);
      const active = state.layouts.find((row) => row.is_active) || null;
      state.layout = active;
      if (active) await openLayout(active.layout_id);
      else state.widgets = [];
    } catch (error) {
      state.error = describe(error);
      state.layouts = [];
      state.layout = null;
      state.widgets = [];
    }
    state.loading = false;
    return state.layout;
  }

  /** 布局层与格子层都发一遍：`widgets()` 回的是服务端事实，降级与坏声明在这里落地。 */
  async function openLayout(layoutId) {
    const layout = state.layouts.find((row) => row.layout_id === layoutId) || (await api.layouts(context.state.siteId)).find((row) => row.layout_id === layoutId);
    if (!layout) {
      state.error = { reason: "layout_missing", message: `布局 ${layoutId} 不在这个场地的列表里` };
      return null;
    }
    releaseAll();
    state.layout = layout;
    const rows = await api.widgets(layout.layout_id);
    state.widgets = rows.map((row) => hydrate(row));
    state.draft.clear();
    state.removed.clear();
    state.selection = state.widgets.length ? state.widgets[0].instance_id : null;
    state.errors = [];
    return layout;
  }

  function hydrate(row) {
    const known = registry.component(row.plugin_id);
    return {
      instance_id: row.instance_id,
      plugin_id: row.plugin_id,
      ...pos_from(row),
      sort: row.sort,
      config: { ...(row.config || {}) },
      server_config: { ...(row.config || {}) },
      effective: row.effective || {},
      declaration_error: row.declaration_error,
      /** 注册表里没有的组件：保留位置与配置，等它回来（规范 §3.3 那条"不能崩"）。 */
      missing: known === null,
      isNew: false,
      lease: lease(row.instance_id),
    };
  }

  const layoutRows = () => state.widgets;
  const rects = () => state.widgets.map((widget) => ({ instance_id: widget.instance_id, x: widget.x, y: widget.y, w: widget.w, h: widget.h }));

  function releaseAll() {
    state.widgets.forEach((widget) => release(widget.instance_id));
    leases.clear();
  }

  // --- 只读闸门 ---------------------------------------------------------------

  /**
   * 为什么不能改：降级布局（结构版本比这份代码新）与汇聚实例（角色只读）是两件事，
   * 页面上必须分开说——前者是"升级后端就能改"，后者是"这台实例就不该改"。
   */
  const layoutBlocker = () =>
    state.layout && state.layout.degraded
      ? {
          reason: state.layout.degrade_reason || "layout_version_too_new",
          message: `布局结构版本 ${state.layout.version} 高于本工作台认识的版本，只读渲染`,
        }
      : null;

  const instanceBlocker = () =>
    context.state.instanceRole === "aggregator"
      ? { reason: "aggregator_read_only", message: "汇聚实例只读：布局与配置只在场地实例上改" }
      : null;

  const readOnlyReason = () => layoutBlocker() || instanceBlocker();

  const refuse = (blocked) => {
    if (blocked) state.errors = [blocked];
    return blocked;
  };

  /** 改**这一份布局的内容**（格子、布局默认、名字）：两道闸门都挡，与服务端 `ensure_writable` 同口径。 */
  const guard = () => refuse(readOnlyReason());

  /**
   * 换看哪一份布局、新建一份布局：只挡实例角色。
   *
   * 降级布局不挡这一条，是因为它挡不出一个说得通的结果——"进来之后连离开都不许"是页面自己
   * 造的牢，而这两件事一个字节都没动那份太新的布局。服务端也正是这么划的（`activate_layout`
   * 按**目标**布局的版本判，不看当前这份）。
   */
  const guardInstance = () => refuse(instanceBlocker());

  // --- 格子级动作（草稿）-------------------------------------------------------

  function nextInstanceId(pluginId) {
    let n = 1;
    const taken = new Set(state.widgets.map((widget) => widget.instance_id));
    while (taken.has(`${pluginId}-${n}`)) n += 1;
    return `${pluginId}-${n}`;
  }

  function add(pluginId, at = null) {
    if (guard()) return null;
    const definition = registry.component(pluginId);
    if (!definition) return null;
    const size = { w: definition.size.w, h: definition.size.h };
    const spot = at || grid.findFreeSpot(rects(), size, { columns });
    const instanceId = nextInstanceId(pluginId);
    const widget = {
      instance_id: instanceId,
      plugin_id: pluginId,
      ...grid.clampRect({ x: spot.x, y: spot.y, ...size }, { columns, minW: definition.min.w, minH: definition.min.h }),
      sort: state.widgets.reduce((high, row) => Math.max(high, row.sort), -1) + 1,
      config: {},
      server_config: {},
      effective: {},
      declaration_error: null,
      missing: false,
      isNew: true,
      /** 租约按**格子**记账：按 `plugin_id` 发会让同类两个实例共用一份，删掉一个就找不到归还对象。 */
      lease: lease(instanceId),
    };
    state.widgets.push(widget);
    state.draft.add(widget.instance_id);
    select(widget.instance_id);
    return widget;
  }

  function remove(instanceId) {
    if (guard()) return false;
    const index = state.widgets.findIndex((widget) => widget.instance_id === instanceId);
    if (index < 0) return false;
    const widget = state.widgets[index];
    release(instanceId);
    state.widgets.splice(index, 1);
    state.draft.delete(instanceId);
    if (!widget.isNew) state.removed.set(instanceId, widget);
    if (state.selection === instanceId) state.selection = state.widgets.length ? state.widgets[0].instance_id : null;
    return true;
  }

  function duplicate(instanceId) {
    if (guard()) return null;
    const source = state.widgets.find((widget) => widget.instance_id === instanceId);
    if (!source) return null;
    const size = { w: source.w, h: source.h };
    const spot = grid.findFreeSpot(rects(), size, { columns });
    const copyId = nextInstanceId(source.plugin_id);
    const copy = {
      ...source,
      instance_id: copyId,
      x: spot.x,
      y: spot.y,
      config: { ...source.config },
      server_config: {},
      /** 新副本还没回过服务端：它的"生效值"与"声明错误"都还没有事实依据，不能继承源格子的。 */
      effective: {},
      declaration_error: null,
      isNew: true,
      lease: lease(copyId),
      sort: state.widgets.reduce((high, row) => Math.max(high, row.sort), -1) + 1,
    };
    state.widgets.push(copy);
    state.draft.add(copy.instance_id);
    select(copy.instance_id);
    return copy;
  }

  /** 拖动与缩放共用一条路径：算新位置 → 请栅格解决重叠 → 谁被推动了就一起标脏。 */
  function applyRect(instanceId, next) {
    if (guard()) return null;
    const before = new Map(state.widgets.map((widget) => [widget.instance_id, { x: widget.x, y: widget.y, w: widget.w, h: widget.h }]));
    const target = state.widgets.find((widget) => widget.instance_id === instanceId);
    if (!target) return null;
    Object.assign(target, next);
    const { rects: placed, unresolved } = grid.resolve(rects(), instanceId, { columns });
    placed.forEach((row) => {
      const widget = state.widgets.find((item) => item.instance_id === row.instance_id);
      if (!widget) return;
      const was = before.get(row.instance_id);
      if (was && (was.x !== row.x || was.y !== row.y || was.w !== row.w || was.h !== row.h)) {
        Object.assign(widget, { x: row.x, y: row.y, w: row.w, h: row.h });
        state.draft.add(row.instance_id);
      }
    });
    state.draft.add(instanceId);
    state.errors = unresolved.length
      ? [{ reason: "grid_unresolved", message: `${unresolved.length} 个格子在当前列宽下放不下：${unresolved.join("、")}` }]
      : [];
    return target;
  }

  function dragBy(instanceId, delta) {
    const widget = state.widgets.find((row) => row.instance_id === instanceId);
    if (!widget) return null;
    return applyRect(instanceId, grid.movedBy(widget, delta, { columns }));
  }

  function resizeTo(instanceId, cell) {
    const widget = state.widgets.find((row) => row.instance_id === instanceId);
    if (!widget) return null;
    const definition = registry.component(widget.plugin_id);
    const min = definition ? definition.min : { w: grid.MIN_W, h: grid.MIN_H };
    return applyRect(instanceId, grid.resizedBy(widget, cell, { columns, minW: min.w, minH: min.h }));
  }

  function hit(point, metrics) {
    return grid.hitTest(rects(), point, metrics);
  }

  /** 局部配置：写这一层的**完整期望内容**，缺键即"这层不设了"（与服务端的替换语义一致）。 */
  function setConfig(instanceId, key, value) {
    if (guard()) return null;
    const widget = state.widgets.find((row) => row.instance_id === instanceId);
    if (!widget) return null;
    const entry = spec(key);
    if (!entry || !entry.layers.includes("instance")) {
      state.errors = [{ reason: "key_not_editable", message: `${key} 不在实例层可写键里` }];
      return null;
    }
    if (value === null || value === undefined) delete widget.config[key];
    else widget.config[key] = value;
    state.draft.add(instanceId);
    state.errors = [];
    return widget;
  }

  function resetConfig(instanceId) {
    const widget = state.widgets.find((row) => row.instance_id === instanceId);
    if (!widget || guard()) return null;
    widget.config = {};
    state.draft.add(instanceId);
    return widget;
  }

  function setLayoutDefault(key, value) {
    if (guard() || !state.layout) return null;
    state.layout = {
      ...state.layout,
      defaults: value === null ? without(state.layout.defaults || {}, key) : { ...(state.layout.defaults || {}), [key]: value },
    };
    state.draft.add(LAYOUT_ONLY);
    return state.layout;
  }

  const without = (object, key) => {
    const copy = { ...object };
    delete copy[key];
    return copy;
  };

  const pendingKeys = (widget) =>
    Object.keys(widget.config).filter((key) => widget.config[key] !== widget.server_config[key]);

  // --- 选中与联动 -------------------------------------------------------------

  function select(instanceId) {
    state.selection = instanceId;
  }

  /** 组件间联动只带 id 与种类，不带"结论"：结论属 S8 的证据链。 */
  function linkTo(kind, id) {
    state.link = { kind, id };
    state.linkSubscribers.forEach((fn) => fn(state.link));
    return state.link;
  }

  function onLink(fn) {
    state.linkSubscribers.add(fn);
    return () => state.linkSubscribers.delete(fn);
  }

  // --- 保存 / 结构动作 --------------------------------------------------------

  function payload(widget) {
    return {
      plugin_id: widget.isNew ? widget.plugin_id : undefined,
      pos_x: widget.x,
      pos_y: widget.y,
      pos_w: widget.w,
      pos_h: widget.h,
      sort: widget.sort,
      config: { ...widget.config },
    };
  }

  /**
   * 保存草稿：先删后写，逐格独立成事——一格被 409 拒了不该把别的格子也拖下水，
   * 但**服务端的容量闸是按整笔事务判的**，所以这里的"逐格"是逐格 PUT，不是一次批量提交。
   */
  async function save() {
    if (guard()) return { ok: false, errors: state.errors };
    if (!state.layout) return { ok: false, errors: [{ reason: "no_layout", message: "还没有布局，没有可保存的对象" }] };
    const errors = [];
    for (const [instanceId] of state.removed) {
      try {
        await api.deleteWidget(state.layout.layout_id, instanceId);
      } catch (error) {
        errors.push({ ...describe(error), instance_id: instanceId });
      }
    }
    state.removed.clear();
    for (const instanceId of [...state.draft]) {
      if (instanceId === LAYOUT_ONLY) {
        try {
          state.layout = await api.patchLayout(state.layout.layout_id, { defaults: state.layout.defaults || {} });
        } catch (error) {
          errors.push({ ...describe(error), layout: true });
        }
        continue;
      }
      const widget = state.widgets.find((row) => row.instance_id === instanceId);
      if (!widget) continue;
      try {
        const row = await api.putWidget(state.layout.layout_id, widget.instance_id, payload(widget));
        Object.assign(widget, {
          effective: row.effective,
          config: { ...row.config },
          server_config: { ...row.config },
          declaration_error: row.declaration_error,
          isNew: false,
        });
      } catch (error) {
        errors.push({ ...describe(error), instance_id: instanceId });
      }
    }
    if (!errors.length) {
      state.draft.clear();
      state.savedAt = new Date().toISOString();
      state.errors = [];
      await loadLayouts(context.state.siteId);
    } else {
      // 有格子被拒：脏标记留在原处，页面把红点打回那块格子，而不是装作保存过了。
      state.errors = errors;
    }
    return { ok: !errors.length, errors };
  }

  async function rename(name) {
    if (guard() || !state.layout) return null;
    try {
      state.layout = await api.patchLayout(state.layout.layout_id, { name });
      await refreshList();
      return state.layout;
    } catch (error) {
      state.errors = [describe(error)];
      return null;
    }
  }

  async function saveAs(name) {
    if (guard() || !state.layout) return null;
    const saved = await save();
    if (!saved.ok) return { ok: false, errors: saved.errors };
    try {
      const copy = await api.cloneLayout(state.layout.layout_id, name ? { name } : {});
      await switchLayout(copy.layout_id);
      return copy;
    } catch (error) {
      state.errors = [describe(error)];
      return null;
    }
  }

  async function switchLayout(layoutId) {
    if (guardInstance()) return null;
    try {
      await api.activateLayout(layoutId);
      await refreshList();
      return await openLayout(layoutId);
    } catch (error) {
      state.errors = [describe(error)];
      return null;
    }
  }

  async function createLayout(name) {
    if (guardInstance()) return null;
    try {
      const created = await api.createLayout({ site_id: context.state.siteId, name });
      await refreshList();
      await switchLayout(created.layout_id);
      return created;
    } catch (error) {
      state.errors = [describe(error)];
      return null;
    }
  }

  async function refreshList() {
    state.layouts = await api.layouts(context.state.siteId);
    const current = state.layout && state.layouts.find((row) => row.layout_id === state.layout.layout_id);
    if (current) state.layout = current;
    return state.layouts;
  }

  /** 丢弃草稿，回到服务端那份。判据是"库里那份没被我没保存的改动污染"，所以清空脏集合。 */
  async function restore() {
    releaseAll();
    state.draft.clear();
    state.removed.clear();
    state.errors = [];
    return loadLayouts(context.state.siteId);
  }

  function describe(error) {
    if (error && error.reason) {
      return {
        reason: error.reason,
        message: message_for(error),
        detail: error.detail,
        fields: error.fields,
      };
    }
    return { reason: "unknown", message: String(error && error.message ? error.message : error) };
  }

  function message_for(error) {
    switch (error.reason) {
      case "ring_capacity_exceeded":
        return `这组刷新周期与时间范围要 ${error.detail.points} 个点，环形缓冲每槽只有 ${error.detail.ring_depth_per_slot}：装不下`;
      case "invalid_settings":
        return `设置被拒：${(error.detail.errors || []).map((item) => `${item.key}=${item.reason}`).join("、")}`;
      case "confirm_required":
        return `这几把键会改变拨号目标，需要你逐个确认：${(error.detail.keys || []).join("、")}`;
      case "duplicate_layout_name":
        return "这个场地里已经有同名布局了";
      case "active_layout_conflict":
        // 正常情况下清旧与置新是两条有先后的语句，撞不到这里；能撞上的只有"两个浏览器
        // 同时抢这一场地的当前布局"。让运营者知道该重选一次，而不是留着那个英文 reason。
        return "有另一笔切换同时抢在了前面：这一份没切成，重新选一次";
      case "layout_version_too_new":
        // 这条是"切不过去"那句回显：只把 reason 甩在页面上等于让运营者去猜一个英文标识符。
        return `这份布局的结构版本是 ${error.detail.version}，本工作台只认到 ${error.detail.supported}：升级工作台之后才切得过去`;
      case "plugin_id_required":
        return `格子 ${error.detail.instance_id} 的配置行没了：先保存这块格子的创建`;
      case "site_required":
        return "缺少 ?site=：布局按场地隔离，页面不该问到别的场地";
      case "network_unreachable":
        return "后端没有答话";
      case "request_invalid":
        return `请求体不合法：${(error.fields || []).map((field) => `${field.field}(${field.type})`).join("、")}`;
      default:
        return error.reason || "未知错误";
    }
  }

  /**
   * 组件读生效值。已保存的格子读服务端算好的那五层——回 `null` 就是"确实没设"，不猜。
   * 新建还没落库的格子没有这一份，退到全局生效值（少的是场地/布局/实例三层，不是换算法）：
   * 否则一块新格子会拿不到 `refresh_ms` 而按页面自己的偏好跑，那正是规范反对的事。
   */
  function effectiveOf(widget, key) {
    const local = widget.effective && widget.effective[key];
    if (!widget.isNew) return local ? local.value : null;
    if (local && local.value !== null && local.value !== undefined) return local.value;
    const globals = context.state.globals && context.state.globals.settings;
    const entry = globals && globals[key];
    return entry ? entry.value : null;
  }

  return {
    state,
    spec,
    editableKeys,
    loadSchema,
    loadLayouts,
    openLayout,
    refreshList,
    add,
    remove,
    duplicate,
    applyRect,
    dragBy,
    resizeTo,
    hit,
    select,
    linkTo,
    onLink,
    setConfig,
    resetConfig,
    setLayoutDefault,
    pendingKeys,
    save,
    rename,
    saveAs,
    switchLayout,
    createLayout,
    restore,
    layoutRows,
    rects,
    lease,
    release,
    releaseAll,
    audit,
    readOnlyReason,
    effectiveOf,
    describe,
    demo,
    widgetsFor: (pluginId) => state.widgets.filter((widget) => widget.plugin_id === pluginId),
  };
}
