/**
 * 目标详情：候选目标（S8 的 `candidate_target`）列表 + 详情面板（时间线 / 覆盖与未知项 /
 * 局限 / 人工复核）。整格读 REST——`GET /api/targets`，与推送口无关。
 *
 * 四条口径：
 *
 * - **局限逐条来自后端**（`limitations`），页面只排版不翻译。把"局限未申报"渲染成
 *   "无局限"是这一格最贵的假话——那两个结论差一次人工判断。
 * - **复核是写侧**。按钮先看实例角色：aggregator 上直接说"只读"（服务端 405 同一句话），
 *   不让运营者点了才知道。复核成功后重读详情：`review_json` 留最近一次决定，改判可反复。
 * - **证据是观测行里拷贝的那份**，不是插件行还活着才有的东西（finding 会被保留清理掉，
 *   拷贝不会）；`evidence.error` 坏了就如实说坏。
 * - **时间线里认不出的 `source` 原样画**（`sourceBadge` 的"来源未知"分支），不并进"真实"。
 */

import { fmtClock, fmtDb, sourceBadge, sourceBadges, empty } from "./parts.js";

export const plugin_id = "target-inspector";

/** 列表轮询节奏（毫秒）。目标由引擎按 5 分钟合并窗聚合，5 s 读一次库足够跟得上。 */
const POLL_MS = 5000;
/** 一格里最多画这么多条目标：详情承担深度，列表只承担"最近出没的是哪些"。 */
const SHOW_CAP = 50;

const KIND_TEXT = {
  threshold_crossing: "越限",
  slot_rise: "档位抬升",
  multi_node_interference: "多节点干扰",
};

const STATUS_TEXT = {
  unreviewed: "未复核",
  confirmed: "已确认",
  rejected: "已否定",
};

export function create(rt, deps) {
  const { h, ref } = rt;

  return {
    name: "TargetInspector",
    props: { widget: { type: Object, required: true } },
    setup(props) {
      const beat = ref(0);
      const widget = props.widget;
      const rows = ref(null);
      const failure = ref(null);
      const open = ref(null);
      const detail = ref(null);
      const detailError = ref(null);
      const note = ref("");
      const reviewBusy = ref(false);
      const reviewError = ref(null);
      let inFlight = false;
      let released = false;

      async function load() {
        const site = deps.context.state.siteId;
        if (released || site === null || site === undefined || inFlight) return;
        inFlight = true;
        try {
          const answer = await deps.api.targetsFeed({ site, limit: SHOW_CAP });
          if (released || deps.context.state.siteId !== site) return;
          rows.value = answer;
          failure.value = null;
        } catch (error) {
          if (released) return;
          failure.value = { reason: error && error.reason ? error.reason : "unknown", message: String((error && error.message) || error) };
        } finally {
          inFlight = false;
        }
      }

      async function readDetail(targetId) {
        detail.value = null;
        detailError.value = null;
        try {
          const answer = await deps.api.targetDetail(targetId, deps.context.state.siteId);
          if (released || open.value !== targetId) return;
          detail.value = answer;
        } catch (error) {
          if (released) return;
          detailError.value = (error && error.reason) || "unknown";
        }
      }

      async function review(targetId, decision) {
        if (reviewBusy.value) return;
        reviewBusy.value = true;
        reviewError.value = null;
        try {
          const payload = { decision };
          const text = note.value.trim();
          if (text) payload.note = text;
          const answer = await deps.api.reviewTarget(targetId, payload);
          detail.value = answer;
          note.value = "";
          load();
        } catch (error) {
          reviewError.value = error && error.reason ? error.reason : String((error && error.message) || error);
        } finally {
          reviewBusy.value = false;
        }
      }

      function schedule() {
        widget.lease.setTimeout(() => {
          if (released) return;
          beat.value += 1;
          load();
          schedule();
        }, POLL_MS);
      }
      schedule();
      rt.onMounted(load);
      widget.lease.onRelease(() => {
        released = true;
        rows.value = null;
        open.value = null;
        detail.value = null;
      });

      return () => {
        beat.value;
        deps.signal.tick.value;
        const site = deps.context.state.siteId;
        const listed = rows.value ? rows.value.rows || [] : [];
        const writable = deps.context.state.instanceRole !== "aggregator";

        const meta = h("p", { class: "meta" }, [
          `场地 ${site ?? "—"} · 最近出没 ${listed.length} 条（上限 ${SHOW_CAP}）`,
          rows.value && rows.value.truncated ? ` · 行数已达上限 ${rows.value.limit}，更早的没进来` : "",
        ]);
        const errorLine = failure.value
          ? h("p", { class: "warn" }, `取数失败：${failure.value.reason}${listed.length ? " · 列表沿用上一次的成功读数" : ""}`)
          : null;

        if (!listed.length) {
          return h("div", { class: "targets" }, [
            meta,
            errorLine,
            empty(
              rt,
              rows.value ? "这一窗里没有候选目标" : "第一次读数还在路上",
              rows.value ? "插件判定过阈值、聚合引擎把它并成目标之后，这里才有行" : null
            ),
          ]);
        }

        const body = h(
          "ul",
          { class: "target-list" },
          listed.map((target) =>
            h("li", {
              class: ["target", open.value === target.target_id ? "is-open" : null],
              onClick: () => {
                open.value = open.value === target.target_id ? null : target.target_id;
                if (open.value !== null) readDetail(target.target_id);
                else detail.value = null;
              },
              title: "展开这条目标的证据、时间线与复核",
            }, [
              h("span", { class: "target-kind" }, KIND_TEXT[target.kind] || `未知类型 ${target.kind}`),
              target.label ? h("span", { class: "target-label" }, target.label) : null,
              h("span", { class: "target-confidence num" }, `置信 ${(target.confidence ?? 0).toFixed(2)}`),
              h("span", { class: `chip chip--${target.review_status}` }, STATUS_TEXT[target.review_status] || target.review_status),
              h("span", { class: "muted" }, `${fmtClock(target.first_seen_at, "UTC")} – ${fmtClock(target.last_seen_at, "UTC")}`),
            ])
          )
        );

        const panel = open.value === null
          ? null
          : h("div", { class: "target-detail" }, [
              detailError.value ? h("p", { class: "warn" }, `详情取不到：${detailError.value}`) : null,
              detail.value ? detailPanel(rt, deps, detail.value, { open, note, review, reviewBusy, reviewError, writable }) : h("p", { class: "muted" }, "详情在路上……"),
            ]);

        return h("div", { class: "targets" }, [meta, errorLine, body, panel]);
      };
    },
  };
}

