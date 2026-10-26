// UI language: English stays the source language in the code; the Chinese
// strings here are applied by walking the DOM on a switch (static text) and by
// t() wherever a view writes text while rendering. Message keys may contain
// {placeholders} filled by t(). GPL-3.0-or-later.

export type Lang = "en" | "zh";

const KEY = "widscan58.lang";
let lang: Lang = "en";
try {
  const saved = localStorage.getItem(KEY);
  if (saved === "zh" || saved === "en") lang = saved;
} catch {
  /* storage unavailable */
}

export function getLang(): Lang {
  return lang;
}

/** A message in the current language; `params` fills {name} placeholders. */
export function t(key: string, params?: Record<string, string | number>): string {
  const s = lang === "zh" ? zh[key] ?? key : key;
  return params ? s.replace(/\{(\w+)\}/g, (m, k) => (k in params ? String(params[k]) : m)) : s;
}

const textSource = new WeakMap<Text, string>();
const attrSource = new WeakMap<Element, Map<string, string>>();
const ATTRS = ["title", "placeholder", "aria-label"];

export function setLang(next: Lang) {
  if (next === lang) return;
  lang = next;
  try {
    localStorage.setItem(KEY, next);
  } catch {
    /* storage unavailable */
  }
  applyLang();
}

/** Apply the language to the document and to every string already in the DOM. */
export function applyLang(root: ParentNode = document.body) {
  document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const node = n as Text;
    const raw = node.data;
    if (!raw.trim()) continue;
    if (lang === "zh") {
      const hit = zh[raw];
      if (hit !== undefined) {
        textSource.set(node, raw);
        node.data = hit;
      }
    } else {
      const src = textSource.get(node);
      if (src !== undefined && node.data === zh[src]) node.data = src;
    }
  }
  for (const el of root.querySelectorAll("*")) {
    for (const attr of ATTRS) {
      const v = el.getAttribute(attr);
      if (v === null) continue;
      if (lang === "zh") {
        const hit = zh[v];
        if (hit === undefined) continue;
        let m = attrSource.get(el);
        if (!m) {
          m = new Map();
          attrSource.set(el, m);
        }
        if (!m.has(attr)) m.set(attr, v);
        el.setAttribute(attr, hit);
      } else {
        const src = attrSource.get(el)?.get(attr);
        if (src !== undefined && v === zh[src]) el.setAttribute(attr, src);
      }
    }
  }
}

