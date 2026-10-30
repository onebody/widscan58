/**
 * 综合频谱（分线对比）：把多个节点（或一个节点的多个档位）拉到**同一张图**上，
 * 一条线一个节点。数据来自 `/ws/live` 的那一条连接（`live.js` 合并好的最后一帧），
 * 不另开取数口——推送里本来就带全场地的帧。
 *
 * 三条口径：
 *
 * - **横轴是档位号，不是频率。** 每条线的点落在"这一档"上，档与档之间不插值、不连线成
 *   连续谱——把离散频道画成频率轴是规范 §2.3 禁止的那件事。图下有"离散频道电平，
 *   不是连续频谱"的常驻标注，注册表里 `spectrum-view` 的"需要真实频率分辨数据"也还在。
 * - **一条线一个节点，颜色顺序即勾选顺序**（`chart.js` 的 `seriesList`）。所有线共用
 *   同一套档位横轴：分线要回答的是"同一档上谁高谁低"。`slots` 配置在这里同样是**筛选**：
 *   只能缩小横轴，不能凭空加档。
 * - **节点没帧就不画线，但要说出来。** 一个节点安静时它的线整个缺席——缺席的原因
 *   （没帧/断线/没声明电平能力）写在节点名旁边，不许让"线不见了"被读成"电平为 0"。
 */

import { colorsFrom, draw, model, resizeCanvas } from "../chart.js";
import { fmtClock, sourceBadges, statePill, empty } from "./parts.js";
import { dataState, linkedSlot, nodeScope } from "./select.js";

export const plugin_id = "combined-spectrum";

export function create(rt, deps) {
  const { h, ref } = rt;

  return {
    name: "CombinedSpectrum",
    props: { widget: { type: Object, required: true } },
    setup(props) {
      const beat = ref(0);
      const canvas = ref(null);
      const widget = props.widget;
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
          schedule();
        }, ms);
      }
      schedule();

      return () => {
        beat.value; // 依赖：每个节拍重读一次生效值与实时数
        deps.signal.tick.value; // 依赖：选中/联动/保存之后跟着重算
        const threshold = deps.store.effectiveOf(widget, "threshold_db");
        const wanted = deps.store.effectiveOf(widget, "slots");
        const linked = linkedSlot(deps);
        const scope = nodeScope(deps, widget);

        if (!scope.boards.length) {
          const shown = { label: "没选板", text: "配置勾选的节点都不在了", tone: "muted" };
          return h("div", { class: "combined" }, [
            h("p", { class: "meta" }, [" · 离散频道电平，不是连续频谱"]),
            empty(rt, scope.missing.length ? `配置勾选的节点 #${scope.missing.join("、#")} 已经不在场地里了` : "这块场地还没有节点", "到属性面板重新勾选，或把节点接回来"),
          ]);
        }

        // 每个节点一条线：这一拍里有帧的档才有点。点带 at/source，画完末点标记后
        // 元信息上的来源角标与"最新时刻"都从这些点来。
        const series = [];
        const newest = { at: null };
        const slotSet = new Set();
        const quiet = [];
        for (const board of scope.boards) {
          const row0 = deps.live.node(board.node_id);
          const present = deps.live.slotsOf(board.node_id);
          const slots = wanted && wanted.length ? present.filter((slot) => wanted.includes(slot)) : present;
          if (!row0 || !slots.length) {
            quiet.push({ board, reason: row0 ? "这一拍里没有帧" : "还没有帧" });
            continue;
          }
          series.push({
            label: board.display_name,
            samples: slots.map((slot) => {
              const point = row0.channels.get(slot);
              slotSet.add(slot);
              if (point.at && (!newest.at || point.at > newest.at)) newest.at = point.at;
              return { tick: slot, rssi_db: point.rssi, at: point.at, source: row0.source };
            }),
          });
        }
        const sources = [...new Set(series.flatMap((group) => group.samples.map((sample) => sample.source)))];
        const xLabels = [...slotSet].sort((a, b) => a - b).map((slot) => ({ tick: slot, text: String(slot + 1) }));
        pending = { series, threshold, xLabels };
        rt.nextTick(paint);

        const names = series.map((group) => group.label);
        const meta = h("p", { class: "meta" }, [
          sources.length ? sourceBadges(rt, sources) : null,
          " ",
          statePill(rt, dataState(deps, scope.boards[0])),
          scope.multi
            ? ` ${scope.boards.length} 个节点分线${names.length ? `：${names.join("、")}` : ""}`
            : ` ${scope.boards[0].display_name}`,
          newest.at ? h("span", { class: "muted" }, ` · 最新 ${fmtClock(newest.at, "UTC")}`) : null,
          ` · ${deps.live.statusLine()}`,
          " · 离散频道电平，不是连续频谱",
        ]);
        const missingNote = scope.missing.length
          ? h("p", { class: "note note--warn" }, `配置勾选的节点 #${scope.missing.join("、#")} 已经不在场地里了，下面只画还在的。`)
          : null;
        const quietNote = quiet.length
          ? h("p", { class: "note" }, `没画出来的线：${quiet.map((item) => `${item.board.display_name}（${item.reason}）`).join("、")}。`)
          : null;
        const linkedNote =
          linked === null
            ? null
            : h("p", { class: "note" }, `联动第 ${linked + 1} 档：点这一档的行（多频道矩阵/热图）会让趋势类组件跟着换档。`);

        if (!series.length) {
          return h("div", { class: "combined" }, [
            meta,
            missingNote,
            empty(rt, wanted && wanted.length ? "配置筛选的档位都没进帧" : "还没有任何节点的帧", quiet.length ? "原因见下面那行" : "等第一帧进来"),
            quietNote,
          ]);
        }
        return h("div", { class: "combined" }, [
          meta,
          missingNote,
          h("canvas", { ref: canvas, class: "chart", "aria-label": `综合电平分线（${names.join("、")}）` }),
          quietNote,
          linkedNote,
          h("p", { class: "note" }, [
            "横轴：档位号（离散频道，档间不插值）；纵轴：该档瞬时 dBFS。虚线是阈值，不是数据。",
            " 颜色顺序即勾选顺序。",
            wanted && wanted.length ? ` 横轴已按配置筛到 ${wanted.length} 档。` : null,
          ]),
        ]);
      };
    },
  };
}
