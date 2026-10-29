#!/usr/bin/env node
/**
 * 工作台浏览器冒烟（S5-10）：规范 §10.1 那五条判据 + 方案 §7 的 S5 出口条件，
 * 在**真 Chrome、真 HTTP、真指针事件**下走一遍。
 *
 * 为什么这一轮不能只靠 `node --test`：那 109 条断的是纯函数与状态机。它们看不见
 * "拖拽把手在真实 pointer 事件下抓不抓得住""一份太新的布局会不会把整页带崩""CDN 全断时
 * 页面还起不起得来"。这三件事只有在浏览器里点一次才知道。
 *
 * 自包含，且只碰临时目录：
 *   夹具库 → 自己调 `rftrack/tools/export_workbench_fixtures.py`（每次重建，默认 /tmp/rftrack-e2e；
 *            回放那一段另导一份到 `<目录>-live`，因为阶段 A 一路保存/删格子，那份库已经不是我
 *            写下的样子了）
 *   服务   → 空闲端口起 uvicorn，`RFTRACK_DIALER_ENABLED=0`（不去拨真机器）；回放那一段再起一台，
 *            只多两个 `RFTRACK_REPLAY_*` 环境变量，喂的就是导出脚本写下的那两份录制
 *   浏览器 → 自己起 headless Chrome，独立 `--user-data-dir`
 *   收尾   → `finally` 杀掉这些由本脚本启动的子进程；截图与 results.json 留在 --out-dir
 * 仓库里的 `backend/data/rftrack.sqlite3`、原有页面、真实接入机都不在这条路径上。
 *
 * 两处诚实声明（阶段报告里也要这么写）：
 *   1. 下拉（场地/位置/接入机/板/布局）用原生 `change` 事件驱动——headless 里原生弹层打不开，
 *      而页面挂的就是 `onChange`，走的仍是同一个处理函数。按钮、拖拽、缩放、输入框是真事件。
 *   2. 判据读的是 DOM 与 `window.rftrackWorkbench`（诊断入口做在产品里就是给这一轮用的），
 *      不比像素。截图只作为人眼复核的材料。
 *
 * 用法：
 *   node rftrack/frontend/e2e/workbench.mjs
 *   node rftrack/frontend/e2e/workbench.mjs --keep-fixture   # 复用上一次导出的夹具库
 *   node rftrack/frontend/e2e/workbench.mjs --fixtures-dir DIR --out-dir DIR
 */

import { spawn } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
// 页面自己那把视图记忆的键名：从它的模块里取，不在这里抄一份——抄错了"清干净"会清不到东西，
// 于是首屏读到上一轮的场地，判据红得没有道理。
import { VIEW_MEMORY_KEY } from "../workbench/context.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const RFTRACK = resolve(HERE, "..", "..");
const BACKEND = join(RFTRACK, "backend");
const WORKBENCH = join(RFTRACK, "frontend", "workbench");
const FIXTURE_SCRIPT = join(RFTRACK, "tools", "export_workbench_fixtures.py");

const CHROME_CANDIDATES = [
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/Applications/Chromium.app/Contents/MacOS/Chromium",
  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
];

/** 与 `app.js` 的 `CELL_H` 同一个数：这里只用来把像素位移换算成"移了几格"。 */
const CELL_H = 84;

// --- 参数 -----------------------------------------------------------------------

function parseArgs(argv) {
  const out = { fixturesDir: "/tmp/rftrack-e2e", outDir: "/tmp/rftrack-workbench-e2e", keepFixture: false };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--keep-fixture") out.keepFixture = true;
    else if (arg === "--fixtures-dir") out.fixturesDir = argv[++i];
    else if (arg === "--out-dir") out.outDir = argv[++i];
    else {
      console.error(`不认识这个参数：${arg}（可用：--keep-fixture --fixtures-dir --out-dir）`);
      process.exit(2);
    }
  }
  return out;
}

const options = parseArgs(process.argv.slice(2));
const SHOTS = join(options.outDir, "shots");
const PROFILE = join(options.outDir, "chrome-profile");

// --- 记账 -----------------------------------------------------------------------

const results = [];
const notes = [];
let currentSection = "起步";

function section(name) {
  currentSection = name;
  console.log(`\n=== ${name} ===`);
}

/** 判据失败不该把后面全部带走：后面的步骤还要提供它们自己的证据。 */
async function check(name, fn) {
  try {
    const evidence = await fn();
    results.push({ section: currentSection, name, ok: true, evidence: evidence === undefined ? "" : String(evidence) });
    console.log(`  ✓ ${name}${evidence === undefined ? "" : ` — ${evidence}`}`);
  } catch (error) {
    results.push({ section: currentSection, name, ok: false, evidence: String(error && error.message ? error.message : error) });
    console.log(`  ✗ ${name} — ${error && error.message ? error.message : error}`);
  }
}

function expect(condition, message) {
  if (!condition) throw new Error(message);
}

const sleep = (ms) => new Promise((ok) => setTimeout(ok, ms));

async function waitFor(label, fn, { timeoutMs = 15000, everyMs = 120 } = {}) {
  const until = Date.now() + timeoutMs;
  for (;;) {
    const value = await fn();
    if (value) return value;
    if (Date.now() > until) throw new Error(`等不到 ${label}（${timeoutMs} ms 超时）${await describeWaitFailure()}`);
    await sleep(everyMs);
  }
}

function freePort() {
  return new Promise((ok, no) => {
    const probe = createServer();
    probe.once("error", no);
    probe.listen(0, "127.0.0.1", () => {
      const port = probe.address().port;
      probe.close(() => ok(port));
    });
  });
}

// --- 后端进程 -------------------------------------------------------------------

/**
 * 起真后端而不是 TestClient：这一轮要测的就是页面打到真实 origin 上会发生什么
 * （静态挂载、`?site=` 的形状、同源相对路径）。`-c` 里只做"读环境变量 → create_app → 跑"，
 * 不往仓库里塞一个假的 ASGI 入口文件。
 *
 * `extraEnv` 是给回放那一段的：同一份代码、同一套夹具，只多两个环境变量就变成
 * "一台真在喂数据的实例"。少起一台进程就多一层"是不是环境不一样"的怀疑。
 */
const SERVER_CODE = `
import os, sys
sys.path.insert(0, os.environ["RFTRACK_BACKEND_DIR"])
import uvicorn
from app.config import Settings
from app.main import create_app
uvicorn.run(create_app(Settings.from_env()), host="127.0.0.1",
            port=int(os.environ["RFTRACK_PORT"]), log_level="warning")
`;

