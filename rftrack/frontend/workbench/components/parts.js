/**
 * 组件共用的渲染小件与格式化。
 *
 * 这里的函数都接 `rt`（Vue 命名空间）作第一个参数而不是自己 `import`：工作台是
 * **无构建**形态，Vue 由 `index.html` 以脚本方式注入，模块顶层引它等于把文件钉死在
 * 浏览器里。带上 `rt` 之后，这些组件可以在 Node 里用一个假 `h` 断言出树形与文案——
 * "MOCK 角标在不在""四态说什么""空状态说什么"这类判据不该只有开浏览器才能验。
 */

export const fmtDb = (value) => (value === null || value === undefined ? "—" : value.toFixed(1));
/** 频率一律 MHz：与 `RH.READ_FREQUENCY` 的 u16 同量纲，页面上不出现第二种写法。 */
export const fmtMhz = (mhz) => (mhz === null || mhz === undefined ? "—" : `${mhz} MHz`);
/**
 * 帧里的 LO/带宽是 kHz 与 Hz（`loKhz` 来自固件的 u32，`obw*Hz` 是解析器按 kHz×1000 换算的）。
 * 页面统一用 MHz 讲频率，但**保留协议原词在 title 里**：对不上板端日志时得有人能核原文。
 */
export const fmtKhzAsMhz = (khz) =>
  khz === null || khz === undefined ? "—" : `${(khz / 1000).toFixed(1)} MHz（${khz} kHz）`;
/** 带符号的频率偏移：`+123 Hz` / `−45 Hz`。偏移不是频率，所以不换成 MHz。 */
export const fmtHz = (hz) =>
  hz === null || hz === undefined ? "—" : `${hz > 0 ? "+" : hz < 0 ? "−" : ""}${Math.abs(hz)} Hz`;
export const fmtMs = (ms) => (ms >= 1000 ? `${(ms / 1000).toFixed(ms % 1000 ? 1 : 0)} s` : `${ms} ms`);

/** 只到秒：状态栏与列表里的时间精度到秒就够，多出来的位数在窄屏上是挤掉区分位的元凶。 */
export function fmtClock(value, tz = "UTC") {
  if (!value) return "—";
  const date = new Date(typeof value === "number" ? value : Date.parse(value));
  if (Number.isNaN(date.getTime())) return "—";
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
      timeZone: tz,
    }).format(date);
  } catch {
    return date.toISOString().slice(11, 19);
  }
}

/**
 * 数据来源角标。**这套词是 S6 出口判据里"MOCK 与真实可区分"的可见形式**，三句话各有对象：
 *
 * - `real`：采集层从板子的字节流里解出来的帧。
 * - `mock`：回放 runner 从录制文件里喂进来的帧——`source` 由写进录制的那一层决定，
 *   所以"这是录的"这件事一路跟着数据，而不是靠页面上一个开关。
 * - `api`：REST 端点读来的当前事实（`node-status`、`site-tree`）。
 *
 * S5 的第四枚 `demo` 随 `demo.js` 一起删了：这一页不再自己造数，也就没有需要标出来的演示数。
 * 认不出的来源回"来源未知"而不是回"实时"：把没见过的东西显示成可信的，是这一层最贵的假话。
 */
export function sourceBadge(rt, source) {
  const { h } = rt;
  if (source === "mock") {
    return h("span", { class: "badge badge--mock", title: "回放/模拟帧：来自录制文件，不是此刻的实时观测" }, "MOCK");
  }
  if (source === "real") {
    return h("span", { class: "badge badge--real", title: "采集层从这块板的字节流里解出来的帧" }, "真实");
  }
  if (source === "api") {
    return h("span", { class: "badge badge--api" }, "实时");
  }
  return h("span", { class: "badge badge--unknown", title: `来源字段是 ${source === null || source === undefined ? "空" : source}` }, "来源未知");
}

/** 一块格子里同时出现过几种来源时，每一枚都要画出来——"混合"本身就是要看得见的事实。 */
export function sourceBadges(rt, sources) {
  const { h } = rt;
  const list = [...new Set(sources || [])];
  // "没有来源可标"与"来源认不出来"是两件事，都印成"无数据"的话，四态牌子（它按规范就该说
  // 无数据/过期/断线/unsupported）会在同一格里被复读一遍，看着像坏了的复读机。
  if (!list.length) return h("span", { class: "badge badge--none", title: "这一格此刻没有帧，所以没有来源可标" }, "未取数");
  return h("span", { class: "badges" }, list.map((source) => sourceBadge(rt, source)));
}

/** 四态的那枚小牌子：短标签由 `select.js` 判出来，整句进 `title`——正文那一句由 `empty()` 说。 */
export function statePill(rt, shown) {
  const { h } = rt;
  if (!shown || !shown.label) return null;
  return h("span", { class: ["pill", `pill--${shown.tone}`, `pill--${shown.state}`], title: shown.text }, shown.label);
}

/**
 * 数还在画、但状态已经不是"新鲜"的那一句。
 *
 * `stale` 是四态里唯一"照常画数"的一态：正文走的是表而不是 `empty()`，所以判据那句
 * （距今几秒 / 判据几秒）没有别的落点。牌子上的短标签只说"过期"，不足以让人判断这是
 * 板子安静了还是页面读得慢——那正是该由页面自己回答的问题，所以它得在正文里出现一次。
 */
export function stateNote(rt, shown) {
  const { h } = rt;
  return shown && shown.state === "stale" ? h("p", { class: "note note--stale" }, shown.text) : null;
}

/** 空状态要说的是"为什么是空的"，不是画一条横线装成有数据（规范 §2.3）。 */
export function empty(rt, text, hint = null) {
  const { h } = rt;
  return h("p", { class: "empty" }, [text, hint ? h("span", { class: "empty-hint" }, `（${hint}）`) : null].filter(Boolean));
}

export function row(rt, cells, extra = {}) {
  const { h } = rt;
  return h("tr", extra, cells.map((cell) => (typeof cell === "string" || typeof cell === "number" ? h("td", String(cell)) : cell)));
}

/** 活动/静默的那一个点：颜色由 CSS 类给，组件只负责说出"超过阈值没有"。 */
export function dot(rt, active) {
  const { h } = rt;
  return h("span", { class: ["dot", active ? "dot--active" : "dot--idle"], "aria-label": active ? "活动" : "静默" });
}

export function table(rt, { columns, rows }) {
  const { h } = rt;
  return h("table", { class: "grid-table" }, [
    h("thead", [h("tr", columns.map((label) => h("th", label)))]),
    h("tbody", rows),
  ]);
}

/**
 * "这一档还没有执行者"之类的声明注脚。用在两个地方：属性面板里 `pending_stage` 的键、
 * 组件库里的未上线条目。写成一处的理由是措辞必须一致——同一件事在两个面板上说法不同，
 * 运营者会怀疑其中一处是随便写的。
 */
export function stageNote(rt, stage) {
  const { h } = rt;
  return h("span", { class: "note note--stage" }, `取数/执行者在 ${stage}`);
}
