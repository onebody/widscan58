/**
 * 频道活动热图：频道（档位）× 时间的格子，每格回答"这一秒这一档越过阈值没有"。
 * 取数与 rssi-trend 同源——`GET /api/samples/summary` 摘要桶，但**不给 slot**，一次拿全部档位。
 *
 * 三条口径：
 *
 * - **活动判据只有一个数：`threshold_db` 生效值。** 规则插件用它判活动、这格用它上色，
 *   两处读的是同一个生效值（`effectiveOf`），页面上不出现第二个阈值。没有阈值就画不出
 *   活动/静默的界线——那种情况下格子照画（电平还在），但整版标注"判据未设"，不猜。
 * - **`rssi_mean` 为空的桶是"一个点都没有"，不是 0。** 把它画成"静默"会让安静时段看起来
 *   像信号被测过了且很弱；它得是第三种颜色，鼠标悬停要能说出为什么。
 * - **行按"桶里出现过的档位"画，不画满 8 行。** 没进过帧的档位画成一行灰格子会被读成
 *   "测过且安静"——channel-matrix 在实时那一侧同理（它只画帧里出现过的档）。
 */

import { fmtClock, sourceBadges, stateNote, statePill, empty, table } from "./parts.js";
import { currentBoard, dataState, linkedSlot } from "./select.js";

export const plugin_id = "channel-activity-heatmap";

/** 摘要桶的最小轮询间隔（毫秒）。与 rssi-trend 同一条理由：读的是库，别把 SQLite 当节拍器。 */
const MIN_POLL_MS = 1000;

