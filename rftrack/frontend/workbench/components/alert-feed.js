/**
 * 告警信息流。规则引擎从判定里推出来、**等人处置**的候选行——它与「事件信息流」不同源：
 * 事件是采集层写的观测事实，告警是"插件认为有问题"的判定 + 人的处置状态。
 * 读 REST（`GET /api/alerts`），与 `target-inspector` 同一条轮询口径；告警不走 `/ws/live`。
 *
 * 四条口径（与 signal-events / target-inspector 同源）：
 *
 * - **短语词只是类型名，详情才是说明**。列表一行只画"哪类问题 + 谁 + 多严重"；
 *   发生了什么、判定依据、能讲到哪一步，全部点开才给——`limitations` 来自后端
 *   （`routers/alerts.py::_limitations`），页面只排版不翻译，不替后端圆话。
 * - **处置是写侧，且状态机从严**。按钮按当前 `status` 画（服务端 409 的 `allowed`
 *   就是这套顺序），aggregator 上直接说"只读"。ack ≠ 复核 ≠ 解决，三个动作分开画。
 * - **静默时长优先配置**。不带 `seconds` 落到场地 `alert_defaults.silence_s`；
 *   两头都没有时服务端 422 `silence_duration_required`，页面把 hint 原样端出来，
 *   不在前端藏一个默认数。
 * - **认不出的 `kind`/`status` 原样显示**，不并进"其他"：将来多一种告警类型，
 *   页面应该继续把它端上来。
 */

import { fmtClock, fmtDb, sourceBadge, empty } from "./parts.js";

export const plugin_id = "alert-feed";

/** 列表轮询节奏（毫秒）。告警由规则引擎在投喂节拍上写，5 s 读一次库足够跟得上。 */
const POLL_MS = 5000;
/** 一格里最多画这么多条：处置承担深度，列表只承担"现在挂着哪些"。 */
const SHOW_CAP = 50;

/** 短语词：列表行上那一眼能认出来的词。认不出的 kind 走"未知类型"。 */
const KIND_TEXT = {
  slot_rise: "档位抬升",
  threshold_crossing: "越限",
  multi_node_interference: "多节点干扰",
};

/** 这类告警在说什么：定义句（不是对当下状态的断言，断言在 evidence 里）。 */
const KIND_NOTE = {
  slot_rise: "同一档位的 RSSI 相对参考窗抬升，且抬出了规则里的容差——可能有飞行器进了这一档。",
  threshold_crossing: "读数越过了规则设置的绝对门限。",
  multi_node_interference: "多个节点在同一时段报出了互相解释得通的强读数——像是同一信号被多块板看见。",
};

const STATUS_TEXT = {
  open: "待处置",
  ack: "已确认",
  silenced: "已静默",
  resolved: "已解决",
};

/** 状态机从严（服务端 `_transition` 的允许集照抄过来）：按钮按当前状态画。 */
const ACTIONS_BY_STATUS = {
  open: ["ack", "silence", "resolve"],
  ack: ["silence", "resolve"],
  silenced: ["resolve"],
  resolved: [],
};