async function startServer(dbPath, extraEnv = {}) {
  const port = await freePort();
  const child = spawn("python3", ["-c", SERVER_CODE], {
    cwd: BACKEND,
    env: {
      ...process.env,
      RFTRACK_BACKEND_DIR: BACKEND,
      RFTRACK_DB_PATH: dbPath,
      RFTRACK_WORKBENCH_DIR: WORKBENCH,
      RFTRACK_PORT: String(port),
      RFTRACK_DIALER_ENABLED: "0",
      PYTHONUNBUFFERED: "1",
      ...extraEnv,
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  const log = [];
  child.stdout.on("data", (chunk) => log.push(String(chunk)));
  child.stderr.on("data", (chunk) => log.push(String(chunk)));
  child.on("exit", (code) => {
    if (code !== 0 && code !== null) notes.push(`后端进程退出码 ${code}：\n${log.join("").slice(-2000)}`);
  });
  const origin = `http://127.0.0.1:${port}`;
  await waitFor("后端 /api/health 答话", async () => {
    try {
      const response = await fetch(`${origin}/api/health`);
      return response.ok ? (await response.json()) : null;
    } catch {
      return null;
    }
  }, { timeoutMs: 30000 });
  return { origin, port, child, log, stop: () => child.kill("SIGTERM") };
}

/**
 * 导出夹具：`--out-dir` 指哪儿建哪儿。回放那一段要**另导一份**自己的库——
 * 阶段 A 一路保存、删格子、另存为，那份库到这一段已经不是导出脚本刚写完的样子了，
 * 而这一段的判据按 `w-detail`、`w-matrix` 这些实例号找格子。
 */
function exportFixtures(dir) {
  console.log(`重建夹具库：${dir}`);
  return new Promise((ok, no) => {
    const child = spawn("python3", [FIXTURE_SCRIPT, "--out-dir", dir], { stdio: "inherit" });
    child.on("exit", (code) => (code === 0 ? ok() : no(new Error(`夹具导出失败，退出码 ${code}`))));
  });
}

/**
 * 主实例的健康端点。收尾还要再问一次，所以这里必须接住"进程已经不在了"：
 * 判据要说的是"哪一台没答话"，而不是一个来自 fetch 的裸异常。
 */
async function healthOfLive() {
  try {
    const response = await fetch(`${server.origin}/api/health`);
    return response.ok ? await response.json() : null;
  } catch {
    return null;
  }
}

// --- CDP ------------------------------------------------------------------------

class Cdp {
  constructor(socket) {
    this.socket = socket;
    this.nextId = 1;
    this.pending = new Map();
    this.handlers = [];
    socket.addEventListener("message", (event) => this.receive(event.data));
  }

  static async connect(url) {
    const socket = new WebSocket(url);
    await new Promise((ok, no) => {
      socket.addEventListener("open", ok, { once: true });
      socket.addEventListener("error", () => no(new Error(`连不上 CDP：${url}`)), { once: true });
    });
    return new Cdp(socket);
  }

  receive(raw) {
    const message = JSON.parse(raw);
    if (message.id !== undefined) {
      const waiter = this.pending.get(message.id);
      if (!waiter) return;
      this.pending.delete(message.id);
      if (message.error) waiter.reject(new Error(`${waiter.method}: ${message.error.message}`));
      else waiter.resolve(message.result || {});
      return;
    }
    this.handlers.forEach((fn) => fn(message.method, message.params || {}));
  }

  send(method, params = {}) {
    const id = this.nextId++;
    this.socket.send(JSON.stringify({ id, method, params }));
    // key 名要和上面 `receive()` 读的字段一致：这里叫 `ok/no` 而那里读 `resolve/reject`，
    // 第一条应答就会把整个进程炸掉。
    return new Promise((resolve, reject) => this.pending.set(id, { resolve, reject, method }));
  }

  on(fn) {
    this.handlers.push(fn);
  }

  off(fn) {
    this.handlers = this.handlers.filter((item) => item !== fn);
  }

  close() {
    try {
      this.socket.close();
    } catch {
      // 已经关了：这一路只负责收尾，不该把退出码带脏。
    }
  }
}

let cdp = null;

// 把 Node 侧写的函数序列化进页面执行；参数一律 JSON 字面量化。
async function inPage(fn, ...args) {
  const source = `(${fn.toString()})(${args.map((arg) => JSON.stringify(arg)).join(", ")})`;
  const response = await cdp.send("Runtime.evaluate", {
    expression: `(async () => await ${source})()`,
    awaitPromise: true,
    returnByValue: true,
  });
  if (response.exceptionDetails) {
    const detail = response.exceptionDetails;
    throw new Error(detail.exception?.description || detail.text || "页面里这段抛了");
  }
  return response.result.value;
}

// --- 真事件 ---------------------------------------------------------------------

async function mouseEvent(type, x, y, buttons) {
  await cdp.send("Input.dispatchMouseEvent", {
    type,
    x,
    y,
    button: type === "mouseMoved" ? "none" : "left",
    clickCount: type === "mouseMoved" ? 0 : 1,
    buttons,
  });
}

async function clickAt(x, y) {
  await mouseEvent("mouseMoved", x, y, 0);
  await mouseEvent("mousePressed", x, y, 1);
  await mouseEvent("mouseReleased", x, y, 0);
}

async function dragTo(from, to, { steps = 10 } = {}) {
  await mouseEvent("mouseMoved", from.x, from.y, 0);
  await mouseEvent("mousePressed", from.x, from.y, 1);
  for (let i = 1; i <= steps; i += 1) {
    await mouseEvent(
      "mouseMoved",
      from.x + ((to.x - from.x) * i) / steps,
      from.y + ((to.y - from.y) * i) / steps,
      1
    );
  }
  await mouseEvent("mouseReleased", to.x, to.y, 0);
}

/** 元素中心点：先滚进视野再取，否则算出来的 y 在滚动容器外面，点下去打不中。 */
const centerOf = (selector) =>
  inPage((sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    el.scrollIntoView({ block: "center", inline: "center" });
    const rect = el.getBoundingClientRect();
    return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
  }, selector);

/**
 * 算一个**画布盒子外面、但仍在窗口里面**的拖拽落点（拿右下角把手往右下拉出去用）。
 *
 * 为什么要专门算这个点：`moveDrag` 挂在 window 上这件事，只有把指针拖出 `.workspace` 盒子
 * 才测得到——落在盒子里时画布自己的监听也能收到，判据就成了自证。
 * 而落点又不能出窗口：Chrome 对窗口外的坐标**根本不派发 `mouseMoved`**
 * （实测 10 段 move 只送到前 2 段），那种"没反应"不是页面的错。
 * 横向刻意留在盒子**里面**：越过右边框就等于落到第 13 列，`clampRect` 会把原点一起往左带
 * （那是栅格自己的既定行为，已由单测钉住），这条判据只想证"拖出去还在跟着放大"，不想把
 * 夹紧的副作用混进来。所以落点 = 往下取"max(一行, 盒子底边 + 10)"，横向取两列。
 */
async function targetOutsideCanvas(selector, { cellH = CELL_H } = {}) {
  const spot = await inPage((sel, rowH) => {
    const el = document.querySelector(sel);
    if (!el) return { ok: false, why: `找不到 ${sel}` };
    el.scrollIntoView({ block: "center", inline: "center" });
    const ws = document.querySelector(".workspace");
    const box = ws.getBoundingClientRect();
    const handle = el.getBoundingClientRect();
    const inner = { w: window.innerWidth, h: window.innerHeight };
    const cellW = parseFloat(ws.style.getPropertyValue("--cell")) || 0;
    // 落点取整：CDP 的 `Input.dispatchMouseEvent` 把 x/y 当整数，浏览器收到的一定是整像素。
    // 期望行列必须用**同一个整数**换算，否则会差在"789.6 到底算第 9 行还是第 10 行"这种小数上
    // ——那种红既不是页面的错，也不是夹具的错，只是两边各自取了一次整。
    const from = { x: Math.round(handle.x + handle.width / 2), y: Math.round(handle.y + handle.height / 2) };
    const boxBottom = box.top + ws.clientHeight;
    const to = {
      x: Math.round(from.x + cellW * 2),
      y: Math.round(Math.min(Math.max(from.y + rowH, boxBottom + 10), inner.h - 10)),
    };
    const problems = [];
    if (from.y < 0 || from.y > inner.h) problems.push(`把手不在窗口里（${Math.round(from.x)},${Math.round(from.y)}，窗口高 ${inner.h}）`);
    if (to.x >= inner.w || to.x > box.right) problems.push(`横向落点 ${Math.round(to.x)} 出了窗口或画布右边（${inner.w} / ${Math.round(box.right)}）：那会顺带触发越界夹紧，把原点往左带`);
    if (to.y >= inner.h) problems.push(`纵向落点 ${Math.round(to.y)} 出窗口（高 ${inner.h}）`);
    if (to.y <= boxBottom) problems.push(`纵向落点 ${Math.round(to.y)} 没越过画布盒子底边（${Math.round(boxBottom)}）：那这条判据就没测到"拖出画布"那一档`);
    // `expectCell()`：把落点像素换算成"页面上应该在第几列/第几行"，用的就是 `grid.pointToCell()`
    // 那一条式子（`metrics.top = rect.top - scrollTop`）。画布一旦内部滚动，可视底边对应的内容行
    // 就比"把手往下拖一行"远得多——判据必须按换算来，不能按一个写死的"+1 行"来。
    const origin = { x: box.left - ws.scrollLeft, y: box.top - ws.scrollTop, cellW, cellH: rowH };
    const expectCell = (px, py) => ({
      x: Math.floor((px - origin.x) / cellW),
      y: Math.floor((py - origin.y) / rowH),
    });
    return {
      ok: problems.length === 0,
      why: problems.join("；"),
      from,
      to,
      cellW,
      expect: expectCell(to.x, to.y),
      scroll: { top: ws.scrollTop, left: ws.scrollLeft, clientH: ws.clientHeight },
      origin,
      box: { right: box.right, bottom: boxBottom, top: box.top, left: box.left },
      inner,
    };
  }, selector, cellH);
  expect(spot.ok, `摆不出"拖出画布盒子但仍在窗口内"的落点：${spot.why}；这条判据要的就是"指针离开画布之后尺寸还在跟着走"，摆不出来就不能算通过`);
  return spot;
}

async function clickSelector(selector, { timeoutMs = 4000 } = {}) {
  const point = await waitFor(`能点的 ${selector}`, () => centerOf(selector), { timeoutMs });
  await clickAt(point.x, point.y);
  return point;
}

/** 按标签文本点按钮（`保存`/`另存为` 这些）：页面没有 data-testid，标题文本就是钩子。 */
async function clickButton(label) {
  const found = await inPage((want) => {
    const hit = [...document.querySelectorAll("button.btn")].find((b) => b.textContent.trim().startsWith(want));
    if (!hit) return null;
    hit.scrollIntoView({ block: "center" });
    const rect = hit.getBoundingClientRect();
    return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2, disabled: hit.disabled, text: hit.textContent.trim() };
  }, label);
  expect(found, `找不到「${label}」按钮`);
  expect(!found.disabled, `「${label}」按钮是灰的（文本：${found.text}）`);
  await clickAt(found.x, found.y);
  return found;
}

/**
 * 只读工具栏里那个"保存"。必须按文本单独取：面板的两个页签按钮也是 `btn--primary`，
 * 用 `.btn--primary` 找灰不灰会数到页签上去（那是恒亮的，判据就废了）。
 */
const saveButton = () =>
  inPage(() => {
    const el = [...document.querySelectorAll(".toolbar button.btn")].find((b) => b.textContent.trim().startsWith("保存"));
    if (!el) return null;
    return { text: el.textContent.trim(), disabled: el.disabled, badge: (el.querySelector(".btn-badge") || {}).textContent || null };
  });

/**
 * 层级下拉按**标签文本**取，不用 `.picker:nth-child(n)`：
 * `.picker-empty` 与 `.picker` 是兄弟节点，列表一空出来那一路就没有 `select` 了，
 * 序号会当场错位（首屏"没选场地"那一步正是这样，跑出来像页面没挂载）。
 */
async function selectByLabel(label, optionLabel) {
  const item = await inPage(
    (want, opt) => {
      const el = [...document.querySelectorAll(".toolbar .picker")]
        .find((row) => (row.querySelector(".picker-label") || {}).textContent?.trim() === want)
        ?.querySelector("select");
      if (!el) return { ok: false, why: `工具栏里没有「${want}」这一路下拉（页面没挂载，或它那一路渲染成了空状态）` };
      const option = [...el.options].find((o) => o.textContent.includes(opt));
      if (!option) return { ok: false, why: `「${want}」里没有含「${opt}」的选项`, options: [...el.options].map((o) => o.textContent) };
      el.value = option.value;
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return { ok: true, value: option.value, text: option.textContent.trim() };
    },
    label,
    optionLabel
  );
  expect(item.ok, item.why + (item.options ? `；现有：${item.options.join(" / ")}` : ""));
  return item;
}

/** 只读某一路下拉当前的选中值（不派发事件）。 */
const pickerValue = (label) =>
  inPage((want) => {
    const el = [...document.querySelectorAll(".toolbar .picker")]
      .find((row) => (row.querySelector(".picker-label") || {}).textContent?.trim() === want)
      ?.querySelector("select");
    return el ? el.value : null;
  }, label);

/** 布局下拉不在层级 picker 那四路里，按 CSS 选择器取。 */
async function selectBySelector(selector, optionLabel) {
  const applied = await inPage((sel, want) => {
    const el = document.querySelector(sel);
    if (!el) return { ok: false, why: `没有 ${sel}` };
    const option = [...el.options].find((o) => o.textContent.includes(want));
    if (!option) return { ok: false, why: `${sel} 里没有含「${want}」的选项`, options: [...el.options].map((o) => o.textContent) };
    el.value = option.value;
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return { ok: true, value: option.value, text: option.textContent.trim() };
  }, selector, optionLabel);
  expect(applied.ok, applied.why + (applied.options ? `；现有：${applied.options.join(" / ")}` : ""));
  return applied;
}

/** 状态栏取某一段的值；缺这段时要说"缺"，不该由 undefined.value 冒名报错。 */
const statusValue = (parts, label) => {
  const row = parts.find((item) => item.label === label);
  if (!row) throw new Error(`状态栏里没有「${label}」这一段（现有：${parts.map((i) => i.label).join("/") || "空的"}）`);
  return row.value;
};

const DIGIT_KEYS = { "-": { code: "Minus", vk: 189 }, ".": { code: "Period", vk: 190 } };

async function pressChar(ch) {
  const known = DIGIT_KEYS[ch] || { code: `Digit${ch}`, vk: 48 + Number(ch) };
  const base = { key: ch, code: known.code, windowsVirtualKeyCode: known.vk, nativeVirtualKeyCode: known.vk };
  await cdp.send("Input.dispatchKeyEvent", { ...base, type: "keyDown", text: ch });
  await cdp.send("Input.dispatchKeyEvent", { ...base, type: "keyUp" });
}

/**
 * 真键盘往数字框里打字，再用"点到别处"提交（`onChange` 在 blur 时才发）。
 * 打字后立刻读回 `input.value`：没打进去就当场报错，免得后面断出一条假结论。
 */
async function typeIntoNumber(selector, text) {
  await clickSelector(selector);
  await inPage((sel) => {
    const el = document.querySelector(sel);
    if (el && el.select) el.select();
  }, selector);
  for (const ch of text) await pressChar(ch);
  const typed = await inPage((sel) => {
    const el = document.querySelector(sel);
    return el ? el.value : null;
  }, selector);
  expect(typed === text, `数字框里打进去的是「${typed}」，不是「${text}」——这条判据不能靠假打字算过`);
  await clickSelector(".masthead h1");
  await inPage(() => new Promise((ok) => setTimeout(ok, 60)));
}

/**
 * 中文名这类非 ASCII 文本：逐键发不出字符，用 CDP 的 `insertText`（等价于输入法提交）。
 * 先全选：布局名框是会留上次的内容的（`layoutName` 只在"新建"成功后才清空），
 * 不清就变成"冒烟副本冒烟改名"这种串起来的字符串，后面每条按名字找的判据都失真。
 */
async function fillText(selector, text) {
  await clickSelector(selector);
  await inPage((sel) => {
    const el = document.querySelector(sel);
    if (el) el.select();
  }, selector);
  await cdp.send("Input.insertText", { text });
  const value = await inPage((sel) => {
    const el = document.querySelector(sel);
    return el ? el.value : null;
  }, selector);
  expect(value === text, `输入框里是「${value}」，不是「${text}」`);
}

// --- 页面事实的取法 ---------------------------------------------------------------

const snapshot = () =>
  inPage(() => {
    const wb = window.rftrackWorkbench;
    const s = wb.store.state;
    const ctx = wb.context.state;
    return {
      siteId: ctx.siteId,
      siteName: (wb.context.site() || {}).name || null,
      relayName: (wb.context.relay() || {}).name || null,
      gatewayName: (wb.context.gateway() || {}).name || null,
      nodeName: (wb.context.node() || {}).display_name || null,
      nodeIdType: typeof ctx.nodeId,
      layout: s.layout
        ? {
            id: s.layout.layout_id, name: s.layout.name, active: !!s.layout.is_active,
            degraded: !!s.layout.degraded, version: s.layout.version,
            defaults: s.layout.defaults === undefined ? "missing" : s.layout.defaults,
            declarationError: s.layout.declaration_error || null, widgets: s.layout.widget_count,
          }
        : null,
      layouts: s.layouts.map((row) => ({ id: row.layout_id, name: row.name, active: !!row.is_active, degraded: !!row.degraded, widgets: row.widget_count })),
      selection: s.selection,
      draft: [...s.draft],
      pendingDelete: [...s.removed.keys()],
      widgets: s.widgets.map((row) => ({
        id: row.instance_id, plugin: row.plugin_id, x: row.x, y: row.y, w: row.w, h: row.h,
        isNew: !!row.isNew, missing: !!row.missing, config: { ...row.config },
        threshold: row.effective && row.effective.threshold_db ? row.effective.threshold_db.value : null,
      })),
      errors: s.errors.map((row) => `${row.reason}｜${row.message || ""}`),
      savedAt: s.savedAt,
      audit: wb.store.audit(),
      readOnly: wb.store.readOnlyReason(),
      // 页面没挂载时这里要回 null，不能把整条 snapshot 炸成 getComputedStyle 的类型错误：
      // "有没有画"该由专门的等待/判据说，不该由一条取列宽的语句冒名。
      cellW: (() => {
        const ws = document.querySelector(".workspace");
        return ws ? Number(getComputedStyle(ws).getPropertyValue("--cell").replace("px", "") || 0) : null;
      })(),
    };
  });

/**
 * 状态栏取的是 `.status-part` 里的 `.status-label`/`.status-value` 两个 span，
 * 不依赖 `[data-label]`——那正是本轮抓出来的那类坑（属性没渲染上时，按属性取的断言
 * 只会得到"缺这一段"，看不出是页面没画还是钩子没挂）。`[data-statusbar]` 单独断一次。
 */
const statusFromDom = () =>
  inPage(() => {
    const footer = document.querySelector("footer.status[data-statusbar]");
    return [...(footer ? footer.querySelectorAll(".status-part") : [])].map((el) => ({
      label: (el.querySelector(".status-label") || {}).textContent?.trim() || "",
      value: (el.querySelector(".status-value") || {}).textContent?.trim() || el.textContent.trim(),
      visible: el.offsetWidth > 0 && el.offsetHeight > 0,
    }));
  });

/**
 * 页面那份实时窗的原样取证：判据要问的是"`live.js` 合并完了什么"，不是 DOM 上的形容词。
 *
 * 两边都读：`status()` 是连接层的账（来源、帧数、未知帧、跨场地行、退避第几次），
 * `nodes` 是每块板的最后一帧。这一轮的数全程只有回放这一个来路，所以这两个都必须能断。
 */
const liveProbe = () => inPage(() => {
  const live = window.rftrackWorkbench.live;
  return {
    status: live.status(),
    nodes: [...live.state.nodes.values()].map((row) => ({
      node_id: row.node_id,
      name: row.display_name,
      source: row.source,
      ep: row.ep,
      frames: row.frames,
      seq: row.seq,
      mode: row.mode,
      lo_khz: row.loKhz,
      age_s: live.ageS(row),
      slots: [...row.channels.keys()].sort((a, b) => a - b),
      channels: [...row.channels.entries()].map(([slot, point]) => ({ slot, rssi: point.rssi })),
    })),
    feed: live.state.feed.map((row) => ({
      id: row.event_id,
      kind: row.kind,
      node: row.node_id,
      source: row.source,
      slot: row.slot,
      ep: row.payload && row.payload.ep,
      silence_s: row.payload && row.payload.silence_s,
    })),
  };
});

/** 逐格的 DOM 事实：牌子、来源角标、`.meta` 与正文，一次取回来供多条判据分着断。 */
const probeWidgets = () => inPage(() => {
  const STATES = ["no_board", "live", "stale", "offline", "unsupported", "empty"];
  return [...document.querySelectorAll(".widget")].map((el) => {
    const pills = [...el.querySelectorAll(".widget-body .pill")];
    return {
      instance: el.getAttribute("data-instance"),
      plugin: el.getAttribute("data-plugin"),
      meta: (el.querySelector(".meta") || {}).textContent?.replace(/\s+/g, " ").trim() || null,
      body: (el.querySelector(".widget-body") || {}).textContent?.replace(/\s+/g, " ").trim() || null,
      mock: el.querySelectorAll(".badge--mock").length,
      real: el.querySelectorAll(".badge--real").length,
      staleNote: Boolean(el.querySelector(".note--stale")),
      linked: el.querySelectorAll("tr.is-linked").length,
      canvas: el.querySelectorAll("canvas").length,
      rows: el.querySelectorAll("tbody tr").length,
      offlineRow: Boolean(el.querySelector(".event--node_offline")),
      offlineMock: el.querySelectorAll(".event--node_offline .badge--mock").length,
      states: pills.map((pill) => [...pill.classList].map((cls) => cls.slice(6)).find((cls) => STATES.includes(cls)) || null),
    };
  });
});

const textOf = (selector) => inPage((sel) => {
  const el = document.querySelector(sel);
  return el ? el.textContent.trim() : null;
}, selector);

const countOf = (selector) => inPage((sel) => document.querySelectorAll(sel).length, selector);

// 按属性取，不用 `el.dataset`：属性没渲染时这里回 null，比 undefined 拼成空串好认。
const widgetIds = () => inPage(() => [...document.querySelectorAll(".widget")].map((el) => el.getAttribute("data-instance")));

const noticeText = () => inPage(() => {
  const el = document.querySelector("[data-notice]");
  return el ? el.textContent.trim() : null;
});

/** 栅格的判据用 DOM 的像素复算一遍，不只看状态对象。回真的重叠对，供断言直接引用。 */
/**
 * 两块格子不许压在彼此头上。失败时把每一块的像素矩形原样打出来——
 * "栅格没推开"和"我量错了"这两种情况得分得开，否则这条判据只会变成一个看不懂的布尔。
 */
async function expectNoOverlap(label) {
  const dump = await inPage(() => {
    const rows = [...document.querySelectorAll(".widget")].map((el) => ({
      id: el.getAttribute("data-instance"),
      left: parseFloat(el.style.left),
      top: parseFloat(el.style.top),
      width: parseFloat(el.style.width),
      height: parseFloat(el.style.height),
    }));
    const hits = [];
    for (let i = 0; i < rows.length; i += 1) {
      for (let j = i + 1; j < rows.length; j += 1) {
        const a = rows[i];
        const b = rows[j];
        if (a.left < b.left + b.width && b.left < a.left + a.width
          && a.top < b.top + b.height && b.top < a.top + a.height) hits.push(`${a.id}×${b.id}`);
      }
    }
    return { hits, rects: rows.map((row) => `${row.id}@${row.left},${row.top} ${row.width}×${row.height}`) };
  });
  expect(!dump.hits.length, `${label}：${dump.hits.join("、")}｜DOM 矩形 ${dump.rects.join(" ")}`);
  return dump;
}

/**
 * 页面里那份调用记录是**有界**的（`AUDIT_LIMIT=200`，满了丢最旧），所以不能用数组下标当增量起点：
 * 一轮冒烟早越过 200 条了，之后 `slice(mark)` 恒为空——"没发出写请求"会假绿，"至少发了 GET"会假红。
 * 这里统一换成绝对序号 `dropped + 下标`，它只增不减，取增量才与记录被截掉多少无关。
 */
async function apiCallsSince(mark) {
  return inPage((from) => {
    const { calls, dropped } = window.rftrackWorkbench.api.audit();
    return calls
      .map((row, index) => ({ ...row, seq: dropped + index }))
      .filter((row) => row.seq >= from)
      .map((row) => `${row.method} ${row.path} → ${row.status}`);
  }, mark);
}

const callMark = () => inPage(() => {
  const { calls, dropped } = window.rftrackWorkbench.api.audit();
  return dropped + calls.length;
});

/** 等"我期望那一次真的发了"：拿调用记录当完成信号，比等 `loading` 那种反向判据可靠。 */
async function waitForApi(mark, pattern, label, { timeoutMs = 20000 } = {}) {
  const calls = await waitFor(label, async () => {
    const rows = await apiCallsSince(mark);
    return rows.some((row) => pattern.test(row)) ? rows : null;
  }, { timeoutMs });
  return calls;
}

/**
 * 卡住时页面手里有什么：光说"等不到 X"分不清是页面没做、还是判据等错了东西。
 * 这几个字段就是页面上那几个状态分支的输入，出问题时它们必须一起进失败消息。
 */
const evidence = () => inPage(() => {
  const wb = window.rftrackWorkbench;
  const { calls, dropped } = wb.api.audit();
  return {
    site: wb.context.state.siteId,
    layout: wb.store.state.layout
      ? `${wb.store.state.layout.name}(id ${wb.store.state.layout.layout_id}${wb.store.state.layout.degraded ? " 降级" : ""})`
      : null,
    storeError: wb.store.state.error ? `${wb.store.state.error.reason}｜${wb.store.state.error.message}` : null,
    contextError: wb.context.state.error ? `${wb.context.state.error.reason}｜${wb.context.state.error.message}` : null,
    errors: wb.store.state.errors.map((row) => `${row.reason}｜${row.message || ""}`),
    readOnly: wb.store.readOnlyReason(),
    widgets: wb.store.state.widgets.map((row) => `${row.instance_id}@${row.x},${row.y} ${row.w}×${row.h}${row.isNew ? " 新" : ""}`).join(" "),
    recent: calls.slice(-8).map((row, index) => `${dropped + calls.length - Math.min(8, calls.length) + index} ${row.method} ${row.path} → ${row.status}`),
  };
});

const describeEvidence = async () => {
  const ev = await evidence();
  return `现场：场地=${ev.site} 布局=${ev.layout} store.error=${ev.storeError} context.error=${ev.contextError} `
    + `errors=[${ev.errors.join(" / ")}] 只读=${ev.readOnly || "否"} 格子=${ev.widgets || "空的"}｜最近调用 ${ev.recent.join(" ; ")}`;
};

/** 页面还没挂载时（起步那几条判据）取不到诊断入口：那就只报超时，别让取证本身炸掉消息。 */
async function describeWaitFailure() {
  try {
    return `｜${await describeEvidence()}`;
  } catch {
    return "";
  }
}

/**
 * 切场地，等到"这一场地的布局列表真从服务端取回来了"才继续。
 *
 * 为什么不等 `store.state.loading`：`actions.selectSite` 先 await 层级那三路 GET，之后才进
 * `loadLayouts`。在那段窗口里 `loading` 还是 false，拿它当"加载完"会在页面仍站在上一个场地时
 * 就放行——于是"A 的当前布局"被读成"B 的当前布局"，判据红得毫无道理（实测就这么红过一次）。
 * 调用记录里那条 `GET /api/layouts` 才是"这一步真的做了"的正面证据。
 */
async function switchSite(siteName) {
  const mark = await callMark();
  await selectByLabel("场地", siteName);
  await waitForApi(mark, /^GET \/api\/layouts → 200$/, `切到 ${siteName}：重新取这一场地的布局列表`);
  await waitFor(
    `${siteName} 的布局读完`,
    () => inPage((want) => {
      const wb = window.rftrackWorkbench;
      return (wb.context.site() || {}).name === want && !wb.store.state.loading;
    }, siteName),
    { timeoutMs: 20000 }
  );
  return mark;
}

// --- 浏览器 ---------------------------------------------------------------------

async function startChrome(cdpPort) {
  const binary = CHROME_CANDIDATES.find((path) => existsSync(path));
  expect(binary, `这台机器上找不到 Chrome/Chromium/Edge（试了 ${CHROME_CANDIDATES.join("、")}）`);
  rmSync(PROFILE, { recursive: true, force: true });
  mkdirSync(PROFILE, { recursive: true });
  const child = spawn(binary, [
    "--headless=new",
    `--remote-debugging-port=${cdpPort}`,
    "--remote-allow-origins=*",
    `--user-data-dir=${PROFILE}`,
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--hide-scrollbars",
    "--window-size=1440,900",
    "about:blank",
  ], { stdio: ["ignore", "pipe", "pipe"] });
  const stderr = [];
  child.stderr.on("data", (chunk) => stderr.push(String(chunk)));
  child.on("exit", (code) => {
    if (code !== 0 && code !== null) notes.push(`浏览器退出码 ${code}：\n${stderr.join("").slice(-1200)}`);
  });

  const target = await waitFor("headless Chrome 的页面靶子", async () => {
    try {
      const list = await (await fetch(`http://127.0.0.1:${cdpPort}/json/list`)).json();
      return list.find((row) => row.type === "page" && row.webSocketDebuggerUrl) || null;
    } catch {
      return null;
    }
  }, { timeoutMs: 20000 });

  const client = await Cdp.connect(target.webSocketDebuggerUrl);
  const consoleErrors = [];
  const exceptions = [];
  client.on((method, params) => {
    if (method === "Runtime.consoleAPICalled" && params.type === "error") {
      consoleErrors.push((params.args || []).map((arg) => arg.value ?? arg.description ?? arg.type).join(" "));
    }
    if (method === "Runtime.exceptionThrown") {
      const detail = params.exceptionDetails || {};
      exceptions.push(detail.exception?.description || detail.text || "未知异常");
    }
    if (method === "Log.entryAdded" && params.entry?.level === "error") {
      consoleErrors.push(`[${params.entry.source}] ${params.entry.text}`);
    }
  });
  await client.send("Runtime.enable");
  await client.send("Log.enable");
  await client.send("Page.enable");
  return { child, client, consoleErrors, exceptions, stop: () => child.kill("SIGTERM") };
}

async function setViewport(width, height, scaleFactor = 1) {
  await cdp.send("Emulation.clearDeviceMetricsOverride");
  await cdp.send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: scaleFactor, mobile: width < 500 });
}

