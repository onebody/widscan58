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
 * - **`nodes` 配置勾了节点就分线对比**：一次请求（后端 `nodes` 参数做并集）拿回所有节点的
 *   行，按 `node_id` 折成一条线一个节点；所有线共用同一档（联动档或 `slots` 首档），
 *   因为"同一档跨节点比高低"才是分线要回答的问题——各看各的档是另一张图的事。
 *
 * 绘图分工沿用 S5：`model()` 把行折成坐标、`draw()` 落笔，两步都在 `paint()` 里，因为
 * **画布的真实尺寸只有那一刻才知道**（拖动格子会立刻改宽度，而像素坐标依赖它）。
 */

import { colorsFrom, draw, model, resizeCanvas } from "../chart.js";
import { fmtClock, sourceBadges, stateNote, statePill, empty } from "./parts.js";
import { currentBoard, dataState, linkedSlot, nodeScope } from "./select.js";

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
        const site = deps.context.state.siteId;
        const slot = slotOf();
        const scope = nodeScope(deps, widget);
        if (!scope.boards.length || site === null || site === undefined || slot === null) {
          history.value = null;
          failure.value = null;
          return;
        }
        const rangeS = deps.store.effectiveOf(widget, "time_range_s") || 30;
        const points = Math.min(MAX_POINTS, Math.max(8, Math.round((rangeS * 1000) / (deps.store.effectiveOf(widget, "refresh_ms") || 200))));
        const from = new Date((Date.now() - rangeS * 1000) - 1000).toISOString();
        inFlight = true;
        try {
          // 多节点分线对比是**一次**请求（后端 `nodes` 参数做并集），不是每节点各打一口。
          const params = { site, slot, from, limit: points };
          if (scope.multi) params.nodes = scope.boards.map((board) => board.node_id).join(",");
          else params.node = scope.boards[0].node_id;
          const answer = await deps.api.samplesSummary(params);
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
        const scope = nodeScope(deps, widget);
        const board = scope.multi ? scope.boards[0] : currentBoard(deps);
        const shown = dataState(deps, board);
        const slot = slotOf();
        const answer = history.value;
        const rows = answer ? answer.rows : [];
        const usable = rows.filter((row) => row.rssi_mean !== null && row.rssi_mean !== undefined);
        const dropped = rows.length - usable.length;
        // 行按 node_id 折成分线：勾选节点按配置顺序出线（颜色顺序稳定），
        // 答案里冒出的配置外节点（刚被删之类）也画，但标 #id 不冒充勾选名单。
        const byNode = new Map();
        for (const row of usable) {
          if (!byNode.has(row.node_id)) byNode.set(row.node_id, []);
          byNode.get(row.node_id).push({
            tick: row.ts_bucket,
            rssi_db: row.rssi_mean,
            at: row.last_at,
            source: row.source,
          });
        }
        const extras = [...byNode.keys()].filter((id) => !scope.boards.some((b) => b.node_id === id));
        const series = [
          ...scope.boards.map((b) => ({ label: b.display_name, samples: byNode.get(b.node_id) || [] })),
          ...extras.map((id) => ({ label: `#${id}`, samples: byNode.get(id) })),
        ];
        pending = { series, threshold };
        // 渲染末尾就画一次：`onUpdated` 要等这一帧提交，而这里的模型已经算好了。
        rt.nextTick(paint);
        const newest = usable.length ? usable[usable.length - 1] : null;
        const names = series.filter((group) => group.samples.length).map((group) => group.label);

        const meta = h("p", { class: "meta" }, [
          answer && answer.sources.length ? sourceBadges(rt, answer.sources) : null,
          statePill(rt, shown),
          scope.multi ? ` ${scope.boards.length} 个节点分线${names.length ? `：${names.join("、")}` : ""}` : ` ${board ? board.display_name : "无板"}`,
          slot === null || slot === undefined ? " · 没有联动的档位" : ` · 第 ${slot + 1} 档`,
          answer ? ` · 桶宽 ${answer.bucket} s · ${usable.length} 点` : " · 还没取到桶",
          newest ? h("span", { class: "muted" }, ` · 最新 ${fmtClock(newest.last_at, "UTC")}`) : null,
        ]);
        // 取数失败时曲线继续画上一答的那一段，但这句话必须挂在图上：静默沿用旧读数
        // 与"这是新数"之间的差别，页面上看不出来，而运营者据它判断板子是不是安静了。
        const errorLine = failure.value
          ? h("p", { class: "warn" }, `取数失败：${failure.value.reason}${usable.length ? " · 曲线沿用上一次的成功读数" : ""}`)
          : null;

        if (!usable.length) {
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
          h("canvas", { ref: canvas, class: "chart", "aria-label": `第 ${(slot ?? 0) + 1} 档 RSSI 趋势（摘要桶均值${scope.multi ? "，多节点分线" : ""}）` }),
          h("p", { class: "note" }, [
            "横轴：桶起点（unix 秒）；纵轴：该桶内 dBFS 均值。虚线是阈值，不是数据。",
            scope.multi ? ` 每个节点一条线（颜色顺序即勾选顺序）。` : null,
            dropped ? ` ${dropped} 个桶没有均值（那些桶里一个点都没有），已从折线里跳过。` : null,
            answer && answer.truncated ? ` 行数已达上限 ${answer.limit}，只画最近这一段。` : null,
          ]),
        ]);
      };
    },
  };
}