export function create(rt, deps) {
  const { h, ref } = rt;

  return {
    name: "AlertFeed",
    props: { widget: { type: Object, required: true } },
    setup(props) {
      const widget = props.widget;
      const open = ref(null);
      const detail = ref(null);
      const detailError = ref(null);
      const rows = ref(null);
      const failure = ref(null);
      const busy = ref(false);
      const actionError = ref(null);
      const silenceNote = ref(null);
      let inFlight = false;
      let released = false;

      const writable = deps.context.state.instanceRole !== "aggregator";

      async function refresh() {
        const site = deps.context.state.siteId;
        if (released || site === null || site === undefined || inFlight) return;
        inFlight = true;
        try {
          const answer = await deps.api.alertsFeed({ site, limit: SHOW_CAP });
          if (released || deps.context.state.siteId !== site) return;
          rows.value = answer;
          failure.value = null;
        } catch (error) {
          if (released) return;
          failure.value = error && error.reason ? { reason: error.reason } : { reason: "unknown" };
        } finally {
          inFlight = false;
        }
      }

      async function readDetail(alertId) {
        detail.value = null;
        detailError.value = null;
        actionError.value = null;
        silenceNote.value = null;
        try {
          const answer = await deps.api.alertDetail(alertId, deps.context.state.siteId);
          if (released || open.value !== alertId) return;
          detail.value = answer;
        } catch (error) {
          if (released) return;
          detailError.value = (error && error.reason) || "unknown";
        }
      }

      /** 处置成功后重读详情：状态与 `silenced_until` 都以服务端回的那份为准。 */
      async function act(alertId, action, payload = {}) {
        if (!writable || busy.value) return;
        busy.value = true;
        actionError.value = null;
        silenceNote.value = null;
        try {
          await deps.api[`${action}Alert`](alertId, payload);
          await readDetail(alertId);
          await refresh();
        } catch (error) {
          if (released) return;
          const reason = (error && error.reason) || "unknown";
          if (reason === "silence_duration_required") {
            silenceNote.value = (error && error.detail && error.detail.hint) || "先在场地设置里配 alert_defaults.silence_s，或稍后再试";
          } else {
            actionError.value = reason;
          }
        } finally {
          busy.value = false;
        }
      }

      function schedule() {
        const ms = Math.max(deps.store.effectiveOf(widget, "refresh_ms") || 200, POLL_MS);
        widget.lease.setTimeout(() => {
          refresh();
          schedule();
        }, ms);
      }

      schedule();
      rt.onMounted(refresh);
      widget.lease.onRelease(() => {
        released = true;
        open.value = null;
        detail.value = null;
      });

      return () => {
        deps.signal.tick.value;
        const site = deps.context.state.siteId;
        const listed = (rows.value && rows.value.rows) || [];
        const openCount = listed.filter((a) => a.status !== "resolved").length;

        const meta = h("p", { class: "meta" }, [
          `场地 ${site ?? "—"} · 挂着的 ${openCount} 条 / 最近 ${listed.length} 条（上限 ${SHOW_CAP}）`,
          rows.value && rows.value.truncated ? ` · 行数已达上限 ${rows.value.limit}，更早的没进来` : "",
        ]);
        const errorLine = failure.value
          ? h("p", { class: "warn" }, `取数失败：${failure.value.reason}${listed.length ? " · 列表沿用上一次的成功读数" : ""}`)
          : null;

        if (!listed.length) {
          return h("div", { class: "alerts" }, [
            meta,
            errorLine,
            empty(
              rt,
              rows.value ? "这一窗里没有告警" : "第一次读数还在路上",
              rows.value
                ? "规则引擎从插件判定里推出来的、等人处置的行会出现在这里；安静不等于没规则在跑"
                : null
            ),
          ]);
        }

        const body = h(
          "ul",
          { class: "alert-list" },
          listed.map((alert) =>
            h("li", {
              class: ["alert", `alert--${alert.status}`, open.value === alert.alert_id ? "is-open" : null],
              onClick: () => {
                open.value = open.value === alert.alert_id ? null : alert.alert_id;
                if (open.value !== null) readDetail(alert.alert_id);
                else detail.value = null;
              },
              title: "展开这条告警的说明、判定依据与处置",
            }, [
              h("span", { class: "alert-kind" }, KIND_TEXT[alert.kind] || `未知类型 ${alert.kind}`),
              h("span", { class: "alert-summary" }, whoOf(alert, deps)),
              alert.value === null || alert.value === undefined
                ? null
                : h("span", { class: "alert-value num" }, `${fmtDb(alert.value)}${alert.metric === "rssi_db" ? " dBFS" : ""}${alert.threshold === null || alert.threshold === undefined ? "" : ` / 门限 ${fmtDb(alert.threshold)}`}`),
              alert.count > 1 ? h("span", { class: "alert-count num" }, `×${alert.count}`) : null,
              h("span", { class: `chip chip--${alert.status}` }, STATUS_TEXT[alert.status] || alert.status),
              h("span", { class: "muted" }, fmtClock(alert.last_ts ?? alert.ts, "UTC")),
            ])
          )
        );

        const panel = open.value === null
          ? null
          : h("div", { class: "alert-detail" }, [
              detailError.value
                ? h("p", { class: "warn" }, `详情取不到：${detailError.value}`)
                : null,
              detail.value ? detailPanel(rt, deps, detail.value, { open, busy, writable, actionError, silenceNote, act }) : h("p", { class: "muted" }, "详情在路上……"),
            ]);

        return h("div", { class: "alerts" }, [meta, errorLine, body, panel]);
      };
    },
  };
}

