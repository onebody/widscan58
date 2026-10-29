/**
 * 组件共用的渲染小件与格式化。
 *
 * 这里的函数都接 `rt`（Vue 命名空间）作第一个参数而不是自己 `import`：工作台是
 * **无构建**形态，Vue 由 `index.html` 以脚本方式注入，模块顶层引它等于把文件钉死在
 * 浏览器里。带上 `rt` 之后，这些组件可以在 Node 里用一个假 `h` 断言出树形与文案——
 * "DEMO 角标在不在""空状态说什么"这类判据不该只有开浏览器才能验。
 */

export const fmtDb = (value) => (value === null || value === undefined ? "—" : value.toFixed(1));
/** 频率一律 MHz：与 `RH.READ_FREQUENCY` 的 u16 同量纲，页面上不出现第二种写法。 */
export const fmtMhz = (mhz) => (mhz === null || mhz === undefined ? "—" : `${mhz} MHz`);
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

export function sourceBadge(rt, source) {
  const { h } = rt;
  if (source === "demo") {
    return h("span", { class: "badge badge--demo", title: "演示数据：后端这个阶段的采样通路还没接" }, "DEMO");
  }
  if (source === "api") {
    return h("span", { class: "badge badge--api" }, "实时");
  }
  return h("span", { class: "badge badge--unknown" }, "来源未知");
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
