/**
 * 多频道矩阵：一次看当前这块板的若干档电平。**数据来自 `/ws/live` 的那一条连接**
 * （`live.js` 合并好的最后一帧），不再是 `demo.js`。
 *
 * 三处口径写在这里，是因为它们在 S6 真的变了形：
 *
 * - **"档"这一列来自帧，不来自配置。** 固件在 LEVELS 模式一帧只报它正在跳的那几个档
 *   （`hops`），把 `slots_per_node=8` 画成八行就等于替没测的档编一个值。配置里的 `slots`
 *   在这里是**筛选**：它只能把看到的那些档缩小，不能凭空加行；被筛掉的档位另说一句"还有 N 档没进帧"。
 * - **没有"频率"这一列。** 帧里只有整帧共用的 `loKhz`，而"这一档中心是 5732 MHz"要靠频道方案
 *   表换算——后端到 S6 为止**没有那个端点**（`/api/channel-plans` 属后续阶段）。
 *   于是这一格把 LO 写在元信息里并明说它是整帧的，不再逐行报频率：报不出来的东西不报。
 * - **一格里只显示一块板，就是四级选择器选中的那一块（`select.js` 的唯一判据）——除非 `nodes`
 *   配置勾了节点。** 勾了就按勾选的分段渲染（一段一节点，各自带四态牌子），候选是场地级、
 *   可跨接入机；一个节点一个段落而不是把所有节点挤进一张表，是因为窄屏上"这一行是谁的"必须一直有答案。
 *   八块板 × 八档画在同一张表里看着更全，但"现在看的是哪块板"这个问题在窄屏上就没了答案。
 */

