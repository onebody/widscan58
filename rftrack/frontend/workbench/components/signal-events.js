/**
 * 事件信息流。本阶段的事件**全部来自演示源**：`kind`/`level` 是给界面摆位用的形状，
 * 不是后端 `event` 表的读数（那是 S6）。所以这里没有"确认""忽略""指派"这类会假装
 * 系统已经能处置事件的按钮，只有选择与联动。
 */

import { fmtClock, fmtDb, sourceBadge, empty } from "./parts.js";

export const plugin_id = "signal-events";

const KIND_TEXT = {
  activity: "活动",
  node_state: "节点状态",
  threshold: "越限",
};

export function create(rt, deps) {
  const { h, ref } = rt;

  return {
    name: "SignalEvents",
    props: { widget: { type: Object, required: true } },
    setup(props) {
      const beat = ref(0);
      const widget = props.widget;

      function schedule() {
        const ms = deps.store.effectiveOf(widget, "refresh_ms") || 200;
        widget.lease.setTimeout(() => {
          beat.value += 1;
        }, Math.max(ms, 500));
      }
      schedule();

      return () => {
        beat.value;
        deps.signal.tick.value;
        const linked = deps.store.state.link;
        const linkedId = linked && linked.kind === "channel" ? linked.id : null;
        const data = deps.demo.events({
          limit: 12,
          refreshMs: deps.store.effectiveOf(widget, "refresh_ms") || 200,
        });
        if (!data.items.length) {
          return empty(rt, "没有事件", "演示源这一拍没产出条目");
        }
        return h("div", { class: "events" }, [
          h("p", { class: "meta" }, [
            sourceBadge(rt, "demo"),
            " 演示事件 · 事件表属 S6，这里不接后端",
          ]),
          h(
            "ul",
            { class: "event-list" },
            data.items.map((item) =>
              h("li", {
                class: ["event", `event--${item.level}`, linkedId === item.slot ? "is-linked" : null],
                onClick: () => deps.actions.linkChannel(item.slot),
                title: "把这事件的频道设为联动频道",
              }, [
                h("span", { class: "event-level" }, item.level),
                h("span", { class: "event-kind" }, KIND_TEXT[item.kind] || item.kind),
                h("span", { class: "event-summary" }, item.summary),
                h("span", { class: "event-slot" }, `${item.slot + 1} 档`),
                h("span", { class: "event-rssi num" }, `${fmtDb(item.rssi_db)} dBFS`),
                h("span", { class: "event-at muted" }, fmtClock(item.at, "UTC")),
              ])
            )
          ),
        ]);
      };
    },
  };
}