async function openWorkbench(url) {
  await setViewport(1440, 900);
  await cdp.send("Page.navigate", { url });
  // 导航会把上面那次 override 冲掉（实测），所以设完再导航回来这一遍是必须的。
  await setViewport(1440, 900);
  await waitFor("工作台挂载完（window.rftrackWorkbench + .shell）", () =>
    inPage(() => Boolean(window.rftrackWorkbench && document.querySelector(".shell"))), { timeoutMs: 45000 });
  // "挂载了"和"层级渲染出来了"是两件事，分开等才能知道慢/断在哪一步：
  // 上一轮就是这里静默过去、下一条判据才撞出 `.toolbar` 不存在。
  await waitFor("工具栏的四路层级标签", () => inPage(() => document.querySelectorAll(".toolbar .picker").length === 4), { timeoutMs: 20000 });
  // 四路标签在首帧就有：`rows` 空时那一路渲染的是 `.picker-empty`，`.picker` 外壳照样是四个。
  // 所以"看到了工具栏"根本不代表 boot 跑完。而 `store.state.loading` 初值也是 false，
  // 等"不在 loading"同样会放行——于是起步那几条判据读到 0 个下拉、0 次调用（第四轮就是这么红的）。
  // 正面信号只有一个：上下文要么拿到了场地，要么如实报了错。
  await waitFor("首屏这次 boot 有了结果（场地列表或错误至少占一样）", () => inPage(() => {
    const c = window.rftrackWorkbench.context.state;
    return c.sites.length > 0 || c.error !== null;
  }), { timeoutMs: 30000 });
  await waitFor("布局加载（不在 loading）", () => inPage(() => !window.rftrackWorkbench.store.state.loading));
}

/** 起步那一步失败时把现场打出来：白屏和"挂在别的元素上"这两种情况得分得开。 */
const bootScene = () =>
  inPage(() => ({
    boot: (document.getElementById("boot") || {}).textContent?.trim().slice(0, 200) || null,
    bootError: document.documentElement.dataset.bootError || null,
    shell: Boolean(document.querySelector(".shell")),
    pickers: document.querySelectorAll(".toolbar .picker select").length,
    widgets: document.querySelectorAll(".widget").length,
  }));

/**
 * 抹掉视图记忆，装作第一位打开这个页面的操作员。
 *
 * 页面会把"上一屏看的是哪一块板"存进 localStorage（规范 §10.1 第三条要的刷新恢复），
 * 而 headless 的 `--user-data-dir` 跨轮复用——不清它，首屏那条"不许猜场地"读到的是
 * 上一轮留下的场地，红得没有道理；反过来，清完这一条才真的在测"第一次打开"。
 */
async function forgetView() {
  const gone = await inPage((key) => {
    localStorage.removeItem(key);
    return localStorage.getItem(key) === null;
  }, VIEW_MEMORY_KEY);
  expect(gone, `清不掉 localStorage 里的 ${VIEW_MEMORY_KEY}：首屏那条判据会读到上一轮的场地`);
}

async function reload() {
  await setViewport(1440, 900);
  await cdp.send("Page.reload", { ignoreCache: true });
  await setViewport(1440, 900);
  await waitFor("刷新后又挂载完", () => inPage(() => Boolean(window.rftrackWorkbench && document.querySelector(".shell"))), { timeoutMs: 40000 });
  await waitFor("工具栏的四路层级标签", () => inPage(() => document.querySelectorAll(".toolbar .picker").length === 4), { timeoutMs: 20000 });
  // `loading` 初值是 false，`load()` 起来才置 true——只等"不在 loading"会在这一帧空档里放行，
  // 于是"刷新回来"那批判据读到的是 0 块格子，看起来像保存没入库。等到有结果为止。
  await waitFor("刷新后这一轮加载有了结果（布局、错误或格子至少占一样）", () => inPage(() => {
    const s = window.rftrackWorkbench.store.state;
    return s.layout !== null || s.error !== null || s.widgets.length > 0;
  }), { timeoutMs: 25000 });
  await waitFor("布局加载（不在 loading）", () => inPage(() => !window.rftrackWorkbench.store.state.loading));
}

async function shot(name) {
  const image = await cdp.send("Page.captureScreenshot", { format: "png" });
  const path = join(SHOTS, `${name}.png`);
  writeFileSync(path, Buffer.from(image.data, "base64"));
  return path;
}

const byId = (widgets, id) => widgets.find((row) => row.id === id);

// --- 主流程 ---------------------------------------------------------------------

let server = null;
/** 回放那一段的第二实例：判据跑到一半也可能崩，收尾必须连它一起收掉。 */
let server2 = null;
let browser = null;