// English source -> Chinese. Technical terms (RSSI, SNR, FFT, dBFS, MHz, LO,
// VTX, GPIO, ...) stay as they are. Long keys are exact UI strings: static text
// is translated by the DOM walk, so the key must match it character for
// character. Dynamic text goes through t() with {placeholders}.
const zh: Record<string, string> = {
  // ---- Header, menu, connection -------------------------------------------
  Connect: "连接",
  Disconnect: "断开",
  Disconnected: "未连接",
  "Flash device": "烧录设备",
  Simulate: "模拟",
  "Replay…": "回放…",
  "● Record": "● 录制",
  "■ Stop recording": "■ 停止录制",
  Menu: "菜单",
  "Install the scanner firmware on a board": "把扫描仪固件安装到开发板",
  "Record the raw device stream to a .widscan58 file": "把原始设备数据流录制为 .widscan58 文件",
  Mode: "模式",
  "Monitor mode": "监视模式",
  "Lap timer mode": "计时模式",
  "Power meter mode": "功率计模式",
  "GPIO Configure": "GPIO 配置",
  "Pins the device sets at startup": "设备启动时设置的引脚",
  "Dark mode": "深色模式",
  About: "关于",
  "GitHub project ↗": "GitHub 项目 ↗",
  Firmware: "固件",
  "not connected": "未连接",
  unknown: "未知",
  "This firmware has no lap timer mode: flash a newer one.": "此固件没有计时模式，请烧录新固件。",
  "Replays are monitor mode only.": "回放只支持监视模式。",
  "Simulated device.": "模拟设备。",
  "Simulated device": "模拟设备",
  "Signals, spurs and IMD products are synthetic.": "信号、杂散和互调产物都是合成的。",
  "Lap timer mode.": "计时模式。",
  "This page polls the device as a RotorHazard server would. Disconnect before a RotorHazard server opens the port.":
    "本页会像 RotorHazard 服务器那样轮询设备。RotorHazard 服务器打开串口前请先断开连接。",
  "Replay.": "回放。",
  "Controls are disabled.": "控件已禁用。",
  "{name}: ": "{name}：",
  "Raceband scaled into {lo}–{hi} MHz (k = {k}). Widths and windows are scaled by k.":
    "Raceband 已缩放为 {lo}–{hi} MHz（k = {k}）。带宽和窗口按 k 缩放。",
  "Disconnected ({why})": "已断开（{why}）",
  "Replay finished": "回放结束",
  "Connection failed ({why})": "连接失败（{why}）",
  "Web Serial is not available: use Chrome, Edge or Opera over https or localhost.":
    "Web Serial 不可用：请使用 Chrome、Edge 或 Opera，并通过 https 或 localhost 打开。",
  "Web Serial unsupported in this browser": "此浏览器不支持 Web Serial",
  "Flashing needs Web Serial (Chrome, Edge or Opera on a computer) or WebUSB (Chrome on Android).":
    "烧录需要 Web Serial（电脑上的 Chrome、Edge 或 Opera）或 WebUSB（Android 上的 Chrome）。",
  "{v} available": "{v} 可用",
  "Reboot the device?": "重启设备？",
  "Reboot into the ROM download mode? The scanner stops until the device is reflashed or reset.":
    "重启进入 ROM 下载模式？在重新烧录或复位之前，扫描仪会停止工作。",
  Language: "语言",

  // ---- Common ---------------------------------------------------------------
  All: "全部",
  None: "无",
  Edit: "编辑",
  Clear: "清除",
  Cancel: "取消",
  OK: "确定",
  Close: "关闭",
  Save: "保存",
  Reset: "重置",
  Revert: "还原",
  Test: "测试",
  Send: "发送",
  Flash: "烧录",
  Capture: "采集",
  Running: "运行中",
  Stopped: "已停止",
  Manual: "手动",
  AGC: "自动增益",
  Full: "完整",
  On: "开",
  Off: "关",
  Raw: "原始",
  max: "最大",
  mean: "平均",
  Live: "实时",
  "Max hold": "最大保持",
  Average: "平均值",
  Nominal: "标称",
  "Band panorama": "频段全景",
  Waterfall: "瀑布图",
  "Channel zoom": "频道缩放",
  "RSSI history": "RSSI 历史",
  "Scanner telemetry": "扫描仪遥测",
  "Protocol console": "协议控制台",
  "Scanner controls": "扫描仪控制",
  "Raw I/Q snapshot": "原始 I/Q 快照",
  Channels: "频道",
  levels: "电平",
  Levels: "电平",
  Power: "功率",
  Receiver: "接收机",
  "Detection events": "检测事件",
  "Timer seats": "计时座位",
  SNR: "SNR",
  "dB SNR": "dB SNR",
  "device is not in lap timer mode": "设备不在计时模式",
  "lap timer: device restarted; setting the seats up again": "计时：设备重启，正在重新设置座位",
  "no data for 3 s: switching the device to monitor mode again": "3 秒无数据：正在把设备切回监视模式",
  RSSI: "RSSI",
  floor: "底噪",
  clear: "净底噪",
  center: "中心",
  peak: "峰值",
  "most occupied": "最占用",
  "OBW 99 %": "99% 占用带宽",
  laps: "圈数",
  "last lap": "上一圈",
  "pass peak": "过门峰值",
  "peak / nadir": "峰值 / 谷值",
  Enter: "进入",
  Exit: "退出",
  now: "当前",
  off: "关闭",
  "not polling": "未轮询",
  "error: {msg}": "错误：{msg}",

  // ---- Channel tiles, badges ----------------------------------------------
  "Choose which channels to scan": "选择要扫描的频道",
  "Scan this channel": "扫描此频道",
  "Connect a device or start the simulator.": "连接设备或启动模拟器。",
  "{n} hop per rotation · grouped LOs": "每轮 {n} 跳 · 合并本振",
  "{n} hop per rotation · one LO per channel": "每轮 {n} 跳 · 每频道独立本振",
  "{n} hops per rotation · grouped LOs": "每轮 {n} 跳 · 合并本振",
  "{n} hops per rotation · one LO per channel": "每轮 {n} 跳 · 每频道独立本振",
  stale: "数据过期",
  signal: "有信号",
  clipping: "削顶",
  "spur masked": "杂散已抑制",
  spur: "杂散",
  "filter edge": "滤波器边缘",
  "Choose which channels to measure": "选择要测量的频道",
  "Measure this channel": "测量此频道",

  // ---- Panorama / zoom / history ------------------------------------------
  "Reset holds": "复位保持",
  "Auto scale": "自动缩放",
  "Fit the dB range to the current spectrum": "按当前频谱自动调整 dB 范围",
  "Live (max of each display bin)": "实时（每个显示频点的最大值）",
  "Noise floor per FFT bin": "每个 FFT 频点的底噪",
  "RSSI window": "RSSI 窗口",
  "newest at top · live trace per rotation": "最新在上 · 每轮一条实时轨迹",
  "Waiting for spectrum frames…": "等待频谱帧…",
  "live": "实时",
  "max hold": "最大保持",
  "average": "平均值",
  "floor / bin": "底噪 / 频点",
  "Most occupied · 99 % band": "最占用 · 99% 带宽",
  "Center (video center)": "中心（视频中心）",
  "dBFS/bin · x: offset from nominal, MHz": "dBFS/频点 · x：相对标称频率的偏移，MHz",
  "Smoothed RSSI": "平滑 RSSI",
  "Raw RSSI": "原始 RSSI",

  // ---- Telemetry / console -------------------------------------------------
  "Rotation rate": "轮转速率",
  "Hop time": "跳频耗时",
  Tune: "调谐",
  DSP: "DSP",
  "Device TX queue": "设备发送队列",
  "Link throughput": "链路吞吐",
  Errors: "错误",
  "Free heap": "空闲堆",
  "Per-hop time breakdown": "每跳耗时分解",
  "last 300 hops": "最近 300 跳",
  "DSP + output": "DSP + 输出",
  "{hops} hops · {rate} hops/s": "{hops} 跳 · {rate} 跳/秒",
  "max {v} ms": "最大 {v} ms",
  "{n} samples": "{n} 个采样",
  "{n} bins": "{n} 个频点",
  "{recent} dropped in last 10 s · {drops} since boot": "最近 10 秒丢弃 {recent} · 开机以来 {drops}",
  "internal RAM": "内部 RAM",
  "{n} MiB total": "共 {n} MiB",
  "{capture} capture · {resync} resync B": "{capture} 次采集 · 重同步 {resync} B",
  "hop −{n}": "第 −{n} 跳",
  tune: "调谐",
  capture: "采集",
  total: "合计",
  "Command, e.g. TIMING?  GAIN MANUAL 45  SPURS?": "命令，例如 TIMING?  GAIN MANUAL 45  SPURS?",

  // ---- Scanner controls ----------------------------------------------------
  Scan: "扫描",
  "Group LOs": "合并本振",
  "One LO per channel": "每频道独立本振",
  "RX gain (PHY table index, not dB)": "RX 增益（PHY 表索引，不是 dB）",
  "Analog filter code (-1 = PHY auto; 60 ≈ 13 MHz, 0 ≈ 69 MHz)": "模拟滤波器代码（-1 = PHY 自动；60 ≈ 13 MHz，0 ≈ 69 MHz）",
  "FFT size (finer bins with larger FFTs, more updates per second with smaller ones)":
    "FFT 尺寸（FFT 越大频点越细，越小每秒更新越多）",
  "{n}-pt FFT ({m} samples)": "{n} 点 FFT（{m} 采样）",
  "Metrics (RSSI / SNR: only what lap timing needs, faster; the rest is left blank)":
    "测量项（RSSI / SNR：只算计圈需要的，更快；其余留空）",
  "RSSI ± kHz · search ± kHz (device units)": "RSSI 窗口 ± kHz · 搜索窗口 ± kHz（设备单位）",
  "Smoothing RSSI · channel floor (ms)": "平滑 RSSI · 净底噪（毫秒）",
  "Display spectrum: every N hops (0 off) · bins · detector": "显示频谱：每 N 跳（0 关闭）· 频点数 · 检测器",
  "RX calibration · settle µs after tune": "RX 校准 · 调谐后稳定 µs",
  "Recalibrate now": "立即重新校准",
  Cache: "缓存",
  Once: "一次",
  "Every LO": "每个本振",
  "Tune skip between full tunes (debug; skipping gain is wrong on the C5)":
    "完整调谐之间跳过的步骤（调试用；在 C5 上跳过增益是错误的）",
  "PLL only": "仅 PLL",
  "Skip RX path": "跳过 RX 通路",
  "Skip gain": "跳过增益",
  "Spectrum orientation": "频谱方向",
  "Conjugate (default)": "共轭（默认）",
  "IQ correction": "IQ 校正",
  "Display dB range (UI only)": "显示 dB 范围（仅界面）",
  Device: "设备",
  Reboot: "重启",
  "Reboot to bootloader": "重启到 bootloader",

  // ---- Raw I/Q -------------------------------------------------------------
  "Use selected channel's LO": "使用所选频道的本振",
  "LO kHz": "本振 kHz",
  "Capture a snapshot to inspect raw samples.": "采集一次快照以查看原始采样。",
  "I/Q constellation · ±{range} transferred counts": "I/Q 星座图 · ±{range} 传输计数",
  "I (blue) · Q (orange), transferred counts": "I（蓝）· Q（橙），传输计数",
  "dBFS/bin · full {rate} MHz capture span · MHz": "dBFS/频点 · 完整 {rate} MHz 采集跨度 · MHz",
  " · conjugated": " · 已共轭",
  "LO {lo} MHz · {rate} MS/s · {n} samples · scale ×{scale} (10-bit counts) · DC ({dci}, {dcq}) · RMS {rms} · peak {peak}/512{conj}":
    "本振 {lo} MHz · {rate} MS/s · {n} 个采样 · 比例 ×{scale}（10 位计数）· DC ({dci}, {dcq}) · RMS {rms} · 峰值 {peak}/512{conj}",

  // ---- RotorHazard page ----------------------------------------------------
  "Edit channels": "编辑频道",
  "Choose the seats' channels (seat order follows frequency)": "选择座位的频道（座位按频率排序）",
  "{n} seat on · poll cycle {ms} ms": "{n} 个座位开启 · 轮询周期 {ms} ms",
  "{n} seats on · poll cycle {ms} ms": "{n} 个座位开启 · 轮询周期 {ms} ms",
  "{hz} updates/s per seat": "每座位 {hz} 次更新/秒",
  "Seat {n}": "座位 {n}",
  "Seat {n} · {ch}": "座位 {n} · {ch}",
  "fastest {n}": "最快 {n} 圈",
  "Set {which} to the average level of the last second": "把 {which} 设为最近一秒的平均电平",
  "EnterAt": "EnterAt",
  "ExitAt": "ExitAt",
  "selected seat: EnterAt / ExitAt and passes": "选中座位：EnterAt / ExitAt 与过门",
  "level (dBFS = level / 4 − 63.75)": "电平（dBFS = 电平 / 4 − 63.75）",
  "level (dB = level / 4)": "电平（dB = 电平 / 4）",
  "No passes yet": "还没有过门",
  "pass": "过门",
  "pass · ignored ({s} s)": "过门 · 已忽略（{s} 秒）",
  "pass · lap {s} s": "过门 · 单圈 {s} 秒",
  "pass · peak {p}": "过门 · 峰值 {p}",
  "peak {p}": "峰值 {p}",
  "fastest {n} · {s} s": "最快 {n} 圈 · {s} 秒",
  "fastest {n} · {s} s (was {p} s)": "最快 {n} 圈 · {s} 秒（原为 {p} 秒）",
  "enter · {level}": "进入 · {level}",
  "exit · {level}": "退出 · {level}",
  "RX gain (PHY table index)": "RX 增益（PHY 表索引）",
  "LO plan": "本振方案",
  "FFT size": "FFT 尺寸",
  "Output value": "输出值",
  "Output filter": "输出滤波器",
  "Save these settings as default": "把这些设置存为默认",
  Grouped: "合并",
  "Per channel": "每频道",
  Median: "中值",
  EMA: "EMA",
  "255ms Median": "255 毫秒中值",
  "Exponential Moving Average": "指数滑动平均",
  "This device is also a ": "本设备同时也是一个 ",
  " node and automatically supports up to 8 seats if plugged into a full lap timer.":
    " 节点：接入完整的计时系统后会自动支持最多 8 个座席。",
  "Link: {reads} reads, {writes} writes, {failed} failed reads": "链路：{reads} 次读、{writes} 次写、{failed} 次读失败",
  "Speak a callout for the selected seat": "朗读所选座位的报时",
  "Clear the events and lap counts": "清除事件和圈数",
  Options: "选项",

  // ---- Detection events dialog --------------------------------------------
  Display: "显示",
  "Split Enter/Exit events": "分开显示进入/退出事件",
  "List each crossing's enter and exit, besides the passes.": "除了过门，还列出每次穿越的进入和退出。",
  "Fastest consecutive": "最快连续圈",
  "List each new fastest run. The seats show it either way.": "每次刷新最快连续圈时列出。座位卡片无论如何都会显示。",
  "Keep screen on": "保持屏幕常亮",
  "While a device is connected.": "连接设备期间。",
  "Not available: this browser has no screen wake lock.": "不可用：此浏览器不支持屏幕唤醒锁。",
  "Lap processing": "圈速处理",
  "Minimum lap time": "最短单圈时间",
  "Ignore passes sooner than this after the last one.": "距上一次过门小于此时间的过门会被忽略。",
  "Fastest consecutive laps": "最快连续圈数",
  "Each seat's fastest run of this many laps.": "每个座位这么多圈中最快的一段。",
  "Voice callouts": "语音报时",
  "Announce laps": "播报单圈",
  "Each lap's channel and time, e.g. “R4 13.7”.": "播报每圈的频道和时间，例如“R4 13.7”。",
  "Speech is not available in this browser.": "此浏览器不支持语音合成。",
  Volume: "音量",
  Rate: "语速",
  Pitch: "音调",
  " laps": " 圈",

  // ---- Channel plan dialog -------------------------------------------------
  "Channels to scan": "要扫描的频道",
  "Select up to {n} channels. Click a band name to switch to that band, or click channels in several bands to mix them.":
    "最多选择 {n} 个频道。点击频段名称可整组切换到该频段；也可以跨频段逐个点选，混合扫描。",
  "Select all of {band} (replaces the selection)": "选中 {band} 的全部频道（替换当前选择）",
  "{n} of {max} selected": "已选 {n} / {max}",
  "(maximum)": "（已达上限）",
  "The plan is full. Clear a channel first.": "已达上限，先取消一个频道。",

  // ---- GPIO dialog ---------------------------------------------------------
  "Not used": "未使用",
  Low: "低",
  High: "高",
  Reserved: "保留",
  "GPIO configuration": "GPIO 配置",
  "Pins the device sets as outputs when it starts, low or high, such as an antenna switch. A change takes effect immediately.":
    "设备启动时设为输出的引脚，高或低，例如天线切换。修改立即生效。",
  "Unsaved changes: in effect now, but the device goes back to its saved settings when it restarts. Save to keep them.":
    "有未保存的修改：现在已生效，但设备重启后会恢复为已保存的设置。点“保存”可保留。",
  "Put the saved settings back in effect": "恢复为已保存的设置",
  "Connect a device to configure its GPIOs.": "连接设备后才能配置 GPIO。",
  "This firmware has no GPIO configuration: flash a newer one.": "此固件没有 GPIO 配置功能，请烧录新固件。",
  "saved: {state}": "已保存：{state}",
  "Used by the flash or USB": "被烧录或 USB 占用",
  "Flash / USB": "烧录 / USB",

  // ---- Flash dialog --------------------------------------------------------
  "Flash firmware": "烧录固件",
  Version: "版本",
  Committed: "提交时间",
  "On the board": "板上",
  "To install": "待安装",
  "Use Chrome or Edge on a computer, or Chrome on Android with a USB OTG adapter.":
    "在电脑上使用 Chrome 或 Edge，或在 Android 上使用 Chrome 加 USB OTG 转接头。",
  "Plug the board in with a USB data cable (some cables only carry power).": "用 USB 数据线连接开发板（有些线只能供电）。",
  "New board, or one that will not connect: ": "新板或无法连接的板：",
  "hold BOOT, press and release RESET, then release BOOT. The board is now waiting to be flashed. A board that already runs this firmware is switched over automatically.":
    "按住 BOOT，按一下并松开 RESET，然后松开 BOOT。此时开发板会等待烧录。已经运行此固件的板会自动切换。",
  "Click Flash and pick the board (“USB JTAG/serial debug unit”) in the browser’s list.":
    "点击“烧录”，并在浏览器列表中选择开发板（“USB JTAG/serial debug unit”）。",
  "Make firmware available offline": "让固件可离线使用",
  Details: "详情",
  "Downloads the firmware (about 1 MB) now and with every app update, so you can flash without internet.":
    "现在下载固件（约 1 MB），并在每次应用更新时同步，以便离线烧录。",
  "Saved on this device ({version}); every app update brings the new firmware.":
    "已保存在此设备（{version}）；每次应用更新都会带来新固件。",
  "Not downloaded yet; it is tried again with the next app update.": "尚未下载；下次应用更新时会重试。",
  "Downloading the firmware…": "正在下载固件…",
  "Download failed ({msg}); it is tried again with the next app update.": "下载失败（{msg}）；下次应用更新时会重试。",
  "No internet connection, and the firmware is not on this device. Connect to the internet to flash.":
    "没有网络连接，且此设备上没有固件。请连接网络后再烧录。",
  "No internet connection, and the firmware is not on this device. Connect to the internet to flash. To flash without internet, turn on Make firmware available offline while connected.":
    "没有网络连接，且此设备上没有固件。请连接网络后再烧录。如需离线烧录，请在联网时开启“让固件可离线使用”。",
  "(unavailable)": "（不可用）",
  "Cannot flash: {msg}.": "无法烧录：{msg}。",
  "Done: {version} is installed and the board is restarting. The scanner reconnects on its own.":
    "完成：{version} 已安装，开发板正在重启。扫描仪会自动重新连接。",
  "No board selected ({msg}).": "未选择开发板（{msg}）。",
  "Flashing failed: {msg}. Do step 3, then click Flash again.": "烧录失败：{msg}。请执行第 3 步，然后再次点击“烧录”。",
  "Flashing failed: the firmware could not be downloaded (no internet connection?). Connect to the internet and click Flash again; press RESET on the board to start the firmware it has.":
    "烧录失败：无法下载固件（是否没有网络？）。请连接网络后再次点击“烧录”；按开发板上的 RESET 可启动板上现有的固件。",
  "unknown version": "未知版本",
  "no firmware is published with this page": "此页面没有提供可烧录的固件。",
  "Connecting to the bootloader…": "正在连接 bootloader…",
  "Downloading firmware…": "正在下载固件…",
  "Writing {version}…": "正在写入 {version}…",
  "Restarting the board…": "正在重启开发板…",

  // ---- Power meter ---------------------------------------------------------
  "⚠ Always connect the VTX through an attenuator": "⚠ VTX 必须经过衰减器接入",
  "Connecting a VTX directly to the board destroys its receiver. Fit the attenuator before powering the VTX, and never power a VTX with nothing on its output. Use at least the attenuation below for the VTX's power. Enter the attenuation you use below.":
    "把 VTX 直接接到开发板会烧毁接收机。给 VTX 上电前先接好衰减器，绝不要在输出端空载时给 VTX 上电。按 VTX 功率至少使用下表中的衰减量。在下方填入你实际使用的衰减量。",
  "VTX power": "VTX 功率",
  Attenuator: "衰减器",
  "At the board": "板上功率",
  Rating: "额定功率",
  "The ESP can read from about −32 dBm to {max} dBm. Note that many attenuators misreport their power reduction-- take the reported power as simply a relative power reading for your measurement setup.":
    "ESP 的可读范围约为 −32 dBm 到 {max} dBm。注意许多衰减器的实际衰减量与标称不符——请把显示的功率仅当作你这套测量装置的相对读数。",
  "Attenuator ": "衰减 ",
  " dB": " dB",
  "Smoothing ": "平滑 ",
  "Power values are approximate from a calibration with a single C5.":
    "功率值来自单个 C5 的校准，仅供参考。",
  "This board ({name}) has no power calibration: power meter mode needs an ESP32-C5 board.":
    "这块板（{name}）没有功率校准：功率计模式需要 ESP32-C5 开发板。",
  "The device is not set up for power measurement (gain, window or LO mode changed): switch modes in the menu to set it up again.":
    "设备未按功率测量配置（增益、窗口或本振模式已改变）：请在菜单中切换模式以重新配置。",
  "no signal": "无信号",
  "over range": "超量程",
  uncalibrated: "未校准",
  "at the board": "板上",
  level: "电平",
  noise: "噪声",

  // ---- RotorHazard race data (server) -------------------------------------
  "RotorHazard race data…": "RotorHazard 比赛数据…",
  "RotorHazard race data": "RotorHazard 比赛数据",
  "Show pilot names": "显示飞手名字",
  "Server address": "服务器地址",
  "Check now": "立即检查",
  "Read the current heat from the RotorHazard server and show each pilot on the matching channel, by frequency.":
    "从 RotorHazard 服务器读取当前轮次，按频率把飞手名字叠加到对应频道上。",
  "Set the server address.": "请填写服务器地址。",
  "Reading…": "正在读取…",
  "Heat {n}": "第 {n} 轮",
  "{name} · {n} pilots": "{name} · {n} 位飞手",  "Practice (no heat): no pilot names": "练习模式（没有轮次）：没有飞手名字",
  "No pilots in this heat.": "本轮没有飞手。",
  "Updated {s} s ago": "{s} 秒前更新",
  "Cannot reach {url}: {msg}": "无法访问 {url}：{msg}",
  timeout: "超时",
  "connection refused or unreachable": "连接被拒绝或不可达",
  "This page is https, so the browser blocks an http:// RotorHazard address. Open the local copy on the machine the board is plugged into (http://localhost), or use an https address.":
    "本页是 https，浏览器会拦截 http:// 的 RotorHazard 地址。请在插着板子的电脑上打开本地副本（http://localhost），或改用 https 地址。",
  "RH: {heat} · {n} pilots": "RH：{heat} · {n} 位飞手",
  "RH: {heat} · no pilots": "RH：{heat} · 无飞手",
  "RH: practice · no pilots": "RH：练习模式 · 无飞手",
  "RH: {msg}": "RH：{msg}",
  "RotorHazard pilot": "RotorHazard 飞手",
  "Enter a full http:// or https:// address.": "请输入完整的 http:// 或 https:// 地址。",
};
