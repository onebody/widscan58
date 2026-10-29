/**
 * 单频道详情：盯住"当前联动的那一档"。
 *
 * 它刻意不显示任何"距离/方向/位置"——本阶段只有 RSSI，把这些字段画出来就是规范 §2.3
 * 禁止的那件事。能力一栏写的是**这份数据能给什么、不能给什么**，而不是留白让人猜。
 */

import { fmtClock, fmtDb, fmtMhz, sourceBadge } from "./parts.js";

export const plugin_id = "channel-detail";

export function create(rt, deps) {
  const { h, ref } = rt;

  return {
    name: "ChannelDetail",
    props: { widget: { type: Object, required: true } },
    setup(props) {
      const beat = ref(0);
      const widget = props.widget;

      function schedule() {
        const ms = deps.store.effectiveOf(widget, "refresh_ms") || 200;
        widget.lease.setTimeout(() => {
          beat.value += 1;
          schedule();
        }, ms);
      }
      schedule();

      return () => {
        beat.value;
        deps.signal.tick.value;
        const slots = deps.store.effectiveOf(widget, "slots") || [...Array(deps.demo.DEMO_SLOTS).keys()];
        const linked = deps.store.state.link;
        const slot = linked && linked.kind === "channel" ? linked.id : slots[0];
        const threshold = deps.store.effectiveOf(widget, "threshold_db");
        const refresh = deps.store.effectiveOf(widget, "refresh_ms") || 200;
        const point = deps.demo.channelMatrix({ slots: [slot], threshold, refreshMs: refresh }).channels[0];

        const field = (label, value, cls = null) =>
          h("div", { class: ["field", cls] }, [h("span", { class: "label" }, label), h("span", { class: "value" }, value)]);

        return h("div", { class: "detail" }, [
          h("p", { class: "meta" }, [sourceBadge(rt, "demo"), " 联动自多频道矩阵 / RSSI 趋势"]),
          field("频道", `${slot + 1} 档 · ${fmtMhz(point.mhz)}`),
          field("当前 RSSI", `${fmtDb(point.rssi_db)} dBFS`, point.active ? "num--active" : null),
          field("活动判据", point.active === null ? "未判（这一格还没拿到阈值）" : point.active ? "高于阈值" : "低于阈值"),
          field("阈值", threshold === null || threshold === undefined ? "未设置" : `${fmtDb(threshold)} dBFS`),
          field("更新时间", fmtClock(point.at, "UTC")),
          // 这一段是"未声明即不支持"的落点：明说这里没有频谱、没有距离，比留白诚实。
          h("ul", { class: "absent" }, [
            h("li", "连续频谱：本阶段无数据源（spectrum-view 标在组件库里）"),
            h("li", "距离/方向/坐标：RSSI 推不出这些，系统不提供"),
          ]),
        ]);
      };
    },
  };
}
