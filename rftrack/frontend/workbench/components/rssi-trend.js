/**
 * RSSI 趋势：一档频道随时间的折线。**画的是节拍序列，不是频谱**——横轴是采样节拍（每拍一个
 * 点），纵轴是 dBFS。把 8 个频道画成连续频率轴是规范 §2.3 禁止的事，那条能力在组件库里
 * 单独立着 `spectrum-view = 不支持`。
 *
 * 绘图分工：`demo.series()` 出数 → `chart.model()` 把它折成坐标 → `chart.draw()` 落笔。
 * 后两步都发生在 `paint()` 里，因为**画布的真实尺寸只有那一刻才知道**（拖动格子会立刻改宽度，
 * 而模型里的像素坐标依赖它）。
 */

import { colorsFrom, draw, model, resizeCanvas } from "../chart.js";
import { fmtClock, fmtMhz, sourceBadge } from "./parts.js";

export const plugin_id = "rssi-trend";

/** 一次最多画这么多点：再多就是把手绘循环变成刷屏，而趋势图要看的本来就是"刚才那一段"。 */
const MAX_POINTS = 900;

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

      function paint() {
        const el = canvas.value;
        if (!el || !pending) return;
        const surface = resizeCanvas(el);
        const built = model({ ...pending, size: { w: surface.w, h: surface.h } });
        draw(surface.ctx, built, colorsFrom((name) => getComputedStyle(el).getPropertyValue(name)));
      }

      function schedule() {
        const ms = deps.store.effectiveOf(widget, "refresh_ms") || 200;
        widget.lease.setTimeout(() => {
          beat.value += 1;
        }, ms);
      }

      schedule();
      rt.onMounted(paint);
      rt.onUpdated(paint);
      // 格子被删除/换布局时组件才会卸载；先把数据摘掉，钩子即便晚一步跑也不会画到空节点上。
      widget.lease.onRelease(() => {
        pending = null;
      });

      return () => {
        beat.value;
        deps.signal.tick.value;
        const threshold = deps.store.effectiveOf(widget, "threshold_db");
        const refresh = deps.store.effectiveOf(widget, "refresh_ms") || 200;
        const rangeS = deps.store.effectiveOf(widget, "time_range_s") || 30;
        const points = Math.min(MAX_POINTS, Math.max(8, Math.round((rangeS * 1000) / refresh)));
        const slots = deps.store.effectiveOf(widget, "slots") || [...Array(deps.demo.DEMO_SLOTS).keys()];
        const linked = deps.store.state.link;
        const slot = linked && linked.kind === "channel" ? linked.id : slots[0];
        const data = deps.demo.series({ slot, points, refreshMs: refresh });
        pending = { samples: data.samples, threshold };
        // 渲染末尾就画一次：`onUpdated` 要等这一帧提交，而这里的模型已经算好了。
        rt.nextTick(paint);
        const newest = data.samples[data.samples.length - 1];
        return h("div", { class: "trend" }, [
          h("p", { class: "meta" }, [
            sourceBadge(rt, "demo"),
            ` ${slot + 1} 档 · ${fmtMhz(data.mhz)} · ${points} 点`,
            newest ? h("span", { class: "muted" }, ` · 最新 ${fmtClock(newest.at, "UTC")}`) : null,
          ]),
          h("canvas", { ref: canvas, class: "chart", "aria-label": `第 ${slot + 1} 档 RSSI 趋势（演示数据）` }),
          h("p", { class: "note" }, "横轴：采样节拍（每拍一个点）；纵轴：dBFS。虚线是阈值，不是数据。"),
        ]);
      };
    },
  };
}
