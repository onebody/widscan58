/**
 * 调用层单测：对着假 fetch 断言**错误形状**。
 *
 * 页面是按 `reason` 分支处理的，所以这里钉的是"每个失败路径都能落成一个可分支的字符串"，
 * 而不是某句自然语言文案。文案会变，`reason` 是契约。
 */

import assert from "node:assert/strict";
import test from "node:test";

import { ApiError, AUDIT_LIMIT, createApi, detectApiBase } from "../api.js";

const response = ({ status = 200, body = null, json = true } = {}) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => (json ? body : (() => { throw new Error("not json"); })()),
});

const fakeFetch = (handler) => async (url, init) => handler(url, init);

test("detectApiBase：末端目录名是 workbench 就剥掉它", () => {
  assert.equal(detectApiBase({ pathname: "/workbench/index.html" }), "");
  assert.equal(detectApiBase({ pathname: "/rftrack/workbench/" }), "/rftrack");
  assert.equal(detectApiBase({ pathname: "/workbench/" }), "");
  // 目录没被改名成 workbench 时不猜"应该是根"，把探测结果原样留着（状态栏会显示它）。
  assert.equal(detectApiBase({ pathname: "/some/where/app.html" }), "/some/where");
  assert.equal(detectApiBase({ pathname: "/Workbench/index.html" }), "");
});

test("detectApiBase：?api= 与 override 是显式覆盖位，原样使用", () => {
  assert.equal(detectApiBase({ pathname: "/workbench/index.html", search: "?api=/backend/v1" }), "/backend/v1");
  assert.equal(detectApiBase({ pathname: "/x", override: "/y/" }), "/y");
  // 空值不算覆盖——否则 `?api=` 会把前缀清成根，比不写更难发现。
  assert.equal(detectApiBase({ pathname: "/rftrack/workbench/a", search: "?api=" }), "/rftrack");
});

test("成功路径：GET 带查询串、空值与 undefined 不进查询", async () => {
  const seen = [];
  const api = createApi({
    base: "",
    fetchImpl: fakeFetch(async (url, init) => { seen.push([url, init.method]); return response({ body: [{ ok: 1 }] }); }),
  });
  const rows = await api.nodes({ site: 3, gateway: undefined });
  assert.deepEqual(rows, [{ ok: 1 }]);
  assert.deepEqual(seen[0], ["/api/nodes?site=3", "GET"]);
});

test("写请求带 JSON 体，204 回 null 而不是解析空响应", async () => {
  let captured = null;
  const api = createApi({
    base: "",
    fetchImpl: fakeFetch(async (url, init) => {
      captured = { url, init };
      return response({ status: 204, body: null });
    }),
  });
  assert.equal(await api.deleteLayout(7), null);
  assert.equal(captured.init.method, "DELETE");
  assert.equal(captured.url, "/api/layouts/7");

  await api.putGlobalSettings({ version: 2 });
  assert.equal(captured.init.headers["Content-Type"], "application/json");
  assert.equal(captured.init.body, JSON.stringify({ version: 2 }));
});

test("约定形状：{detail:{reason}} 原样成为 ApiError.reason", async () => {
  const api = createApi({
    base: "",
    fetchImpl: fakeFetch(async () => response({ status: 409, body: { detail: { reason: "plugin_immutable", detail: "x" } } })),
  });
  await assert.rejects(api.activateLayout(1), (error) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.status, 409);
    assert.equal(error.reason, "plugin_immutable");
    assert.equal(error.path, "/api/layouts/1/activate");
    assert.equal(error.method, "POST");
    return true;
  });
});

test("FastAPI 校验数组收成一个口径：request_invalid + 逐字段", async () => {
  const api = createApi({
    base: "",
    fetchImpl: fakeFetch(async () => response({
      status: 422,
      body: { detail: [{ loc: ["body", "settings", "refresh_ms"], type: "greater_than", msg: "Input should be greater than 0" }] },
    })),
  });
  await assert.rejects(api.putGlobalSettings({ version: 0, settings: {} }), (error) => {
    assert.equal(error.reason, "request_invalid");
    assert.deepEqual(error.fields, [{ field: "settings.refresh_ms", type: "greater_than", message: "Input should be greater than 0" }]);
    return true;
  });
});

test("认不出来的响应不编 reason：unexpected_response 带着原 bodies", async () => {
  const api = createApi({
    base: "",
    fetchImpl: fakeFetch(async () => response({ status: 500, body: "Internal Server Error", json: false })),
  });
  await assert.rejects(api.health(), (error) => {
    assert.equal(error.reason, "unexpected_response");
    assert.deepEqual(error.detail, { body: null });
    return true;
  });
});

test("网络层失败：status 0 + network_unreachable，页面还能说清是谁没答话", async () => {
  const api = createApi({
    base: "",
    fetchImpl: async () => { throw new TypeError("Failed to fetch"); },
  });
  await assert.rejects(api.health(), (error) => {
    assert.equal(error.status, 0);
    assert.equal(error.reason, "network_unreachable");
    assert.match(error.detail.cause, /Failed to fetch/);
    return true;
  });
  assert.deepEqual(api.audit().calls.at(-1), { method: "GET", path: "/api/health", status: 0 });
});

test("调用账是有界的：满了丢最旧并计数（这条判据靠它，不是靠人盯网络面板）", async () => {
  const api = createApi({
    base: "",
    auditLimit: 3,
    fetchImpl: fakeFetch(async () => response({ body: [] })),
  });
  for (let i = 0; i < 5; i += 1) await api.sites();
  const { calls, dropped } = api.audit();
  assert.equal(calls.length, 3);
  assert.equal(dropped, 2);
  assert.equal(AUDIT_LIMIT, 200);
});

test("touched()：断言「切场地没碰写端点」用的就是它", async () => {
  const api = createApi({
    base: "",
    fetchImpl: fakeFetch(async () => response({ body: [] })),
  });
  await api.relays(1);
  await api.gateways(1);
  await api.nodes({ site: 1 });
  assert.equal(api.touched("/api/layouts", "POST"), false);
  assert.equal(api.touched("/api/settings/global", "PUT"), false);
  assert.equal(api.touched("/api/relays", "GET"), true);
});
