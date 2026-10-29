/**
 * 场地上下文：**"我现在看的是哪一块板"必须有地方常驻说清楚**。
 *
 * 四级层级（场地 → 位置 → 接入机 → 节点）之后，只靠 URL 参数、下拉勾选项或悬浮提示
 * 都不够——窄屏上省略号会把最关键的区分位截掉，而手机上没有 hover。所以这里产出的
 * 是一条结构化状态栏（`statusBar()`），页面原样渲染，测试原样断言。
 *
 * 另一条纪律：**切场地/切位置/切接入机只换视图，不换后台**。拨号循环与插件的生命周期
 * 不在这一层手里（规范 §10.4），这一层也一次都不该去碰写端点——`api.audit()` 就是
 * 用来证明这句话的，不是用来装饰的。
 */

/** 视图记忆的存储键。带上版本号：形状变了要能认出来是旧数据而不是"没选过"。 */
export const VIEW_MEMORY_KEY = "rftrack.workbench.view.v1";

const EMPTY_MEMORY = () => ({ siteId: null, bySite: {} });

/**
 * 读浏览器里那份"上一屏"。认不出来就当没有：
 * 视图记忆只是**提示**，不是事实来源——库里那行 `current_site_id` 和 `is_active` 才是，
 * 所以这段任何一次失败都必须静默回落，不能让一次坏数据把页面变成错误页。
 */
export function readViewMemory(storage) {
  if (!storage) return EMPTY_MEMORY();
  try {
    const raw = storage.getItem(VIEW_MEMORY_KEY);
    if (!raw) return EMPTY_MEMORY();
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || typeof parsed.bySite !== "object" || parsed.bySite === null) {
      return EMPTY_MEMORY();
    }
    return { siteId: parsed.siteId ?? null, bySite: parsed.bySite };
  } catch {
    return EMPTY_MEMORY();
  }
}

