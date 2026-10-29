/**
 * 当前板与四态判据：**"这一格现在该说什么话"只在这里决定一次**。
 *
 * S6 的出口判据里有四个状态要能呈现（无数据 / 过期 / 断线 / unsupported）。这四个词一旦
 * 在四个组件里各判各的，就会出现"矩阵说断线、趋势说没数据"这种同一时刻两句话的页面——
 * 而运营者据此判断的是后端坏了还是页面坏了。所以判据集中在这里，组件只负责把结果画出来。
 *
 * 判据顺序（先问"有没有主体"，再问"主体有没有数"，最后才问"为什么没有"）：
 *
 * 1. **没有选中的板** → `no_board`。四级层级下 `context.state.nodeId` 是唯一真相：它由
 *    选择器与 `node-status` 的点击共同设置，并被 `reconcile()` 保证"确实还在这一级范围内"。
 * 2. **这块板有帧** → 按新鲜度出 `live` 或 `stale`。有数就画数，哪怕连接此刻是断的——
 *    "最后一次看到的值 + 它已经不新鲜"比"整格变成断线"更有用，而连接状态另有常驻的一行在说。
 * 3. **没帧且连接不是 open** → `offline`。这时候"没有数据"的原因是连接，不是板子。
 * 4. **没帧、连接正常、但电平能力没声明** → `unsupported`。这是"未声明即不支持"（规范 §2.3）
 *    的落点：说出板端原话，不替它编一个"应该是八档"。
 * 5. 其余 → `empty`：连接是好的，这一拍确实还没有这块板的帧。
 *
 * 能力只在这里当**解释**用，不当闸：`unsupported` 只在"没数据"时才成立。真帧进来了就画真帧，
 * 声明与实测不符是板端的事，页面把它当成"没测"来隐藏才是造假。
 *
 * 组件画这段话用的是 `parts.js` 的 `empty()`/`statePill()`——牌子拿 `label`、正文拿 `text`，
 * 两边都出自这里，所以"空状态说什么"与"牌子上说什么"不会是两句话。
 */

export const STATE = {
  NO_BOARD: "no_board",
  LIVE: "live",
  STALE: "stale",
  OFFLINE: "offline",
  UNSUPPORTED: "unsupported",
  EMPTY: "empty",
};

/**
 * 每个状态在页面上的那句话，以及它该用的语气（`tone--*` 的 CSS 类）。
 *
 * `label` 是牌子上那两三个字，`text` 是能直接印出来的整句。分开是因为一块格子窄时只有
 * 84 px 高：牌子要一眼扫得出"这格现在是什么态"，而完整那句（含判据数字）要占一整行才能
 * 读顺——两处都印全句的话，正文里就会出现一模一样的两行。
 */
const VOICE = {
  [STATE.NO_BOARD]: { label: "没选板", text: "这一级里没有选中的板", tone: "muted" },
  [STATE.LIVE]: { label: null, text: null, tone: "ok" },
  [STATE.STALE]: { label: "过期", text: "最后一帧已经不新鲜", tone: "warn" },
  [STATE.OFFLINE]: { label: "断线", text: "推送断了", tone: "warn" },
  [STATE.UNSUPPORTED]: { label: "不支持", text: "这块板没声明电平帧能力", tone: "unknown" },
  [STATE.EMPTY]: { label: "无数据", text: "还没有这块板的帧", tone: "muted" },
};

/** 能力对象的原话（`services/capabilities.py` 平铺渲染，没有 `limits` 子对象）。 */
const CAPABILITY_REASON = {
  undeclared: "板端未声明",
  declared_unsupported: "板端声明不支持",
  missing_limits: "声明缺上限",
  malformed: "声明格式非法",
};

/** 当前联动的那一档频道；没联动就是 `null`，由组件决定它的回落顺序。 */
export function linkedSlot(deps) {
  const link = deps.store.state.link;
  return link && link.kind === "channel" ? link.id : null;
}

/** 这一格该读数的那块板。回落与"说不出"都由调用方按 `no_board` 处理，不在这里猜。 */
export function currentBoard(deps) {
  const id = deps.context.state.nodeId;
  if (id === null || id === undefined) return null;
  return deps.context.state.scopeNodes.find((row) => row.node_id === id) || null;
}

/** 电平帧能力：只有 `supported === true` 才算声明了。`null`（不认识）与缺失都算没声明。 */
export function levelsDeclared(board) {
  const declared = board && board.capabilities ? board.capabilities.levels : null;
  return Boolean(declared && declared.supported === true);
}

function levelsReason(board) {
  const declared = board && board.capabilities ? board.capabilities.levels : null;
  if (!declared) return CAPABILITY_REASON.undeclared;
  if (declared.reason) return CAPABILITY_REASON[declared.reason] || `板端原话：${declared.reason}`;
  return "板端声明不支持";
}

/**
 * 四态判据。回 `{ state, tone, text }`，`text` 已经是能直接印出来的句子。
 *
 * `stale` 把"多久算过期"与"距今几秒"一起说：只讲"过期"不讲判据，运营者就没法判断这是
 * 板子安静了还是页面读得慢，而那正是该由页面自己回答的问题。
 */
export function dataState(deps, board) {
  const live = deps.live;
  if (!board) return { state: STATE.NO_BOARD, ...VOICE[STATE.NO_BOARD] };
  const row = live.node(board.node_id);
  if (row) {
    if (!live.stale(row)) return { state: STATE.LIVE, ...VOICE[STATE.LIVE] };
    return {
      state: STATE.STALE,
      ...VOICE[STATE.STALE],
      text: `${VOICE[STATE.STALE].text}：最后一帧距今 ${live.ageS(row)} s，判据是 ${live.status().stale_after_s} s`,
    };
  }
  if (live.status().status !== "open") {
    return { state: STATE.OFFLINE, ...VOICE[STATE.OFFLINE], text: `${VOICE[STATE.OFFLINE].text}：${live.statusLine()}` };
  }
  if (!levelsDeclared(board)) {
    return {
      state: STATE.UNSUPPORTED,
      ...VOICE[STATE.UNSUPPORTED],
      text: `${VOICE[STATE.UNSUPPORTED].text}（${levelsReason(board)}），这一格没有可画的数据`,
    };
  }
  return {
    state: STATE.EMPTY,
    ...VOICE[STATE.EMPTY],
    text: `${VOICE[STATE.EMPTY].text}：连接正常，但这一拍里没有这块板的帧（板子没发、或还没归属到节点行）`,
  };
}
