/**
 * DEMO 数据源。**S5 的五个演示组件里，凡是带数据的那部分都出自这里**，而且每一条读数
 * 都自带 `source: "demo"`——规范 §2.3 要求模拟数据可区分，页面右上角的 `DEMO` 角标只是
 * 这句话的可见形式，真正的凭据在数据对象上。
 *
 * 为什么不做成"后端假数据"：S5 交付的是工作台壳，`/ws/live` 与摘要桶属 S6。在这里加一条
 * 假端点，到 S6 就得先拆掉它再接真的——两遍工，还留一个可能被当成真数据的入口。
 *
 * 数值是**离散频道的 RSSI**，不是频谱采样：`rssi-trend` 画的是一条条频道随时间的变化，
 * `spectrum-view`（连续频率轴）在注册表里就标着"本阶段不支持"，别拿这里的数组冒充它。
 */

/** 8 个频道：与 `Settings.slots_per_node` 同一个事实（40+ 频道属固件 M3，不在软件阶段）。 */
export const DEMO_SLOTS = 8;

/** 每档一个可辨识的中心频率，**单位 MHz**：与 `RH.READ_FREQUENCY` 那个 u16 同量纲
 *  （`web/src/proto/rh.test.ts:6` 的 `u16(5658)`），档位表照抄仓库里已有的 Raceband 八档
 *  （`web/src/mock/mockDevice.ts:143`），不自己编第二套频道。 */
const SLOT_MHZ = [5658, 5695, 5732, 5769, 5806, 5843, 5880, 5917];

/** mulberry32：不用 `Math.random()`，为的是同一个 (seed, slot, tick) 永远给同一个数——
 *  演示要能复现，测试要能断言，随机数两者都妨碍。 */
function prng(seed) {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const round1 = (value) => Math.round(value * 10) / 10;

/**
 * 一个 (slot, tick) 的 RSSI（dBFS，量纲与协议一致：int16/100）。
 * 让第 3 档常态偏高、第 7 档偶发尖峰，是为了让"多频道矩阵"和"事件流"一眼看得出结构，
 * 而不是八条一样的噪声——演示数据也得能被读懂。
 */
export function rssi(slot, tick, seed = 1) {
  const noise = prng(seed * 1000 + slot * 37 + tick * 7)();
  const carrier = [-92, -88, -74, -95, -90, -86, -60, -97][slot % DEMO_SLOTS];
  const wobble = prng(seed * 91 + slot)() * 6 - 3;
  const burst = slot === 6 && noise > 0.82 ? 12 * (noise - 0.82) * 5 : 0;
  return round1(Math.max(-128, Math.min(0, carrier + wobble + (noise - 0.5) * 8 + burst)));
}

export function createDemoSource({ seed = 20261029, now = () => Date.now() } = {}) {
  /** 当前节拍：`refresh_ms` 是生效值，节拍跟着它走，页面重算同一个节拍得到同一批数。 */
  const tickAt = (refreshMs) => Math.floor(now() / Math.max(1, refreshMs));

  /**
   * 当前读数。`threshold` 传 `null`（= 这一格确实没设阈值）时 `active` 回 `null` 而不是
   * `false`：**"没判"与"判过且静默"是两件事**，把前者画成后者就是规范 §2.3 禁止的伪造。
   */
  function channelMatrix({ slots = [0, 1, 2, 3], threshold = null, refreshMs = 200 } = {}) {
    const tick = tickAt(refreshMs);
    const judged = threshold !== null && threshold !== undefined;
    return {
      source: "demo",
      tick,
      refresh_ms: refreshMs,
      threshold_db: judged ? threshold : null,
      channels: slots.map((slot) => {
        const value = rssi(slot, tick, seed);
        return {
          source: "demo",
          slot,
          mhz: SLOT_MHZ[slot % DEMO_SLOTS],
          rssi_db: value,
          active: judged ? value > threshold : null,
          at: tick * refreshMs,
        };
      }),
    };
  }

  /** 一段时间内的单频道序列：点数受栅格容量约束，这里只做 requested 的那一段。 */
  function series({ slot = 0, points = 60, refreshMs = 200 } = {}) {
    const tick = tickAt(refreshMs);
    const from = tick - Math.max(1, points) + 1;
    const samples = [];
    for (let index = from; index <= tick; index += 1) {
      samples.push({ source: "demo", at: index * refreshMs, tick: index, rssi_db: rssi(slot, index, seed) });
    }
    return { source: "demo", slot, mhz: SLOT_MHZ[slot % DEMO_SLOTS], refresh_ms: refreshMs, samples };
  }

  /**
   * 事件流：`level` 只有 `info|warning|alert` 三档。这里**不编造节点身份**——
   * 演示数据一旦挂上"某块板"，它和真实告警在页面上就分不出来了；也没有坐标、没有距离、
   * 没有"轨迹"，按 RSSI 编出这些是规范 §2.3 明令禁止的。
   */
  function events({ limit = 12, refreshMs = 200 } = {}) {
    const tick = tickAt(refreshMs);
    const items = [];
    for (let offset = 0; offset < limit; offset += 1) {
      const at = tick - offset * 3;
      const sample = rssi(6, at, seed);
      const noise = prng(seed * 7 + at)();
      items.push({
        source: "demo",
        event_id: `demo-${at}`,
        at: at * refreshMs,
        slot: 6 + (Math.floor(noise * 2) % 2),
        level: sample > -55 ? "alert" : sample > -70 ? "warning" : "info",
        kind: noise > 0.66 ? "activity" : noise > 0.33 ? "node_state" : "threshold",
        summary: noise > 0.66 ? "第 7 档出现持续活动" : noise > 0.33 ? "接入机重连一次" : "越过阈值",
        rssi_db: sample,
      });
    }
    return { source: "demo", items };
  }

  return { channelMatrix, series, events, tickAt, DEMO_SLOTS };
}
