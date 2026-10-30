/**
 * 后端调用层。**不含任何 UI 依赖**，所以 `node --test` 可以直接拿它对着假 fetch 断言
 * 错误形状——页面按 `reason` 分支处理，而不是去解析自然语言。
 *
 * 三条存在的理由：
 * 1. **前缀在运行时探测**（`detectApiBase`）。页面挂在 `/workbench/` 还是
 *    `/rftrack/workbench/` 是部署的事，写死一次就等于把工作台钉在某一种部署形态上。
 * 2. **两种 422 形状收成一个口径**。`{"reason": …}` 是全站约定（方案 §4），但请求体本身
 *    不合法时 FastAPI 回的是校验数组；页面不该为这一个端点写特例，这里统一成
 *    `{reason: "request_invalid", fields: […]}`。
 * 3. **调用要有账**。"切场地/切位置/切接入机不触碰后台"这条判据要能断言，
 *    就得有一份有界的调用记录（`audit()`），而不是靠人盯网络面板。
 */

/** 调用记录的上限。和采集队列同一条纪律：有界，满了丢最旧。 */
export const AUDIT_LIMIT = 200;

export class ApiError extends Error {
  constructor({ status, reason, detail, path, method, fields }) {
    super(`${method} ${path} → ${status} ${reason}`);
    this.name = "ApiError";
    this.status = status;
    this.reason = reason;
    this.detail = detail;
    this.fields = fields || [];
    this.path = path;
    this.method = method;
  }
}

/**
 * 从当前页面地址推 API 根。
 *
 * 规则只有两条：末端目录名是 `workbench` 就剥掉它，其余部分就是前缀。
 * `?api=` 是给"目录被改名/页面另有静态宿主"留的显式覆盖位，覆盖后原样使用。
 * 剥不中也不报错——把探测到的前缀显示在状态栏里，错了看得出，比猜一台"应该是根"好。
 */
export function detectApiBase({ pathname = "/", search = "", override = null } = {}) {
  const explicit = override ?? new URLSearchParams(search).get("api");
  if (explicit !== null && explicit !== undefined && explicit !== "") {
    return trimSlash(explicit);
  }
  const dir = trimSlash(pathname.replace(/\/[^/]*$/, ""));
  return dir.toLowerCase().endsWith("/workbench") ? dir.slice(0, -"/workbench".length) : dir;
}

const trimSlash = (value) => value.replace(/\/+$/, "");

/** FastAPI 的校验数组 → 逐字段的可读错误；认不出来就原样带着，不编一个 reason。 */
function normalize({ status, body, path, method }) {
  const detail = body && typeof body === "object" ? body.detail : undefined;
  if (detail && typeof detail === "object" && !Array.isArray(detail) && detail.reason) {
    return { status, reason: detail.reason, detail, path, method };
  }
  if (Array.isArray(detail)) {
    return {
      status,
      reason: "request_invalid",
      detail: { raw: detail },
      path,
      method,
      fields: detail.map((item) => ({
        field: (item.loc || []).slice(1).join(".") || (item.loc || []).join("."),
        type: item.type,
        message: item.msg,
      })),
    };
  }
  return {
    status,
    reason: "unexpected_response",
    detail: { body: body === undefined ? null : body },
    path,
    method,
  };
}

function query_string(query) {
  const params = new URLSearchParams();
  for (const [name, value] of Object.entries(query || {})) {
    if (value === undefined || value === null || value === "") continue;
    params.set(name, String(value));
  }
  const rendered = params.toString();
  return rendered ? `?${rendered}` : "";
}

