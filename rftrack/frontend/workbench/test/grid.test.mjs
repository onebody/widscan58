/**
 * 栅格几何的单测。**不引浏览器**：`§10.1` 的"拖动/缩放不重叠/越界夹紧/放不下要说话"
 * 这几条判据，用真浏览器点满所有边界组合反而最难，纯函数在这里一次钉死。
 */

import assert from "node:assert/strict";
import test from "node:test";

import * as grid from "../grid.js";

const rect = (instance_id, x, y, w, h) => ({ instance_id, x, y, w, h });

test("列数与最小尺寸是声明出来的，不是页面里散落的数", () => {
  assert.equal(grid.COLUMNS, 12);
  assert.equal(grid.MIN_W, 2);
  assert.equal(grid.MIN_H, 1);
});

test("clampRect：越界夹紧，宽度上限随列数走", () => {
  assert.deepEqual(
    { ...grid.clampRect(rect("a", -3, -1, 20, 0)) },
    { instance_id: "a", x: 0, y: 0, w: 12, h: 1 }
  );
  // 10 宽时必须至少留 2 列的左边距，否则右边缘就出画布了。
  assert.equal(grid.clampRect(rect("a", 100, 0, 2, 2)).x, grid.COLUMNS - 2);
  // 非整数坐标（像素换算来的）先取整再判定，避免出现 1.5 列。
  assert.equal(grid.clampRect(rect("a", 2.6, 3.2, 4.4, 1.6)).x, 3);
});

test("overlaps：只贴边不算重叠", () => {
  assert.equal(grid.overlaps(rect("a", 0, 0, 2, 2), rect("b", 2, 0, 2, 2)), false);
  assert.equal(grid.overlaps(rect("a", 0, 0, 2, 2), rect("b", 1, 0, 2, 2)), true);
});

test("resolve：被拖的那块不动，压在它下面的往下推", () => {
  // 约定：调用方（`store.applyRect`）已经把新位置写进 `rects` 里，`resolve` 只负责收拾重叠。
  const keep = grid.resolve([rect("a", 0, 0, 4, 2), rect("b", 0, 1, 4, 2)], "a");
  assert.deepEqual(keep.unresolved, []);
  assert.deepEqual(
    keep.rects.map((row) => `${row.instance_id}@${row.x},${row.y}`),
    ["a@0,0", "b@0,2"]
  );

  const swapped = grid.resolve([rect("a", 0, 0, 4, 2), rect("b", 0, 0, 4, 2)], "b");
  assert.equal(swapped.rects.find((r) => r.instance_id === "b").y, 0);
  assert.equal(swapped.rects.find((r) => r.instance_id === "a").y, 2);
  assert.deepEqual(swapped.unresolved, []);
});

test("resolve：链式推挤能收敛且不留下重叠", () => {
  // c 被拖到第 0 行，正好压住 a；a 让下去又压住 b，于是又一轮推挤。
  const { rects, unresolved } = grid.resolve(
    [rect("c", 0, 0, 12, 1), rect("a", 0, 0, 12, 1), rect("b", 0, 1, 12, 1)],
    "c"
  );
  assert.deepEqual(unresolved, []);
  assert.equal(rects.find((r) => r.instance_id === "c").y, 0);
  const rows = new Set(rects.map((row) => row.y));
  assert.equal(rows.size, rects.length, "三块 12 列宽的格子必须各占一行");
  for (let i = 0; i < rects.length; i += 1) {
    for (let j = i + 1; j < rects.length; j += 1) {
      assert.equal(grid.overlaps(rects[i], rects[j]), false, `${rects[i].instance_id} 盖住 ${rects[j].instance_id}`);
    }
  }
});

test("resolve：推挤轮数封顶后如实报未解决，而不是循环到死", () => {
  // 正常情况下一轮就能解开；这里用 maxPasses:0 检验"没解开"这条路真的会报出来。
  const { unresolved } = grid.resolve([rect("a", 0, 0, 12, 1), rect("b", 0, 0, 12, 1)], "a", { maxPasses: 0 });
  assert.deepEqual(unresolved.sort(), ["a", "b"]);
});

