/**
 * 工作台根实例：**只做编排**——业务判断都在 `store.js`（格子与布局）、`context.js`
 * （场地层级与状态栏）、`live.js`（整页一条 `/ws/live`）、`grid.js`（几何）、`registry.js`
 * （组件声明）里，这个文件负责把它们接到 Vue 与真实指针事件上。
 *
 * 四条形态上的约束：
 *
 * 1. **无构建**。Vue 由 `index.html` 以脚本注入（三个来源，最后一个是仓库副本），所以这里
 *    不能 `import { h } from "vue"`；`mount()` 从 `globalThis.Vue` 取命名空间，再以 `rt`
 *    传下去。取不到就报错并停下——白屏加控制台不算错误状态。
 * 2. **store 不是响应式的**。它是纯 JS，为的是能在 Node 里被 `node --test` 断言。页面用一根
 *    `signal.tick` 计数器把"我刚改了状态"告诉 Vue：所有会改状态的动作都走 `actions.*`，
 *    那里统一 bump。组件里凡是读 store/context/live 的渲染都要先读一次自己的节拍。
 * 3. **实时数据只有一条连接，挂在场地上**。`live` 由 `boot()` 与 `actions.selectSite` 两处
 *    决定订哪一个 `?site=`，卸载时 `close()`；它不是响应式的，所以渲染靠每一格自己的
 *    `refresh_ms` 节拍去重读（`live.js` 文件头写了为什么帧不直接催渲染）。
 * 4. **诊断入口做在产品里**（`window.rftrackWorkbench`）。"切场地到底有没有碰写端点"这种
 *    问题不该靠人盯网络面板：`api.audit()` 那份有界调用记录与 `live.status()` 那份连接账
 *    就摆在这里，页面底部显示条数，e2e 与人工排查读的是同一个对象。
 */

import { createApi } from "./api.js";
import { build as buildComponents, declaredWithoutImpl, implWithoutDeclared } from "./components/index.js";
import { fmtClock, stageNote } from "./components/parts.js";
import { createContext } from "./context.js";
import * as grid from "./grid.js";
import { createLive } from "./live.js";
import * as registry from "./registry.js";
import { createStore } from "./store.js";

/** 一行格子的高度（CSS 像素）。栅格的纵向尺寸只有这一个真相，`styles.css` 不再另设一份。 */
const CELL_H = 84;
/** 窄屏上给每列留的下限：再窄字就挤成一团，此时工作区横向滚动而不是把字压掉。 */
const MIN_CELL_W = 56;