async function run() {
  mkdirSync(options.outDir, { recursive: true });
  mkdirSync(SHOTS, { recursive: true });

  const dbPath = join(options.fixturesDir, "workbench-fixtures.sqlite3");
  if (!options.keepFixture || !existsSync(dbPath)) await exportFixtures(options.fixturesDir);
  else console.log(`复用夹具库：${dbPath}`);
  const manifest = JSON.parse(readFileSync(join(options.fixturesDir, "manifest.json"), "utf8"));
  const [siteA, siteB] = manifest.sites;
  const unknownPlugin = manifest.degraded.unknown_plugin;

  server = await startServer(dbPath);
  console.log(`后端：${server.origin}（库 ${dbPath}，拨号循环关）`);

  const cdpPort = await freePort();
  browser = await startChrome(cdpPort);
  cdp = browser.client;

  const url = `${server.origin}/workbench/`;

  // ---------------------------------------------------------------- 起步：未选场地
  section("起步：两个场地的实例第一眼不该猜一个场地");
  await openWorkbench(url);
  await forgetView();
  await openWorkbench(url);

  await check("Vue 起来了，且没走 boot 失败分支", async () => {
    const info = await inPage(() => ({
      version: (globalThis.Vue || {}).version || null,
      bootError: document.documentElement.dataset.bootError || null,
      source: performance.getEntriesByType("resource").map((row) => row.name).filter((name) => name.includes("vue")).map((name) => new URL(name).host),
    }));
    expect(info.bootError === null, `boot 失败了：${info.bootError}`);
    expect(info.version, "globalThis.Vue 不在，页面用的不是那个三来源的加载器");
    const scene = await bootScene();
    expect(scene.shell, `引擎在但页面没画出来：${JSON.stringify(scene)}`);
    return `Vue ${info.version}，来源 ${info.source.join(" / ")}；${scene.pickers} 个层级下拉 / ${scene.widgets} 块格子`;
  });

  await check("两个场地且没配当前场地时不猜：状态栏说「未选」，画布说「还没选场地」", async () => {
    const snap = await snapshot();
    const canvas = await textOf(".workspace .state");
    expect(snap.siteId === null, `首屏自己选了场地 ${snap.siteId}，而规范说不许猜`);
    expect(/还没选场地/.test(canvas || ""), `画布说的是：${canvas}`);
    return `画布：${canvas}`;
  });

  await check("只读地停下来：起步这一段一个写请求都没发", async () => {
    const calls = await apiCallsSince(0);
    expect(calls.length > 0, "起步一次后端都没问");
    expect(calls.every((row) => row.startsWith("GET")), `起步阶段出现了写请求：${calls.join(", ")}`);
    return `${calls.length} 次调用全是 GET`;
  });

  // ---------------------------------------------------------------- 场地 A
  section(`选进${siteA.name}：五格渲染，认不出的那一格不许把页面带崩`);
  await selectByLabel("场地", siteA.name);
  await waitFor("五块格子", () => inPage(() => document.querySelectorAll(".widget").length >= 5));

  await check("格子帧上的 data-* 钩子真在 DOM 上（Vue 的 h() 不吃 `dataset:{…}`）", async () => {
    const hooks = await inPage(() =>
      [...document.querySelectorAll(".widget")].map((el) => ({
        instance: el.getAttribute("data-instance"),
        plugin: el.getAttribute("data-plugin"),
        source: el.getAttribute("data-source"),
      }))
    );
    expect(hooks.every((row) => row.instance && row.plugin && row.source), `这些属性没落到 DOM 上：${JSON.stringify(hooks)}`);
    expect(await countOf("footer.status[data-statusbar] .status-part") > 0, "状态栏容器或分段没渲染出来");
    return `${hooks.length} 块格子的 data-instance/data-plugin/data-source 齐全`;
  });

  await check("未注册组件保住位置：徽标说未注册、数据源说 unknown、别的格子照常", async () => {
    const ids = await widgetIds();
    const frame = await inPage((instance) => {
      const el = document.querySelector(`.widget[data-instance="${instance}"]`);
      if (!el) return null;
      return {
        plugin: el.dataset.plugin,
        source: el.dataset.source,
        badge: (el.querySelector(".badge--missing") || {}).textContent || null,
        title: (el.querySelector(".widget-title") || {}).textContent || null,
      };
    }, unknownPlugin.instance_id);
    expect(frame, `${unknownPlugin.instance_id} 这一格没画出来（现有：${ids.join(", ")}）`);
    expect(frame.badge === "未注册", `徽标是「${frame.badge}」`);
    expect(frame.source === "unknown", `数据源标的是 ${frame.source}，认不出的组件不许冒充实时`);
    expect(ids.length === 5, `格子数 ${ids.length}，未注册那一格不该被丢掉也不该拖累别人`);
    return `${frame.plugin} 仍在原位，标题回显库里那个 id：${frame.title}`;
  });

  await check("S6 起没有 DEMO 角标：头部那枚说的是通路，组件库把没上线的连同原因一起说", async () => {
    const demo = await countOf(".badge--demo");
    const heads = await inPage(() => [...document.querySelectorAll(".widget")].map((w) => ({
      instance: w.dataset.instance,
      plugin: w.dataset.plugin,
      missing: w.classList.contains("widget--missing"),
      badge: (w.querySelector(".widget-head .badge") || {}).textContent?.trim() || null,
    })));
    const registered = heads.filter((row) => !row.missing);
    expect(demo === 0, `页面上还有 ${demo} 枚 DEMO 角标：这四块格子现在读的是 /ws/live 与 REST，不再自己造数`);
    expect(registered.length === 4, `已注册的格子该是 4 块（第 5 块是未注册占位），读到 ${registered.length}：${JSON.stringify(heads)}`);
    expect(registered.every((row) => /^(实时推送|REST 拉取)$/.test(row.badge || "")),
      `这些格子的头部角标没把自己的通路说出来：${JSON.stringify(registered)}`);
    expect(!heads.some((row) => row.badge === "来源未知"),
      "头部角标把注册表里写着的通路说成了「来源未知」：通路词表与帧来源词表是两件事，混用就把已知说成未知");
    const planned = await inPage(() => {
      const off = [...document.querySelectorAll(".lib-planned")];
      return off.length ? { name: off[0].textContent.replace(/\s+/g, " ").trim(), reason: (off[0].parentElement.querySelector(".lib-reason") || {}).textContent || "" } : null;
    });
    expect(planned, "组件库里没有一条「没上线 + 原因」");
    expect(/不上线的原因/.test(planned.reason), `灰条目没说为什么：${planned.reason}`);
    return `${demo} 枚 DEMO 角标；通路角标 ${[...new Set(registered.map((row) => row.badge))].join("/")}；库里第一条灰项：${planned.name}｜${planned.reason}`;
  });

  await check("状态栏把当前场地/位置/接入机/板逐个写出来（不是只藏在下拉里）", async () => {
    const parts = await statusFromDom();
    const want = ["场地", "位置", "接入机", "板", "布局", "改动", "实例"];
    const byLabel = Object.fromEntries(parts.map((row) => [row.label, row.value]));
    want.forEach((label) => expect(byLabel[label], `状态栏缺「${label}」这一段`));
    expect(byLabel.场地 === siteA.name, `状态栏的场地是「${byLabel.场地}」`);
    expect(byLabel.板 && byLabel.板 !== "无节点", `板那段是「${byLabel.板}」，得说得出当前是哪一块`);
    expect(parts.every((row) => row.visible), "状态栏有段落被压成 0 尺寸");
    return want.map((label) => `${label}=${byLabel[label]}`).join(" · ");
  });

  await check("一块格子一份租约：5 格 5 份，定时器都记在账上", async () => {
    const snap = await snapshot();
    expect(snap.audit.leases === snap.widgets.length, `租约 ${snap.audit.leases} ≠ 格子 ${snap.widgets.length}`);
    expect(snap.audit.intervals + snap.audit.timeouts > 0, "演示组件的心跳没走租约记账，那清理断言就是空的");
    return `${snap.audit.leases} 份租约 / ${snap.audit.intervals} 个定时器`;
  });

  // ---------------------------------------------------------------- 层级切换不写后台
  section("切位置/切接入机/切板：只换视图，一个写请求都不许发");
  const mark = await callMark();
  await selectByLabel("位置", "看台");
  await selectByLabel("接入机", "pi-1-看台");
  await selectByLabel("板", "uart");

  await check("这三步一笔请求都不该发：作用域筛选是本地的事", async () => {
    const calls = await apiCallsSince(mark);
    // 场地的 relays/gateways/nodes 在选进场地那一步就一次拉全了，切位置/切接入机/切板
    // 只在已加载的列表里筛（`context.reconcile()`）。所以这里最强的口径是"零请求"：
    // 一旦这几步回去问后端，切换就会带上网络抖动，判据"切场地/切位置不触碰后台"就破了。
    expect(calls.length === 0, `这三步发了 ${calls.length} 笔：${calls.join(", ")}`);
    const snap = await snapshot();
    expect(snap.relayName === "看台" && snap.gatewayName === "pi-1-看台", `位置=${snap.relayName} 接入机=${snap.gatewayName}`);
    expect(/uart/.test(snap.nodeName || ""), `板选中了 ${snap.nodeName}`);
    const boards = await inPage(() => window.rftrackWorkbench.context.state.scopeNodes.map((row) => row.node_id));
    expect(boards.length > 0, "切完接入机后本接入机的板列表是空的，那筛选压根没生效");
    return `零请求；${snap.relayName}/${snap.gatewayName} 下 ${boards.length} 块板，选中 ${snap.nodeName}`;
  });

  await check("板下拉选中哪一块就停在哪一块（数字框里的是整数，下拉给的是字符串）", async () => {
    const snap = await snapshot();
    expect(snap.nodeIdType === "number", `context.state.nodeId 是 ${snap.nodeIdType}：不换算就会掉回第一块板`);
    const boards = await inPage(() => window.rftrackWorkbench.context.state.scopeNodes.map((row) => row.node_id));
    const picked = Number(await pickerValue("板"));
    expect(boards.includes(picked), `下拉选的是 ${picked}，本接入机的板是 ${boards.join(",")}`);
    const boardShown = statusValue(await statusFromDom(), "板");
    expect(boardShown === snap.nodeName, `状态栏的板是「${boardShown}」，真正选中的是「${snap.nodeName}」`);
    return `选中 ${snap.nodeName}（${picked}），状态栏同步`;
  });

  // ---------------------------------------------------------------- 增
  section("加格子：落到空位、标未保存、与谁都不重叠");
  const beforeAdd = (await snapshot()).widgets.length;
  await clickButton("属性");
  const addButton = await inPage(() => {
    const btn = [...document.querySelectorAll(".lib-add")].find((b) => b.textContent.includes("多频道矩阵"));
    if (!btn) return null;
    btn.scrollIntoView({ block: "center" });
    const rect = btn.getBoundingClientRect();
    return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2, disabled: btn.disabled, text: btn.textContent.replace(/\s+/g, " ").trim() };
  });
  expect(addButton, "组件库里找不到「多频道矩阵」那条");
  expect(!addButton.disabled, `那条是灰的：${addButton.text}`);

  await check("点组件库那条 → 新格子出现、带未保存徽标、落进空位", async () => {
    await clickAt(addButton.x, addButton.y);
    await waitFor("新格子", () => inPage(() => Boolean(document.querySelector('.widget[data-instance="channel-matrix-1"]'))));
    const snap = await snapshot();
    expect(snap.widgets.length === beforeAdd + 1, `格子数 ${snap.widgets.length}`);
    const added = byId(snap.widgets, "channel-matrix-1");
    expect(added.isNew, "新格子没被标成未保存");
    const newBadge = await countOf('.widget[data-instance="channel-matrix-1"] .badge--new');
    expect(newBadge === 1, "DOM 上没有「未保存」角标");
    await expectNoOverlap('新格子压在了别的格子上');
    return `id=${added.id} 落点 ${added.x},${added.y} ${added.w}×${added.h}（原画布 ${beforeAdd} 格已占满 0-7 行）`;
  });

  // ---------------------------------------------------------------- 移动 + 缩放
  section("拖动标题栏与右下角把手：真实 pointer 事件，落点按栅格取整");
  const cell = await inPage(() => parseFloat(getComputedStyle(document.querySelector(".workspace")).getPropertyValue("--cell")));
  const soloBefore = byId((await snapshot()).widgets, "channel-matrix-1");

  // 拖与缩都拿新加的那块做：夹具那五格把 0-7 行占满了，动它们必然引发推挤连锁，
  // 那条判据该由"重叠推动"那一节专门断，不该混在这里当噪声。
  await dragTo(await centerOf('.widget[data-instance="channel-matrix-1"] .widget-head'), {
    x: (await centerOf('.widget[data-instance="channel-matrix-1"] .widget-head')).x + cell * 3,
    y: (await centerOf('.widget[data-instance="channel-matrix-1"] .widget-head')).y + CELL_H,
  });

  await check("拖了三列一行：状态里的 x/y 各 +3/+1，格子标脏，DOM 跟着挪", async () => {
    const snap = await snapshot();
    const moved = byId(snap.widgets, "channel-matrix-1");
    expect(moved.x === soloBefore.x + 3, `x 从 ${soloBefore.x} 变成了 ${moved.x}，期望 +3`);
    expect(moved.y === soloBefore.y + 1, `y 从 ${soloBefore.y} 变成了 ${moved.y}，期望 +1`);
    expect(snap.draft.includes("channel-matrix-1"), `脏集合里没有它：${snap.draft.join(",")}`);
    expect(await countOf('.widget[data-instance="channel-matrix-1"].widget--dirty') === 1, "DOM 上没打脏标记，保存按钮的角标就数不到它");
    await expectNoOverlap('拖完出现重叠');
    return `${soloBefore.x},${soloBefore.y} → ${moved.x},${moved.y}；列宽 ${Math.round(cell)}px`;
  });

  const handleBefore = byId((await snapshot()).widgets, "channel-matrix-1");
  const spot = await targetOutsideCanvas('.widget[data-instance="channel-matrix-1"] .handle');
  // 取证用的临时探针（只服务这一节）：数一数这段拖动里有多少段 `pointermove` 打在画布盒子**外面**。
  // 页面不需要它，判据需要——"拖出画布还在跟着放大"靠的就是挂在 window 上的那条监听；
  // 哪天有人把它挪回画布元素上，这里就会变成 0 次，尺寸也会停在盒子边线上。
  await inPage(() => {
    window.__probe = { total: 0, outside: 0 };
    window.addEventListener("pointermove", (event) => {
      const ws = document.querySelector(".workspace");
      if (!ws) return;
      const box = ws.getBoundingClientRect();
      const boxBottom = box.top + ws.clientHeight;
      window.__probe.total += 1;
      if (event.clientX > box.right || event.clientY > boxBottom) window.__probe.outside += 1;
    }, true);
    return true;
  });
  await dragTo(spot.from, spot.to);

  await check("拉右下角把手拖出画布盒子：右下角跟到指针那一格，越界夹紧、原点不被带跑", async () => {
    const snap = await snapshot();
    const grown = byId(snap.widgets, "channel-matrix-1");
    const probe = await inPage(() => window.__probe);
    // 期望值由落点像素换算而来（`spot.expect`，式子与 `grid.pointToCell` 同一条），不是写死的
    // "+1 行/+2 列"：画布内部滚动多少取决于这一屏有几格、窗口多高，写死的那个数只在一个
    // 特定现场下成立——红的时候既可能是页面错，也可能是夹具错，分不开。
    const wantW = spot.expect.x - handleBefore.x + 1;
    const wantH = spot.expect.y - handleBefore.y + 1;
    expect(probe.total > 0, `这段拖动里页面一条 pointermove 都没收到（把手 ${Math.round(spot.from.x)},${Math.round(spot.from.y)} → 落点 ${Math.round(spot.to.x)},${Math.round(spot.to.y)}）：那是事件没派发，不是页面的事`);
    expect(probe.outside > 0, `${probe.total} 段 pointermove 全在画布盒子内（右 ${Math.round(spot.box.right)} / 底 ${Math.round(spot.box.bottom)}）：这条判据没测到"拖出画布"那一档`);
    expect(grown.w >= 2, `放大没跟着指针走：宽还是 ${grown.w}（落点在第 ${spot.expect.x} 列，期望 ${wantW}）`);
    expect(grown.h >= handleBefore.h + 1, `放大没跟着指针走：高从 ${handleBefore.h} 变成 ${grown.h}（落点在第 ${spot.expect.y} 行，期望 ${wantH}）`);
    expect(grown.w === wantW, `宽的右下角停错列：库里的 w=${grown.w}，按落点像素该是 ${wantW}（指针 ${Math.round(spot.to.x)}，原点在内容坐标 ${Math.round(spot.origin.x)}，列宽 ${Math.round(spot.cellW * 100) / 100}）`);
    expect(grown.h === wantH, `高的右下角停错行：库里的 h=${grown.h}，按落点像素该是 ${wantH}（指针 ${Math.round(spot.to.y)}，内容原点 ${Math.round(spot.origin.y)}=盒顶 ${Math.round(spot.box.top)} 减滚动 ${Math.round(spot.scroll.top)}，行高 ${CELL_H}）`);
    expect(grown.x + grown.w <= 12, `越界没夹紧：x=${grown.x} w=${grown.w}，右边到了第 ${grown.x + grown.w} 列（画布只有 12 列）`);
    expect(grown.x === handleBefore.x && grown.y === handleBefore.y, `原点被拉把手带跑了：${grown.x},${grown.y}`);
    await expectNoOverlap('放大之后与别的格子重叠了，栅格没推开');
    return `${handleBefore.w}×${handleBefore.h} → ${grown.w}×${grown.h}（右下角落在第 ${spot.expect.x} 列 / 第 ${spot.expect.y} 行，右边缘第 ${grown.x + grown.w} 列）；${probe.outside}/${probe.total} 段 move 在画布盒子外（盒子右 ${Math.round(spot.box.right)} / 底 ${Math.round(spot.box.bottom)}，落点 ${Math.round(spot.to.x)},${Math.round(spot.to.y)}，画布已滚 ${Math.round(spot.scroll.top)}px）`;
  });

  await check("把一块拖到另一块身上：谁都不许盖住谁，被推走的那块也一起标脏", async () => {
    const before = await snapshot();
    const matrix = byId(before.widgets, "w-matrix");
    const detail = byId(before.widgets, "w-detail");
    // 落点取被撞那格的标题栏中心：x 与 y 都给，才是真撞上（只改 x 的话两块根本不在同一行）。
    await dragTo(await centerOf('.widget[data-instance="w-detail"] .widget-head'),
      await centerOf('.widget[data-instance="w-matrix"] .widget-head'));
    const after = await snapshot();
    const movedDetail = byId(after.widgets, "w-detail");
    const movedMatrix = byId(after.widgets, "w-matrix");
    expect(movedDetail.x !== detail.x || movedDetail.y !== detail.y, `没拖过去（还在 ${movedDetail.x},${movedDetail.y}）——这条判据要的是真撞上`);
    const pushed = movedMatrix.x !== matrix.x || movedMatrix.y !== matrix.y || movedMatrix.h !== matrix.h;
    expect(pushed, `拖过去之后 w-matrix 原位不动（${movedMatrix.x},${movedMatrix.y}）：那两块现在是叠着的`);
    try {
      await expectNoOverlap("两块最终在像素上重叠");
    } catch (error) {
      throw new Error(`${error.message}；页面上的说法：${after.errors.join("｜") || "无"}`);
    }
    expect(after.draft.includes("w-matrix"), `被推走的格子没进脏集合（${after.draft.join(",")}）：保存只写回拖的那块，下次刷新它会弹回原位`);
    return `w-detail ${detail.x},${detail.y}→${movedDetail.x},${movedDetail.y}；w-matrix ${matrix.x},${matrix.y}→${movedMatrix.x},${movedMatrix.y}`;
  });

  // ---------------------------------------------------------------- 复制
  section("复制一格：新实例号续上，配置跟着拷，生效值不许继承");
  await check("点 ⧉ → 出现第二块同类格子，实例号不撞车", async () => {
    const copyButton = await inPage(() => {
      const btn = document.querySelector('.widget[data-instance="w-matrix"] .icon');
      const rect = btn.getBoundingClientRect();
      return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2, text: btn.textContent.trim() };
    });
    expect(copyButton.text === "⧉", `第一个图标按钮是「${copyButton.text}」，页面结构变了`);
    await clickAt(copyButton.x, copyButton.y);
    await waitFor("副本格子", () => inPage(() => Boolean(document.querySelector('.widget[data-instance="channel-matrix-2"]'))));
    const snap = await snapshot();
    const source = byId(snap.widgets, "w-matrix");
    const copy = byId(snap.widgets, "channel-matrix-2");
    expect(JSON.stringify(copy.config) === JSON.stringify(source.config), `配置没拷过来：${JSON.stringify(copy.config)}`);
    expect(copy.isNew, "副本没被标成未保存");
    expect(Object.keys(copy.config).length > 0, `源格子本来就没配置，这条判据是空的：${JSON.stringify(source.config)}`);
    await expectNoOverlap('副本落在了别的东西上面');
    return `${source.id} → ${copy.id}，配置 ${JSON.stringify(copy.config).slice(0, 48)}`;
  });

  await check("副本不继承源格子的生效值：它还没回过服务端，来源链此刻没有事实依据", async () => {
    const copy = byId((await snapshot()).widgets, "channel-matrix-2");
    expect(copy.threshold === null, `新副本的生效值是从源格子抄来的（${copy.threshold}）`);
    const emptyState = await inPage(() => {
      window.rftrackWorkbench.store.select("channel-matrix-2");
      // 读整块面板，不读第一个 `.muted`：面板顶部本来就有一句组件摘要也是 `.muted`，
      // 按第一个取到的是摘要，判据会在"生效值确实没继承"的情况下报成找不到话术。
      const el = document.querySelector(".panel--props");
      return el ? el.textContent.replace(/\s+/g, " ").trim() : null;
    });
    expect(/还没保存/.test(emptyState || ""), `属性面板里没有"还没保存"这句话，整块是：${(emptyState || "").slice(0, 200)}`);
    return emptyState.slice(emptyState.indexOf("生效值"), emptyState.indexOf("生效值") + 60);
  });

  // ---------------------------------------------------------------- 局部配置 + 保存 + 刷新
  section("局部配置：在属性面板真打字改阈值，保存后刷新回来还得是它");
  await clickSelector('.widget[data-instance="w-detail"] .widget-head');
  await check("点中格子后属性面板出现实例层 threshold_db 控件，边界来自服务端声明", async () => {
    const selector = 'label.key[data-key="threshold_db"][data-layer="instance"] input[type="number"]';
    const found = await inPage((sel) => {
      const el = document.querySelector(sel);
      return el ? { value: el.value, min: el.min, max: el.max, step: el.step } : null;
    }, selector);
    expect(found, "属性面板里没有实例层的 threshold_db 数字框");
    expect(found.min === "-128" && found.max === "0", `边界来自服务端声明，现在 min=${found.min} max=${found.max}：页面自己抄了一份？`);
    return `当前值 ${found.value || "（未设）"}，步长 ${found.step}，边界 ${found.min}…${found.max}（来自 /api/settings/schema）`;
  });

  await typeIntoNumber('label.key[data-key="threshold_db"][data-layer="instance"] input[type="number"]', "-92");

  await check("打字生效：这一层标了已设、脏计数上去了、保存按钮不再灰", async () => {
    const snap = await snapshot();
    expect(byId(snap.widgets, "w-detail").config.threshold_db === -92, `实例层配置是 ${JSON.stringify(byId(snap.widgets, "w-detail").config)}`);
    const setFlag = await countOf('label.key[data-key="threshold_db"][data-layer="instance"].key--set');
    expect(setFlag === 1, `「本层已设」标记数 ${setFlag}`);
    const save = await saveButton();
    expect(save.badge, `保存按钮上没有待保存条数角标：${save.text}`);
    expect(!save.disabled, `有 ${save.badge} 笔待保存，保存按钮却还是灰的（文本：${save.text}）`);
    return `角标 ${save.badge}，格子 ${snap.widgets.length} 块，脏 ${snap.draft.length} + 待删 ${snap.pendingDelete.length}`;
  });

  await check("点保存 → 每格一笔 PUT，全 200，角标清零", async () => {
    const markSave = await callMark();
    const dirty = (await snapshot()).draft.length;
    await clickButton("保存");
    await waitFor("保存收尾（脏集合与待删都清空）", () => inPage(() => {
      const wb = window.rftrackWorkbench;
      return wb.store.state.draft.size === 0 && wb.store.state.removed.size === 0;
    }), { timeoutMs: 20000 });
    const calls = await apiCallsSince(markSave);
    const puts = calls.filter((row) => row.startsWith("PUT"));
    expect(puts.length === dirty, `脏 ${dirty} 笔却发了 ${puts.length} 笔 PUT：${calls.join(", ")}`);
    expect(puts.every((row) => /→ 200$/.test(row)), `有 PUT 没成：${puts.join(", ")}`);
    const notice = await noticeText();
    expect(notice === "已保存", `状态栏那句提示是「${notice}」`);
    expect((await saveButton()).disabled, "保存完了保存按钮还亮着");
    return `${puts.length} 笔 PUT 全 200；提示「${notice}」`;
  });

  // 刷新前先把"上一屏"记下来：状态栏那四段文本 + 点中的格子。回来逐段比，
  // 才算断言了"刷新后布局与组件局部配置恢复正确"里**恢复**这两个字。
  const lastBarParts = await statusFromDom();
  const lastBar = {};
  for (const label of ["场地", "位置", "接入机", "板"]) lastBar[label] = statusValue(lastBarParts, label);

  await reload();

  await check("刷新回来：新增、拖动、放大、局部阈值四件事都在库里", async () => {
    const snap = await snapshot();
    const ids = snap.widgets.map((row) => row.id);
    expect(ids.includes("channel-matrix-1") && ids.includes("channel-matrix-2"), `两块新格子没回来：${ids.join(",")}`);
    const solo = byId(snap.widgets, "channel-matrix-1");
    expect(solo.x === soloBefore.x + 3, `拖动没入库：x=${solo.x}，期望 ${soloBefore.x + 3}`);
    expect(solo.w > handleBefore.w, `放大没入库：宽回到 ${solo.w}`);
    const detail = byId(snap.widgets, "w-detail");
    expect(detail.config.threshold_db === -92, `阈值回退成 ${detail.config.threshold_db}`);
    expect(detail.threshold === -92, `服务端算的生效值不是 -92（${detail.threshold}）`);
    expect(byId(snap.widgets, "w-matrix").y !== 0, "被推走的那格没入库：刷新后弹回了原位");
    // 阈值这一格在"没有帧"时本体画的是四态那句话，不是阈值——`-92` 是否真的生效由上面两条
    // （库里的 `config.threshold_db` 与服务端算出的 `threshold`）说了算。这里断言的是另一件事：
    // 没数时必须**说出为什么没数**，而且不许摆出一个像是测出来的 dBFS（任务书：不支持/没有的
    // 东西不许画成有）。DOM 里的真数字要到回放那一段才断言。
    const cell = await inPage(() => {
      const el = document.querySelector('.widget[data-instance="w-detail"] .widget-body');
      if (!el) return null;
      const why = el.querySelector(".empty");
      return {
        why: why ? why.textContent.replace(/\s+/g, " ").trim() : null,
        body: el.textContent.replace(/\s+/g, " ").trim().slice(0, 140),
      };
    });
    expect(cell, "w-detail 这一格没画出来");
    expect(cell.why, `这一格既没摆数也没说为什么没数：${cell.body}`);
    expect(!/dBFS/.test(cell.body), `没有帧却把 dBFS 摆上了页面（那是编出来的数）：${cell.body}`);
    expect(snap.draft.length === 0 && snap.audit.leases === snap.widgets.length, "刷新后还有脏标记或租约对不上");
    return `7 格都在库里（w-matrix 现在 ${byId(snap.widgets, "w-matrix").x},${byId(snap.widgets, "w-matrix").y}）；生效值 ${detail.threshold}`;
  });

  await check("刷新回来的不只是库里的数据，还有上一屏：场地、位置/接入机/板、点中的那一格", async () => {
    const snap = await snapshot();
    const parts = await statusFromDom();
    expect(snap.siteId === siteA.site_id, `刷新后自己选的场地是 ${snap.siteId}，上一屏看的是 ${siteA.site_id}`);
    for (const label of ["场地", "位置", "接入机", "板"]) {
      const now = statusValue(parts, label);
      expect(now === lastBar[label], `状态栏「${label}」这一段刷新后变了：上一屏「${lastBar[label]}」→ 现在「${now}」`);
    }
    const selected = await inPage(() => window.rftrackWorkbench.store.state.selection);
    expect(selected === "w-detail", `刷新后点中的格子是 ${selected}，上一屏点的是 w-detail（属性面板停在别处，生效值那一屏就丢了）`);
    const memo = await inPage((key) => JSON.parse(localStorage.getItem(key) || "null"), VIEW_MEMORY_KEY);
    expect(memo && memo.bySite[String(siteA.site_id)], `localStorage 里没有这一屏的记忆：${JSON.stringify(memo)}`);
    return `场地 ${snap.siteId} · ${lastBar.位置} / ${lastBar.接入机} / ${lastBar.板} · 选中 ${selected}；记忆 ${JSON.stringify(memo.bySite)}`;
  });

  await check("生效值来源链在页面上看得见：这一格是实例层定的", async () => {
    await clickSelector('.widget[data-instance="w-detail"] .widget-head');
    const row = await inPage(() => {
      const tr = [...document.querySelectorAll("table.kv-table tr")].find((t) => t.querySelector("th") && t.querySelector("th").textContent === "threshold_db");
      return tr ? tr.textContent.replace(/\s+/g, " ").trim().slice(0, 160) : null;
    });
    expect(row && /instance/.test(row), `来源链那行是：${row}`);
    expect(/-92/.test(row || ""), `来源链里没带 -92：${row}`);
    return row;
  });

  // ---------------------------------------------------------------- 刷新恢复
  section("刷新恢复：草稿丢回去，库里那份不许被它污染，而且不为此发一个写请求");
  await check("拖一格 → 点刷新恢复 → 位置回到库里那一份", async () => {
    const markRestore = await callMark();
    const before = byId((await snapshot()).widgets, "channel-matrix-1");
    const head = await centerOf('.widget[data-instance="channel-matrix-1"] .widget-head');
    await dragTo(head, { x: head.x + cell * 2, y: head.y });
    const dragged = byId((await snapshot()).widgets, "channel-matrix-1");
    expect(dragged.x !== before.x, "没拖动成功，这条判据断不到东西");
    await clickButton("刷新恢复");
    // 不能只等"脏集合空了"：`store.restore()` 先同步清草稿，再去读库里那一份。
    // 等前者会在"数据还没回来"这一帧就放行，读到的是拖动后那个还没被替换的位置（实测就这么红过）。
    await waitForApi(markRestore, /^GET \/api\/layouts\/\d+\/widgets → 200$/, "刷新恢复重新读库里那一份");
    await waitFor("草稿清空", () => inPage(() => window.rftrackWorkbench.store.state.draft.size === 0), { timeoutMs: 15000 });
    const after = byId((await snapshot()).widgets, "channel-matrix-1");
    expect(after.x === before.x, `恢复后 x=${after.x}（拖到的是 ${dragged.x}），库里那份是 ${before.x}｜${await describeEvidence()}`);
    const calls = await apiCallsSince(markRestore);
    expect(calls.every((row) => row.startsWith("GET")), `恢复过程中发出了写请求：${calls.join(", ")}`);
    const notice = await noticeText();
    return `${before.x} → ${dragged.x} → ${after.x}；${calls.length} 次调用全 GET；提示「${notice}」`;
  });

  // ---------------------------------------------------------------- 删除 + 保存
  section("删除：点 ✕ 之后保存必须还能把这两笔 DELETE 发出去");
  await check("删掉两块新格子：待删计数也要让保存按钮亮起来（只数脏格子的话它永远点不动）", async () => {
    for (const id of ["channel-matrix-1", "channel-matrix-2"]) {
      await clickSelector(`.widget[data-instance="${id}"] .icon[title="删除这块格子"]`);
    }
    const snap = await snapshot();
    expect(snap.pendingDelete.length === 2, `待删清单里是 ${snap.pendingDelete.join(",")}`);
    const save = await saveButton();
    expect(save.badge === "2", `待删两格时保存按钮的角标是「${save.badge}」（文本 ${save.text}）`);
    expect(!save.disabled, "有待删记录却按不动保存：删除永远落不了库");
    return `待删 ${snap.pendingDelete.join(",")}，角标 ${save.badge}`;
  });

  await check("保存发出 2 笔 DELETE，再刷新就真没了", async () => {
    const markDelete = await callMark();
    await clickButton("保存");
    await waitFor("待删清单清空", () => inPage(() => window.rftrackWorkbench.store.state.removed.size === 0), { timeoutMs: 20000 });
    const calls = await apiCallsSince(markDelete);
    const deletes = calls.filter((row) => row.startsWith("DELETE"));
    expect(deletes.length === 2, `DELETE 是 ${deletes.length} 笔：${calls.join(", ")}`);
    expect(deletes.every((row) => /→ 204$/.test(row)), `DELETE 没都成功：${deletes.join(", ")}`);
    await reload();
    const ids = await widgetIds();
    expect(ids.length === 5 && !ids.includes("channel-matrix-1"), `刷新后格子是 ${ids.join(",")}`);
    return `${deletes.map((row) => row.split(" ")[1]).join(" / ")}；刷新后回到 5 格`;
  });

  // ---------------------------------------------------------------- 布局级结构动作
  section("另存为 / 重命名 / 新建 / 删除：这些是立刻打服务端的结构动作");
  await check("另存为 → 副本成为当前布局，格子数一样", async () => {
    await fillText(".name-input", "冒烟副本");
    const markClone = await callMark();
    await clickButton("另存为");
    await waitFor("切到副本", () => inPage(() => {
      const layout = window.rftrackWorkbench.store.state.layout;
      return layout && layout.name === "冒烟副本";
    }), { timeoutMs: 20000 });
    const snap = await snapshot();
    expect(snap.layouts.length === 3, `${siteA.name}的布局数 ${snap.layouts.length}`);
    const current = snap.layouts.find((row) => row.id === snap.layout.id);
    expect(current.active === true, `副本没被服务端设为当前（is_active=${current.active}）`);
    expect(snap.widgets.length === 5, `副本里的格子数 ${snap.widgets.length}`);
    const calls = await apiCallsSince(markClone);
    expect(calls.some((row) => /POST \/api\/layouts\/\d+\/clone → 201/.test(row)), `没走克隆端点：${calls.join(",")}`);
    return `布局 3 份，当前「${snap.layout.name}」(id ${snap.layout.id}, is_active) 5 格；调用 ${calls.length} 笔`;
  });

  await check("未注册那一格跟着副本一起活下来（克隆不许把它当垃圾丢掉）", async () => {
    const frame = await inPage((instance) => {
      const el = document.querySelector(`.widget[data-instance="${instance}"]`);
      return el ? { plugin: el.dataset.plugin, badge: (el.querySelector(".badge--missing") || {}).textContent || null } : null;
    }, unknownPlugin.instance_id);
    expect(frame && frame.badge === "未注册", `副本里的 ${unknownPlugin.instance_id}：${JSON.stringify(frame)}`);
    return `${frame.plugin} 仍然是未注册占位，位置与配置都在`;
  });

  await check("重命名 → 下拉与状态栏一起换", async () => {
    await fillText(".name-input", "冒烟改名");
    await clickButton("重命名");
    await waitFor("名字换掉", () => inPage(() => window.rftrackWorkbench.store.state.layout.name === "冒烟改名"), { timeoutMs: 15000 });
    const option = await inPage(() => {
      const el = document.querySelector(".layout-pick");
      const picked = [...el.options].find((o) => o.selected);
      return picked ? picked.textContent.trim() : null;
    });
    const status = await statusFromDom();
    expect(/冒烟改名/.test(option || ""), `下拉里选中的是「${option}」`);
    expect(statusValue(status, "布局").includes("冒烟改名"), "状态栏的布局段没跟着换");
    return `下拉显示「${option}」；状态栏布局=${statusValue(status, "布局")}`;
  });

  await check("切布局：租约随格子走，切换后不留下多出来的定时器", async () => {
    await selectBySelector(".layout-pick", "值班视图");
    await waitFor("切回值班视图", () => inPage(() => window.rftrackWorkbench.store.state.layout.name === "值班视图"), { timeoutMs: 15000 });
    const snap = await snapshot();
    expect(snap.audit.leases === snap.widgets.length, `租约 ${snap.audit.leases} ≠ 格子 ${snap.widgets.length}`);
    expect(snap.audit.intervals <= snap.widgets.length * 2, `定时器总数 ${snap.audit.intervals}，旧布局的没清`);
    expect(snap.audit.released >= 5, `released 才 ${snap.audit.released}，切布局没走归还路径`);
    return `租约 ${snap.audit.leases}/${snap.widgets.length}，已归还累计 ${snap.audit.released}`;
  });

  await check("新建空布局 → 画布说的是「这块画布还是空的」而不是「还没选场地」", async () => {
    await fillText(".name-input", "冒烟空布局");
    await clickButton("新建");
    await waitFor("切到新布局", () => inPage(() => window.rftrackWorkbench.store.state.layout.name === "冒烟空布局"), { timeoutMs: 20000 });
    const canvas = await textOf(".workspace .state");
    expect(/这块画布还是空的/.test(canvas || ""), `空布局时画布说的是：${canvas}`);
    expect((await snapshot()).layouts.length === 4, "布局数没增加");
    return canvas;
  });

  await check("删掉当前这份空布局：不许偷偷激活别的，也不许让下拉长得像选了第一项", async () => {
    await clickButton("布局默认");
    await clickButton("删除本布局");
    await waitFor("布局数回到 3", () => inPage(() => window.rftrackWorkbench.store.state.layouts.length === 3), { timeoutMs: 20000 });
    const snap = await snapshot();
    expect(snap.layout === null, `删掉当前布局后页面自己激活了 ${snap.layout && snap.layout.name}：服务端说不自动激活`);
    const picked = await inPage(() => {
      const el = document.querySelector(".layout-pick");
      const option = [...el.options].find((o) => o.selected);
      return option ? option.textContent.trim() : null;
    });
    expect(/没有当前布局/.test(picked || ""), `下拉里选中项是「${picked}」——没有当前布局时必须给出占位`);
    const status = await statusFromDom();
    expect(statusValue(status, "布局") === "未加载", "状态栏没说出「未加载」");
    const canvas = await textOf(".workspace .state");
    expect(/本场地没有当前布局/.test(canvas || ""), `画布说的是：${canvas}`);
    return `下拉：${picked}；状态栏布局=未加载；画布：${canvas}`;
  });

  await selectBySelector(".layout-pick", "值班视图");
  await waitFor("值班视图回来了", () => inPage(() => (window.rftrackWorkbench.store.state.layout || {}).name === "值班视图"), { timeoutMs: 15000 });

  // ---------------------------------------------------------------- 各场地各留自己的当前布局
  section("切场地：A 与 B 各留各的当前布局，选中态也各留一份");
  await selectBySelector(".layout-pick", "备用视图");
  await waitFor("A 切到备用视图", () => inPage(() => window.rftrackWorkbench.store.state.layout.name === "备用视图"), { timeoutMs: 15000 });
  await clickSelector(`.widget[data-instance="${siteA.secondary_instance}"] .widget-head`);
  const aLayoutId = (await snapshot()).layout.id;
  const aSelection = (await snapshot()).selection;
  expect(aSelection === siteA.secondary_instance, `A 的备用视图里没选中那块（${aSelection}）`);

  await switchSite(siteB.name);

  await check("B 的当前布局没被 A 的操作带走（服务端按场地各存一份 is_active）", async () => {
    const snap = await snapshot();
    expect(snap.siteName === siteB.name, `现在站在 ${snap.siteName}`);
    expect(snap.layout.id !== aLayoutId, `B 里看到的布局 id 竟然是 A 那份 ${aLayoutId}`);
    expect(snap.layout.degraded === true, `B 的当前布局该是那份版本超前的，现在 degraded=${snap.layout.degraded}`);
    const ids = snap.widgets.map((row) => row.id);
    expect(ids.includes(siteB.secondary_instance), `B 的备用视图格子是 ${ids.join(",")}，没读到 B 自己那一份`);
    expect(!ids.includes(siteA.secondary_instance), `A 的格子 ${siteA.secondary_instance} 串到 B 来了`);
    return `B 的当前=${snap.layout.name}(id ${snap.layout.id}, 版本 ${snap.layout.version})，格子 ${ids.join(",")}；A 的当前=${aLayoutId}`;
  });

  await check("切回 A：它自己的当前布局与我上次点中的那块格子都还原", async () => {
    await switchSite(siteA.name);
    const snap = await snapshot();
    expect(snap.layout.id === aLayoutId, `A 的当前布局变成了 ${snap.layout.id}，上次是 ${aLayoutId}`);
    expect(snap.selection === aSelection, `选中格子是 ${snap.selection}，上次点的是 ${aSelection}`);
    expect(await countOf(".widget--selected") === 1, `DOM 上选中的格子有 ${await countOf(".widget--selected")} 块`);
    const status = await statusFromDom();
    expect(statusValue(status, "场地") === siteA.name, "状态栏还停在 B");
    return `布局 ${snap.layout.name}(id ${snap.layout.id})，选中 ${snap.selection}`;
  });

  await check("切场地这一个动作本身不写后台：只有 GET", async () => {
    const markSwitch = await switchSite(siteB.name);
    const calls = await apiCallsSince(markSwitch);
    expect(calls.length > 0 && calls.every((row) => row.startsWith("GET")), `切场地写出了：${calls.join(", ")}`);
    return `${calls.length} 次调用：${[...new Set(calls.map((row) => row.split(" ")[0]))].join(",")}`;
  });

  // ---------------------------------------------------------------- 降级布局
  section(`${siteB.name}第一眼撞到的就是那份 version=99 的布局：只读、说得清、出得去`);
  await check("只读提示说得出是哪一件事；只有保存这一个按钮是灰的", async () => {
    const notice = await inPage(() => {
      const el = document.querySelector(".notice--readonly");
      return el ? { reason: el.dataset.reason, text: el.textContent.replace(/\s+/g, " ").trim() } : null;
    });
    expect(notice, "降级布局没有渲染只读提示");
    expect(notice.reason === "layout_version_too_new", `data-reason 是 ${notice.reason}`);
    expect(/只读渲染/.test(notice.text), notice.text);
    const save = await saveButton();
    expect(save.disabled, "降级状态下保存按钮竟然还能点");
    const rename = await inPage(() => [...document.querySelectorAll(".toolbar button.btn")]
      .find((b) => b.textContent.trim().startsWith("新建")));
    expect(!rename.disabled, "新建布局被一起灰掉了：那这份降级布局就成了出不去的牢");
    return notice.text;
  });

  await check("库里存坏的 defaults 说「按未设置处理」，而不是当成 -90 用", async () => {
    await clickButton("布局默认");
    const line = await textOf(".panel--props .warn");
    expect(line && /malformed_json/.test(line) && /按未设置处理/.test(line), `那行是：${line}`);
    const snap = await snapshot();
    expect(snap.layout.defaults === null, `半截 JSON 被当对象读了：${JSON.stringify(snap.layout.defaults)}`);
    return `${line}｜服务端回的 defaults=${JSON.stringify(snap.layout.defaults)}`;
  });

  await check("降级那份格子的组件照常渲染：只读不等于不画，没数据也要说得出为什么", async () => {
    const frame = await inPage((instance) => {
      const STATES = ["no_board", "live", "stale", "offline", "unsupported", "empty"];
      const el = document.querySelector(`.widget[data-instance="${instance}"]`);
      if (!el) return null;
      const pill = el.querySelector(".widget-body .pill");
      // 牌子上挂着 `pill--<语气>` 与 `pill--<状态>` 两个类：按"哪个是认识的状态名"来认，
      // 不能拿第一个 `pill--*` 当状态——`unsupported` 的语气恰好也叫 `unknown`，取错就判成不合法。
      const state = pill ? [...pill.classList].map((cls) => cls.slice(6)).find((cls) => STATES.includes(cls)) : null;
      return {
        body: el.querySelector(".widget-body")?.textContent.replace(/\s+/g, " ").trim().slice(0, 120) || null,
        label: pill ? pill.textContent.trim() : null,
        state,
        title: (el.querySelector(".widget-title") || {}).textContent?.trim() || null,
      };
    }, siteB.secondary_instance);
    expect(frame, `${siteB.secondary_instance} 这一格根本没画出来`);
    // S6 起这一格读的是 `/ws/live`：这一轮里没有帧，所以"照常渲染"的证据不是"里面有数"，
    // 而是**组件跑了、并且如实说出为什么没数**。四态之外的"空白一格"才是这一层的不许。
    expect(!!frame.state, `格子里没有四态那枚牌子（读到 ${JSON.stringify(frame)}）：只读布局不许把组件渲染也一起关掉`);
    expect(frame.body && /离散频道电平，不是频谱/.test(frame.body), `这一格没说自己画的是离散频道：${frame.body}`);
    expect(frame.body.split("这块板没声明电平帧能力").length - 1 === 1 || frame.state !== "unsupported",
      `牌子与正文把同一句印了两遍：${frame.body}`);
    expect(!/DEMO|演示/.test(frame.body || ""), `降级格子里出现了演示字样：${frame.body}`);
    return `${frame.title}·${frame.state}「${frame.label}」｜${frame.body.slice(0, 72)}…`;
  });

  await check("降级状态加格子、拖格子都不许改到状态：页面上给出理由", async () => {
    const before = await snapshot();
    const addButton = await inPage(() => {
      const btn = [...document.querySelectorAll(".lib-add")][0];
      const rect = btn.getBoundingClientRect();
      return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
    });
    await clickAt(addButton.x, addButton.y);
    const head = await centerOf(`.widget[data-instance="${siteB.secondary_instance}"] .widget-head`);
    await dragTo(head, { x: head.x + cell * 2, y: head.y });
    const after = await snapshot();
    expect(after.widgets.length === before.widgets.length, `加格子被接受了：${before.widgets.length} → ${after.widgets.length}`);
    const target = byId(after.widgets, siteB.secondary_instance);
    expect(target.x === byId(before.widgets, siteB.secondary_instance).x, "拖拽改动了降级布局的格子位置");
    expect(after.draft.length === 0, `脏集合非空：${after.draft.join(",")}`);
    const errors = await inPage(() => [...document.querySelectorAll(".workspace .state--error")].map((el) => el.textContent.replace(/\s+/g, " ").trim()));
    expect(errors.length > 0, "拒了却没在页面上说为什么");
    const notice = await noticeText();
    return `${errors.join(" ｜ ")}；状态栏提示「${notice}」`;
  });

  await check("出不去才是真事故：从降级布局切到好布局走得动（服务端也只按目标判）", async () => {
    await selectBySelector(".layout-pick", "值班视图");
    await waitFor("切到值班视图", () => inPage(() => window.rftrackWorkbench.store.state.layout.name === "值班视图"), { timeoutMs: 20000 });
    const good = await snapshot();
    expect(good.readOnly === null, `切到好布局后还是只读：${JSON.stringify(good.readOnly)}`);
    expect(good.widgets.length === 5, `B 的值班视图格子数 ${good.widgets.length}`);
    return `格子 ${good.widgets.map((row) => row.id).join(",")}；只读闸门已解除`;
  });

  await check("板的状态如实：会停 RH、能力上限、未声明的不支持——都来自后端那一行", async () => {
    await selectByLabel("板", "uart");
    const body = await inPage(() => {
      const el = document.querySelector('.widget[data-plugin="node-status"] .widget-body');
      return el ? el.textContent.replace(/\s+/g, " ").trim() : null;
    });
    expect(body, "B 的值班视图里没有 node-status 那一格");
    expect(/会停 RH/.test(body), `②a 那块 UART 板没显示"会停 RH"：${body.slice(0, 120)}`);
    expect(/512 bins/.test(body), `频谱上限没如实带出来（有界能力不许渲染成无界）：${body.slice(0, 120)}`);
    expect(/不支持（板端未声明）/.test(body), `usb-bridge 那块板该是"未声明即不支持"：${body.slice(0, 160)}`);
    expect(!/DEMO/.test(body), `node-status 里出现了 DEMO 角标：${body.slice(0, 80)}`);
    const status = await statusFromDom();
    return `状态栏板=${statusValue(status, "板")}；${body.slice(0, 96)}…`;
  });

  await check("切回那份降级的被服务端拒：页面给的是人话，且当前布局不许换", async () => {
    await selectBySelector(".layout-pick", "备用视图");
    await waitFor("被拒的提示出现", () => inPage(() => document.querySelectorAll(".workspace .state--error").length > 0), { timeoutMs: 20000 });
    const line = await textOf(".workspace .state--error");
    expect(/结构版本是 99/.test(line || ""), `切不过去时页面只给了：${line}`);
    expect(/只认到 1/.test(line || ""), `没把本工作台认识的版本说出来：${line}`);
    expect(!/^[a-z_]+$/.test(line || ""), `页面上只剩一个英文标识符：${line}`);
    const stuck = await snapshot();
    expect(stuck.layout.name === "值班视图", `被拒之后当前布局竟然换成了 ${stuck.layout.name}`);
    return line;
  });

  // ---------------------------------------------------------------- 资源清理
  section("资源清理：把所有格子删掉之后，定时器与订阅必须归零（不保存，纯草稿）");
  await switchSite(siteA.name);
  await selectBySelector(".layout-pick", "值班视图");
  await waitFor("A 的值班视图 5 格", () => inPage(() => document.querySelectorAll(".widget").length === 5), { timeoutMs: 15000 });

  await check("逐块点 ✕ 删到空：租约、定时器、订阅全部归零", async () => {
    for (let guard = 0; guard < 12; guard += 1) {
      const left = await inPage(() => document.querySelectorAll(".widget").length);
      if (!left) break;
      await clickSelector('.widget .icon[title="删除这块格子"]');
    }
    const snap = await snapshot();
    expect(snap.widgets.length === 0, `还剩 ${snap.widgets.length} 块格子`);
    expect(snap.audit.leases === 0, `删空了还有 ${snap.audit.leases} 份租约没还`);
    expect(snap.audit.intervals === 0 && snap.audit.timeouts === 0, `还有 ${snap.audit.intervals}/${snap.audit.timeouts} 个定时器在跑`);
    expect(snap.audit.subscriptions === 0, `还有 ${snap.audit.subscriptions} 份订阅`);
    expect(snap.audit.released >= 5, `released=${snap.audit.released}，删除路径没走记账`);
    return `租约 0 / 定时器 0 / 订阅 0；累计归还 ${snap.audit.released}`;
  });

  await check("空画布上「组件库」这条路还走得动（删空不等于页面死了）", async () => {
    const canvas = await textOf(".workspace .state");
    expect(/这块画布还是空的/.test(canvas || ""), `删空后画布说：${canvas}`);
    const add = await countOf(".lib-add:not([disabled])");
    expect(add > 0, "删空之后组件库一个都加不了");
    return `${canvas} · 可加条目 ${add}`;
  });

  await clickButton("刷新恢复");
  await waitFor("格子回来了", () => inPage(() => document.querySelectorAll(".widget").length === 5), { timeoutMs: 20000 });

  // ---------------------------------------------------------------- 窄屏
  section("390×844：状态栏的区分位必须还看得见");
  await setViewport(390, 844, 2);
  await sleep(400);

  await check("窄屏下栅格按 12 列下限走横向滚动，字没被压掉", async () => {
    const snap = await snapshot();
    expect(snap.cellW > 50 && snap.cellW <= 57, `列宽 ${snap.cellW} 不在这个形态的下限区间（56）`);
    const scroll = await inPage(() => {
      const el = document.querySelector(".workspace");
      return { scrollWidth: el.scrollWidth, clientWidth: el.clientWidth };
    });
    expect(scroll.scrollWidth > scroll.clientWidth, `工作区没有横向滚动：${JSON.stringify(scroll)}`);
    return `列宽 ${snap.cellW}px，工作区 ${scroll.clientWidth} 可视 / ${scroll.scrollWidth} 内容`;
  });

  await check("状态栏四段在窄屏上仍然逐段可见、值不为空", async () => {
    const parts = await statusFromDom();
    const want = ["场地", "位置", "接入机", "板"];
    want.forEach((label) => {
      const row = parts.find((item) => item.label === label);
      expect(row && row.value, `窄屏上「${label}」这一段没了`);
      expect(row.visible, `窄屏上「${label}」被压成 0 尺寸`);
    });
    const overflow = await inPage(() => {
      const el = document.querySelector("footer.status");
      return el.scrollWidth - el.clientWidth;
    });
    expect(overflow <= 2, `状态栏横向溢出 ${overflow}px（flex-wrap 没生效？）`);
    return want.map((label) => `${label}=${statusValue(parts, label)}`).join(" · ");
  });

  await shot("01-narrow-390");
  await setViewport(1440, 900);
  await sleep(300);
  await shot("02-wide-1440");
  await clickButton("布局默认");
  await shot("03-layout-defaults");

  // ---------------------------------------------------------------- 断 CDN
  section("把两个 CDN 掐断：页面必须靠仓库里那份 vendor 起来");
  const blocked = [];
  const interceptor = (method, params) => {
    if (method !== "Fetch.requestPaused") return;
    blocked.push(params.request.url);
    cdp.send("Fetch.failRequest", { requestId: params.requestId, errorReason: "Failed" }).catch(() => {});
  };
  cdp.on(interceptor);
  await cdp.send("Fetch.enable", {
    patterns: [
      { urlPattern: "*jsdelivr*", requestStage: "Request" },
      { urlPattern: "*npmmirror*", requestStage: "Request" },
    ],
  });
  await cdp.send("Page.reload", { ignoreCache: true });
  await setViewport(1440, 900);
  await check("CDN 全断时页面照样挂载（仓库里的 vendor 副本不是摆设）", async () => {
    await waitFor("挂载完", () => inPage(() => Boolean(window.rftrackWorkbench && document.querySelector(".shell"))), { timeoutMs: 25000 });
    expect(blocked.length >= 2, `只掐断了 ${blocked.length} 条：${blocked.join(", ")}`);
    const info = await inPage(() => ({
      version: (globalThis.Vue || {}).version || null,
      bootError: document.documentElement.dataset.bootError || null,
      vendor: performance.getEntriesByType("resource").map((row) => row.name).filter((name) => name.includes("/vendor/")),
      widgets: document.querySelectorAll(".widget").length,
    }));
    expect(info.bootError === null, `boot 标了 ${info.bootError}`);
    expect(info.vendor.length === 1, `本地副本走了 ${info.vendor.length} 次：${info.vendor.join(",")}`);
    expect(info.widgets > 0, "页面起来了但一块格子都没画");
    return `掐断 ${blocked.length} 条 CDN 请求，改用 ${info.vendor[0].split("/").pop()}（Vue ${info.version}，${info.widgets} 格）`;
  });
  await cdp.send("Fetch.disable");
  cdp.off(interceptor);
  await shot("04-offline-vendor");

  // ---------------------------------------------------------------- 回放实例
  /*
    阶段 A 全程不碰真串口（`RFTRACK_DIALER_ENABLED=0`），所以那一轮的页面里一块真帧都没有：
    四态断的是"没数的时候说什么"。S6 的出口判据要的是另一半——**真数值流经 `/ws/live`**，
    而唯一能喂进真字节的通路是回放 runner 读一份录制。于是这一段起第二实例：

    · 新 origin ⇒ localStorage 干净，首屏"不许猜场地"在这一轮重新成立一次；
    · 另导一份夹具库 ⇒ 阶段 A 保存过、删过格子的那份库不许影响这里按实例号找格子的判据；
    · 两份录制（A 20 s、B 60 s）⇒ A 短到能在同一轮里演完，于是"演完之后距今几秒"与
      "失联事件"都真出现过；B 长到切过去时它还在演，"清空旧场地的数"与"接上新场地的数"
      落在同一条时间线上，不必把服务再起一遍。

    断的是"录制里的字节 → `/ws/live` 的一帧 → DOM 上那串数字"整条路。期望值不在这文件里
    写死：它们从清单里的 `expect_last` 来，而那份数是导出脚本用读取端把文件回读一遍算的。
  */
  section("第二实例 + 两份录制：页面上那串数字真是从字节流里解出来的");

  const liveDir = `${options.fixturesDir}-live`;
  const liveDb = join(liveDir, "workbench-fixtures.sqlite3");
  if (!options.keepFixture || !existsSync(liveDb)) await exportFixtures(liveDir);
  else console.log(`复用夹具库：${liveDb}`);
  const liveManifest = JSON.parse(readFileSync(join(liveDir, "manifest.json"), "utf8"));
  const [liveA, liveB] = liveManifest.sites;
  const planA = liveA.replay;
  const planB = liveB.replay;
  expect(planA && planB, `清单里没有回放条目：${JSON.stringify(Object.keys(liveManifest))}`);

  server2 = await startServer(liveDb, {
    RFTRACK_REPLAY_DIR: liveManifest.replay.dir,
    RFTRACK_REPLAY_LOOP: "0",
  });
  console.log(`  回放实例：${server2.origin}（库 ${liveDb}，录制 ${liveManifest.replay.dir}）`);
  const health2 = async () => {
    try {
      return await (await fetch(`${server2.origin}/api/health`)).json();
    } catch {
      return null;
    }
  };
  /** 按实例号取那一格的 DOM 事实（牌子、角标、meta、正文），判据才知道该问哪一格。 */
  const cellFacts = async (instance) => (await probeWidgets()).find((row) => row.instance === instance);

  /**
   * 该跟着新鲜度一起说过期的四格。判据只有一个来源：`components/select.js` 的 `dataState()`
   * 对**当前这块板**的那一行帧算 `stale`，而这四格都把结果画成牌子（`channel-matrix.js:72`、
   * `channel-detail.js:66`、`rssi-trend.js:138`、`signal-events.js:117`）。
   * `w-nodes` 不在列：它画的是节点清单每一行自己的态（在线/失联/未声明），不是"某一格的数
   * 多久没更新"，把它的牌子算进来就是替页面编一条它没做过的承诺。
   */
  const STALE_EXPECTED = ["w-matrix", "w-detail", "w-trend", "w-events"];

  /**
   * 等某个场地的录制**真的演到最后一帧**（服务端 `replay.done` 里有它的名字）。
   *
   * 为什么这是判据的前提而不是收尾：清单里的 `expect_last` 记的是**文件最后一帧**的数，
   * 而页面上此刻显的是"已经喂进来的那一帧"。A 那份只有 19.5 s，爬坡公式在第 12 帧时是
   * −53.95 dB —— 上一轮矩阵那条红就是红在这里（读到 −53.95，判据要 −95）。等放完，
   * 页面上那个数与 `expect_last` 才是同一个东西；这一条等待是正面的、有界的。
   */
  async function waitForPlayed(name, { timeoutMs = 45000 } = {}) {
    return waitFor(`${name} 放完`, async () => {
      const health = await health2();
      return health && health.replay.done.includes(name) ? health : null;
    }, { timeoutMs });
  }

  /**
   * 等**那一格的 DOM** 长成某个样子，而不是等内存里的数变了。
   *
   * `live.js` 有意不在帧到的那一刻催渲染：组件按自己的 `refresh_ms` 重读（`app.js` 文件头
   * 第 2 条），事件流那一格的下限还是 500 ms。于是"内存里已经变了"与"DOM 上看得见"之间
   * 天然差最多一拍——第一轮四条判据红的就是这个（`channel-detail.js:98` 画的就是 `row0.seq`，
   * 内存里 39、DOM 里 38，差的那一帧就是那一拍）。判据问的是页面上那串数字，等待条件
   * 就必须落在 DOM 上；等不到还是红，但那才是产品该红的样子。
   */
  async function waitForCell(instance, label, predicate, { timeoutMs = 20000 } = {}) {
    return waitFor(`${instance} 的 DOM：${label}`, async () => {
      const row = await cellFacts(instance);
      return row && predicate(row) ? row : null;
    }, { timeoutMs });
  }

  /** 详情那一格的逐字段读数（label/value 成对），判据问"页面上写的是几分贝"就走这里。 */
  const detailFields = () => inPage(() =>
    [...document.querySelectorAll('.widget[data-instance="w-detail"] .field')].map((el) => ({
      label: (el.querySelector(".label") || {}).textContent?.trim() || "",
      value: (el.querySelector(".value") || {}).textContent?.trim() || "",
    })));

  const openedAt = Date.now();
  await openWorkbench(`${server2.origin}/workbench/`);
  console.log(`  · 页面起来用了 ${Date.now() - openedAt} ms（A 那份录制只有 ${planA.span_s} s，还在演）`);

  /*
    这一页订不订 `/ws/live` 是**切场地之后**的事：新 origin 没有视图记忆，而这一库有两个场地，
    `preferredSite()` 的第四条写的是"多个场地又谁都没配过时不猜"。所以首屏 siteId=null 是设计，
    不是没起来——在这里等 `live.state.siteId` 等不到（第一轮就是这么红的，红得正是那条设计）。
    反过来，这一屏正好是那条判据唯一能被真页面证明的地方，于是把它写成一条真判据。
  */
  await check("新 origin 的首屏不猜场地：两个场地谁都没配过，状态栏说的是「未选」而不是随机一个", async () => {
    const scene = await inPage(() => {
      const wb = window.rftrackWorkbench;
      return {
        sites: wb.context.state.sites.length,
        siteId: wb.context.state.siteId,
        liveSite: wb.live.state.siteId,
        liveStatus: wb.live.state.status,
        error: wb.context.state.error,
      };
    });
    expect(scene.error === null, `boot 报了错：${JSON.stringify(scene.error)}`);
    expect(scene.sites === 2, `这一库应该有 2 个场地，读到 ${scene.sites}——"不猜"的前提没了`);
    expect(scene.siteId === null, `首屏竟然自己选了场地 ${scene.siteId}：那这条页面无从证明"不猜"`);
    expect(scene.liveSite === null && scene.liveStatus === "idle", `没选场地却已经开了推送：${JSON.stringify(scene)}`);
    const parts = await statusFromDom();
    const value = statusValue(parts, "场地");
    expect(value === "未选", `状态栏「场地」那一段说的是「${value}」，不是「未选」`);
    return `2 个场地、0 个选中、推送口 ${scene.liveStatus}；状态栏场地=${value}`;
  });

  await switchSite(liveA.name);
  await waitFor("这一页订上了 /ws/live", async () => {
    const opened = await inPage((want) => {
      const live = window.rftrackWorkbench.live;
      return live.state.siteId === want && live.state.status === "open" ? live.state.siteId : null;
    }, liveA.site_id);
    return opened !== null;
  }, { timeoutMs: 20000 });
  await selectByLabel("接入机", planA.gateway_name);
  await selectByLabel("板", planA.display_name);
  await waitFor("A 的回放板进了页面的实时窗", async () => {
    const probe = await liveProbe();
    return probe.nodes.some((row) => row.node_id === planA.node_id) ? probe : null;
  }, { timeoutMs: 25000 });

  await check("这一实例认下了两份录制：来源写着 mock、不循环、一个字节都没被拒", async () => {
    const health = await health2();
    expect(health, "健康端点没答话：第二实例起不来，这一段所有判据都没有对象");
    const r = health.replay;
    expect(r.enabled, `回放没启用：${JSON.stringify(r)}`);
    expect(r.source === "mock", `回放来源竟然是 ${r.source}：这一条是设计而不是遗漏`);
    expect(r.loop === false, "loop 竟然是 true：这一段要的是放完就安静下来");
    expect(r.rejected.length === 0, `有文件被拒：${JSON.stringify(r.rejected)}`);
    expect(Object.keys(r.unmatched).length === 0, `有录制找不到归属，一个字节都不会喂：${JSON.stringify(r.unmatched)}`);
    const names = r.recordings.map((row) => row.name).sort();
    const want = [planA.name, planB.name].sort().join("+");
    expect(names.join("+") === want, `演的是 ${names.join("+")}，清单里写的是 ${want}`);
    expect(r.chunks_fed > 0, `一个字节都没喂出去：${JSON.stringify(r)}`);
    const chunks = r.recordings.reduce((sum, row) => sum + row.chunks, 0);
    return `${names.join(" + ")} · ${chunks} 块在档，已喂 ${r.chunks_fed} 块`;
  });

  await check("帧真的在往这一页送：实时窗里的帧计数在涨", async () => {
    const first = await liveProbe();
    expect(first.status.frames > 0, `一帧都没收到：${JSON.stringify(first.status)}`);
    const grew = await waitFor(
      "live 的帧计数增加",
      async () => {
        const again = await liveProbe();
        return again.status.frames > first.status.frames ? again : null;
      },
      { timeoutMs: 12000 }
    );
    const row = grew.nodes.find((node) => node.node_id === planA.node_id);
    expect(row.frames > 0, `这块板的帧数竟然是 ${row.frames}`);
    return `推送口送了 ${first.status.frames} → ${grew.status.frames} 帧，其中 ${row.frames} 帧是 ${planA.display_name} 的`;
  });

  await check("换板只换视图：切到别的板，原来那台的帧还在往窗里进", async () => {
    // `live.js` 只在**换场地**时清窗；换板换的只是"这一格说哪块板"。这条断的是别把
    // 视图选择写成数据订阅：选了一下别的板，这一场地的采集就停了，那是没人要的行为。
    const first = (await liveProbe()).nodes.find((row) => row.node_id === planA.node_id);
    expect(first.seq !== null && first.seq !== undefined, `这块板进窗了却没有帧序：${JSON.stringify(first)}`);
    await selectByLabel("板", "uart");
    const name = await waitFor("状态栏切到 uart 那块板", async () => {
      const value = statusValue(await statusFromDom(), "板");
      return /uart/.test(value) ? value : null;
    }, { timeoutMs: 10000 });
    const grew = await waitFor(
      "切走之后原来那块板的帧序还在涨",
      async () => {
        const row = (await liveProbe()).nodes.find((node) => node.node_id === planA.node_id);
        return row && row.seq > first.seq ? row : null;
      },
      { timeoutMs: 12000 }
    );
    await selectByLabel("板", planA.display_name);
    expect(grew.source === "mock", `切回来来源变成 ${grew.source}`);
    return `选中 ${name} 时，${planA.display_name} 的 seq ${first.seq} → ${grew.seq}`;
  });

  await check("整页的来源只有 MOCK：没有一枚「真实」角标，状态栏那一段也跟着说", async () => {
    const probe = await liveProbe();
    expect(probe.status.sources.join("+") === "mock", `来源集合是 ${JSON.stringify(probe.status.sources)}，这一轮不该有第二种`);
    const cells = await probeWidgets();
    const real = cells.filter((row) => row.real > 0);
    expect(!real.length, `这些格子里出现了「真实」角标：${JSON.stringify(real.map((row) => row.instance))}`);
    const carrying = cells.filter((row) => row.mock > 0).map((row) => row.instance);
    expect(carrying.includes("w-matrix") && carrying.includes("w-detail"), `读数格子没挂 MOCK：${carrying.join(",") || "空的"}`);
    const status = await statusFromDom();
    const data = statusValue(status, "数据");
    expect(/MOCK/.test(data), `状态栏「数据」那一段说的是：${data}`);
    expect(/已连接/.test(data), `状态栏没说上连接态：${data}`);
    return `来源 ["mock"] · ${carrying.join("/")} 挂着 MOCK · 状态栏「${data}」`;
  });

  await check("A 场地的推送里读不到 B 场地的板：窗、DOM、串场计数三处都对得上", async () => {
    const probe = await liveProbe();
    const allowed = new Set(liveA.nodes);
    const foreign = probe.nodes.filter((row) => !allowed.has(row.node_id));
    expect(!foreign.length, `窗里有别的场地的板：${JSON.stringify(foreign.map((row) => row.node_id))}`);
    expect(probe.status.cross_site_rows === 0, `页面侧拦到 ${probe.status.cross_site_rows} 行跨场地数据`);
    expect(probe.status.unknown_frames === 0, `认不出的帧 ${probe.status.unknown_frames} 条：B 场地的帧不许混进这一桶`);
    expect(probe.status.site_id === liveA.site_id, `订的是场地 ${probe.status.site_id}，选的是 ${liveA.site_id}`);
    const text = await inPage(() => document.body.textContent);
    for (const word of [planB.transport_addr, planB.display_name, planB.gateway_name]) {
      expect(!text.includes(word), `页面上出现了 B 场地的「${word}」`);
    }
    return `窗里 ${probe.nodes.length} 块板全是 A 的 · 串场 0 · 未知帧 0`;
  });

  await check("矩阵只画进过帧的档位：三行、每行的数与清单一致，没测的档不许补出来", async () => {
    await waitForPlayed(planA.name);
    const slotsWanted = planA.slots.join(",");
    const slots = await waitFor(
      `${planA.display_name} 的档位进矩阵`,
      async () => {
        const probe = await liveProbe();
        const row = probe.nodes.find((node) => node.node_id === planA.node_id);
        return row && row.slots.length ? row.slots.join(",") : null;
      },
      { timeoutMs: 20000 }
    );
    expect(slots === slotsWanted, `帧里的档位是 ${slots}，录制里是 ${slotsWanted}`);
    // 先等 DOM 追上（档位是逐帧攒出来的、数是一帧一帧换的，两者只会往上走），再断它**没越过**
    // 三行：顺序反过来的话，"八档画满"这一条会被等待超时吞掉，红出来看不出是越界还是没跟上。
    const wanted = Object.entries(planA.expect_last);
    const m = await waitForCell(
      "w-matrix",
      "三行都画出、且最后一帧的三档数都落到 DOM 上",
      (row) => row.rows >= planA.slots.length
        && wanted.every(([, db]) => (row.body || "").includes(Number(db).toFixed(1))),
      { timeoutMs: 30000 }
    );
    expect(m.rows === planA.slots.length, `矩阵画了 ${m.rows} 行，档位只有 ${planA.slots.length} 个：配置里的八档不许画满`);
    expect(!/DEMO|演示/.test(m.meta || ""), `矩阵元信息里有演示字样：${m.meta}`);
    for (const [slot, want] of wanted) {
      const point = (await liveProbe()).nodes
        .find((row) => row.node_id === planA.node_id)
        .channels.find((entry) => entry.slot === Number(slot));
      expect(point, `第 ${Number(slot) + 1} 档没有读数`);
      expect(Math.abs(point.rssi - want) < 1e-6, `第 ${Number(slot) + 1} 档读到 ${point.rssi}，录制里是 ${want}`);
      // 页面上那一行也得印出同一个数：只对内存里的窗，等于没测"S6 出口判据"那句真数值流到页面。
      expect(m.body.includes(Number(want).toFixed(1)), `第 ${Number(slot) + 1} 档 ${want} 进了窗，DOM 那一行却没有：${m.body.slice(0, 120)}`);
    }
    return `档位 ${slots.split(",").map((s) => Number(s) + 1).join("、")} 档，读数与录制逐档对得上`;
  });

  await check("详情那一格的数字就是录制最后一帧的原值：连帧序、LO 与端点名一起对", async () => {
    const done = await waitForPlayed(planA.name);
    const row = (await liveProbe()).nodes.find((node) => node.node_id === planA.node_id);
    expect(row.frames === planA.frames, `页面收到 ${row.frames} 帧，录制里是 ${planA.frames} 帧`);
    expect(row.seq === planA.last_seq, `帧序停在 ${row.seq}，录制最后一帧是 ${planA.last_seq}`);
    expect(row.ep === planA.ep, `端点是 ${row.ep}，录制归属是 ${planA.ep}`);
    expect(row.source === "mock" && row.mode === planA.mode && row.lo_khz === planA.lo_khz, `帧头不对：${JSON.stringify(row)}`);
    /*
      DOM 上那一格只可能**落后**于内存里的实时窗（组件按拍重读，见 `waitForCell` 上方那段），
      所以等待条件写成"追上窗里那个 seq"，而不是"等于清单里那个数"：后者会把一拍之差报成产品
      报错。A 这一刻已经放完了（上面刚等过 `done`），窗里的 seq 不再动，这个等待才有终点。
    */
    const windowSeq = (await liveProbe()).nodes.find((node) => node.node_id === planA.node_id).seq;
    const fields = await waitFor(`详情那一格的帧序追上实时窗里的 ${windowSeq}`, async () => {
      const listed = await detailFields();
      const shown = (listed.find((field) => field.label === "帧序") || {}).value || null;
      return shown === String(windowSeq) ? listed : null;
    }, { timeoutMs: 20000 });
    const read = (label) => (fields.find((field) => field.label === label) || {}).value || null;
    // 与 `parts.js` 的 `fmtDb` 同一个换算：`-95.0` 从 JSON 里读回来是数字 `-95`，
    // 直接 `String()` 会印成 "-95"，DOM 上却是 "-95.0"——那是判据自己的错，不是页面的。
    const want = Number(planA.expect_last[String(planA.slots[0])]).toFixed(1);
    expect(read("当前 RSSI") === `${want} dBFS`, `详情里的当前 RSSI 是「${read("当前 RSSI")}」，录制里是 ${want}`);
    expect(read("帧序") === String(planA.last_seq), `详情里的帧序是「${read("帧序")}」`);
    expect(read("板") === planA.display_name, `详情里的板是「${read("板")}」`);
    expect(done.replay.chunks_fed > 0, "放完了却一块都没喂");
    return `第 ${planA.slots[0] + 1} 档 ${want} dBFS · 帧序 ${planA.last_seq} · LO ${planA.lo_khz} kHz · ${row.ep}`;
  });

  await check("点矩阵那一行设成联动：趋势随即从 REST 拿到带 MOCK 的摘要桶", async () => {
    const target = await inPage(() => {
      const tr = document.querySelector('.widget[data-instance="w-matrix"] tbody tr');
      if (!tr) return null;
      const rect = tr.getBoundingClientRect();
      return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2, text: tr.textContent.replace(/\s+/g, " ").trim() };
    });
    expect(target, "矩阵里没有可点的一行：那一格没画出档位");
    await clickAt(target.x, target.y);
    await waitFor("联动行标出来", () => countOf('.widget[data-instance="w-matrix"] tr.is-linked'), { timeoutMs: 10000 });
    const t = await waitFor(
      "趋势拿到摘要桶",
      async () => {
        const trend = await cellFacts("w-trend");
        return trend.mock > 0 && /桶宽/.test(trend.meta || "") ? trend : null;
      },
      { timeoutMs: 25000 }
    );
    expect(t.canvas === 1, `趋势那一格没有画布（canvas ${t.canvas}）`);
    expect(!/取数失败/.test(t.body || ""), `趋势说取数失败：${t.body}`);
    return `联动后趋势元信息「${t.meta.slice(0, 72)}…」`;
  });

  await check("演完之后页面如实补一句失联：事件行带着 mock 与录制端点名", async () => {
    const gone = await waitFor(
      `${planA.display_name} 的失联事件进窗`,
      async () => {
        const probe = await liveProbe();
        return probe.feed.find((row) => row.kind === "node_offline" && row.node === planA.node_id) || null;
      },
      { timeoutMs: 30000 }
    );
    expect(gone.source === "mock", `这条事件的来源是 ${gone.source}：回放写的事件不许被洗成真实`);
    expect(gone.ep === planA.ep, `事件里的端点是 ${gone.ep}，录制归属是 ${planA.ep}`);
    expect(gone.silence_s >= liveManifest.replay.silence_after_s, `安静判据只有 ${gone.silence_s} s，配置是 ${liveManifest.replay.silence_after_s} s`);
    // 事件流那一格的重画下限是 500 ms（`signal-events.js:82`），比内存里那条事件晚一拍是常态。
    const ev = await waitForCell("w-events", "失联那一行带上 MOCK", (row) => row.offlineRow && row.offlineMock > 0);
    expect(/实时窗 \d+ 条/.test(ev.meta || ""), `事件流的窗账没说出来：${ev.meta}`);
    const caps = (await liveProbe()).status.caps.events;
    return `安静 ${gone.silence_s} s · 来源 mock · 事件窗 ${ev.meta.match(/实时窗 \d+ 条/)[0]}（上限 ${caps}）`;
  });

  await check("过期不等于没数：牌子、判据那句与 −95.0 同时在画", async () => {
    const old = await waitFor(
      `${planA.display_name} 过了新鲜度线`,
      async () => {
        const row = (await liveProbe()).nodes.find((node) => node.node_id === planA.node_id);
        return row && row.age_s >= liveManifest.replay.stale_after_s ? row : null;
      },
      { timeoutMs: 30000 }
    );
    // 过期是个持久态（距今只会越来越大），所以逐块等牌子换过来再数。直接数会把"某一格
    // 还差自己的那一拍"读成"页面不肯说过期"：矩阵 200 ms、事件 500 ms、趋势轮询 1 s，
    // 同一条新鲜度线在三格里落地本来就有先后。
    const staleCells = await waitFor(
      "四格数据牌子都说过期",
      async () => {
        const rows = await probeWidgets();
        const byId = new Map(rows.map((row) => [row.instance, row]));
        return STALE_EXPECTED.every((id) => (byId.get(id) || { states: [] }).states.includes("stale"))
          ? rows
          : null;
      },
      { timeoutMs: 30000 }
    );
    const cells = staleCells;
    const stale = cells.filter((row) => row.states.includes("stale"));
    expect(stale.length >= STALE_EXPECTED.length, `说过期的格子是 ${stale.map((row) => row.instance).join("+")}，应该是 ${STALE_EXPECTED.join("+")}：${cells.map((row) => `${row.instance}:${row.states.join("/")}`).join(" ")}`);
    const m = cells.find((row) => row.instance === "w-matrix");
    expect(m.staleNote, "矩阵里没有那句带判据数字的过期说明");
    const line = new RegExp(`最后一帧距今 \\d+ s，判据是 ${liveManifest.replay.stale_after_s} s`);
    expect(line.test(`${m.meta} ${m.body}`), `那句是：${m.body.slice(0, 120)}`);
    expect(m.body.includes(`${Number(planA.expect_last[String(planA.slots[0])]).toFixed(1)} dBFS`),
      "数不在了：过期这一态把读数也一起收走了");
    expect(old.seq === planA.last_seq, `过期了却还收到新帧：seq ${old.seq}`);
    return `距今 ${old.age_s} s（判据 ${liveManifest.replay.stale_after_s} s）· ${stale.length} 块格子说过期 · 数还在`;
  });

  await shot("05-replay-mock");

  await check("切到 B：A 的读数一个都不留，B 那份录制此刻还在送帧", async () => {
    await switchSite(liveB.name);
    await selectByLabel("接入机", planB.gateway_name);
    await selectByLabel("板", planB.display_name);
    const probe = await waitFor(
      `${planB.display_name} 进窗`,
      async () => {
        const again = await liveProbe();
        return again.nodes.some((row) => row.node_id === planB.node_id) ? again : null;
      },
      { timeoutMs: 25000 }
    );
    expect(!probe.nodes.some((row) => row.node_id === planA.node_id), "切了场地，A 那块板的读数还留在窗里");
    expect(probe.nodes.every((row) => liveB.nodes.includes(row.node_id)), `窗里有 B 名单外的板：${JSON.stringify(probe.nodes.map((row) => row.node_id))}`);
    expect(probe.status.nodes === 1, `B 的窗里有 ${probe.status.nodes} 块板，这一轮只喂了一份录制`);
    expect(probe.status.frames > 0, "B 的快照一帧都没带过来");
    expect(probe.status.sources.join("+") === "mock", `换了场地来源变成 ${JSON.stringify(probe.status.sources)}`);
    const b = (await liveProbe()).nodes.find((row) => row.node_id === planB.node_id);
    expect(b.channels.some((entry) => entry.slot === planB.slots[0]), `B 的档位没进帧：${JSON.stringify(b.channels)}`);
    const text = await inPage(() => document.body.textContent);
    for (const word of [planA.display_name, planA.gateway_name]) {
      expect(!text.includes(word), `换了场地，页面上还留着 A 的「${word}」`);
    }
    return `B 窗里 ${probe.status.nodes} 块板 / ${probe.status.frames} 帧，A 的一条不在 · ${b.name} seq ${b.seq}`;
  });

  /*
    夹具库里 B 的当前布局是那份"版本超前"的备用视图（`export_workbench_fixtures.py` 故意把
    它设成 active，用来测降级渲染），它的格子实例号与 A 那份不一样。后面两条断线判据问的是
    "画着这块板的那一格说了什么"，得先有一格可对——所以把 B 切回它的值班视图，五格实例号
    与 A 一致。这一步不是判据，是把对象摆到台上；摆不上台，后面的等待自己会红。
  */
  await selectBySelector(".layout-pick", "值班视图");
  await waitFor("B 切到值班视图、五格就位", async () => {
    const ids = await widgetIds();
    return (await inPage(() => window.rftrackWorkbench.store.state.layout.name)) === "值班视图"
      && ids.includes("w-matrix") && ids.includes("w-events") ? ids : null;
  }, { timeoutMs: 20000 });

  // 断线只能这么测：数还新鲜时页面**不许**喊断线（"有数就画数"），所以那一态要由一块
  // 从没进过帧的板来说。先把服务端的扇出账取下来（这一页确实在桶里），再杀掉这台实例——
  // 热重载、退避、断线文案全是真发生过的，不是页面自己 `live.close()` 合成出来的。
  const hubBefore = await health2();
  const markDead = browser.consoleErrors.length;
  server2.stop();
  await waitFor("页面进入退避重连", async () => {
    const probe = await liveProbe();
    return probe.status.status === "retrying" ? probe : null;
  }, { timeoutMs: 25000 });

  await check("后端没了：数还新鲜时只说重连，不喊断线", async () => {
    const probe = await liveProbe();
    expect(probe.status.attempts >= 1, `竟然一次都没打算重连：${JSON.stringify(probe.status)}`);
    const m = await waitForCell("w-matrix", "元信息里说出重连第几次", (row) => /重连中（第 \d+ 次）/.test(row.meta || ""));
    const cells = await probeWidgets();
    const claimed = cells.filter((row) => row.states.includes("offline"));
    expect(!claimed.length, `这块还在画的板上喊了断线：${JSON.stringify(claimed.map((row) => row.instance))}`);
    expect(/离散频道电平，不是频谱/.test(m.meta || ""), `元信息丢了那句口径说明：${m.meta}`);
    return `第 ${probe.status.attempts} 次重连中 · 数照旧画 · 「${m.meta.slice(-40)}」`;
  });

  await check("换到一块从没进过帧的板：断线那一态与它的判据一起说出来", async () => {
    // 四态的牌子出自 `select.js`，`node-status` 那一格画的是 REST 的板清单（它没有状态牌子），
    // 所以这里问矩阵：它在"不是 live 也不是 stale"时把 `shown.text` 整句印进正文。
    await selectByLabel("板", "uart");
    const found = await waitForCell("w-matrix", "牌子上写断线", (row) => row.states.includes("offline"), { timeoutMs: 20000 });
    expect(/推送断了：重连中（第 \d+ 次）/.test(found.body), `那一格说的是：${found.body.slice(0, 140)}`);
    const status = await statusFromDom();
    expect(/重连中/.test(statusValue(status, "数据")), `状态栏「数据」那一段没跟上：${statusValue(status, "数据")}`);
    const hub = hubBefore ? hubBefore.live : null;
    expect(hub && hub.subscribers >= 1, `服务端压根没把这一页记进桶：${JSON.stringify(hub)}`);
    expect(hub.delivered > 0, `服务端一条都没送过，那"断"的是不存在过的连接：${JSON.stringify(hub)}`);
    return `「断线」＋${found.body.match(/推送断了：[^ ·]{0,18}/)[0]} · 断之前服务端记着 ${hub.subscribers} 页、送过 ${hub.delivered} 条`;
  });

  await check("断线带来的控制台报错全部指向这台被我们杀掉的后端", async () => {
    const fresh = browser.consoleErrors.slice(markDead).filter((row) => !/jsdelivr|npmmirror/.test(row));
    const unrelated = fresh.filter((row) => !new RegExp(`127\\.0\\.0\\.1:${server2.port}`).test(row));
    expect(!unrelated.length, `这些报错与断的后端无关：\n${unrelated.join("\n").slice(0, 600)}`);
    notes.push(`回放实例被主动杀掉，带来 ${fresh.length} 条资源失败，全部指向 127.0.0.1:${server2.port}`);
    return `${fresh.length} 条，全是 ${server2.origin} 的资源失败`;
  });

  // ---------------------------------------------------------------- 收尾体检
  section("收尾：控制台、异常与后端");
  await check("整轮没有未捕获异常", async () => {
    expect(browser.exceptions.length === 0, `未捕获 ${browser.exceptions.length} 条：\n${browser.exceptions.join("\n").slice(0, 800)}`);
    return "0 条";
  });
  await check("控制台报错为 0（我们自己掐断 CDN、自己杀掉回放实例造成的资源失败除外）", async () => {
    const ours = browser.consoleErrors.filter((row) => !/jsdelivr|npmmirror|net::ERR_FAILED|Failed to load resource/.test(row));
    expect(ours.length === 0, `控制台报错 ${ours.length} 条：\n${ours.join("\n").slice(0, 800)}`);
    return `0 条（另有 ${browser.consoleErrors.length - ours.length} 条是这一轮自己掐 CDN、自己杀后端造成的资源加载失败）`;
  });
  await check("阶段 A 那台实例到收尾还活着：回放那一段杀的是它自己那台", async () => {
    const health = await healthOfLive();
    expect(health, `主实例 ${server.origin} 不答话了——那一句"杀后端"杀错了进程`);
    expect(health.workbench.mounted === true, `工作台掉下来了：${JSON.stringify(health.workbench)}`);
    return `status=${health.status}，两条 WS 各归各的实例`;
  });
  await check("后端健康：结构自检齐、工作台挂着、拨号循环没跑", async () => {
    const health = await healthOfLive();
    expect(health, "主实例的健康端点没答话");
    expect((health.schema_gaps || []).length === 0, `结构不齐：${health.schema_gaps.join(",")}`);
    expect(health.workbench.mounted === true, `工作台没挂上：${JSON.stringify(health.workbench)}`);
    expect(health.dialer.enabled === false || health.dialer.running === false, "拨号循环在这一轮居然是开着的，它会在断言之前改库");
    return `status=${health.status}，workbench=${health.workbench.dir.split("/").pop()}，dialer=${JSON.stringify({ enabled: health.dialer.enabled, running: health.dialer.running })}`;
  });
}