function detailPanel(rt, deps, target, ui) {
  const { h } = rt;
  const observations = target.observations || [];
  const reviewValue = target.review && target.review.value;

  const reviewButtons = h("p", { class: "review-actions" }, [
    h("button", {
      class: "btn",
      type: "button",
      disabled: ui.reviewBusy.value || !ui.writable,
      onClick: () => ui.review(target.target_id, "confirmed"),
    }, "确认"),
    h("button", {
      class: "btn",
      type: "button",
      disabled: ui.reviewBusy.value || !ui.writable,
      onClick: () => ui.review(target.target_id, "rejected"),
    }, "否定"),
    h("input", {
      class: "review-note",
      type: "text",
      placeholder: "复核说明（可空，≤2000 字）",
      value: ui.note.value,
      onInput: (event) => {
        ui.note.value = event.target.value;
      },
    }),
  ]);

  return h("div", { class: "target-panel" }, [
    h("p", { class: "meta" }, [
      `目标 ${target.target_id} · 场地 ${target.site_id}`,
      target.producer ? ` · 产出 ${target.producer}` : "",
      ` · 置信 ${(target.confidence ?? 0).toFixed(2)}（${target.confidence_basis || "无声明"}）`,
    ]),
    h("p", { class: "note" }, [
      `${STATUS_TEXT[target.review_status] || target.review_status}`,
      target.reviewed_at ? ` · 最近复核 ${fmtClock(target.reviewed_at, "UTC")}` : "",
      reviewValue ? ` · 决定 ${reviewValue.decision}${reviewValue.note ? `：${reviewValue.note}` : ""}` : "",
    ]),
    h("ul", { class: "absent" }, (target.limitations || []).map((line) => h("li", line))),
    observations.length
      ? h("table", { class: "grid-table" }, [
          h("thead", [h("tr", ["时刻", "节点", "档", "指标", "读数", "来源", "证据"].map((label) => h("th", label)))]),
          h("tbody", observations.map((obs) =>
            h("tr", [
              h("td", fmtClock(obs.ts, "UTC")),
              h("td", obs.node_id === null || obs.node_id === undefined ? "—" : String(obs.node_id)),
              h("td", obs.slot === null || obs.slot === undefined ? "—" : String(obs.slot + 1)),
              h("td", obs.metric || "—"),
              h("td", { class: "num" }, obs.value === null || obs.value === undefined ? "—" : `${fmtDb(obs.value)}${obs.metric === "rssi_db" ? " dBFS" : ""}`),
              h("td", sourceBadge(rt, obs.source)),
              h("td", [
                h("pre", { class: "evidence" },
                  obs.evidence && obs.evidence.error ? `证据链解析失败：${obs.evidence.error}` : JSON.stringify(obs.evidence ? obs.evidence.value : null, null, 2)
                ),
              ]),
            ])
          )),
        ])
      : h("p", { class: "muted" }, "时间线上还没有观测行"),
    h("p", { class: "meta" }, [
      sourceBadges(rt, observations.map((obs) => obs.source)),
    ]),
    h("p", { class: "note" }, [
      "覆盖与未知项：",
      target.coverage && target.coverage.error
        ? `coverage_json 坏了（${target.coverage.error}）`
        : target.coverage && target.coverage.value
          ? JSON.stringify(target.coverage.value)
          : "没写",
    ]),
    ui.writable
      ? reviewButtons
      : h("p", { class: "warn" }, "汇聚实例只读：复核要在场地实例上做"),
    ui.reviewError.value ? h("p", { class: "warn" }, `复核没有成功：${ui.reviewError.value}`) : null,
    (() => {
      const nodes = [...new Set(observations.map((obs) => obs.node_id).filter((id) => id !== null && id !== undefined))];
      if (!nodes.length) return null;
      return h("p", { class: "note" }, nodes.map((nodeId) =>
        h("button", {
          class: "btn",
          type: "button",
          key: nodeId,
          onClick: () => deps.actions.selectNode(nodeId),
        }, `切到节点 ${nodeId}`)
      ));
    })(),
  ]);
}
