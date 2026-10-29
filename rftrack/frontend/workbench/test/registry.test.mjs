/**
 * 组件注册表单测。这里钉的是"**未声明即不支持**"这条纪律的数据侧：
 * 今天点不了的条目必须自带理由与阶段，页面才能原样把那句话显示出来，而不是灰掉一个按钮让人猜。
 */

import assert from "node:assert/strict";
import test from "node:test";

import * as registry from "../registry.js";

test("五个演示组件 + 两个实时组件，就是 S5 交付的全部实现", () => {
  assert.deepEqual(registry.COMPONENTS.map((entry) => entry.id), [
    "channel-matrix", "channel-detail", "rssi-trend", "signal-events", "node-status", "site-tree",
  ]);
  assert.deepEqual(
    registry.COMPONENTS.filter((entry) => entry.source === "demo").map((entry) => entry.id),
    ["channel-matrix", "channel-detail", "rssi-trend", "signal-events"]
  );
  assert.deepEqual(
    registry.COMPONENTS.filter((entry) => entry.source === "api").map((entry) => entry.id),
    ["node-status", "site-tree"]
  );
});

test("每条声明都带尺寸、下限、需要哪些能力；source 只有 demo/api 两种", () => {
  for (const entry of registry.COMPONENTS) {
    assert.ok(entry.size.w >= entry.min.w && entry.size.h >= entry.min.h, `${entry.id} 默认尺寸不许小于自身下限`);
    assert.ok(entry.min.w >= 2 && entry.min.h >= 1, `${entry.id} 的下限不许破栅格的 MIN_W`);
    assert.ok(entry.size.w <= 12, `${entry.id} 不许比整幅画布还宽`);
    assert.ok(Array.isArray(entry.needs) && entry.needs.length, `${entry.id} 必须声明依赖哪一路能力`);
    assert.ok(["demo", "api"].includes(entry.source), `${entry.id} 的来源必须可判定`);
    assert.ok(typeof entry.summary === "string" && entry.summary.length > 0);
  }
});

test("演示组件永远可点，实时组件要看能力的脸色", () => {
  const bare = registry.library({ capabilities: [] });
  const live = bare.filter((entry) => entry.kind === "live");
  assert.equal(live.find((entry) => entry.id === "channel-matrix").available, true);
  assert.equal(live.find((entry) => entry.id === "node-status").available, false);
  assert.match(live.find((entry) => entry.id === "node-status").reason, /缺少能力：nodes/);

  const full = registry.library({ capabilities: ["demo", "nodes", "hierarchy"] });
  assert.equal(full.every((entry) => entry.kind === "planned" || entry.available), true);
});

test("未上线条目必须写清理由与阶段，空着不写才是问题", () => {
  assert.ok(registry.PLANNED.length >= 4);
  for (const entry of registry.PLANNED) {
    assert.equal(entry.available, false);
    assert.ok(entry.reason.length > 0, `${entry.id} 缺理由`);
    if (entry.stage === null) {
      // 没有阶段的那一条就是"这事我们不做"，理由必须落到规范上，否则后人会当成漏掉的排期。
      assert.match(entry.reason, /规范|不做|没有/, `${entry.id} 说不上为什么永远不上`);
    } else {
      assert.match(entry.stage, /^S\d+$/);
    }
  }
  const spectrum = registry.PLANNED.find((entry) => entry.id === "spectrum-view");
  assert.match(spectrum.reason, /连续频谱/, "把离散频道当频谱是 §2.3 禁止的那件事，必须在数据里就说明白");
});

test("component() 认不到就回 null，而不是抛错——坏布局要能降级渲染", () => {
  assert.equal(registry.component("nope"), null);
  assert.ok(registry.component("channel-matrix"));
  assert.equal(registry.KNOWN_IDS.has("site-tree"), true);
  assert.equal(registry.KNOWN_IDS.has("spectrum-view"), false, "未上线的条目不算已注册的组件");
});

test("库里两半拼起来：live 在前、planned 在后，planned 也给尺寸以免页面为它写特例", () => {
  const rows = registry.library({ capabilities: ["demo", "nodes", "hierarchy"] });
  assert.equal(rows.length, registry.COMPONENTS.length + registry.PLANNED.length);
  const firstPlanned = rows.findIndex((entry) => entry.kind === "planned");
  assert.equal(firstPlanned, registry.COMPONENTS.length);
  for (const entry of rows.filter((row) => row.kind === "planned")) {
    assert.ok(entry.size && entry.size.w > 0 && entry.size.h > 0);
  }
});
