/**
 * 自研 12 列栅格。**纯函数、不碰 DOM、不引第三方网格库**（方案 §6 的自研决定）。
 *
 * 之所以单独成一个文件而不是写进组件里：`§10.1` 的"拖动、缩放不重叠、越界夹紧"这些
 * 判据，用真浏览器去点反而最难测全（边界与并列组合太多）。这里全部是可以直接
 * `node --test` 的几何函数，页面只负责把 pointer 事件换成正方形坐标进来。
 */

export const COLUMNS = 12;
export const MIN_W = 2;
export const MIN_H = 1;
/** 推挤循环的上限：超过它就是"这两个格子谁也放不下谁"，要如实报未解决而不是死循环。 */
export const MAX_PASSES = 200;

const id = (rect) => rect.instance_id;

export function clampRect(rect, { columns = COLUMNS, minW = MIN_W, minH = MIN_H } = {}) {
  const w = Math.max(minW, Math.min(Math.round(rect.w), columns));
  const h = Math.max(minH, Math.round(rect.h));
  const x = Math.min(Math.max(0, Math.round(rect.x)), columns - w);
  const y = Math.max(0, Math.round(rect.y));
  return { ...rect, x, y, w, h };
}

export function overlaps(a, b) {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

function firstOverlap(items, movedId) {
  for (let i = 0; i < items.length; i += 1) {
    for (let j = i + 1; j < items.length; j += 1) {
      if (!overlaps(items[i], items[j])) continue;
      // 被拖的那块永远不动：它是用户此刻唯一在指的东西。
      const pushed = id(items[i]) === movedId ? items[j] : items[i];
      return pushed;
    }
  }
  return null;
}

/**
 * 把 `movedId` 放到它的新位置，其余重叠者往下推。
 * 回 `{rects, unresolved}`：`unresolved` 非空就是"这组尺寸在当前列数下放不下"，
 * 页面必须说这句话，而不是画两块互相盖住的板让运营者自己猜。
 */
export function resolve(rects, movedId, { columns = COLUMNS, maxPasses = MAX_PASSES } = {}) {
  const items = rects.map((rect) => ({ ...rect }));
  const moved = items.find((rect) => id(rect) === movedId);
  if (moved) Object.assign(moved, clampRect(moved, { columns }));
  for (let pass = 0; pass < maxPasses; pass += 1) {
    const pushed = firstOverlap(items, movedId);
    if (!pushed) return { rects: items, unresolved: [] };
    const anchor = items.find((rect) => rect !== pushed && overlaps(rect, pushed));
    pushed.y = anchor.y + anchor.h;
  }
  const stuck = new Set();
  for (const a of items) {
    for (const b of items) {
      if (a !== b && overlaps(a, b)) {
        stuck.add(id(a));
        stuck.add(id(b));
      }
    }
  }
  return { rects: items, unresolved: [...stuck] };
}

/** 找一个能放下的空位：按 y、x 从上到下从左到右扫，放不下就追加到底部。 */
export function findFreeSpot(rects, size, { columns = COLUMNS } = {}) {
  const probe = { w: size.w, h: size.h };
  for (let y = 0; ; y += 1) {
    for (let x = 0; x + probe.w <= columns; x += 1) {
      const candidate = { ...probe, x, y };
      if (!rects.some((rect) => overlaps(rect, candidate))) return { x, y };
    }
    if (y > 400) return { x: 0, y: nextRow(rects) };
  }
}

export const nextRow = (rects) => rects.reduce((low, rect) => Math.max(low, rect.y + rect.h), 0);

/**
 * 像素 → 格子。`metrics` 由页面按实际容器算好后传进来（这里不认识布局，只认数字），
 * 所以同一份换算在 1280 与 390 宽度下都能被单测钉住。
 */
export function pointToCell(point, metrics) {
  const x = Math.floor((point.x - metrics.left) / metrics.cellW);
  const y = Math.floor((point.y - metrics.top) / metrics.cellH);
  return { x: Math.max(0, x), y: Math.max(0, y) };
}

/**
 * 指针落在哪块格子的哪个把手上。返回 `null` 就是"点在了空白处"（取消选中）。
 * 逆栈序扫：盖在上面的那块才是用户看见并想抓的那块。
 */
export function hitTest(rects, point, metrics, { handle = 12 } = {}) {
  const cell = pointToCell(point, metrics);
  for (let i = rects.length - 1; i >= 0; i -= 1) {
    const rect = rects[i];
    if (cell.x < rect.x || cell.x >= rect.x + rect.w) continue;
    if (cell.y < rect.y || cell.y >= rect.y + rect.h) continue;
    const right = metrics.left + (rect.x + rect.w) * metrics.cellW;
    const bottom = metrics.top + (rect.y + rect.h) * metrics.cellH;
    const mode = point.x > right - handle && point.y > bottom - handle ? "resize" : "move";
    return { instance_id: rect.instance_id, mode };
  }
  return null;
}

/** 拖拽的位移换算：按"从按下点移了几格"算，而不是按当前指针落在哪个格——后者会让大块跳动。 */
export function movedBy(rect, delta, { columns = COLUMNS } = {}) {
  return clampRect({ ...rect, x: rect.x + delta.x, y: rect.y + delta.y }, { columns });
}

/** 缩放：右下角跟到 `cell` 为止，最小尺寸与列宽上限同 `clampRect`。 */
export function resizedBy(rect, cell, { columns = COLUMNS, minW = MIN_W, minH = MIN_H } = {}) {
  return clampRect(
    { ...rect, w: cell.x - rect.x + 1, h: cell.y - rect.y + 1 },
    { columns, minW, minH }
  );
}