import { fmtClock, fmtDb, fmtKhzAsMhz, sourceBadges, stateNote, statePill, empty, dot, row, table } from "./parts.js";
import { currentBoard, dataState, levelsDeclared, linkedSlot, nodeScope } from "./select.js";

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
        beat.value; // 依赖：每个节拍重读一次生效值与实时数（重画由这一拍驱动，帧来了不催渲染）
        deps.signal.tick.value; // 依赖：选中/联动/保存之后跟着重算
        const threshold = deps.store.effectiveOf(widget, "threshold_db");
        const wanted = deps.store.effectiveOf(widget, "slots");
        const linked = linkedSlot(deps);
        const scope = nodeScope(deps, widget);
        if (scope.multi) {
          const rowOf = (nodeId) => deps.live.node(nodeId);
          const sources = [...new Set(scope.boards.map((b) => rowOf(b.node_id)).filter(Boolean).map((r) => r.source))];
          const meta = h("p", { class: "meta" }, [
            sourceBadges(rt, sources),
            " ",
            ` ${scope.boards.length} 个节点（按配置勾选）`,
            rowOf(scope.boards[0]?.node_id) ? h("span", { class: "muted" }, ` · ${deps.live.statusLine()}`) : "",
            " · 离散频道电平，不是频谱",
          ]);
          const missingNote = scope.missing.length
            ? h("p", { class: "note note--warn" }, `配置勾选的节点 #${scope.missing.join("、#")} 已经不在场地里了，下面只画还在的。`)
            : null;
          if (!scope.boards.length) {
            return h("div", { class: "matrix" }, [meta, missingNote, empty(rt, "配置勾选的节点都不在了", "到属性面板重新勾选，或把节点接回来")]);
          }
          const sections = scope.boards.map((board) => {
            const row0 = rowOf(board.node_id);
            const present = deps.live.slotsOf(board.node_id);
            const slots = wanted && wanted.length ? present.filter((slot) => wanted.includes(slot)) : present;
            const missing = wanted && wanted.length ? wanted.filter((slot) => !present.includes(slot)) : [];
            const shown = dataState(deps, board);
            const head = h("p", { class: "meta" }, [
              statePill(rt, shown),
              ` ${board.display_name}`,
              row0 ? ` · 模式 ${row0.mode ?? "未报"}` : " · 无帧",
              row0 && row0.loKhz !== null && row0.loKhz !== undefined
                ? h("span", { title: "整帧共用一个 LO，不是每一档的中心频率" }, ` · 帧 LO ${fmtKhzAsMhz(row0.loKhz)}`)
                : null,
            ]);
            if (shown.state !== "live" && shown.state !== "stale") {
              return h("section", { class: "matrix-board", "data-node": board.node_id }, [head, empty(rt, shown.text)]);
            }
            if (!slots.length) {
              return h("section", { class: "matrix-board", "data-node": board.node_id }, [
                head,
                empty(rt, "这一帧里没有档位", wanted && wanted.length ? `配置里的 ${wanted.length} 档都没进帧` : "帧头报的通道数是 0"),
              ]);
            }
            return h("section", { class: "matrix-board", "data-node": board.node_id }, [
              head,
              table(rt, {
                columns: ["档", "RSSI", "EMA", "SNR", "活动", "更新"],
                rows: slots.map((slot) => {
                  const point = row0.channels.get(slot);
                  const active =
                    threshold === null || threshold === undefined || point.rssi === null || point.rssi === undefined
                      ? null
                      : point.rssi > threshold;
                  return row(
                    rt,
                    [
                      String(slot + 1),
                      h("td", { class: active ? "num num--active" : "num" }, `${fmtDb(point.rssi)} dBFS`),
                      h("td", { class: "num" }, fmtDb(point.rssiEma)),
                      h("td", { class: "num" }, fmtDb(point.snr)),
                      active === null ? h("td", { class: "muted" }, "未判") : h("td", [dot(rt, active)]),
                      h("td", { class: "muted" }, fmtClock(point.at, "UTC")),
                    ],
                    {
                      class: ["clickable", linked === slot ? "is-linked" : null],
                      onClick: () => deps.actions.linkChannel(slot),
                      title: "设为当前联动频道",
                    }
                  );
                }),
              }),
              missing.length ? h("p", { class: "note" }, `配置里的 ${missing.map((slot) => slot + 1).join("、")} 档还没进帧。`) : null,
            ]);
          });
          return h("div", { class: "matrix matrix--multi" }, [meta, missingNote, ...sections]);
        }
        const board = currentBoard(deps);
        const shown = dataState(deps, board);
        const row0 = board ? deps.live.node(board.node_id) : null;
        const present = board ? deps.live.slotsOf(board.node_id) : [];
        const slots = wanted && wanted.length ? present.filter((slot) => wanted.includes(slot)) : present;
        const missing = wanted && wanted.length ? wanted.filter((slot) => !present.includes(slot)) : [];

        const meta = h("p", { class: "meta" }, [
          // 没帧时说"无数据"而不是"来源未知"：后者描述的是一份来路不明的数，而这里的真相是没有数。
          sourceBadges(rt, row0 ? [row0.source] : []),
          " ",
          statePill(rt, shown),
          ` ${board ? board.display_name : "无板"} · ${row0 ? `模式 ${row0.mode ?? "未报"}` : "无帧"}`,
          row0 && row0.loKhz !== null && row0.loKhz !== undefined
            ? h("span", { title: "整帧共用一个 LO，不是每一档的中心频率" }, ` · 帧 LO ${fmtKhzAsMhz(row0.loKhz)}`)
            : null,
          row0 ? h("span", { class: "muted" }, ` · ${deps.live.statusLine()}`) : null,
          " · 离散频道电平，不是频谱",
        ]);
        const caveat =
          row0 && !levelsDeclared(board)
            ? h("p", { class: "note note--warn" }, "这块板没声明电平帧能力，但帧确实在进：上面画的是帧里的原值，能力声明与实测不一致要以板端为准。")
            : null;

        if (shown.state !== "live" && shown.state !== "stale") {
          return h("div", { class: "matrix" }, [meta, shown.text ? empty(rt, shown.text) : null]);
        }
        if (!slots.length) {
          return h("div", { class: "matrix" }, [
            meta,
            empty(rt, "这一帧里没有档位", wanted && wanted.length ? `配置里的 ${wanted.length} 档都没进帧` : "帧头报的通道数是 0"),
          ]);
        }
        return h("div", { class: "matrix" }, [
          meta,
          caveat,
          stateNote(rt, shown),
          table(rt, {
            columns: ["档", "RSSI", "EMA", "SNR", "活动", "更新"],
            rows: slots.map((slot) => {
              const point = row0.channels.get(slot);
              const active =
                threshold === null || threshold === undefined || point.rssi === null || point.rssi === undefined
                  ? null
                  : point.rssi > threshold;
              return row(
                rt,
                [
                  String(slot + 1),
                  h("td", { class: active ? "num num--active" : "num" }, `${fmtDb(point.rssi)} dBFS`),
                  h("td", { class: "num" }, fmtDb(point.rssiEma)),
                  h("td", { class: "num" }, fmtDb(point.snr)),
                  active === null ? h("td", { class: "muted" }, "未判") : h("td", [dot(rt, active)]),
                  h("td", { class: "muted" }, fmtClock(point.at, "UTC")),
                ],
                {
                  class: ["clickable", linked === slot ? "is-linked" : null],
                  onClick: () => deps.actions.linkChannel(slot),
                  title: "设为当前联动频道",
                }
              );
            }),
          }),
          h("p", { class: "note" }, [
            "EMA/SNR 为空是这一模式确实没测（LEVELS 的 RSSI 模式不发 EMA），不是 0。",
            missing.length ? ` 配置里的 ${missing.map((slot) => slot + 1).join("、")} 档还没进帧。` : null,
            threshold === null || threshold === undefined ? " 未设阈值：活动列不判。" : null,
          ]),
        ]);
      };
    },
  };
}
