/**
 * 组件注册表：**元数据，不含实现**。这样它可以在 Node 里被测，也可以在浏览器里被
 * `components/index.js` 拿去和真实组件对账（少一个 / 多一个都会被测试钉住）。
 *
 * 两条纪律直接来自规范：
 * - **能力由声明渲染，未声明即不支持**（§2.3）。库里那些"今天点不了"的条目不是装饰：
 *   它们把"为什么不能用、哪个阶段才可用"写在数据里，页面照原样显示，
 *   而不是灰掉一个按钮让人猜。
 * - `source: "demo"` 的组件必须自己带 DEMO 角标；`source: "api"` 的组件只读后端真有的东西，
 *   读不到就显示空状态，**不拿演示数据补位**。
 */

export const COMPONENTS = [
  {
    id: "channel-matrix",
    name: "多频道矩阵",
    summary: "同时看若干频道的当前 RSSI、活动态与更新时间",
    size: { w: 6, h: 4 },
    min: { w: 3, h: 2 },
    source: "demo",
    needs: ["demo"],
  },
  {
    id: "channel-detail",
    name: "单频道详情",
    summary: "看住选中的那一个频道：当前值、元信息与来源",
    size: { w: 3, h: 4 },
    min: { w: 2, h: 3 },
    source: "demo",
    needs: ["demo"],
    links: "channel",
  },
  {
    id: "rssi-trend",
    name: "RSSI 趋势",
    summary: "一段时间内的 RSSI 变化（离散频道序列，不是频谱）",
    size: { w: 6, h: 4 },
    min: { w: 3, h: 3 },
    source: "demo",
    needs: ["demo"],
    links: "channel",
  },
  {
    id: "signal-events",
    name: "事件信息流",
    summary: "活动、异常、节点状态与告警事件（本阶段为演示事件）",
    size: { w: 6, h: 5 },
    min: { w: 3, h: 3 },
    source: "demo",
    needs: ["demo"],
    links: "channel",
  },
  {
    id: "node-status",
    name: "节点状态",
    summary: "接入机与节点的真实连接与能力声明",
    size: { w: 4, h: 4 },
    min: { w: 3, h: 3 },
    source: "api",
    needs: ["nodes"],
    links: "node",
  },
  {
    id: "site-tree",
    name: "场地层级树",
    summary: "场地 → 位置 → 接入机 → 节点，选中即切视图（不停后台）",
    size: { w: 3, h: 6 },
    min: { w: 2, h: 3 },
    source: "api",
    needs: ["hierarchy"],
  },
];

/**
 * 声明出来但**本阶段不可用**的条目。理由与阶段写在数据里，页面上直接呈现。
 * 空着不写才是问题：运营者会以为"这功能坏了"，而不是"这功能还没到"。
 */
export const PLANNED = [
  {
    id: "spectrum-view",
    name: "真实频谱",
    summary: "需要真实频率分辨数据",
    available: false,
    reason: "本阶段没有连续频谱数据源；离散频道 RSSI 不会画成频谱（规范 §2.3）",
    stage: "S6",
  },
  {
    id: "waterfall-view",
    name: "真实瀑布图",
    summary: "需要频率—时间—幅度三轴真实采样",
    available: false,
    reason: "同上，瀑布图依赖 RAW IQ 通路，S6 接线后才谈启用",
    stage: "S6",
  },
  {
    id: "channel-activity-heatmap",
    name: "频道活动热图",
    summary: "频道 × 时间的活动强度",
    available: false,
    reason: "取数走 /api/samples/summary（摘要桶），该表属 S6",
    stage: "S6",
  },
  {
    id: "target-inspector",
    name: "目标详情",
    summary: "候选目标的证据、时间线与人工复核",
    available: false,
    reason: "事件与候选目标表属 S6/S8，今天没有任何真实目标可展示",
    stage: "S8",
  },
  {
    id: "node-map",
    name: "节点地图",
    summary: "仅在位置数据可靠时启用",
    available: false,
    reason: "系统里没有可靠位置来源，不做按 RSSI 猜坐标那一件事（规范 §2.3）",
    stage: null,
  },
];

export const KNOWN_IDS = new Set(COMPONENTS.map((entry) => entry.id));

export function component(id) {
  return COMPONENTS.find((entry) => entry.id === id) || null;
}

/** 库里每一行的可点性与理由：`facts.capabilities` 是节点能力集合（今天来自 /api/nodes）。 */
export function library(facts = { capabilities: [] }) {
  const ready = new Set(facts.capabilities || []);
  const live = COMPONENTS.map((entry) => {
    const missing = entry.needs.filter((need) => need !== "demo" && !ready.has(need));
    return {
      ...entry,
      kind: "live",
      available: missing.length === 0,
      reason: missing.length ? `缺少能力：${missing.join("、")}` : null,
    };
  });
  return live.concat(PLANNED.map((entry) => ({ ...entry, kind: "planned", size: { w: 4, h: 3 } })));
}
