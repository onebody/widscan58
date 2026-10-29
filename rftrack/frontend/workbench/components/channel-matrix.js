/**
 * 多频道矩阵：一次看若干档的当前 RSSI 与活动态。**数据来自 `demo.js`**，每一行都带
 * `source: "demo"`，页面上那枚 `DEMO` 角标是这句话的可见形式。
 *
 * 点某一行 = 把这一档设为"当前联动频道"（`store.linkTo("channel", slot)`）：单频道详情、
 * RSSI 趋势与事件流都跟着它走。这是 S5 唯一允许的跨组件关系，传的是**频道号**而不是结论。
 */

import { fmtClock, fmtDb, fmtMhz, sourceBadge, empty, dot, row, table } from "./parts.js";

export const plugin_id = "channel-matrix";

export function create(rt, deps) {
  const { h, ref } = rt;

  return {
    name: "ChannelMatrix",
    props: { widget: { type: Object, required: true } },
    setup(props) {
      const beat = ref(0);
      const widget = props.widget;

      /** 心跳用 `lease.setTimeout` 自排：换刷新周期不用重建组件，且释放一定被记账。 */
      function schedule() {
        const ms = deps.store.effectiveOf(widget, "refresh_ms") || 200;
        widget.lease.setTimeout(() => {
          beat.value += 1;
          schedule();
        }, ms);
      }
      schedule();

      return () => {
        beat.value; // 依赖：每个节拍重读一次生效值与演示数
        deps.signal.tick.value; // 依赖：选中/联动/保存之后跟着重算
        const threshold = deps.store.effectiveOf(widget, "threshold_db");
        const refresh = deps.store.effectiveOf(widget, "refresh_ms") || 200;
        const slots = deps.store.effectiveOf(widget, "slots") || [...Array(deps.demo.DEMO_SLOTS).keys()];
        const data = deps.demo.channelMatrix({ slots, threshold, refreshMs: refresh });
        const linked = deps.store.state.link;
        const linkedId = linked && linked.kind === "channel" ? linked.id : null;
        if (!data.channels.length) {
          return empty(rt, "没有可显示的档位", "实例配置里的 slots 是空表");
        }
        return h("div", { class: "matrix" }, [
          h("p", { class: "meta" }, [
            sourceBadge(rt, "demo"),
            ` ${threshold === null || threshold === undefined ? "未设阈值（活动列不判）" : `阈值 ${fmtDb(threshold)} dBFS`}`,
            " · 离散频道 RSSI，不是频谱",
          ]),
          table(rt, {
            columns: ["档", "频率", "RSSI", "活动", "更新"],
            rows: data.channels.map((item) =>
              row(
                rt,
                [
                  String(item.slot + 1),
                  fmtMhz(item.mhz),
                  h("td", { class: item.active ? "num num--active" : "num" }, `${fmtDb(item.rssi_db)} dBFS`),
                  item.active === null
                    ? h("td", { class: "muted" }, "未判")
                    : h("td", [dot(rt, item.active)]),
                  h("td", { class: "muted" }, fmtClock(item.at, "UTC")),
                ],
                {
                  class: ["clickable", linkedId === item.slot ? "is-linked" : null],
                  onClick: () => deps.actions.linkChannel(item.slot),
                  title: "设为当前联动频道",
                }
              )
            ),
          }),
        ]);
      };
    },
  };
}
