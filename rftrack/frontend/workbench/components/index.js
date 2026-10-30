/**
 * 组件实现表：`plugin_id` → 工厂。**这里的键必须与 `registry.js` 的 `COMPONENTS` 一一对**，
 * `test/components.test.mjs` 拿注册表来对账（少一个 → 页面出现"组件未注册"；多一个 → 死代码）。
 *
 * 工厂签名统一为 `create(rt, deps)`：
 * - `rt` 是 Vue 命名空间（`h`/`ref`/`onMounted`/`onUpdated`/`nextTick`）。无构建形态下 Vue 是
 *   脚本注入的全局量，模块顶层不能 `import` 它；从参数拿，这些文件才能在 Node 里被断言。
 * - `deps` 是 `{store, context, live, actions, signal, registry, api}`。层级与场地上下文读
 *   `context` 的那份状态，写后端一律走 `store`（它有只读闸门与脏标记），实时帧读 `live`
 *   （整页一条 `/ws/live`），按时间取历史一律走 `api` 的 REST 端点——组件不许自己开第二条连接。
 */

import * as registry from "../registry.js";
import { create as alertFeed } from "./alert-feed.js";
import { create as channelDetail } from "./channel-detail.js";
import { create as channelMatrix } from "./channel-matrix.js";
import { create as channelActivityHeatmap } from "./channel-activity-heatmap.js";
import { create as combinedSpectrum } from "./combined-spectrum.js";
import { create as nodeStatus } from "./node-status.js";
import { create as rssiTrend } from "./rssi-trend.js";
import { create as signalEvents } from "./signal-events.js";
import { create as siteTree } from "./site-tree.js";
import { create as targetInspector } from "./target-inspector.js";
import { create as unknownPlugin } from "./unknown-plugin.js";

export const IMPLEMENTED = {
  "channel-matrix": channelMatrix,
  "channel-detail": channelDetail,
  "combined-spectrum": combinedSpectrum,
  "rssi-trend": rssiTrend,
  "signal-events": signalEvents,
  "alert-feed": alertFeed,
  "channel-activity-heatmap": channelActivityHeatmap,
  "target-inspector": targetInspector,
  "node-status": nodeStatus,
  "site-tree": siteTree,
};

/** 注册表声明了但没有实现（对账用；正常应为空，非空就是这次发布少装了文件）。 */
export const declaredWithoutImpl = () =>
  registry.COMPONENTS.filter((entry) => !IMPLEMENTED[entry.id]).map((entry) => entry.id);

/** 有实现但注册表没声明（页面上永远点不到，属死代码）。 */
export const implWithoutDeclared = () =>
  Object.keys(IMPLEMENTED).filter((id) => !registry.KNOWN_IDS.has(id));

export function build(rt, deps) {
  // 占位件也要过一遍工厂：直接放模块命名空间的话，它没有 `setup`，
  // 而"认不出组件不能崩"（规范 §3.3）恰恰是唯一会走到这条路的场景。
  const components = { "*unknown*": unknownPlugin(rt, deps) };
  for (const [id, factory] of Object.entries(IMPLEMENTED)) components[id] = factory(rt, deps);
  return {
    components,
    /** 认不出的 `plugin_id` 一律落到未注册占位件：`registry.component()` 回 null 也一样。 */
    resolve(widget) {
      return components[widget.plugin_id] || components["*unknown*"];
    },
  };
}
