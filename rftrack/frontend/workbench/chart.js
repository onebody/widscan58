/**
 * 手绘折线。**只用 Canvas 2D，不引图表库**（方案 §6：无构建 = 不装 bundler，也不装第三方运行时）。
 *
 * 分成两半是有意的：
 * - `model()` 以上是**纯数据**——刻度、坐标、路径。`node --test` 拿一个假 ctx 记录调用序列，
 *   就能钉住"阈值线画在哪""点序有没有跟着时间走"，不必开浏览器。
 * - `draw()` 以下才碰 `ctx`。
 *
 * 刻度算法（`niceStep`）与轴内边距沿用原 `web/src/util/canvas.ts`：同一套数据在两处画出来
 * 刻度不一样，运营者会以为其中一处读错了。
 *
 * 这个图只画**离散频道的 RSSI 随时间**，不画频率轴——把频道序号当频率画连续谱是
 * 规范 §2.3 明令禁止的那件事，`spectrum-view` 在注册表里因此标着"本阶段不支持"。
 */

export const PAD = { left: 46, right: 10, top: 10, bottom: 22 };

/** 取"看起来像人写的"刻度步长：1/2/5 × 10ⁿ。 */
export function niceStep(range, target) {
  const raw = range / Math.max(1, target);
  const mag = 10 ** Math.floor(Math.log10(raw || 1));
  const n = raw / mag;
  return (n < 1.5 ? 1 : n < 3 ? 2 : n < 7 ? 5 : 10) * mag;
}

/**
 * 值域 → 像素。`y` 轴这里固定是 dBFS：它的量纲来自协议（int16/100），没有第二种单位，
 * 所以不开放成参数——开放了就等于暗示存在一套换算。
 */
export function frame(size, domain, pad = PAD) {
  const [x0, x1] = domain.x;
  const [y0, y1] = domain.y;
  const left = pad.left;
  const right = Math.max(left + 1, size.w - pad.right);
  const top = pad.top;
  const bottom = Math.max(top + 1, size.h - pad.bottom);
  const span = (value, [from, to]) => (to === from ? 0 : (value - from) / (to - from));
  return {
    left,
    right,
    top,
    bottom,
    sx: (value) => left + span(value, [x0, x1]) * (right - left),
    sy: (value) => bottom - span(value, [y0, y1]) * (bottom - top),
    ix: (px) => x0 + ((px - left) / (right - left)) * (x1 - x0),
  };
}

/**
 * 把一次采样序列折成绘图模型。`axis` 用 tick（整数节拍）而不是时间戳：
 * 演示与真实帧都是"每拍一个点"，跟着节拍走才不会因为某拍迟到而把线拉长。
 */
export function model({ samples, threshold = null, size = { w: 480, h: 180 }, ticks = 4, dBLabels = 4 }) {
  const ticksX = samples.map((sample) => sample.tick);
  const values = samples.map((sample) => sample.rssi_db);
  const x0 = ticksX.length ? Math.min(...ticksX) : 0;
  const x1 = ticksX.length ? Math.max(...ticksX) : 1;
  const lo = values.length ? Math.min(...values, threshold ?? Infinity) : -128;
  const hi = values.length ? Math.max(...values, threshold ?? -Infinity) : 0;
  // 上下各留半个步长：线贴到边框就没法判断它是"真这么高"还是"被裁了"。
  const step = Math.max(1, niceStep(Math.max(1, hi - lo), dBLabels));
  const y0 = Math.floor(lo / step) * step - step;
  const y1 = Math.ceil(hi / step) * step + step;
  const axis = frame(size, { x: [x0, x1 === x0 ? x0 + 1 : x1], y: [y0, y1] });
  const gridY = [];
  for (let value = y0; value <= y1 + 1e-9; value += step) gridY.push(value);
  return {
    axis,
    size,
    gridY,
    domain: { x: [x0, x1], y: [y0, y1] },
    points: samples.map((sample) => ({
      x: axis.sx(sample.tick),
      y: axis.sy(sample.rssi_db),
      at: sample.at,
      tick: sample.tick,
      rssi_db: sample.rssi_db,
      source: sample.source,
    })),
    threshold: threshold === null ? null : { value: threshold, y: axis.sy(threshold) },
  };
}

/** DPR 后备缓冲尺寸：CSS 像素与设备像素混用是这种图最常见的"在手机上糊成一片"。 */
export function resizeCanvas(canvas, dpr = globalThis.devicePixelRatio || 1) {
  const w = canvas.clientWidth || 1;
  const h = canvas.clientHeight || 1;
  const bw = Math.max(1, Math.round(w * dpr));
  const bh = Math.max(1, Math.round(h * dpr));
  if (canvas.width !== bw || canvas.height !== bh) {
    canvas.width = bw;
    canvas.height = bh;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx, w, h };
}

/**
 * 画一个模型。`colors` 由调用方从 CSS 变量读好后传进来（不在这里读样式：
 * 那样这个文件就得活在 DOM 里，也就没法在 Node 里被断言）。
 */
export function draw(ctx, built, colors) {
  const { axis, gridY, points, threshold } = built;
  ctx.strokeStyle = colors.grid;
  ctx.lineWidth = 1;
  ctx.font = colors.font;
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  for (const value of gridY) {
    const y = axis.sy(value);
    ctx.beginPath();
    ctx.moveTo(axis.left, y);
    ctx.lineTo(axis.right, y);
    ctx.stroke();
    ctx.fillStyle = colors.muted;
    ctx.fillText(String(value), axis.left - 6, y);
  }
  if (threshold !== null) {
    // 阈值线单独成色并虚线：它是"判据"，不是数据，不能被读成第 N 条曲线。
    ctx.strokeStyle = colors.threshold;
    ctx.setLineDash([5, 4]);
    ctx.beginPath();
    ctx.moveTo(axis.left, threshold.y);
    ctx.lineTo(axis.right, threshold.y);
    ctx.stroke();
    ctx.setLineDash([]);
  }
  ctx.strokeStyle = colors.series;
  ctx.lineWidth = 1.6;
  ctx.beginPath();
  points.forEach((point, index) => {
    if (index === 0) ctx.moveTo(point.x, point.y);
    else ctx.lineTo(point.x, point.y);
  });
  ctx.stroke();
  const last = points[points.length - 1];
  if (last) {
    ctx.fillStyle = colors.series;
    ctx.beginPath();
    ctx.arc(last.x, last.y, 2.6, 0, Math.PI * 2);
    ctx.fill();
  }
  return { points: points.length, gridLines: gridY.length };
}

/** 从样式表取色；`--s1` 等变量与原 `web/` 同名同义（同一套分类色板）。 */
export function colorsFrom(getVariable, { fallback = {}, font = "11px ui-sans-serif, system-ui" } = {}) {
  const read = (name, alt) => {
    const value = getVariable(name);
    return value && value.trim() ? value.trim() : alt;
  };
  return {
    grid: read("--grid", fallback.grid || "#333"),
    muted: read("--muted", fallback.muted || "#888"),
    series: read("--s1", fallback.series || "#3987e5"),
    threshold: read("--warning", fallback.threshold || "#c98500"),
    font,
  };
}