export function create(rt, deps) {
  const { h, ref } = rt;

  return {
    name: "ChannelActivityHeatmap",
    props: { widget: { type: Object, required: true } },
    setup(props) {
      const beat = ref(0);
      const widget = props.widget;
      const answer = ref(null);
      const failure = ref(null);
      let inFlight = false;
      let released = false;

      function slotOf() {
        const linked = linkedSlot(deps);
        if (linked !== null) return linked;
        const slots = deps.store.effectiveOf(widget, "slots");
        return slots && slots.length ? slots[0] : null;
      }

      /** 一次取数，参数在发起那一刻定型；回来时场地已换就丢掉（迟到答案不作数）。 */
      async function load() {
        if (released || inFlight) return;
        const board = currentBoard(deps);
        const site = deps.context.state.siteId;
        if (board === null || site === null || site === undefined) {
          answer.value = null;
          failure.value = null;
          return;
        }
        const rangeS = deps.store.effectiveOf(widget, "time_range_s") || 30;
        // 桶宽由后端挑（它知道该场地存过哪几档粒度）；行数上限按"桶数 × 档数"估，截断由后端说。
        const buckets = Math.max(8, Math.round((rangeS * 1000) / (deps.store.effectiveOf(widget, "refresh_ms") || 200)));
        const from = new Date(Date.now() - rangeS * 1000 - 1000).toISOString();
        inFlight = true;
        try {
          // 注意没有 slot：一次取这块板全部档位，热图才画得成频道 × 时间。
          const got = await deps.api.samplesSummary({
            site,
            node: board.node_id,
            from,
            limit: Math.min(600, buckets * 8),
          });
          if (released) return;
          if (deps.context.state.siteId !== site) return;
          answer.value = got;
          failure.value = null;
        } catch (error) {
          if (released) return;
          failure.value = { reason: error && error.reason ? error.reason : "unknown", message: String((error && error.message) || error) };
        } finally {
          inFlight = false;
        }
      }

      function schedule() {
        const ms = Math.max(MIN_POLL_MS, deps.store.effectiveOf(widget, "refresh_ms") || 200);
        widget.lease.setTimeout(() => {
          beat.value += 1;
          load();
          schedule();
        }, ms);
      }

      schedule();
      rt.onMounted(load);
      widget.lease.onRelease(() => {
        released = true;
        answer.value = null;
      });

      return () => {
        beat.value;
        deps.signal.tick.value;
        const threshold = deps.store.effectiveOf(widget, "threshold_db");
        const board = currentBoard(deps);
        const shown = dataState(deps, board);
        const answer_ = answer.value;
        const rows = answer_ ? answer_.rows : [];

        // 把行折成 [档位 → 该档的桶序列]。行来自后端升序，直接分组即可。
        const bySlot = new Map();
        const bucketTicks = [];
        for (const r of rows) {
          if (!bySlot.has(r.slot)) bySlot.set(r.slot, new Map());
          bySlot.get(r.slot).set(r.ts_bucket, r);
          if (!bucketTicks.includes(r.ts_bucket)) bucketTicks.push(r.ts_bucket);
        }
        bucketTicks.sort((a, b) => a - b);
        const slots = [...bySlot.keys()].sort((a, b) => a - b);
        // 阈值缺失时不判活动：有均值的格子走"灰"（有电平），没有的走"空"，界线那句 warn 会说清。
        const cellClass = (mean) =>
          mean === null || mean === undefined ? "heat-cell heat-cell--empty" : threshold !== null && mean >= threshold ? "heat-cell heat-cell--active" : "heat-cell heat-cell--idle";
        const titleFor = (r, slot, tick) =>
          r
            ? `第 ${slot + 1} 档 ${fmtClock(r.ts_bucket, "UTC")} · 均值 ${r.rssi_mean === null ? "无（桶里没有点）" : `${r.rssi_mean.toFixed(1)} dBFS`}${threshold === null ? "" : ` · 判据 ≥ ${threshold} dBFS`}`
            : `第 ${slot + 1} 档 ${fmtClock(tick, "UTC")} 无摘要行`;

        const grid =
          !rows.length
            ? null
            : table(rt, {
                columns: ["档", ...bucketTicks.map((tick) => h("th", { class: "heat-tick", title: new Date(tick * 1000).toISOString() }, fmtClock(tick, "UTC")))],
                rows: slots.map((slot) =>
                  h("tr", [
                    h("td", {
                      class: ["clickable", linkedSlot(deps) === slot ? "is-linked" : null],
                      onClick: () => deps.actions.linkChannel(slot),
                    }, String(slot + 1)),
                    ...bucketTicks.map((tick) => {
                      const r = bySlot.get(slot).get(tick) || null;
                      const mean = r ? r.rssi_mean : null;
                      return h("td", { class: cellClass(mean), title: titleFor(r, slot, tick) }, "");
                    }),
                  ])
                ),
              });

        const meta = h("p", { class: "meta" }, [
          answer_ && answer_.sources.length ? sourceBadges(rt, answer_.sources) : null,
          statePill(rt, shown),
          ` ${board ? board.display_name : "无板"}`,
          slotOf() === null || slotOf() === undefined ? "" : ` · 联动第 ${slotOf() + 1} 档`,
          answer_ ? ` · 桶宽 ${answer_.bucket} s · ${slots.length} 档 × ${bucketTicks.length} 桶` : " · 还没取到桶",
        ]);

        const thresholdNote =
          threshold === null
            ? h("p", { class: "warn" }, "没有生效的 threshold_db：活动/静默画不出界线，格子按「有电平/无均值」两色显示")
            : null;
        const errorLine = failure.value
          ? h("p", { class: "warn" }, `取数失败：${failure.value.reason}${rows.length ? " · 色块沿用上一次的成功读数" : ""}`)
          : null;

        if (!rows.length) {
          return h("div", { class: "heatmap" }, [
            meta,
            thresholdNote,
            errorLine,
            empty(
              rt,
              shown.state === "no_board" ? shown.text : "摘要桶里还没有这一窗口的行",
              answer_ ? `窗口 ${answer_.range.span_s} s 内 0 行；刚接上的板要等第一个桶闭合（写入档 ${answer_.bucket} s）` : "第一次读数还在路上"
            ),
          ]);
        }
        return h("div", { class: "heatmap" }, [
          meta,
          thresholdNote,
          errorLine,
          stateNote(rt, shown),
          grid,
          h("p", { class: "note" }, [
            "绿格：桶均值越过阈值（活动）；灰格：没越过；空格：那个桶里一个点都没有，不是 0。",
            answer_ && answer_.truncated ? ` 行数已达上限 ${answer_.limit}，只画最近这一段。` : null,
          ]),
        ]);
      };
    },
  };
}