export function createApi(options = {}) {
  const fetchImpl = options.fetchImpl ?? globalThis.fetch;
  const location = options.location ?? globalThis.location ?? { pathname: "/", search: "" };
  const base = options.base ?? detectApiBase({ pathname: location.pathname, search: location.search });
  const auditLimit = options.auditLimit ?? AUDIT_LIMIT;
  const calls = [];
  let dropped = 0;

  function record(entry) {
    calls.push(entry);
    if (calls.length > auditLimit) {
      calls.shift();
      dropped += 1;
    }
  }

  async function call(method, path, { query, body } = {}) {
    const url = `${base}${path}${query_string(query)}`;
    let response;
    try {
      response = await fetchImpl(url, {
        method,
        headers: body === undefined ? {} : { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (cause) {
      // 网络层失败没有状态码也没有 reason，页面必须还能说清"后端没答话"这件事。
      record({ method, path, status: 0 });
      throw new ApiError({
        status: 0,
        reason: "network_unreachable",
        detail: { cause: String(cause) },
        path,
        method,
      });
    }
    record({ method, path, status: response.status });
    if (response.status === 204) return null;
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    if (!response.ok) throw new ApiError({ ...normalize({ status: response.status, body: payload, path, method }) });
    return payload;
  }

  const get = (path, query) => call("GET", path, { query });
  const send = (method, path, body, query) => call(method, path, { body, query });

  return {
    base,
    audit: () => ({ calls: calls.slice(), dropped }),
    /** 只回最近一次某端点的调用，供"我根本没碰写端点"这类断言用。 */
    touched: (path, method = "POST") => calls.some((c) => c.path === path && c.method === method),

    health: () => get("/api/health"),
    schema: () => get("/api/settings/schema"),
    globalSettings: () => get("/api/settings/global"),
    putGlobalSettings: (payload) => send("PUT", "/api/settings/global", payload),
    siteSettings: (siteId) => get(`/api/settings/site/${siteId}`),
    putSiteSettings: (siteId, payload) => send("PUT", `/api/settings/site/${siteId}`, payload),

    sites: () => get("/api/sites"),
    relays: (siteId) => get("/api/relays", { site: siteId }),
    gateways: (siteId) => get("/api/gateways", { site: siteId }),
    nodes: ({ site, gateway } = {}) => get("/api/nodes", { site, gateway }),
    endpoints: (gatewayId) => get("/api/endpoints", { gateway: gatewayId }),

    /** 摘要桶（S6）：参数名与端点一致，`slot: 0` 不许被当成"没给"。 */
    samplesSummary: (params = {}) => get("/api/samples/summary", params),
    eventsFeed: (params = {}) => get("/api/events", params),
    /** 事件详情：`site` 可缺省（主键读不是跨场地扫表），带上了就是"这个场地下没有它"→ 404。 */
    eventDetail: (eventId, site = null) => get(`/api/events/${eventId}`, { site }),

    /** 候选目标（S8）：列表按场地的"最近出没"窗口取，详情/复核同一条场地口径。 */
    targetsFeed: (params = {}) => get("/api/targets", params),
    targetDetail: (targetId, site = null) => get(`/api/targets/${targetId}`, { site }),
    reviewTarget: (targetId, payload) => send("POST", `/api/targets/${targetId}/review`, payload),

    layouts: (siteId) => get("/api/layouts", { site: siteId }),
    createLayout: (payload) => send("POST", "/api/layouts", payload),
    patchLayout: (layoutId, payload) => send("PATCH", `/api/layouts/${layoutId}`, payload),
    /** `payload` 原样发出去：这里的第二参是请求体，不是名字——把包装再套一层就成了 `{"name":{"name":…}}`。 */
    cloneLayout: (layoutId, payload = {}) => send("POST", `/api/layouts/${layoutId}/clone`, payload),
    activateLayout: (layoutId) => send("POST", `/api/layouts/${layoutId}/activate`),
    deleteLayout: (layoutId) => send("DELETE", `/api/layouts/${layoutId}`),

    widgets: (layoutId) => get(`/api/layouts/${layoutId}/widgets`),
    putWidget: (layoutId, instanceId, payload) =>
      send("PUT", `/api/layouts/${layoutId}/widgets/${instanceId}`, payload),
    deleteWidget: (layoutId, instanceId) =>
      send("DELETE", `/api/layouts/${layoutId}/widgets/${instanceId}`),
  };
}
