/**
 * RSSI 趋势：一档频道随时间的折线。**数据来自 `GET /api/samples/summary`（摘要桶）**，
 * 不是 `/ws/live`：推送口只保证"最后一帧"，历史曲线要的是一段窗口里的桶，那是 REST 的活。
 *
 * 三条口径：
 *
 * - **横轴是桶起点（unix 秒），不是频率。** 把 8 个频道画成连续频率轴是规范 §2.3 禁止的事；
 *   这一格的 x 是时间，每一拍是"这一秒的均值"。`chart.model()` 拿 `tick` 当 x，所以这里
 *   传 `ts_bucket` 而不是行号——行号会把断掉的那 20 分钟压成"看起来连续的 20 个点"。
 * - **`rssi_mean` 为空的桶不进折线，并且要数出来。** 空均值意味着那个桶里一个点都没有；
 *   把它当 0 或当上一个值画进去，曲线就会在安静时段贴底，而"贴底"读起来像"信号很弱"。
 * - **轮询有下限**：这一口读的是库，不是内存。200 ms 刷一次不会让曲线更新（桶宽本来就是
 *   秒级），只会把 SQLite 变成这一页的节拍器。所以下限 1 s，与格子生效的 `refresh_ms` 取大。
 *
 * 绘图分工沿用 S5：`model()` 把行折成坐标、`draw()` 落笔，两步都在 `paint()` 里，因为
 * **画布的真实尺寸只有那一刻才知道**（拖动格子会立刻改宽度，而像素坐标依赖它）。
 */

import { colorsFrom, draw, model, resizeCanvas } from "../chart.js";
import { fmtClock, sourceBadges, stateNote, statePill, empty } from "./parts.js";
import { currentBoard, dataState, linkedSlot } from "./select.js";

export const plugin_id = "rssi-trend";

/** 一次最多画这么多点：再多就是把手绘循环变成刷屏，而趋势图要看的本来就是"刚才那一段"。 */
const MAX_POINTS = 900;
/** 摘要桶的最小轮询间隔（毫秒）。见模块开头第三条。 */
const MIN_POLL_MS = 1000;

export function create(rt, deps) {
  const { h, ref } = rt;

  return {
    name: "RssiTrend",
    props: { widget: { type: Object, required: true } },
    setup(props) {
      const beat = ref(0);
      const canvas = ref(null);
      const widget = props.widget;
      /** 最近一次要画的东西；`null` 就是"这一版还没数据"，`paint` 据此什么都不画而不是报错。 */
      let pending = null;
      /** 上一答回来的摘要：读数失败时沿用，但要和错误一起说，不能悄悄换成旧数装成新的。 */
      const history = ref(null);
      const failure = ref(null);
      let inFlight = false;
      let released = false;

      function paint() {
        const el = canvas.value;
        if (!el || !pending) return;
        const surface = resizeCanvas(el);
        const built = model({ ...pending, size: { w: surface.w, h: surface.h } });
        draw(surface.ctx, built, colorsFrom((name) => getComputedStyle(el).getPropertyValue(name)));
      }

      function slotOf() {
        const linked = linkedSlot(deps);
        if (linked !== null) return linked;
        const slots = deps.store.effectiveOf(widget, "slots");
        return slots && slots.length ? slots[0] : null;
      }

      /** 一次取数。`site`/`node`/`slot` 全部取自**发起那一刻**的上下文，回来时换了就不采纳。 */
      async function load() {
        if (released || inFlight) return;
        const board = currentBoard(deps);
        const site = deps.context.state.siteId;
        const slot = slotOf();
        if (board === null || site === null || site === undefined || slot === null) {
          history.value = null;
          failure.value = null;
          return;
        }
        const rangeS = deps.store.effectiveOf(widget, "time_range_s") || 30;
        const points = Math.min(MAX_POINTS, Math.max(8, Math.round((rangeS * 1000) / (deps.store.effectiveOf(widget, "refresh_ms") || 200))));
        const from = new Date((Date.now() - rangeS * 1000) - 1000).toISOString();
        inFlight = true;
        try {
          const answer = await deps.api.samplesSummary({ site, node: board.node_id, slot, from, limit: points });
          if (released) return;
          if (deps.context.state.siteId !== site) return; // 换场地之后的迟到答案：丢掉
          history.value = answer;
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
      rt.onMounted(() => {
        load();
        paint();
      });
      rt.onUpdated(paint);
      // 格子被删除/换布局时组件才会卸载；先把数据摘掉，钩子即便晚一步跑也不会画到空节点上。
      widget.lease.onRelease(() => {
        released = true;
        pending = null;
        history.value = null;
      });

      return () => {
        beat.value;
        deps.signal.tick.value;
        const threshold = deps.store.effectiveOf(widget, "threshold_db");
        const board = currentBoard(deps);
        const shown = dataState(deps, board);
        const slot = slotOf();
        const answer = history.value;
        const rows = answer ? answer.rows : [];
        const usable = rows.filter((row) => row.rssi_mean !== null && row.rssi_mean !== undefined);
        const dropped = rows.length - usable.length;
        const samples = usable.map((row) => ({
          tick: row.ts_bucket,
          rssi_db: row.rssi_mean,
          at: row.last_at,
          source: row.source,
        }));
        pending = { samples, threshold };
        // 渲染末尾就画一次：`onUpdated` 要等这一帧提交，而这里的模型已经算好了。
        rt.nextTick(paint);
        const newest = samples[samples.length - 1];

        const meta = h("p", { class: "meta" }, [
          answer && answer.sources.length ? sourceBadges(rt, answer.sources) : null,
          statePill(rt, shown),
          ` ${board ? board.display_name : "无板"}`,
          slot === null || slot === undefined ? " · 没有联动的档位" : ` · 第 ${slot + 1} 档`,
          answer ? ` · 桶宽 ${answer.bucket} s · ${usable.length} 点` : " · 还没取到桶",
          newest ? h("span", { class: "muted" }, ` · 最新 ${fmtClock(newest.at, "UTC")}`) : null,
        ]);
        // 取数失败时曲线继续画上一答的那一段，但这句话必须挂在图上：静默沿用旧读数
        // 与"这是新数"之间的差别，页面上看不出来，而运营者据它判断板子是不是安静了。
        const errorLine = failure.value
          ? h("p", { class: "warn" }, `取数失败：${failure.value.reason}${samples.length ? " · 曲线沿用上一次的成功读数" : ""}`)
          : null;

        if (!samples.length) {
          return h("div", { class: "trend" }, [
            meta,
            errorLine,
            empty(
              rt,
              shown.state === "no_board" ? shown.text : "摘要桶里还没有这一档的行",
              answer
                ? `窗口 ${answer.range.span_s} s 内 0 行；刚接上的板要等第一个桶闭合（写入档 ${answer.bucket} s）`
                : "第一次读数还在路上"
            ),
          ]);
        }
        return h("div", { class: "trend" }, [
          meta,
          errorLine,
          stateNote(rt, shown),
          h("canvas", { ref: canvas, class: "chart", "aria-label": `第 ${(slot ?? 0) + 1} 档 RSSI 趋势（摘要桶均值）` }),
          h("p", { class: "note" }, [
            "横轴：桶起点（unix 秒）；纵轴：该桶内 dBFS 均值。虚线是阈值，不是数据。",
            dropped ? ` ${dropped} 个桶没有均值（那些桶里一个点都没有），已从折线里跳过。` : null,
            answer && answer.truncated ? ` 行数已达上限 ${answer.limit}，只画最近这一段。` : null,
          ]),
        ]);
      };
    },
  };
}
