/**
 * 实时数据层：**整页一条 `/ws/live?site=` 连接**，四个接真实数的组件读同一份内存视图。
 *
 * 为什么是一页一条而不是每格一条：扇出按场地分桶，同一页开四条连接就是同一个桶里坐四位
 * 订阅者——服务端队列（`live_queue_limit`）按订阅者算，多出来的三条不增加信息，只会把
 * "丢了几条"这个数变成三条里最糟的那一条，而页面上看不出来是哪一条。
 *
 * 四条口径：
 *
 * - **换场地立刻清空**。旧场地的最后一帧留在 `nodes` 里，新场地的快照还在路上，那一段时间
 *   页面上就是"用 A 场地的数画 B 场地的图"——S6 出口判据说的是服务端不许串场，这一条是
 *   页面侧不许拿旧数装新数。清完的状态是 `connecting` + 无数据，不是"还是刚才那些"。
 * - **只有帧类型是 `event` 的才当事件**。外层 `kind` 是帧类型（`snapshot`/`samples`/`event`），
 *   事件自己的类型在 `event.kind`（`node_offline` 那一个）。这层与 `GET /api/events` 那一行
 *   同形，所以同一个事件从 WS 到、从 REST 到，`event_id` 是同一个数——去重因此是免费的。
 * - **认不出的帧只记账不崩页**。`unknown_frames` 与 `bad_frames` 两个数摆在那里：服务端将来
 *   加一种帧，这一页应该继续显示它认得的东西，而不是整块白屏。
 * - **被拒不重连**。`1008` 那两个理由（`site_required`/`site_not_found`）重连多少次都是同一个
 *   答案，热循环只会把服务端的连接日志刷满；其他关闭码才走退避重连，退避有顶。
 *
 * 渲染节拍不在这一层：帧到了只改数据，组件仍按自己那格生效的 `refresh_ms` 重读。否则八块板
 * 的帧率会变成重渲染频率，而"哪一格该多久刷一次"本来是人配的东西。`version()` 因此只是一个
 * 给诊断与测试看的计数器，**不是渲染依赖**——这一层从头到尾不认识 Vue，才能在 Node 里被直接断言。
 */

/** 一页记住多少块板的最后读数。超出后挤掉最久没来的那一块。 */
export const NODE_CAP = 32;
/** 事件流在前端留多少条。服务端另有 `live_event_window`，那是"快照带几条"的口径，不是这里的。 */
export const EVENT_CAP = 200;
/** 退避序列（毫秒），到顶之后一直用最后一个。 */
export const RECONNECT_STEPS_MS = [1000, 2000, 4000, 8000, 10000];
/** 策略性拒绝：这两个理由重连没有意义。 */
export const REFUSE_CODES = new Set([1008]);

export const STATUS = {
  IDLE: "idle",
  CONNECTING: "connecting",
  OPEN: "open",
  RETRYING: "retrying",
  CLOSED: "closed",
  REFUSED: "refused",
};

/**
 * 推送口的地址。前缀跟着 `api.base` 走——页面挂在 `/rftrack/workbench/` 还是根上，
 * 是部署的事，这里写死一次就把工作台钉在那一种形态上（与 `api.js` 同一条理由）。
 *
 * `base` 有两种形状，都得认：同源部署下它是**路径前缀**（`""`、`/rftrack`），跟着页面的
 * origin 走；`?api=` 覆盖下它是**绝对 URL**（`http://192.168.1.20:8000`），那时推送必须跟着
 * 它而不是跟着页面——否则纯 API 部署形态里 WS 会连到静态宿主上去，那里根本没有 `/ws/live`。
 */