/** 列表里"是谁的问题"：有节点说节点（带档），没有就说接入机，都不带就不指认。 */
function whoOf(alert, deps) {
  const node = alert.node_id
    ? deps.context.state.nodes.find((row) => row.node_id === alert.node_id)?.display_name || `节点 ${alert.node_id}`
    : null;
  const slot = alert.slot === null || alert.slot === undefined ? null : `${alert.slot + 1} 档`;
  const gw = alert.gateway_id && !node ? `接入机 ${alert.gateway_id}` : null;
  return [node, slot, gw].filter(Boolean).join(" · ") || "未指认主体";
}

function detailPanel(rt, deps, alert, ui) {
  const { h } = rt;
  const actions = ACTIONS_BY_STATUS[alert.status] || [];

  const evidenceValue = alert.evidence && alert.evidence.value;
  const evidence = h("pre", { class: "evidence" },
    alert.evidence && alert.evidence.error
      ? `证据链解析失败：${alert.evidence.error}`
      : JSON.stringify(evidenceValue ?? null, null, 2)
  );

  const actionButtons = h("p", { class: "review-actions" }, [
    actions.includes("ack")
      ? h("button", { class: "btn", type: "button", disabled: ui.busy.value, onClick: () => ui.act(alert.alert_id, "ack") }, "确认（有人看见了）")
      : null,
    actions.includes("silence")
      ? h("button", { class: "btn", type: "button", disabled: ui.busy.value, onClick: () => ui.act(alert.alert_id, "silence") }, "静默（场地默认时长）")
      : null,
    actions.includes("resolve")
      ? h("button", { class: "btn", type: "button", disabled: ui.busy.value, onClick: () => ui.act(alert.alert_id, "resolve") }, "解决（结束这一条）")
      : null,
  ]);

  return h("div", { class: "alert-panel" }, [
    h("p", { class: "meta" }, [
      sourceBadge(rt, alert.source),
      ` 告警 ${alert.alert_id} · 规则 ${alert.rule_id} · 场地 ${alert.site_id} · ${fmtClock(alert.ts, "UTC")}`,
      alert.count > 1 ? ` 起最近一次 ${fmtClock(alert.last_ts, "UTC")}` : "",
    ]),
    h("p", { class: "note" }, KIND_NOTE[alert.kind] || null),
    h("p", { class: "note" }, [
      `指标 ${alert.metric || "—"} · 读数 ${alert.value === null || alert.value === undefined ? "—" : fmtDb(alert.value)}`,
      alert.threshold === null || alert.threshold === undefined ? "" : ` · 门限 ${fmtDb(alert.threshold)}`,
      ` · 合并命中 ${alert.count} 次`,
      alert.silenced_until ? ` · 静默到 ${fmtClock(alert.silenced_until, "UTC")}` : "",
      alert.reviewed_at ? ` · 最近复核 ${fmtClock(alert.reviewed_at, "UTC")}` : "",
    ]),
    h("p", { class: "meta" }, "判定依据（插件写的证据，原样贴）："),
    evidence,
    h("p", { class: "meta" }, "能讲到哪一步（后端申报的局限）："),
    h("ul", { class: "absent" }, (alert.limitations || []).map((line) => h("li", line))),
    ui.writable
      ? actions.length ? actionButtons : null
      : h("p", { class: "warn" }, "汇聚实例只读：处置要在场地实例上做"),
    ui.silenceNote.value ? h("p", { class: "warn" }, `静默没有执行：${ui.silenceNote.value}`) : null,
    ui.actionError.value ? h("p", { class: "warn" }, `处置没有成功：${ui.actionError.value}`) : null,
  ]);
}
