/**
 * 事件信息流。**这一格的数据是观测事实**：WS 推来的那一窗（`live.js`）+ `GET /api/events`
 * 回填的历史，两条路撞到同一条记录时按 `event_id` 去重（帧里的 `event` 与那一行同形，
 * 所以去重是免费的）。S5 那版读的是 `demo.js` 编出来的形状，已经换掉。
 *
 * 三条口径：
 *
 * - **窗口有顶，并且顶上写着**。前端留 `EVENT_CAP` 条，这一格只画最近 `SHOW_CAP` 条；
 *   更早的走 REST 的窗口与上限，页面上明说"这一格不是归档"。
 * - **`kind` 认不出来就原样显示**，不并进"其他"，也不因为不认识就不画：事件类型由采集层
 *   写，将来多出一种，页面应该继续把它端上来（与 `node-status` 里"不认识 ≠ 不支持"同一条理由）。
 * - **详情里那句"能讲到哪一步"是后端说的**。`limitations` 来自 `routers/events.py`，
 *   页面只负责排出来——把"这块板安静了"翻译成"板子断电了"是这一层最容易犯的假话，
 *   所以那句话必须来自服务端而不是组件的形容词。
 */

import { fmtClock, fmtDb, fmtMhz, sourceBadge, sourceBadges, stateNote, statePill, empty } from "./parts.js";
import { currentBoard, dataState } from "./select.js";

export const plugin_id = "signal-events";

/** 一格里最多画这么多条：窗是 200 条，画 200 条会把这一格变成另一件事。 */
const SHOW_CAP = 50;
/** REST 回填的最快节奏（毫秒）。比 WS 慢得多是有意的：回填补的是历史，不是新事件。 */
const BACKFILL_MS = 5000;

const KIND_TEXT = {
  node_offline: "节点失联",
  node_online: "节点恢复",
};

export function create(rt, deps) {
  const { h, ref } = rt;

  return {
    name: "SignalEvents",
    props: { widget: { type: Object, required: true } },
    setup(props) {
      const beat = ref(0);
      const widget = props.widget;
      const open = ref(null);
      const detail = ref(null);
      const detailError = ref(null);
      let inFlight = false;
      let released = false;

      /**
       * 把 REST 的那一窗并进事件流。
       *
       * 场地取自**发起那一刻**的上下文；回来时已经换了场地就不采纳——回填是页面的礼貌行为，
       * 拿上一场地的历史塞进这一场地的窗里，正是 S6 出口判据要挡住的那件事。
       */
      async function backfill() {
        const site = deps.context.state.siteId;
        if (released || site === null || site === undefined || inFlight) return;
        inFlight = true;
        try {
          const answer = await deps.api.eventsFeed({ site, limit: SHOW_CAP });
          if (released || deps.context.state.siteId !== site) return;
          deps.live.backfill(answer.rows || []);
        } catch {
          // 回填失败不动 WS 那一窗：这一格的正文由推送说话，REST 只是补历史。
        } finally {
          inFlight = false;
        }
      }

      async function readDetail(eventId) {
        detail.value = null;
        detailError.value = null;
        try {
          const answer = await deps.api.eventDetail(eventId, deps.context.state.siteId);
          if (released || open.value !== eventId) return;
          detail.value = answer;
        } catch (error) {
          if (released) return;
          detailError.value = (error && error.reason) || "unknown";
        }
      }

      function schedule() {
        const ms = Math.max(deps.store.effectiveOf(widget, "refresh_ms") || 200, 500);
        widget.lease.setTimeout(() => {
          beat.value += 1;
          schedule();
        }, ms);
      }
      function scheduleBackfill() {
        widget.lease.setTimeout(() => {
          if (released) return;
          backfill();
          scheduleBackfill();
        }, BACKFILL_MS);
      }

      schedule();
      scheduleBackfill();
      rt.onMounted(backfill);
      widget.lease.onRelease(() => {
        released = true;
        open.value = null;
        detail.value = null;
      });

      return () => {
        beat.value;
        deps.signal.tick.value;
        const board = currentBoard(deps);
        const shown = dataState(deps, board);
        const feed = deps.live.state.feed;
        const listed = feed.slice(0, SHOW_CAP);
        const badges = sourceBadges(rt, feed.map((event) => event.source));

        const meta = h("p", { class: "meta" }, [
          badges,
          " ",
          statePill(rt, shown),
          ` 实时窗 ${feed.length} 条（上限 ${deps.live.status().caps.events}）· 这一格只画最近 ${listed.length} 条`,
          feed.length > listed.length ? "，其余在窗里没画" : "",
          ` · 更早的走 GET /api/events`,
        ]);

        if (!listed.length) {
          return h("div", { class: "events" }, [
            meta,
            empty(rt, shown.state === "no_board" ? shown.text : "这一窗里没有事件", "断流与恢复才会写事件：安静不等于没发生"),
          ]);
        }

        const staleLine = stateNote(rt, shown);
        const body = h(
          "ul",
          { class: "event-list" },
          listed.map((event) => {
            const known = KIND_TEXT[event.kind];
            return h("li", {
              class: ["event", event.kind ? `event--${event.kind}` : null, open.value === event.event_id ? "is-open" : null],
              onClick: () => {
                open.value = open.value === event.event_id ? null : event.event_id;
                if (open.value !== null) readDetail(event.event_id);
                else detail.value = null;
              },
              title: "展开这条记录的证据链与局限说明",
            }, [
              h("span", { class: "event-kind" }, known || `未知类型 ${event.kind}`),
              h("span", { class: "event-summary" }, summaryOf(event, deps)),
              event.slot === null || event.slot === undefined ? null : h("span", { class: "event-slot" }, `${event.slot + 1} 档`),
              event.value === null || event.value === undefined
                ? null
                : h("span", { class: "event-rssi num" }, `${fmtDb(event.value)}${event.metric === "rssi_db" ? " dBFS" : ""}`),
              sourceBadge(rt, event.source),
              // 画 `at`（`live.js` 已经按"没带偏移就补 Z"折成毫秒数）而不是裸串：
              // 直接 `Date.parse` 一个 naive ISO 会按本机时区解释，UTC 场地的时刻就错了几个小时。
              h("span", { class: "event-at muted" }, fmtClock(event.at ?? event.ts, "UTC")),
            ]);
          })
        );

        return h("div", { class: "events" }, [
          meta,
          staleLine,
          body,
          open.value === null
            ? null
            : h("div", { class: "event-detail" }, [
                detailError.value
                  ? h("p", { class: "warn" }, `详情取不到：${detailError.value}`)
                  : null,
                detail.value ? detailPanel(rt, deps, detail.value, open.value) : h("p", { class: "muted" }, "详情在路上……"),
              ]),
        ]);
      };
    },
  };
}

