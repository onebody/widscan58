/**
 * 单频道详情：盯住"当前联动的那一档"，把**这一帧里这一档真报出来的量**原样摆开。
 *
 * 它刻意不显示任何"距离/方向/位置"，也不显示"这一档是多少 MHz"——前者是规范 §2.3 禁止的
 * 推论，后者是**此刻真拿不到**的量：帧里只有整帧共用的 `loKhz`，档位→中心频率要靠频道方案表
 * 换算，而后端到今天没有那个端点。所以这一段把"没有"写成一行看得见的说明，而不是留白让人猜。
 *
 * 空值的口径：`null` 一律画成"—"并在下面说一句"这一模式确实没测"。LEVELS 的 RSSI 模式不发
 * EMA、不发 SNR、不发 peak，把缺字段渲染成 0 就是把"没测"说成"测到 0"。
 */

import { fmtClock, fmtDb, fmtHz, fmtKhzAsMhz, fmtMs, sourceBadges, stateNote, statePill, empty } from "./parts.js";
import { currentBoard, dataState, linkedSlot } from "./select.js";

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
        const slots = deps.store.effectiveOf(widget, "slots");
        const threshold = deps.store.effectiveOf(widget, "threshold_db");
        const board = currentBoard(deps);
        const shown = dataState(deps, board);
        const row0 = board ? deps.live.node(board.node_id) : null;
        const present = board ? deps.live.slotsOf(board.node_id) : [];
        const linked = linkedSlot(deps);
        const fallback = slots && slots.length ? slots[0] : present.length ? present[0] : null;
        const slot = linked !== null ? linked : fallback;

        const field = (label, value, cls = null, title = null) =>
          h("div", { class: ["field", cls] }, [
            h("span", { class: "label" }, label),
            h("span", { class: "value", title }, value),
          ]);

        const meta = h("p", { class: "meta" }, [
          // 没帧时说"无数据"而不是"来源未知"：后者描述的是一份来路不明的数，而这里的真相是没有数。
          sourceBadges(rt, row0 ? [row0.source] : []),
          " ",
          statePill(rt, shown),
          " 联动自多频道矩阵 / RSSI 趋势 / 事件流",
        ]);
        if (slot === null || slot === undefined || !row0 || !row0.channels.has(slot)) {
          const why =
            slot === null || slot === undefined
              ? "还没有联动的档位，也没有可回落的配置档位"
              : shown.state === "live" || shown.state === "stale"
                ? `这一帧里没有第 ${slot + 1} 档`
                : shown.text;
          return h("div", { class: "detail" }, [meta, empty(rt, why)]);
        }

        const point = deps.live.pointOf(board.node_id, slot);
        const active =
          threshold === null || threshold === undefined || point.rssi === null || point.rssi === undefined
            ? null
            : point.rssi > threshold;
        const band =
          point.obwLoHz === null || point.obwHiHz === null
            ? "—"
            : `${fmtHz(point.obwLoHz)} … ${fmtHz(point.obwHiHz)}`;

        return h("div", { class: "detail" }, [
          meta,
          stateNote(rt, shown),
          field("板", row0.display_name || `节点 ${row0.node_id}`),
          field("频道", `${slot + 1} 档`),
          field("当前 RSSI", `${fmtDb(point.rssi)} dBFS`, active ? "num--active" : null),
          field("活动判据", active === null ? "未判（这一格还没拿到阈值）" : active ? "高于阈值" : "低于阈值"),
          field("阈值", threshold === null || threshold === undefined ? "未设置" : `${fmtDb(threshold)} dBFS`),
          field("EMA", fmtDb(point.rssiEma), null, "rssiEma：LEVELS 的 RSSI 模式不发这一项"),
          field("底噪", fmtDb(point.floor), null, "floor"),
          field("SNR", fmtDb(point.snr), null, "snr"),
          field("曾清底噪", fmtDb(point.clearFloor), null, "clearFloor：这一档历史最低，没清过就是空"),
          field("峰值", fmtDb(point.peak), null, "peak"),
          field("峰值偏移", fmtHz(point.peakOffHz)),
          field("质心偏移", fmtHz(point.centroidOffHz)),
          field("占用带宽", band, null, "obwLoHz / obwHiHz：相对帧 LO 的偏移"),
          field("帧序", row0.seq === null || row0.seq === undefined ? "—" : String(row0.seq)),
          field("帧模式", row0.mode ?? "未报"),
          field("板内时刻", row0.tMs === null || row0.tMs === undefined ? "—" : `${fmtMs(row0.tMs)}`),
          field("帧 LO", fmtKhzAsMhz(row0.loKhz), null, "整帧共用一个 LO，不是这一档的中心频率"),
          field("饱和样本数", point.saturated === null || point.saturated === undefined ? "—" : String(point.saturated), null, "帧头 `saturated`：这一帧里顶到 ADC 量程的样本数，0 是没顶到"),
          field("档位标志", point.flags === null || point.flags === undefined ? "—" : String(point.flags), null, "固件的 CHF_* 位图，本系统按原值透传（frame_parser 不解码），所以这里只给原数"),
          field("更新时间", fmtClock(point.at, "UTC")),
          // 这一段是"未声明即不支持"与"拿不到就不编"的落点：明说这里没有的东西，比留白诚实。
          h("ul", { class: "absent" }, [
            h("li", "这一档的中心频率：帧里只有整帧的 LO，档位→频率要频道方案表，后端还没有那个端点"),
            h("li", "连续频谱：推送口只发 `samples`/`event` 两类帧，`spectrum` 帧在采集层按 `skipped_frames` 丢弃"),
            h("li", "距离/方向/坐标：RSSI 推不出这些，系统不提供"),
          ]),
        ]);
      };
    },
  };
}