// --- 入口与收尾 ------------------------------------------------------------------

const watchdog = setTimeout(() => {
  console.error("\n整轮超时（>600 s），强制收尾。");
  finish(1);
}, 600000);

/** 用时打出来，好让上面那个上限是量出来的而不是猜的（回放那一段自己就要走 60 s 的录制）。 */
const STARTED_AT = Date.now();

let finishing = false;
async function finish(code) {
  if (finishing) return code;
  finishing = true;
  clearTimeout(watchdog);
  const passed = results.filter((row) => row.ok).length;
  const failed = results.length - passed;
  console.log(`\n=== 汇总 ===\n通过 ${passed} / ${results.length}，失败 ${failed}，用时 ${((Date.now() - STARTED_AT) / 1000).toFixed(1)} s`);
  results.filter((row) => !row.ok).forEach((row) => console.log(`  ✗ [${row.section}] ${row.name} — ${row.evidence}`));
  const useful = notes.filter((row) => row.length);
  useful.forEach((row) => console.log(`  · ${row}`));
  writeFileSync(
    join(options.outDir, "results.json"),
    JSON.stringify({ ok: failed === 0, passed, failed, results, notes: useful }, null, 2),
    "utf8"
  );
  console.log(`截图：${SHOTS}\n明细：${join(options.outDir, "results.json")}`);
  try {
    if (cdp) cdp.close();
  } catch { /* 收尾不带退出码 */ }
  try { if (browser) browser.stop(); } catch { /* 同上 */ }
  try { if (server) server.stop(); } catch { /* 同上 */ }
  // 回放那台要么已经被判据杀掉了（断线那一条），要么这一段没跑完；两种都要收掉，
  // 否则留下的是一台还在喂录制、还在改夹具库的后端进程。
  try { if (server2) server2.child.kill("SIGKILL"); } catch { /* 同上 */ }
  await sleep(300);
  process.exit(failed === 0 ? 0 : 1);
  return code;
}

for (const signalName of ["SIGINT", "SIGTERM"]) {
  process.on(signalName, () => {
    console.log(`\n收到 ${signalName}，收尾。`);
    finish(130);
  });
}

// CDP 的应答在 websocket 回调里处理，那里抛出的东西 `run()` 的 try/catch 接不到（本脚本
// 第一次实跑就是死在这条路上：进程直接没了，后端和浏览器留成孤儿）。这两条钩子唯一的
// 作用是把"没跑完"变成一条能看见的失败，并把进程收掉。
for (const event of ["uncaughtException", "unhandledRejection"]) {
  process.on(event, (error) => {
    console.error(`\n本轮在 ${event} 里断了：${error && error.stack ? error.stack : error}`);
    results.push({
      section: currentSection,
      name: `未捕获的 ${event}`,
      ok: false,
      evidence: String((error && error.message) || error),
    });
    finish(1);
  });
}

try {
  await run();
} catch (error) {
  console.error(`\n这一轮没跑完：${error && error.stack ? error.stack : error}`);
  results.push({ section: currentSection, name: "整轮跑完", ok: false, evidence: String(error && error.message ? error.message : error) });
}
await finish(results.every((row) => row.ok) ? 0 : 1);