test("findFreeSpot：从上到下、从左到右找第一个空位", () => {
  const taken = [rect("a", 0, 0, 3, 1), rect("b", 3, 0, 3, 1)];
  assert.deepEqual(grid.findFreeSpot(taken, { w: 3, h: 1 }), { x: 6, y: 0 });
  assert.deepEqual(grid.findFreeSpot(taken, { w: 7, h: 1 }), { x: 0, y: 1 });
  assert.deepEqual(grid.findFreeSpot([], { w: 6, h: 4 }), { x: 0, y: 0 });
});

test("nextRow：追加到底部只看真实占用的最深一行", () => {
  assert.equal(grid.nextRow([rect("a", 0, 5, 2, 3), rect("b", 4, 0, 2, 1)]), 8);
  assert.equal(grid.nextRow([]), 0);
});

test("pointToCell：像素换格子，负值夹到 0", () => {
  const metrics = { left: 100, top: 50, cellW: 96, cellH: 84 };
  assert.deepEqual(grid.pointToCell({ x: 100, y: 50 }, metrics), { x: 0, y: 0 });
  assert.deepEqual(grid.pointToCell({ x: 100 + 96 * 2 + 10, y: 50 + 84 * 3 + 10 }, metrics), { x: 2, y: 3 });
  assert.deepEqual(grid.pointToCell({ x: 0, y: 0 }, metrics), { x: 0, y: 0 });
});

test("hitTest：逆栈序——盖在上面的才是用户想抓的那块", () => {
  const metrics = { left: 0, top: 0, cellW: 100, cellH: 100 };
  const stacked = [rect("under", 0, 0, 4, 2), rect("over", 2, 0, 4, 2)];
  assert.deepEqual(grid.hitTest(stacked, { x: 250, y: 50 }, metrics), { instance_id: "over", mode: "move" });
  assert.deepEqual(grid.hitTest(stacked, { x: 50, y: 50 }, metrics), { instance_id: "under", mode: "move" });
  assert.equal(grid.hitTest(stacked, { x: 900, y: 50 }, metrics), null);
});

test("hitTest：右下角 12px 内是 resize，其余是 move", () => {
  const metrics = { left: 0, top: 0, cellW: 100, cellH: 100 };
  const rows = [rect("a", 0, 0, 3, 2)];
  assert.deepEqual(grid.hitTest(rows, { x: 296, y: 196 }, metrics), { instance_id: "a", mode: "resize" });
  assert.deepEqual(grid.hitTest(rows, { x: 100, y: 50 }, metrics), { instance_id: "a", mode: "move" });
});

test("movedBy：按位移几格算，且整体夹紧在画布内", () => {
  const base = rect("a", 2, 2, 4, 2);
  assert.deepEqual({ ...grid.movedBy(base, { x: 3, y: -1 }) }, rect("a", 5, 1, 4, 2));
  // 往右拖过头：夹到 x = columns - w，而不是让它出画布。
  assert.equal(grid.movedBy(base, { x: 50, y: 0 }).x, grid.COLUMNS - 4);
  assert.equal(grid.movedBy(base, { x: -50, y: 0 }).x, 0);
  assert.equal(grid.movedBy(base, { x: 0, y: -50 }).y, 0);
});

test("resizedBy：右下角跟到哪儿，最小尺寸不许破", () => {
  const base = rect("a", 0, 0, 4, 2);
  assert.deepEqual({ ...grid.resizedBy(base, { x: 6, y: 3 }) }, rect("a", 0, 0, 7, 4));
  assert.deepEqual({ ...grid.resizedBy(base, { x: 0, y: 0 }) }, rect("a", 0, 0, grid.MIN_W, grid.MIN_H));
  assert.deepEqual({ ...grid.resizedBy(base, { x: 0, y: 0 }, { minW: 3, minH: 2 }) }, rect("a", 0, 0, 3, 2));
  assert.equal(grid.resizedBy(base, { x: 99, y: 99 }).w, grid.COLUMNS);
});