/**
 * 列表里那一行的"发生了什么"。只由**记录里有的字段**拼：
 * 有节点就说节点，没有就说接入机，`slot` 为空不假装是某个档位。
 */
function summaryOf(event, deps) {
  const who = event.node_id
    ? deps.context.state.nodes.find((row) => row.node_id === event.node_id)?.display_name || `节点 ${event.node_id}`
    : event.gateway_id
      ? `接入机 ${event.gateway_id}`
      : "未指认主体";
  const silence = event.payload && event.payload.silence_s !== undefined ? ` · 安静 ${event.payload.silence_s} s` : "";
  const freq = event.freq_khz ? ` · ${fmtMhz(event.freq_khz / 1000)}` : "";
  return `${who}${freq}${silence}`;
}

/** 详情：证据链原样贴，局限逐条列。`payload_error` 是后端说的"这段 JSON 坏了"，不替它圆。 */
function detailPanel(rt, deps, event, eventId) {
  const { h } = rt;
  return h("div", { class: "event-panel" }, [
    h("p", { class: "meta" }, [sourceBadge(rt, event.source), ` 记录 ${eventId} · 场地 ${event.site_id} · ${fmtClock(event.ts, "UTC")}`]),
    h("p", { class: "note" }, `类型 ${event.kind}${event.metric ? ` · 指标 ${event.metric}` : ""}${event.quality === null || event.quality === undefined ? "" : ` · 质量 ${event.quality}`}`),
    h(
      "pre",
      { class: "evidence" },
      event.payload_error ? `证据链解析失败：${event.payload_error}` : JSON.stringify(event.payload ?? null, null, 2)
    ),
    h("ul", { class: "absent" }, (event.limitations || []).map((line) => h("li", line))),
    event.slot === null || event.slot === undefined
      ? null
      : h("button", { class: "btn", type: "button", onClick: () => deps.actions.linkChannel(event.slot) }, "把这一档设为联动频道"),
  ]);
}