export function liveUrl({ base = "", location, site }) {
  const where = location ?? globalThis.location ?? { protocol: "http:", host: "" };
  const absolute = /^https?:\/\/[^/]*/i.exec(base);
  const host = absolute ? absolute[0].replace(/^https?:\/\//, "") : where.host || "";
  const scheme = (absolute ? absolute[0] : `${where.protocol || "http:"}//${host}`).startsWith("https")
    ? "wss:"
    : "ws:";
  const path = absolute ? base.slice(absolute[0].length) : base;
  return `${scheme}//${host}${path}/ws/live?site=${encodeURIComponent(String(site))}`;
}

const toEpochMs = (value) => {
  if (value === null || value === undefined) return null;
  if (typeof value === "number") return value;
  // 后端发的是不带偏移的 ISO 串（`utcnow().isoformat()` 在 UTC 库里就是 `…+00:00`，
  // 但历史行读回来可能是 naive）：认不出 Z 就补一个，否则本机时区会把新鲜度算错。
  const text = String(value);
  const stamp = Date.parse(/([zZ]|[+-]\d{2}:?\d{2})$/.test(text) ? text : `${text}Z`);
  return Number.isNaN(stamp) ? null : stamp;
};

export function createLive(options = {}) {
  const {
    base = "",
    location = null,
    wsFactory = (url) => new globalThis.WebSocket(url),
    timers = globalThis,
    now = () => Date.now(),
    staleAfterS = 15,
    nodeCap = NODE_CAP,
    eventCap = EVENT_CAP,
    steps = RECONNECT_STEPS_MS,
  } = options;

  const state = {
    status: STATUS.IDLE,
    reason: null,
    siteId: null,
    /** 计数只在诊断时有用：它们回答的是"这一页看到过什么"，不是服务端的全局账。 */
    opened: 0,
    closed: 0,
    attempts: 0,
    frames: 0,
    snapshots: 0,
    events: 0,
    unknownFrames: 0,
    badFrames: 0,
    /** REST 回填里认不出的场地行：服务端那道分桶是判据，这里是页面侧的第二道拦截。 */
    crossSite: 0,
    lastFrameAt: null,
    lastEventAt: null,
    nodes: new Map(),
    feed: [],
  };

  const seen = new Set();
  let socket = null;
  let retry = null;
  /** 每一次真正的状态变化递增。**给诊断与测试读**：渲染不看它，节拍在组件那一层（见文件头）。 */
  let version = 0;

  const touch = () => {
    version += 1;
    return version;
  };

  function dropNode(nodeId) {
    let oldest = null;
    for (const [id, row] of state.nodes) {
      if (id === nodeId) continue;
      if (!oldest || row.ts === null || row.ts < oldest.ts) oldest = row;
    }
    if (oldest) state.nodes.delete(oldest.node_id);
  }

  /** 快照与增量走同一条合并路：`channels` 按档位合并，每档留自己的时刻。 */
  function mergeSamples(frame) {
    const nodeId = frame.node_id;
    if (nodeId === null || nodeId === undefined) return;
    let node = state.nodes.get(nodeId);
    if (!node) {
      if (state.nodes.size >= nodeCap) dropNode(nodeId);
      node = {
        node_id: nodeId,
        site_id: frame.site_id ?? state.siteId,
        gateway_id: frame.gateway_id ?? null,
        display_name: frame.display_name ?? null,
        transport_addr: frame.transport_addr ?? null,
        ep: frame.ep ?? null,
        source: frame.source ?? null,
        seq: null,
        mode: null,
        loKhz: null,
        tMs: null,
        ts: null,
        frames: 0,
        channels: new Map(),
      };
      state.nodes.set(nodeId, node);
    }
    node.site_id = frame.site_id ?? node.site_id;
    node.gateway_id = frame.gateway_id ?? node.gateway_id;
    node.display_name = frame.display_name ?? node.display_name;
    node.transport_addr = frame.transport_addr ?? node.transport_addr;
    node.ep = frame.ep ?? node.ep;
    node.source = frame.source ?? node.source;
    node.seq = frame.seq ?? node.seq;
    node.mode = frame.mode ?? node.mode;
    node.loKhz = frame.loKhz ?? node.loKhz;
    node.tMs = frame.tMs ?? node.tMs;
    node.ts = toEpochMs(frame.ts) ?? node.ts;
    node.frames += 1;
    for (const point of frame.channels || []) {
      if (!point || point.id === undefined || point.id === null) continue;
      node.channels.set(point.id, { ...point, at: toEpochMs(point.ts) });
    }
    state.frames += 1;
    state.lastFrameAt = node.ts ?? state.lastFrameAt;
  }

  /**
   * 事件按 id 去重（REST 回填与 WS 推送会撞到同一条），**按时刻倒序插入**。
   *
   * 插入而不是头插：这一份窗现在有两条到达路——WS 是"最新的先来"，`backfill()` 从 REST 拿的是
   * "一整批更老的"。头插会把老批次顶到最前面，列表就变成"上一批"压在"这一批"之上，而事件流
   * 唯一的读法就是从上往下时间递减。窗最长 200 条，一次线性查找比排序便宜。
   */
  function offerEvent(event) {
    if (!event || event.event_id === undefined || event.event_id === null) return false;
    if (seen.has(event.event_id)) return false;
    seen.add(event.event_id);
    const row = { ...event, at: toEpochMs(event.ts) };
    const older = state.feed.findIndex((entry) => (entry.at ?? 0) < (row.at ?? 0));
    if (older === -1) state.feed.push(row);
    else state.feed.splice(older, 0, row);
    state.events += 1;
    state.lastEventAt = state.feed[0].at;
    while (state.feed.length > eventCap) {
      const gone = state.feed.pop();
      seen.delete(gone.event_id);
    }
    return true;
  }

  function ingestSnapshot(message) {
    state.snapshots += 1;
    state.nodes.clear();
    seen.clear();
    state.feed = [];
    for (const frame of message.samples || []) mergeSamples(frame);
    for (const frame of message.events || []) offerEvent(frame.event);
  }

  /**
   * REST 回填：把 `GET /api/events` 的行并进同一条事件窗。
   *
   * 快照只回答"这一层发布过什么"（服务端的内存窗），更早的历史是 REST 的活；两条路撞在同一条
   * 记录上是常态而不是故障，因为帧里的 `event` 与那一行同形、`event_id` 是同一个数。
   * 不是当前场地的行**在这里就丢掉**：这是页面侧的第二次拦截，服务端那道才是判据。
   */
  function backfill(rows) {
    let added = 0;
    for (const row of rows || []) {
      if (state.siteId !== null && row && row.site_id !== undefined && Number(row.site_id) !== state.siteId) {
        state.crossSite += 1;
        continue;
      }
      if (offerEvent(row)) added += 1;
    }
    if (added) touch();
    return added;
  }

  function handleText(text) {
    let message;
    try {
      message = JSON.parse(text);
    } catch {
      state.badFrames += 1;
      touch();
      return;
    }
    if (!message || typeof message !== "object") {
      state.badFrames += 1;
      touch();
      return;
    }
    // 分桶在服务端做，但"这一页只该看到订的那个场地"仍是句可以验的话：串了就当场丢弃。
    if (message.site_id !== undefined && message.site_id !== null && state.siteId !== null) {
      if (Number(message.site_id) !== Number(state.siteId)) {
        state.unknownFrames += 1;
        touch();
        return;
      }
    }
    switch (message.kind) {
      case "snapshot":
        ingestSnapshot(message);
        break;
      case "samples":
        mergeSamples(message);
        break;
      case "event":
        offerEvent(message.event);
        break;
      default:
        state.unknownFrames += 1;
        touch();
        return;
    }
    touch();
  }

  function clearRetry() {
    if (retry !== null) {
      timers.clearTimeout(retry);
      retry = null;
    }
  }

  function scheduleRetry() {
    const wait = steps[Math.min(state.attempts, steps.length - 1)];
    state.attempts += 1;
    state.status = STATUS.RETRYING;
    touch();
    retry = timers.setTimeout(() => {
      retry = null;
      open();
    }, wait);
  }

  function open() {
    if (state.siteId === null) return;
    clearRetry();
    state.status = STATUS.CONNECTING;
    state.reason = null;
    touch();
    let created;
    try {
      created = wsFactory(liveUrl({ base, location, site: state.siteId }));
    } catch (error) {
      state.reason = String(error && error.message ? error.message : error);
      scheduleRetry();
      return;
    }
    socket = created;
    created.onopen = () => {
      state.status = STATUS.OPEN;
      state.opened += 1;
      state.attempts = 0;
      touch();
    };
    created.onmessage = (event) => handleText(event.data);
    created.onerror = () => {
      state.reason = "socket_error";
      touch();
    };
    created.onclose = (event) => {
      socket = null;
      state.closed += 1;
      const code = event && event.code !== undefined ? event.code : null;
      state.reason = (event && event.reason) || (code === null ? null : `code ${code}`);
      if (code !== null && REFUSE_CODES.has(code)) {
        state.status = STATUS.REFUSED;
        touch();
        return;
      }
      // `close()` 主动断开时 siteId 已经被摘掉：那不是故障，不该起退避定时器。
      if (state.siteId === null) {
        state.status = STATUS.CLOSED;
        touch();
        return;
      }
      scheduleRetry();
    };
  }

  /** 换场地：先清账再连新的。旧场地的最后一帧一格都不许留下。 */
  function attach(siteId) {
    const id = siteId === null || siteId === undefined || siteId === "" ? null : Number(siteId);
    if (id === null || Number.isNaN(id)) {
      // 没有可订的场地：回到"未选场地"，不是"断线"。把两者混在一起，状态栏就在说谎。
      close(null);
      state.status = STATUS.IDLE;
      touch();
      return;
    }
    if (state.siteId === id && (state.status === STATUS.OPEN || state.status === STATUS.CONNECTING)) {
      return;
    }
    const sameSite = state.siteId === id;
    if (socket !== null) detachSocket(socket);
    clearRetry();
    state.siteId = id;
    state.status = STATUS.IDLE;
    state.reason = null;
    state.attempts = 0;
    if (!sameSite) {
      state.nodes.clear();
      state.feed = [];
      seen.clear();
      // 场地级的账全部归零：`frames`/`events` 归零而"串场计数"留着，就是在用旧场地的
      // 脏账给新场地作保。连接级的账（`opened`/`closed`/`snapshots`/`attempts`）不清，
      // 它们说的是"这一页一共发生过几次连接"，跟看的是哪个场地无关。
      state.frames = 0;
      state.events = 0;
      state.unknownFrames = 0;
      state.badFrames = 0;
      state.crossSite = 0;
      state.lastFrameAt = null;
      state.lastEventAt = null;
    }
    touch();
    open();
  }

  function detachSocket(target) {
    target.onopen = null;
    target.onmessage = null;
    target.onerror = null;
    target.onclose = null;
    try {
      target.close();
    } catch {
      // 已经断了的 socket 再关一次不是页面的错：状态该由 close() 说话，而不是由这里抛。
    }
  }

  /** 收尾：不再有定时器、不再有 socket、数据清空。切场地与卸载走同一条路。 */
  function close(reason = null) {
    clearRetry();
    if (socket !== null) detachSocket(socket);
    socket = null;
    state.siteId = null;
    state.status = STATUS.CLOSED;
    state.reason = reason;
    state.nodes.clear();
    state.feed = [];
    seen.clear();
    state.crossSite = 0;
    touch();
  }

  const node = (nodeId) => state.nodes.get(nodeId) || null;

  /**
   * 这一块板**进过帧**的档位，升序。
   *
   * 不补 `0..7`：`slots_per_node` 是配置，不是"这一帧里有几档"的事实。固件在 LEVELS 模式下
   * 一帧只报它正在跳的那几个档（`hops`），把配置里的八档画满就等于替没测的档编一个值。
   */
  const slotsOf = (nodeId) => {
    const row = node(nodeId);
    return row ? [...row.channels.keys()].sort((a, b) => a - b) : [];
  };

  /** 一块板的某一档：没测过就是 `null`，不是 0，也不是"上一帧的值"。 */
  const pointOf = (nodeId, slot) => {
    const row = node(nodeId);
    return row ? row.channels.get(slot) || null : null;
  };

  function stale(row) {
    if (!row || row.ts === null) return true;
    return now() - row.ts > staleAfterS * 1000;
  }

  /** 这一块板的读数距今几秒；没有读数回 `null` 而不是 0——"0 秒前"是一句假话。 */
  const ageS = (row) => (row && row.ts !== null ? Math.max(0, Math.round((now() - row.ts) / 1000)) : null);

  /** 这一页现在看到的来源：`real` 与 `mock` 混在一起时必须说得清有几种。 */
  function sources() {
    const found = new Set();
    for (const row of state.nodes.values()) if (row.source) found.add(row.source);
    for (const event of state.feed) if (event.source) found.add(event.source);
    return [...found].sort();
  }

  function statusLine() {
    switch (state.status) {
      case STATUS.OPEN:
        return state.nodes.size ? `已连接 · ${state.nodes.size} 块板` : "已连接 · 还没有帧";
      case STATUS.CONNECTING:
        return "连接中";
      case STATUS.RETRYING:
        return `重连中（第 ${state.attempts} 次）`;
      case STATUS.REFUSED:
        return `被拒：${state.reason || "无理由"}`;
      case STATUS.CLOSED:
        return state.reason ? `已断开：${state.reason}` : "未连接";
      default:
        return "未选场地";
    }
  }

  /** 来源词表与 `parts.js` 的角标同一套词：状态栏和格子里说同一件事时不能有两种叫法。 */
  const SOURCE_WORD = { real: "真实", mock: "MOCK" };

  /**
   * 状态栏"数据"那一段的正文：来源 + 连接态。
   *
   * 两件合成一段是有意的——它们要一起看才有意义（"MOCK"配上"已连接 2 块板"说的是"回放正在播"，
   * 单独任何一半都会读错）。这一页常驻的段数每多一段，390 px 上就少一段能一行放得下。
   */
  function dataLabel() {
    const found = sources();
    const words = found.length ? found.map((source) => SOURCE_WORD[source] || `未知(${source})`).join("+") : "无来源";
    return `${words} · ${statusLine()}`;
  }

  /** 那一段的语气：连上且见过帧才算 ok；被拒/断开/重连是 warn；没选场地与刚连上是中性。 */
  function dataTone() {
    if (state.status === STATUS.OPEN) return state.nodes.size ? "ok" : "muted";
    if (state.status === STATUS.IDLE || state.status === STATUS.CONNECTING) return "muted";
    return "warn";
  }

  return {
    state,
    attach,
    close,
    backfill,
    node,
    slotsOf,
    pointOf,
    stale,
    ageS,
    sources,
    statusLine,
    dataLabel,
    dataTone,
    version: () => version,
    /** 诊断入口（`window.rftrackWorkbench.live`）与测试读的是同一个对象。 */
    status: () => ({
      status: state.status,
      reason: state.reason,
      site_id: state.siteId,
      nodes: state.nodes.size,
      events: state.feed.length,
      frames: state.frames,
      snapshots: state.snapshots,
      opened: state.opened,
      closed: state.closed,
      attempts: state.attempts,
      unknown_frames: state.unknownFrames,
      bad_frames: state.badFrames,
      cross_site_rows: state.crossSite,
      last_frame_at: state.lastFrameAt,
      sources: sources(),
      line: statusLine(),
      stale_after_s: staleAfterS,
      caps: { nodes: nodeCap, events: eventCap },
    }),
    handleText,
  };
}