export function createContext({ api, now = () => new Date(), staleAfterS = 120, storage = null }) {
  const store = storage ?? globalThis.localStorage ?? null;
  const persisted = readViewMemory(store);
  const state = {
    loading: true,
    error: null,
    sites: [],
    relays: [],
    gateways: [],
    nodes: [],
    /** 能力的事实来源就是"这一路调用真的答过话"，不是页面自己抄的一张表。 */
    loaded: { hierarchy: false, nodes: false },
    /** 每个场地自己记住的视图状态：切回 A 场地不该看到 B 场地的布局。 */
    memory: new Map(),
    siteId: null,
    relayId: null,
    gatewayId: null,
    nodeId: null,
    scopeGateways: [],
    scopeNodes: [],
    instanceRole: "site",
    health: null,
  };

  const memoryFor = (siteId) => {
    if (!state.memory.has(siteId)) {
      const saved = persisted.bySite[String(siteId)] || {};
      state.memory.set(siteId, {
        layoutId: saved.layoutId ?? null,
        instanceId: saved.instanceId ?? null,
        relayId: saved.relayId ?? null,
        gatewayId: saved.gatewayId ?? null,
        nodeId: saved.nodeId ?? null,
      });
    }
    return state.memory.get(siteId);
  };

  // 先把浏览器里那份旧记忆装进 Map：`persist()` 是按 Map 整体重写的，
  // 不先装的话，一次"没选任何场地"的刷新就会把历史条目抹掉。
  for (const key of Object.keys(persisted.bySite)) {
    const id = Number(key);
    if (Number.isFinite(id)) memoryFor(id);
  }

  /**
   * 把"这一屏"写回浏览器。**布局不落这里**：谁是当前布局只有服务端 `is_active` 一份真相，
   * 页面记住的只有"我在看哪个位置/哪台接入机/哪块板、点中了哪格"。
   * 存储不可用（无痕、配额满）时静默失败：刷新后回到"未选"是可接受的降级，白屏不是。
   */
  function persist() {
    if (!store) return false;
    if (state.siteId !== null) {
      const memo = memoryFor(state.siteId);
      memo.relayId = state.relayId;
      memo.gatewayId = state.gatewayId;
      memo.nodeId = state.nodeId;
    }
    const bySite = {};
    for (const [siteId, memo] of state.memory) bySite[String(siteId)] = { ...memo };
    try {
      store.setItem(VIEW_MEMORY_KEY, JSON.stringify({ siteId: state.siteId, bySite }));
      return true;
    } catch {
      return false;
    }
  }

  const find = (rows, key, value) => rows.find((row) => row[key] === value) || null;

  async function loadHierarchy(siteId) {
    const [relays, gateways, nodes] = await Promise.all([
      api.relays(siteId),
      api.gateways(siteId),
      api.nodes({ site: siteId }),
    ]);
    state.relays = relays;
    state.gateways = gateways;
    state.nodes = nodes;
    state.loaded.hierarchy = true;
    state.loaded.nodes = true;
  }

  /** 选中的对象可能被别人删掉了：回落规则只有一条——**回落了就要说出来**。 */
  function reconcile() {
    if (state.relayId !== null && !find(state.relays, "relay_id", state.relayId)) state.relayId = null;
    if (state.relayId === null && state.relays.length) state.relayId = state.relays[0].relay_id;
    const inScope = state.relayId === null
      ? state.gateways
      : state.gateways.filter((row) => row.relay_id === state.relayId);
    if (state.gatewayId !== null && !inScope.some((row) => row.gateway_id === state.gatewayId)) {
      state.gatewayId = null;
    }
    if (state.gatewayId === null && inScope.length) state.gatewayId = inScope[0].gateway_id;
    const boards = state.gatewayId === null
      ? []
      : state.nodes.filter((row) => row.gateway_id === state.gatewayId);
    if (state.nodeId !== null && !boards.some((row) => row.node_id === state.nodeId)) state.nodeId = null;
    if (state.nodeId === null && boards.length) state.nodeId = boards[0].node_id;
    state.scopeGateways = inScope;
    state.scopeNodes = boards;
  }

  async function boot({ siteId } = {}) {
    state.error = null;
    try {
      const [health, sites, globals] = await Promise.all([
        api.health(),
        api.sites(),
        api.globalSettings(),
      ]);
      state.health = health;
      state.instanceRole = health.instance_role;
      state.sites = sites;
      state.globals = globals;
      const wanted = siteId ?? preferredSite(sites, globals);
      if (wanted === null) {
        state.siteId = null;
        state.loading = false;
        return state;
      }
      if (!sites.some((row) => row.site_id === wanted)) {
        // 配置指向一个已经不存在的场地：说出来，不要偷偷挑一个"看起来对"的。
        state.error = { reason: "configured_site_missing", message: `当前场地配置指向 ${wanted}，库里没有它` };
        state.loading = false;
        return state;
      }
      await selectSite(wanted);
    } catch (error) {
      state.error = { reason: error.reason || "load_failed", message: String(error.message || error) };
    }
    state.loading = false;
    return state;
  }

  /**
   * 首屏看哪个场地，顺序是：**库里那行配置 → 这个浏览器上次看的场地 → 只有一个场地 → 不选**。
   *
   * `current_site_id` 是实例级事实（S4 的键注册表里唯一 `requires_restart` 那把键），
   * 所以页面第一眼就该听它。第二条是本浏览器看过的上一屏——规范 §10.1 的"刷新后布局与
   * 组件局部配置恢复正确"要求刷新能回到同一张屏，而布局与配置都在服务端，前提是先回到
   * 那个场地。它只是提示：认不出来（场地被删、存储坏）就往下走，不报错也不猜。
   * 多个场地又谁都没配过时不猜：状态栏那句"未选"比随机进 A 场地
   * 更容易被发现——猜错的代价是运营者看着 B 场地的数据以为在看 A。
   */
  function preferredSite(sites, globals) {
    const configured = globals && globals.settings && globals.settings.current_site_id;
    if (configured && configured.value) return configured.value;
    if (persisted.siteId !== null && sites.some((row) => row.site_id === persisted.siteId)) {
      return persisted.siteId;
    }
    if (sites.length === 1) return sites[0].site_id;
    return null;
  }

  async function selectSite(siteId) {
    state.siteId = siteId;
    const memo = memoryFor(siteId);
    // 上一屏的位置/接入机/板先接着用，`reconcile()` 负责把已经不存在的那几级回落掉。
    state.relayId = memo.relayId ?? null;
    state.gatewayId = memo.gatewayId ?? null;
    state.nodeId = memo.nodeId ?? null;
    await loadHierarchy(siteId);
    reconcile();
    persist();
    return memo;
  }

  function selectRelay(relayId) {
    state.relayId = relayId;
    state.gatewayId = null;
    state.nodeId = null;
    reconcile();
    persist();
  }

  function selectGateway(gatewayId) {
    state.gatewayId = gatewayId;
    state.nodeId = null;
    reconcile();
    persist();
  }

  function selectNode(nodeId) {
    state.nodeId = nodeId;
    persist();
  }

  const site = () => find(state.sites, "site_id", state.siteId);
  const relay = () => find(state.relays, "relay_id", state.relayId);
  const gateway = () => find(state.gateways, "gateway_id", state.gatewayId);
  const node = () => find(state.nodes, "node_id", state.nodeId);

  /** 汇聚来的场地必须自带滞后说明：把上次同步的值画成实时曲线是规范 §2.3 禁止的事。 */
  function lag(siteRow) {
    if (!siteRow || siteRow.origin !== "aggregated") return null;
    const lagS = siteRow.lag_s;
    const unknown = lagS === null || lagS === undefined;
    return {
      lag_s: lagS,
      // "从未同步"比"滞后很大"更该置灰：一个空值不该被当成新鲜。
      stale: unknown ? true : lagS > staleAfterS,
      last_sync_at: siteRow.last_sync_at,
      label: unknown
        ? "从未同步"
        : `滞后 ${Math.round(lagS)} 秒 · 最近同步 ${clock(siteRow.last_sync_at, siteRow.timezone)}`,
    };
  }

  function clock(value, tz) {
    if (!value) return "—";
    const stamp = typeof value === "number" ? new Date(value) : new Date(String(value).includes("Z") ? value : `${value}Z`);
    try {
      return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", timeZone: tz || "UTC" })
        .format(stamp);
    } catch {
      // 库里那句时区名我们不认识（它不是本文件的判据）：退回 UTC 显示，但绝不显示空。
      return `${String(value).slice(11, 16)}Z`;
    }
  }

  /**
   * 状态栏的那一行。`tone` 给页面配色，`text` 给窄屏换行读、也给测试断言。
   * 每一段都可能是"没选/没有"，那句话必须照样说出来——空字符串在这里等于事故。
   *
   * `data` 是调用方（`app.js`）从 `live.js` 取好的那一段："数据 真实 · 已连接 · 2 块板"。
   * 措辞放在调用方而不是这里，是因为这一层只懂四级层级，不懂推送；把 `live` 引进来就等于
   * 让状态栏依赖一条 WebSocket，而状态栏在纯 REST 的部署形态里照样得画得出来。
   */
  function statusBar({ layout = null, dirty = false, data = null } = {}) {
    const parts = [
      { label: "场地", value: site() ? site().name : (state.sites.length ? "未选" : "还没有场地"), tone: "site" },
      { label: "位置", value: relay() ? relay().name : "未选位置", tone: relay() ? "ok" : "muted" },
      { label: "接入机", value: gateway() ? `${gateway().name} · ${gateway().dial_state || "未拨号"}` : "未选接入机", tone: gateway() ? (gateway().dial_state === "online" ? "ok" : "warn") : "muted" },
      { label: "板", value: node() ? node().display_name : (state.gatewayId === null ? "无接入机" : "无节点"), tone: node() ? "ok" : "muted" },
      { label: "布局", value: layout ? `${layout.name}${layout.degraded ? "（降级）" : ""}` : "未加载", tone: layout && layout.degraded ? "warn" : "ok" },
      { label: "改动", value: dirty ? "未保存" : "已保存", tone: dirty ? "warn" : "ok" },
      { label: "实例", value: state.instanceRole, tone: state.instanceRole === "aggregator" ? "warn" : "ok" },
    ];
    if (data && data.value) parts.push({ label: "数据", value: data.value, tone: data.tone || "muted" });
    const extra = lag(site());
    if (extra) parts.push({ label: "汇聚", value: extra.label, tone: extra.stale ? "stale" : "ok" });
    return { parts, text: parts.map((part) => `${part.label} ${part.value}`).join(" · ") };
  }

  /**
   * 组件库那句"这个组件现在能不能用"的事实来源：只有答过话的那一路才算有能力。
   *
   * S6 起 `demo` 这一路没了——四个演示组件改读真实通路，于是库里那四格的第一次"可用"
   * 不再是白送的：它要等 `context` 选上一个场地（`live` 这条推送才有 `?site=` 可订）。
   * 判的是**通路**而不是"有没有数据"：通路在、板子不在，格子照样进，只是里面写着"无数据"。
   */
  function capabilities() {
    const ready = [];
    if (state.loaded.hierarchy) ready.push("hierarchy");
    if (state.loaded.nodes) ready.push("nodes");
    if (state.siteId !== null && state.siteId !== undefined) ready.push("live");
    return ready;
  }

  return {
    state,
    boot,
    selectSite,
    selectRelay,
    selectGateway,
    selectNode,
    reconcile,
    memoryFor,
    persist,
    statusBar,
    capabilities,
    site,
    relay,
    gateway,
    node,
    lag,
    clock,
  };
}
