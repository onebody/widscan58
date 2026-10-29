/**
 * 组件注册表：**元数据，不含实现**。这样它可以在 Node 里被测，也可以在浏览器里被
 * `components/index.js` 拿去和真实组件对账（少一个 / 多一个都会被测试钉住）。
 *
 * 三条纪律直接来自规范：
 * - **能力由声明渲染，未声明即不支持**（§2.3）。库里那些"今天点不了"的条目不是装饰：
 *   它们把"为什么不能用、哪个阶段才可用"写在数据里，页面照原样显示，
 *   而不是灰掉一个按钮让人猜。
 * - `source` 到 S6 只剩两路：`"live"`（`/ws/live` 推来的帧）与 `"api"`（REST 读来的当前事实）。
 *   **S5 那一路 `demo` 已经整份删掉**——每一格读不到真数据就显示空状态，不拿演示数补位。
 *   "这是录的"这件事由数据自己带着（回放帧的 `source` 是 `mock`），角标在 `parts.js`。
 * - `needs` 写的是**这一格要哪一路数据源才说得出话**，由 `context.capabilities()` 那份"今天
 *   真答过话的通路"来判可用：`live` 指"整页那条 `/ws/live` 有场地可订"，`nodes` 指
 *   `GET /api/nodes` 回过，`hierarchy` 指四级层级回过。它判的是**通路**，不是"有没有数据"——
 *   通路在但没有板子，格子照样进 `无数据`，那一句话由 `components/select.js` 说。
 */

export const COMPONENTS = [
  {
    id: "channel-matrix",
    name: "多频道矩阵",
    summary: "当前这块板每一档的电平、EMA、SNR、活动态与更新时间",
    size: { w: 6, h: 4 },
    min: { w: 3, h: 2 },
    source: "live",
    needs: ["live"],
  },
  {
    id: "channel-detail",
    name: "单频道详情",
    summary: "联动那一档在帧里报出来的全部量：电平、偏移、带宽与饱和",
    size: { w: 3, h: 4 },
    min: { w: 2, h: 3 },
    source: "live",
    needs: ["live"],
    links: "channel",
  },
  {
    id: "rssi-trend",
    name: "RSSI 趋势",
    summary: "一段时间内的 RSSI 变化（离散频道序列，不是频谱）",
    size: { w: 6, h: 4 },
    min: { w: 3, h: 3 },
    source: "api",
    // 曲线读的是 `GET /api/samples/summary`（摘要桶），那是 REST 而不是推送；
    // 但"没有板子就没有 node_id"这条依赖跟四态判据同源，所以 `live` 也在需求里。
    needs: ["live"],
    links: "channel",
  },
  {
    id: "signal-events",
    name: "事件信息流",
    summary: "采集层写的观测事件：断流与恢复，含证据链与局限说明",
    size: { w: 6, h: 5 },
    min: { w: 3, h: 3 },
    source: "live",
    needs: ["live"],
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
    reason:
      "S6 的推送口只发 samples（电平帧）与 event 两类；频谱帧在采集层计入 skipped_frames 后整帧丢弃（services/capture.py:573），页面这一侧没有那条通路。离散频道的 RSSI 不会冒充连续频谱（规范 §2.3）",
    stage: null,
  },
  {
    id: "waterfall-view",
    name: "真实瀑布图",
    summary: "需要频率—时间—幅度三轴真实采样",
    available: false,
    reason: "同上：瀑布图的三轴要 RAW IQ 或频谱帧，而 `/ws/live` 里没有这两类帧；本系统的原始 IQ 通路在原 `web/` 与 Android 端，不在这条链上",
    stage: null,
  },
  {
    id: "channel-activity-heatmap",
    name: "频道活动热图",
    summary: "频道 × 时间的活动强度",
    available: false,
    reason: "取数口 `GET /api/samples/summary` 已在 S6 落地，缺的是按频道×时间闭合的活动判据（阈值来自 S8 的规则层），§7 把这格分给 S8",
    stage: "S8",
  },
  {
    id: "target-inspector",
    name: "目标详情",
    summary: "候选目标的证据、时间线与人工复核",
    available: false,
    reason: "事件表已在 S6 落地（`signal_event`），候选目标与人工复核那张表属 S8，今天没有任何真实目标可展示",
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

/**
 * `source` 这个词怎么说给人听。词表放这里是因为 **`source` 的取值只有注册表知道**，
 * 页面拿它渲染时不该再自己拆一遍三元——漏掉一个取值就会把 REST 那格说成"实时推送"。
 * 认不出的来源原样吐出来：那是这次发布少了一个分支的证据，不该被显示成一个已知的词。
 */
const SOURCE_WORD = { live: "实时推送", api: "REST 拉取" };

export const sourceWord = (source) => SOURCE_WORD[source] || `未知来源 ${source}`;

export function component(id) {
  return COMPONENTS.find((entry) => entry.id === id) || null;
}

/** 库里每一行的可点性与理由：`facts.capabilities` 是节点能力集合（今天来自 /api/nodes）。 */
export function library(facts = { capabilities: [] }) {
  const ready = new Set(facts.capabilities || []);
  const live = COMPONENTS.map((entry) => {
    const missing = entry.needs.filter((need) => !ready.has(need));
    return {
      ...entry,
      kind: "live",
      available: missing.length === 0,
      reason: missing.length ? `缺少能力：${missing.join("、")}` : null,
    };
  });
  return live.concat(PLANNED.map((entry) => ({ ...entry, kind: "planned", size: { w: 4, h: 3 } })));
}