export function mount(el, rtOverride) {
  const rt = rtOverride || globalThis.Vue;
  if (!rt || typeof rt.createApp !== "function") {
    throw new Error("Vue 运行时不在：index.html 的三个来源都没加载成功");
  }
  const { h, ref } = rt;

  const api = createApi({ location: globalThis.location });
  const context = createContext({ api, staleAfterS: 120 });
  // 整页一条 /ws/live：它跟着"当前场地"走，切换场地由 `actions.selectSite` 负责换订。
  // 判过期比后端那句 `node_offline`（`capture_silence_s`，默认 5 s）晚，是有意的：页面这一侧
  // 要覆盖"帧整体在进、只是这一拍的还没到"的抖动，而事件流已经把真断流先说出去了。
  const live = createLive({ base: api.base, location: globalThis.location, staleAfterS: 15 });
  const store = createStore({ api, context, registry, timers: globalThis });
  const tick = ref(0);
  const signal = { tick, bump: () => { tick.value += 1; } };
  const drag = ref(null);
  const ws = ref(null);
  const cellW = ref(MIN_CELL_W);
  const panel = ref("props");
  const notice = ref(null);
  const layoutName = ref("");
  // 观察器要等 workspace 元素真的渲染出来才有目标；卸载钩子必须在 setup 主体注册（见 root.setup）。
  let observer = null;

  /** 上一屏点中的那一格：格子还在就把它选回来，不在了就当没这回事（不硬造一个选中态）。 */
  function restoreSelection(siteId) {
    const memo = context.memoryFor(siteId);
    if (memo.instanceId && store.state.widgets.some((row) => row.instance_id === memo.instanceId)) {
      store.select(memo.instanceId);
    }
  }

  /** 动作层：唯一被允许改状态的地方。每个动作改完就 bump，页面才可能看见结果。 */
  const actions = {
    async selectSite(siteId) {
      const id = Number(siteId);
      // 先记下**离开前**这一场地的那一屏，再切上下文：反过来的话记进去的是上一场地的布局号。
      remember();
      await context.selectSite(id);
      // 换订跟着 `context` 真正选中的那一个，而不是传进来的 `id`：非法 id 会被规范化成默认场地，
      // 跟着原始参数走就会订到一个页面上根本没显示的场地。
      live.attach(context.state.siteId);
      // 「当前布局」只有一份真相：服务端按场地各存一份 `is_active`，所以切场地天然各留各的。
      await store.loadLayouts(id);
      restoreSelection(id);
      remember();
      signal.bump();
    },
    selectRelay(relayId) {
      context.selectRelay(Number(relayId));
      remember();
      signal.bump();
    },
    selectGateway(gatewayId) {
      context.selectGateway(Number(gatewayId));
      remember();
      signal.bump();
    },
    selectNode(nodeId) {
      // 下拉给的是字符串，`node_id` 是整数：不同一处，选中态会当场掉回第一块板，
      // 状态栏那句"板 = …"就成了假的（`selectRelay`/`selectGateway` 同一条口径）。
      const id = Number(nodeId);
      context.selectNode(id);
      store.linkTo("node", id);
      remember();
      signal.bump();
    },
    linkChannel(slot) {
      store.linkTo("channel", slot);
      remember();
      signal.bump();
    },
    select(instanceId) {
      store.select(instanceId);
      remember();
      signal.bump();
    },
    add(pluginId) {
      if (!store.add(pluginId)) {
        notice.value = registry.component(pluginId)
          ? "这块格子加不上：先看上方只读说明"
          : "注册表里没有这个组件";
      }
      remember();
      signal.bump();
    },
    remove(instanceId) {
      store.remove(instanceId);
      remember();
      signal.bump();
    },
    duplicate(instanceId) {
      store.duplicate(instanceId);
      remember();
      signal.bump();
    },
    setConfig(instanceId, key, value) {
      store.setConfig(instanceId, key, value);
      signal.bump();
    },
    resetConfig(instanceId) {
      store.resetConfig(instanceId);
      signal.bump();
    },
    setLayoutDefault(key, value) {
      store.setLayoutDefault(key, value);
      signal.bump();
    },
    async save() {
      const result = await store.save();
      notice.value = result.ok ? "已保存" : null;
      remember();
      signal.bump();
      return result;
    },
    async restore() {
      await store.restore();
      notice.value = "已回到库里那一份";
      signal.bump();
    },
    async rename() {
      if (!layoutName.value.trim()) return report("重命名要先填布局名");
      if (await store.rename(layoutName.value.trim())) notice.value = "已改名";
      signal.bump();
    },
    async saveAs() {
      if (!layoutName.value.trim()) return report("另存为要先填新布局名");
      const copy = await store.saveAs(layoutName.value.trim());
      if (copy) notice.value = `已另存为「${copy.name}」并切过去了`;
      signal.bump();
    },
    async createLayout() {
      if (!layoutName.value.trim()) return report("新建要先填布局名");
      if (!context.state.siteId) return report("先选一个场地");
      const created = await store.createLayout(layoutName.value.trim());
      if (created) {
        notice.value = `已建并切到「${created.name}」`;
        layoutName.value = "";
      }
      signal.bump();
    },
    async switchLayout(layoutId) {
      await store.switchLayout(Number(layoutId));
      remember();
      signal.bump();
    },
    async removeLayout(layoutId) {
      try {
        await api.deleteLayout(Number(layoutId));
        await store.loadLayouts(context.state.siteId);
        notice.value = "布局已删除";
      } catch (error) {
        store.state.errors = [store.describe(error)];
      }
      signal.bump();
    },

    // --- 建库引导（site-tree 向导调用；成败与原因由组件就地显示） -------------
    // 这些动作**只许**在用户点向导按钮时被调到——"切场地不碰写端点"的审计判据
    // 依然成立：组件依旧不直接持有 api 写口，写只从这里过。
    async createSite(payload) {
      const created = await api.createSite(payload);
      await context.refreshSites();
      await actions.selectSite(created.site_id);
      return created;
    },
    async createRelay(payload) {
      const created = await api.createRelay(payload);
      await context.loadHierarchy(context.state.siteId);
      context.selectRelay(created.relay_id);
      remember();
      signal.bump();
      return created;
    },
    async createGateway(payload) {
      const created = await api.createGateway(payload);
      await context.loadHierarchy(context.state.siteId);
      context.selectGateway(created.gateway_id);
      remember();
      signal.bump();
      return created;
    },
    async probeGateway(gatewayId) {
      const result = await api.probeGateway(Number(gatewayId));
      // probe 会更新 dial_state/last_seen：层级重取一次，树上那枚拨号徽标说的才是现在。
      await context.loadHierarchy(context.state.siteId);
      context.reconcile();
      signal.bump();
      return result;
    },
  };

  function report(message) {
    notice.value = message;
    return null;
  }

  /**
   * 记住本场地这一屏的选中态（格子/位置/接入机/联动），切回来还是原来那一张。
   * 末尾那次 `persist()` 是为了"刷新一下也回到这一屏"——规范 §10.1 第三条要的正是它。
   */
  function remember() {
    if (!context.state.siteId) return;
    const memo = context.memoryFor(context.state.siteId);
    memo.layoutId = store.state.layout ? store.state.layout.layout_id : null;
    memo.instanceId = store.state.selection;
    context.persist();
  }

  const deps = { store, context, live, actions, signal, registry, api };
  const kit = buildComponents(rt, deps);

  // --- 几何 -------------------------------------------------------------------

  function metrics() {
    const el = ws.value;
    if (!el) return { left: 0, top: 0, cellW: cellW.value, cellH: CELL_H };
    const rect = el.getBoundingClientRect();
    return {
      left: rect.left - el.scrollLeft,
      top: rect.top - el.scrollTop,
      cellW: cellW.value,
      cellH: CELL_H,
    };
  }

  function frameStyle(widget) {
    return {
      left: `${widget.x * cellW.value}px`,
      top: `${widget.y * CELL_H}px`,
      width: `${widget.w * cellW.value}px`,
      height: `${widget.h * CELL_H}px`,
    };
  }

  function measure() {
    const el = ws.value;
    if (!el) return;
    // 内容宽度取"元素实宽"与"12 列下限宽"的较大者：窄屏时后者生效，工作区横向滚动。
    const width = Math.max(el.clientWidth, grid.COLUMNS * MIN_CELL_W);
    cellW.value = width / grid.COLUMNS;
    // 列宽同时写给 CSS：背景网格线必须和格子对齐，否则"看着对齐了"是假的。
    el.style.setProperty("--cell", `${cellW.value}px`);
  }

  // --- 拖拽与缩放 -------------------------------------------------------------

  /**
   * 拖拽期间的 move/up 挂在 `window` 上，只在一次拖动的生命周期里存在。
   *
   * 为什么不光靠画布上的监听：把手往下拉出画布那一段（底部几行放大、或快速拖到右侧面板上）
   * 指针已经不在 `.workspace` 盒子里了，挂在画布上的监听直接断掉，尺寸改到一半停住。
   * `setPointerCapture` 是给真指针兜底的那一条，但它不是唯一一条路：CDP 合成的指针事件里
   * 捕获是成功的（`hasPointerCapture` 回 true），事件却照样打在 `cols`/`status` 上。
   * 所以这里把 window 监听当主路，捕获当附加保险。
   */
  const dragHooks = { move: null, up: null, cancel: null };

  function watchDrag() {
    if (dragHooks.move || !globalThis.addEventListener) return;
    dragHooks.move = (event) => moveDrag(event);
    dragHooks.up = (event) => endDrag(event);
    dragHooks.cancel = (event) => endDrag(event);
    globalThis.addEventListener("pointermove", dragHooks.move);
    globalThis.addEventListener("pointerup", dragHooks.up);
    globalThis.addEventListener("pointercancel", dragHooks.cancel);
  }

  function unwatchDrag() {
    if (!dragHooks.move || !globalThis.removeEventListener) return;
    globalThis.removeEventListener("pointermove", dragHooks.move);
    globalThis.removeEventListener("pointerup", dragHooks.up);
    globalThis.removeEventListener("pointercancel", dragHooks.cancel);
    dragHooks.move = dragHooks.up = dragHooks.cancel = null;
  }

  function startDrag(event, widget, mode) {
    if (store.readOnlyReason()) return;
    store.select(widget.instance_id);
    // 抓把手本身就等于"这一格是我要的那一格"：不记一笔，刷新回来面板会停在别的格子上。
    remember();
    drag.value = {
      instance_id: widget.instance_id,
      mode,
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      rect: { instance_id: widget.instance_id, x: widget.x, y: widget.y, w: widget.w, h: widget.h },
      last: null,
    };
    const el = ws.value;
    if (el && el.setPointerCapture) {
      try {
        el.setPointerCapture(event.pointerId);
      } catch {
        // 指针在这一刻不可捕获（合成事件、或它已经自己结束了）：window 那条路还在。
      }
    }
    watchDrag();
    event.preventDefault();
    signal.bump();
  }

  function moveDrag(event) {
    const current = drag.value;
    if (!current) return;
    const widget = store.state.widgets.find((row) => row.instance_id === current.instance_id);
    if (!widget) {
      drag.value = null;
      return;
    }
    const definition = registry.component(widget.plugin_id);
    const min = definition ? definition.min : { w: grid.MIN_W, h: grid.MIN_H };
    let next;
    if (current.mode === "resize") {
      const cell = grid.pointToCell(
        { x: event.clientX, y: event.clientY },
        metrics()
      );
      next = grid.resizedBy(current.rect, cell, { columns: grid.COLUMNS, minW: min.w, minH: min.h });
    } else {
      next = grid.movedBy(
        current.rect,
        {
          x: Math.round((event.clientX - current.startX) / cellW.value),
          y: Math.round((event.clientY - current.startY) / CELL_H),
        },
        { columns: grid.COLUMNS }
      );
    }
    // 只在真的换了一格时写状态并重画：pointermove 是几十 Hz，逐事件刷会把主线程变成刷屏。
    const same = current.last && current.last.x === next.x && current.last.y === next.y
      && current.last.w === next.w && current.last.h === next.h;
    if (same) return;
    current.last = next;
    store.applyRect(current.instance_id, next);
    signal.bump();
  }

  function endDrag(event) {
    if (!drag.value) return;
    const el = ws.value;
    if (el && el.releasePointerCapture && drag.value.pointerId !== undefined) {
      try {
        el.releasePointerCapture(drag.value.pointerId);
      } catch {
        // 指针已经自己结束了（浏览器在某些平台会先释放）：这里没什么可收拾的。
      }
    }
    unwatchDrag();
    drag.value = null;
    if (event) event.preventDefault();
    signal.bump();
  }

  // --- 各段渲染 ---------------------------------------------------------------

  function toolbar() {
    const ctx = context.state;
    const option = (value, label, selected) =>
      h("option", { value: String(value), selected }, label);
    const picker = (label, rows, key, nameOf, current, onPick, emptyText) =>
      h("label", { class: "picker" }, [
        h("span", { class: "picker-label" }, label),
        rows.length
          ? h(
              "select",
              { value: String(current ?? ""), onChange: (event) => onPick(event.target.value) },
              rows.map((row) => option(row[key], nameOf(row), row[key] === current))
            )
          : h("span", { class: "picker-empty" }, emptyText),
      ]);

    return h("div", { class: "toolbar" }, [
      picker("场地", ctx.sites, "site_id", (row) => row.name, ctx.siteId, actions.selectSite, "库里没有场地"),
      picker("位置", ctx.relays, "relay_id", (row) => row.name, ctx.relayId, actions.selectRelay, "本场地没有位置"),
      picker(
        "接入机",
        ctx.scopeGateways,
        "gateway_id",
        (row) => `${row.name}（${row.dial_state || "未拨号"}）`,
        ctx.gatewayId,
        actions.selectGateway,
        "本位置没有接入机"
      ),
      picker("板", ctx.scopeNodes, "node_id", (row) => row.display_name, ctx.nodeId, actions.selectNode, "这台接入机没有板"),
      h("span", { class: "spacer" }),
      store.state.layouts.length
        ? h(
            "select",
            {
              class: "layout-pick",
              // 没有当前布局时必须给一条占位：否则浏览器把第一项显示成"选中"的样子，
              // 而服务端那边 `is_active` 是空的——下拉长得像在值班视图，其实谁都不是。
              value: String(store.state.layout ? store.state.layout.layout_id : ""),
              onChange: (event) => actions.switchLayout(event.target.value),
              title: "当前布局（服务端 is_active 那一份）",
            },
            (store.state.layout ? [] : [option("", "（没有当前布局，选一份）", true)]).concat(
              store.state.layouts.map((row) =>
                option(row.layout_id, `${row.name}${row.is_active ? " ★" : ""}${row.degraded ? "（降级）" : ""}`,
                  store.state.layout && row.layout_id === store.state.layout.layout_id)
              )
            )
          )
        : h("span", { class: "picker-empty" }, "本场地没有布局"),
      h("input", {
        class: "name-input",
        type: "text",
        placeholder: "布局名",
        value: layoutName.value,
        onInput: (event) => { layoutName.value = event.target.value; },
      }),
      button("新建", () => actions.createLayout()),
      button("重命名", () => actions.rename()),
      button("另存为", () => actions.saveAs()),
      button("刷新恢复", () => actions.restore()),
      button("保存", () => actions.save(), {
        primary: true,
        disabled: !dirtyCount(),
        badge: dirtyCount() ? String(dirtyCount()) : null,
      }),
    ]);
  }

  const button = (label, onClick, { primary = false, disabled = false, badge = null } = {}) =>
    h(
      "button",
      {
        class: ["btn", primary ? "btn--primary" : null],
        type: "button",
        disabled,
        onClick,
      },
      [label, badge ? h("span", { class: "btn-badge" }, badge) : null]
    );

  function library() {
    const facts = { capabilities: context.capabilities() };
    const rows = registry.library(facts);
    const drift = [].concat(declaredWithoutImpl(), implWithoutDeclared());
    return h("aside", { class: "panel panel--library" }, [
      h("h2", "组件库"),
      h("p", { class: "muted" }, "未声明即不支持：灰掉的条目写的是缺哪一路能力，不是坏了"),
      h(
        "ul",
        { class: "lib" },
        rows.map((entry) =>
          h("li", { class: ["lib-item", entry.available ? null : "lib-item--off"] }, [
            entry.kind === "live"
              ? h("button", {
                  class: "lib-add",
                  type: "button",
                  disabled: !entry.available,
                  onClick: () => actions.add(entry.id),
                  title: entry.reason || `加一块 ${entry.name}（${entry.size.w}×${entry.size.h}）`,
                }, [
                  h("strong", entry.name),
                  h("span", { class: "lib-src" }, registry.sourceWord(entry.source)),
                  h("span", { class: "lib-size" }, `${entry.size.w}×${entry.size.h}`),
                ])
              : h("div", { class: "lib-planned" }, [
                  h("strong", entry.name),
                  h("span", { class: "lib-size" }, `计划 ${entry.stage || "未定"}`),
                ]),
            h("p", { class: "lib-summary" }, entry.kind === "live" ? entry.summary : entry.reason),
            entry.kind === "planned" ? h("p", { class: "lib-reason" }, `不上线的原因：${entry.reason}`) : null,
            !entry.available && entry.kind === "live" ? h("p", { class: "lib-reason" }, entry.reason) : null,
          ])
        )
      ),
      drift.length
        ? h("p", { class: "warn" }, `注册表与实现不一致：${drift.join("、")}（这次发布少装了文件或声明没撤）`)
        : null,
    ]);
  }

  /** 待删的格子也是脏的：只看 `draft` 的话"删完格子"这一件事永远点不动保存。 */
  const dirtyCount = () => store.state.draft.size + store.state.removed.size;

  /**
   * 页面主题取"布局默认 → 全局生效值 → dark"。
   *
   * `theme` 在服务端是可挂到实例层的键，但文档只有一套 `data-theme`：一块格子改了自己的值
   * 不能把整页重画（那会变成"点谁谁改脸"）。所以这里只认布局默认与全局两层，
   * 实例那层的值照常出现在生效表里（它的来源链是真的），只是不驱动页面。
   */
  function pageTheme() {
    const defaults = store.state.layout && store.state.layout.defaults;
    if (defaults && defaults.theme) return defaults.theme;
    const globals = store.state.globals && store.state.globals.settings;
    const entry = globals && globals.theme;
    return entry && entry.value ? entry.value : "dark";
  }

  function applyTheme() {
    const theme = pageTheme();
    if (globalThis.document) globalThis.document.documentElement.dataset.theme = theme;
    return theme;
  }

  function workspace() {
    const blocked = store.readOnlyReason();
    const widgets = store.state.widgets;
    const body = [];
    if (store.state.loading) body.push(h("p", { class: "state" }, "正在读这个场地的布局……"));
    if (store.state.error) {
      body.push(
        h("p", { class: "state state--error" }, [
          `读布局失败：${store.state.error.reason}`,
          store.state.error.message ? h("span", { class: "muted" }, ` ${store.state.error.message}`) : null,
        ])
      );
    }
    if (blocked) {
      body.push(
        h("p", { class: ["notice", "notice--readonly"], "data-reason": blocked.reason }, [
          h("strong", "只读："),
          blocked.message,
          h("span", { class: "muted" }, `（${blocked.reason}）`),
        ])
      );
    }
    if (!widgets.length && !store.state.loading && !store.state.error) {
      // 「没有场地」「有场地但没当前布局」「布局是空的」是三件事，说成一句会让人去点错那一层。
      body.push(h("p", { class: "state" },
        !context.state.siteId
          ? "还没选场地：上方『场地』里选一个（本实例配了当前场地就不会走到这句）"
          : store.state.layout
            ? "这块画布还是空的：从左边组件库加一格"
            : "本场地没有当前布局（刚删掉的就是当前那份）：在上方下拉里选一份，或点『新建』"));
    }
    return h(
      "section",
      {
        class: ["workspace", drag.value ? "workspace--dragging" : null],
        ref: ws,
        // move/up 不在这里挂：拖动期间它们挂在 window 上（见 `watchDrag`），画布只管"点在空白处取消选中"。
        onPointerdown: (event) => {
          if (event.target === ws.value) {
            store.select(null);
            remember();
            signal.bump();
          }
        },
        style: { minHeight: `${CELL_H * 8}px` },
      },
      body.concat(
        widgets.map((widget) => frame(widget)),
        store.state.errors.length
          ? h("p", { class: "state state--error" }, store.state.errors.map((error) => error.message || error.reason).join("；"))
          : null
      ).filter(Boolean)
    );
  }

  function frame(widget) {
    const definition = registry.component(widget.plugin_id);
    const selected = store.state.selection === widget.instance_id;
    const dirty = store.state.draft.has(widget.instance_id);
    const mode = widget.missing ? null : definition && definition.source;
    return h("div", {
      class: [
        "widget",
        selected ? "widget--selected" : null,
        dirty ? "widget--dirty" : null,
        widget.missing ? "widget--missing" : null,
        drag.value && drag.value.instance_id === widget.instance_id ? "widget--dragging" : null,
      ],
      style: frameStyle(widget),
      // 写成 `data-*` 字面属性而不是 `dataset: {...}`：Vue 的 `h()` 会把 `dataset` 当成
      // "元素上已有的属性"去赋值，而 `el.dataset` 是只读的 getter，prod 包里连警告都没有，
      // 整批 DOM 钩子会静默消失（冒烟就是这么撞出来的）。
      "data-instance": widget.instance_id,
      "data-plugin": widget.plugin_id,
      "data-source": mode || "unknown",
    }, [
      h("header", {
        class: "widget-head",
        onPointerdown: (event) => startDrag(event, widget, "move"),
        title: "按住标题栏拖动",
      }, [
        h("span", { class: "widget-title" }, definition ? definition.name : widget.plugin_id),
        widget.missing
          ? h("span", { class: "badge badge--missing" }, "未注册")
          : h(
              "span",
              {
                class: ["badge", `badge--path-${definition.source}`],
                // 头部这枚说的是**通路**（这一格设计上读哪条路），格子里那枚说的才是
                // **此刻这份数据的来源**（真实帧 / 回放帧 / REST 读数）。两者不能共用一张词表：
                // 拿 `sourceBadge` 画通路会把 `live` 显示成"来源未知"——通路明明是注册表里写着的。
                title: `这一格的数据通路：${registry.sourceWord(definition.source)}（格子里那枚才是此刻这份数的来源）`,
              },
              registry.sourceWord(definition.source)
            ),
        widget.isNew ? h("span", { class: "badge badge--new" }, "未保存") : null,
        dirty && !widget.isNew ? h("span", { class: "badge badge--dirty" }, "有改动") : null,
        widget.declaration_error
          ? h("span", { class: "badge badge--bad", title: widget.declaration_error }, "配置行有问题")
          : null,
        h("span", { class: "spacer" }),
        h("button", {
          class: "icon",
          type: "button",
          title: "复制这块格子",
          onPointerdown: (event) => event.stopPropagation(),
          onClick: () => actions.duplicate(widget.instance_id),
        }, "⧉"),
        h("button", {
          class: "icon",
          type: "button",
          title: "删除这块格子",
          onPointerdown: (event) => event.stopPropagation(),
          onClick: () => actions.remove(widget.instance_id),
        }, "✕"),
      ]),
      h("div", { class: "widget-body" }, [h(kit.resolve(widget), { widget })]),
      h("span", {
        class: "handle",
        title: "拉右下角改尺寸",
        onPointerdown: (event) => { event.stopPropagation(); startDrag(event, widget, "resize"); },
      }),
    ]);
  }

  function propsPanel() {
    const widget = store.state.widgets.find((row) => row.instance_id === store.state.selection) || null;
    return h("aside", { class: "panel panel--props" }, [
      h("div", { class: "panel-tabs" }, [
        button("属性", () => { panel.value = "props"; signal.bump(); }, { primary: panel.value === "props" }),
        button("布局默认", () => { panel.value = "layout"; signal.bump(); }, { primary: panel.value === "layout" }),
      ]),
      panel.value === "layout" ? layoutDefaults() : widget ? widgetProps(widget) : h("p", { class: "muted" }, "点一块格子看它的生效值与来源链"),
    ]);
  }

  function widgetProps(widget) {
    const definition = registry.component(widget.plugin_id);
    const pending = store.pendingKeys(widget);
    return h("div", { class: "props" }, [
      h("h2", definition ? definition.name : "未注册组件"),
      definition ? h("p", { class: "muted" }, definition.summary) : null,
      h("dl", { class: "kv" }, [
        h("dt", "组件 id"), h("dd", h("code", widget.plugin_id)),
        h("dt", "实例 id"), h("dd", h("code", widget.instance_id)),
        h("dt", "位置"), h("dd", `${widget.x},${widget.y} · ${widget.w}×${widget.h}`),
        h("dt", "数据源"), h("dd", definition ? registry.sourceWord(definition.source) : "未知"),
        h("dt", "改动"), h("dd", pending.length ? `待保存：${pending.join("、")}` : "无"),
      ]),
      h("h3", "实例层覆写"),
      store.state.error && !store.state.schema.length
        ? h("p", { class: "muted" }, "还没拿到键注册表，覆写控件读不到声明")
        : h(
            "div",
            { class: "keys" },
            store.editableKeys("instance").map((entry) => control(widget, entry, "instance"))
          ),
      h("p", { class: "panel-actions" }, [
        button("取消这一层全部覆写", () => actions.resetConfig(widget.instance_id), { disabled: !Object.keys(widget.config).length }),
        button("删除这块格子", () => actions.remove(widget.instance_id)),
      ]),
      h("h3", "生效值与来源链"),
      effectiveTable(widget),
      h("p", { class: "muted" }, "顺序：内置默认 < 全局 < 场地 < 布局 < 实例。标了『被拒』的是库里存着但没生效的覆写。"),
    ]);
  }

  function effectiveTable(widget) {
    const keys = Object.keys(widget.effective || {});
    if (!keys.length) return h("p", { class: "muted" }, widget.isNew ? "还没保存，服务端没算过这一格的生效值" : "服务端没给出生效值");
    const kindOf = (key) => {
      const spec = store.state.schema.find((entry) => entry.key === key);
      return spec ? spec.kind : null;
    };
    return h(
      "table",
      { class: "kv-table" },
      keys.map((key) => {
        const entry = widget.effective[key];
        const kind = kindOf(key);
        return h("tr", [
          h("th", key),
          h("td", [
            h("span", { class: "val" }, `${renderValue(entry.value, kind)}${entry.unit ? ` ${entry.unit}` : ""}`),
            h("span", { class: `src src--${entry.source}` }, entry.source),
          ]),
          h("td", { class: "chain muted" }, (entry.chain || []).map((link) => chainCell(link, kind)).join(" | ")),
          entry.rejected && entry.rejected.length
            ? h("td", { class: "warn" }, `被拒：${entry.rejected.map((item) => `${item.layer}=${item.reason}`).join("、")}`)
            : h("td", ""),
        ]);
      })
    );
  }

  /** 来源链的一格：只说这层设了什么或被拒了；"未设置的层只带层名"是服务端定的形状。 */
  function chainCell(link, kind) {
    if (link.declaration_error) return `${link.layer}:声明有问题`;
    if (link.rejected) return `${link.layer}:设了 ${renderValue(link.set_value, kind)}（被拒 ${link.rejected}）`;
    if ("value" in link) return `${link.layer}: ${renderValue(link.value, kind)}`;
    return `${link.layer}: 未设置`;
  }

  function layoutDefaults() {
    if (!store.state.layout) return h("p", { class: "muted" }, "还没有布局");
    const defaults = store.state.layout.defaults || {};
    /** 布局默认这一层没有服务端算好的生效值可读（那是每个格子各一份），只回显库里这一份。 */
    const carrier = { config: defaults, instance_id: "layout", isNew: false };
    return h("div", { class: "props" }, [
      h("h2", `布局默认 · ${store.state.layout.name}`),
      h("p", { class: "muted" }, "这一层落到本布局的每一个格子上；格子的实例覆写仍优先。"),
      store.state.layout.declaration_error
        ? h("p", { class: "warn" }, `布局默认存坏了：${store.state.layout.declaration_error}（这一层按未设置处理，下层继续生效）`)
        : null,
      h(
        "div",
        { class: "keys" },
        store.editableKeys("layout").map((entry) => control(carrier, entry, "layout"))
      ),
      h("p", { class: "panel-actions" }, [
        button("删除本布局", () => actions.removeLayout(store.state.layout.layout_id), {
          disabled: store.state.layout.widget_count > 0,
        }),
        h("span", { class: "muted" }, store.state.layout.widget_count > 0
          ? `还有 ${store.state.layout.widget_count} 块格子：先清空再删（服务端同一条规则）`
          : "只在没有格子时删得掉，服务端不会级联删任何数据"),
      ]),
    ]);
  }

  function control(widget, entry, layer) {
    const set = widget.config && Object.prototype.hasOwnProperty.call(widget.config, entry.key);
    const current = set ? widget.config[entry.key] : null;
    const write = (value) => {
      if (layer === "layout") actions.setLayoutDefault(entry.key, value);
      else actions.setConfig(widget.instance_id, entry.key, value);
    };
    const flags = [
      entry.key,
      entry.unit ? h("span", { class: "key-unit" }, entry.unit) : null,
      set ? h("span", { class: "key-flag" }, "本层已设") : null,
      entry.requires_restart ? h("span", { class: "key-flag key-flag--restart" }, "改它会换拨号对象，要重启采集") : null,
      entry.pending_stage ? h("span", { class: "key-flag" }, stageNote(rt, entry.pending_stage)) : null,
    ].filter(Boolean);
    return h("label", { class: ["key", set ? "key--set" : null], "data-key": entry.key, "data-layer": layer }, [
      h("span", { class: "key-name" }, flags),
      controlInput(entry, current, write),
    ]);
  }

  /** 控件形态全部来自服务端声明（`/api/settings/schema`）：类型、候选值、边界、单位，页面一样都不抄。 */
  function controlInput(entry, current, write) {
    const unset = current === null || current === undefined;
    if (entry.kind === "choice") {
      return h("select", {
        value: unset ? "" : String(current),
        onChange: (event) => write(event.target.value === "" ? null : event.target.value),
      }, [h("option", { value: "" }, "（不设）")].concat((entry.choices || []).map((choice) =>
        h("option", { value: choice, selected: choice === current }, choice)
      )));
    }
    if (entry.kind === "bool") {
      return h("input", {
        type: "checkbox",
        checked: current === true,
        onChange: (event) => write(event.target.checked),
      });
    }
    if (entry.kind === "int" || entry.kind === "float") {
      return h("input", {
        type: "number",
        min: entry.minimum === null || entry.minimum === undefined ? undefined : String(entry.minimum),
        max: entry.maximum === null || entry.maximum === undefined ? undefined : String(entry.maximum),
        step: entry.kind === "float" ? "0.1" : "1",
        value: unset ? "" : String(current),
        onChange: (event) => {
          const raw = event.target.value;
          if (raw === "") return write(null);
          const parsed = Number(raw);
          if (!Number.isFinite(parsed)) return report(`${entry.key} 要的是数，收到「${raw}」`);
          write(parsed);
        },
      });
    }
    if (entry.kind === "slots") {
      // 复选而不是自由数：档位号必须落在固件认识的范围内，越界的值服务端会整笔拒掉。
      const chosen = new Set(Array.isArray(current) ? current : []);
      const high = entry.maximum === null || entry.maximum === undefined ? grid.COLUMNS : entry.maximum;
      return h(
        "span",
        { class: "slots" },
        Array.from({ length: high }, (_, slot) =>
          h("label", { class: "slot" }, [
            h("input", {
              type: "checkbox",
              checked: chosen.has(slot),
              onChange: (event) => {
                if (event.target.checked) chosen.add(slot); else chosen.delete(slot);
                const list = [...chosen].sort((a, b) => a - b);
                write(list.length ? list : null);
              },
            }),
            h("span", String(slot + 1)),
          ])
        )
      );
    }
    if (entry.kind === "nodes") {
      // 多选节点而不是自由数：候选名单来自当前场地的节点表，写的是 node_id 整数列表；
      // 不勾任何节点 = 未设置，组件回落到当前选中的单板。
      const chosen = new Set(Array.isArray(current) ? current : []);
      const candidates = context.state.nodes;
      if (!candidates.length) return h("span", { class: "muted" }, "这个场地还没有节点");
      return h(
        "span",
        { class: "nodes" },
        candidates.map((node) =>
          h("label", { class: "node-check", "data-node": node.node_id }, [
            h("input", {
              type: "checkbox",
              checked: chosen.has(node.node_id),
              onChange: (event) => {
                if (event.target.checked) chosen.add(node.node_id); else chosen.delete(node.node_id);
                const list = [...chosen];
                write(list.length ? list : null);
              },
            }),
            h("span", `${node.display_name}（#${node.node_id}）`),
          ])
        )
      );
    }
    if (entry.kind === "object") {
      return h("span", { class: "muted" }, "这一档的值由设置页维护，工作台不就地改");
    }
    return h("input", {
      type: "text",
      value: unset ? "" : String(current),
      onChange: (event) => write(event.target.value === "" ? null : event.target.value),
    });
  }

  /** 生效值回显：整数不补小数点（`refresh_ms=200` 不该显示成 `200.0`）；
   *  数组按 kind 分语义：slots 是 0 基档位号（回显 +1），nodes 是 node_id 原样。 */
  function renderValue(value, kind = null) {
    if (value === null || value === undefined) return "未设置";
    if (Array.isArray(value)) {
      if (kind === "nodes") return value.map((item) => `#${item}`).join("、");
      return value.map((item) => item + 1).join(",");
    }
    if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toFixed(1);
    if (typeof value === "boolean") return value ? "是" : "否";
    return String(value);
  }

  function statusBar() {
    const bar = context.statusBar({
      layout: store.state.layout,
      dirty: dirtyCount() > 0,
      data: { value: live.dataLabel(), tone: live.dataTone() },
    });
    const calls = api.audit();
    const blocked = store.readOnlyReason();
    return h("footer", { class: "status", "data-statusbar": "1", title: bar.text }, [
      bar.parts.map((part) =>
        h("span", { class: ["status-part", `tone--${part.tone}`], "data-label": part.label }, [
          h("span", { class: "status-label" }, part.label),
          h("span", { class: "status-value" }, part.value),
        ])
      ),
      h("span", { class: "spacer" }),
      blocked ? h("span", { class: "status-part tone--warn" }, [h("span", { class: "status-label" }, "写"), h("span", { class: "status-value" }, "只读")]) : null,
      store.state.savedAt
        ? h("span", { class: "status-part" }, [h("span", { class: "status-label" }, "上次保存"), h("span", { class: "status-value" }, fmtClock(store.state.savedAt, "UTC"))])
        : null,
      h("span", { class: "status-part" }, [h("span", { class: "status-label" }, "API"), h("span", { class: "status-value" }, api.base || "根")]),
      h("span", { class: "status-part" }, [h("span", { class: "status-label" }, "调用"), h("span", { class: "status-value" }, `${calls.calls.length}${calls.dropped ? `（丢 ${calls.dropped}）` : ""}`)]),
      h("span", { class: "status-part" }, [h("span", { class: "status-label" }, "租约"), h("span", { class: "status-value" }, `${store.audit().leases}/${store.audit().widgets}`)]),
      notice.value ? h("span", { class: ["status-part", "tone--ok"], "data-notice": "1" }, notice.value) : null,
    ]);
  }

  /**
   * 状态栏单独成一个组件，而不是在根实例的渲染里直接调 `statusBar()`。
   *
   * 两个理由：①"数据"那一段每秒都可能变，跟着根实例一起重渲染就等于每帧重画组件库 + 十二列
   * 栅格 + 属性面板，而栅格里的每一格本来有自己的 `refresh_ms` 节拍；②它需要一条自己的节拍，
   * 因为 `live.js` 有意不做渲染依赖（帧到了只改数据），状态栏上的"N 块板 / 重连第 K 次 /
   * 已断开"要靠这一秒一跳才变得动。
   *
   * 但它**不能只有节拍**：`context.state` 不是响应式的（为的是能在 Node 里断言），单独成组件之后
   * 父组件重渲染并不会带着它一起重渲染——点完"板"下拉，选中态立刻换了，状态栏那句"板 = …"却要
   * 等下一跳才跟上。这一格说的正是"现在看的是哪一块"，慢一秒就是让运营者按一秒前的现场做判断。
   * 所以这里也读 `signal.tick`：所有会改状态的动作都走 `actions.*`，那里统一 bump，状态栏跟着一起变。
   */
  const statusBarView = {
    name: "StatusBar",
    setup() {
      const beat = ref(0);
      const timer = globalThis.setInterval(() => {
        beat.value += 1;
      }, 1000);
      rt.onBeforeUnmount(() => globalThis.clearInterval(timer));
      return () => {
        beat.value;
        signal.tick.value;
        return statusBar();
      };
    },
  };

  function bootScreen() {
    if (!context.state.loading && !context.state.error) return null;
    return h("div", { class: "boot-state" }, [
      context.state.loading ? h("p", "正在问后端：健康、场地与全局设置……") : null,
      context.state.error
        ? h("p", { class: "state--error" }, [
            `启动失败：${context.state.error.reason}`,
            context.state.error.message ? h("span", { class: "muted" }, ` ${context.state.error.message}`) : null,
            h("button", { class: "btn", type: "button", onClick: async () => { await boot(); signal.bump(); } }, "重试"),
          ])
        : null,
    ]);
  }

  function attachObserver() {
    if (observer || !globalThis.ResizeObserver || !ws.value) return;
    observer = new ResizeObserver(measure);
    observer.observe(ws.value);
  }

  /** boot 之后必须等一帧：启动屏期间 workspace 元素根本不存在，观察器没有目标可挂。 */
  async function afterBoot() {
    signal.bump();
    await rt.nextTick();
    measure();
    attachObserver();
  }

  async function boot() {
    await context.boot({});
    // 连接先于布局：数据通路不该等"布局读回来了"才开始收帧，而格子什么时候出现是另一回事。
    live.attach(context.state.siteId);
    if (context.state.siteId) {
      await store.loadLayouts(context.state.siteId);
      // 刷新回来的不只是场地：点中的那一格也要在，否则面板先回到"没有选中"再被人重新点，
      // 规范 §10.1 第三条要的是"刷新后布局与组件局部配置恢复正确"。
      restoreSelection(context.state.siteId);
    }
    await store.loadSchema();
    remember();
  }

  const root = {
    name: "Workbench",
    setup() {
      // 主题跟着状态走：boot 拿到全局生效值、改了布局默认、保存回读，都只需要 bump。
      rt.watchEffect(() => {
        tick.value;
        applyTheme();
      });
      rt.onMounted(async () => {
        await boot();
        await afterBoot();
      });
      rt.onBeforeUnmount(() => {
        if (observer) { observer.disconnect(); observer = null; }
        // 拖到一半被卸载（切场地、降级重建）：window 上那三条监听是这次拖动自己加的，必须跟着还掉。
        unwatchDrag();
        drag.value = null;
        // 页面级的那条推送也要还：留着它，卸载重挂（例如降级重建）就会在同一场地的桶里坐两位订阅者。
        live.close();
      });
      return () => {
        tick.value;
        return h("div", { class: "shell" }, [
          h("header", { class: "masthead" }, [
            h("h1", "RF Track 工作台"),
            h("span", { class: "muted" }, "S6：栅格 + 组件库 + 属性面板 + 层级选择 · 数据走 /ws/live 与 REST，回放帧带 MOCK 角标"),
            h("span", { class: "spacer" }),
            context.state.instanceRole === "aggregator"
              ? h("span", { class: "badge badge--agg" }, "汇聚实例只读")
              : null,
          ]),
          toolbar(),
          bootScreen() || h("div", { class: "cols" }, [library(), workspace(), propsPanel()]),
          h(statusBarView),
        ]);
      };
    },
  };

  const app = rt.createApp(root);
  const instance = app.mount(el);

  // 产品内的诊断入口：排查"切场地有没有碰写端点""租约还得干净吗"不必再另开脚本。
  // `live` 也在里面：e2e 与人工排查读的是同一份连接账（帧数、来源、未知帧、跨场地行）。
  globalThis.rftrackWorkbench = { api, context, store, registry, actions, live, instance, app };
  return { api, context, store, actions, live, app: instance };
}
