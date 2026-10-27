# widscan58 版本更新日志

这份文档把**四个发布物**的每次对外变化放在一条时间线上：Android App、Web 页面、桌面版（Electron 壳）、以及 Pages 站点上可烧录的固件。工作区根目录留着同名的本地副本。

- 版本号从 0.7（App）/ 0.8（Web）起按“同一次迭代用同一个号”来推，发布标签是 `app-v<号>` 与 `web-v<号>`；`app-v0.9` 与 `web-v0.9` 指的是同一次迭代。
- 每个标签都指向 fork `onebody/widscan58` 的 main，附件的 SHA-256 与 GitHub 回填的 `assets[].digest` 逐一核对过。
- 单个版本的完整分析（架构、验证现状、风险）在 [`PROJECT-REPORT.zh-CN.md`](PROJECT-REPORT.zh-CN.md)。

---

## 0.9 — 2026-10-27

同一次迭代里的三件事：手机能自报版本并自我升级、同一页面变成 Windows/macOS 本地应用、烧录清单不再依赖打包时的快照。

### Android App `app-v0.9`（`versionCode 9` / `versionName 0.9`）

- **新增「⋯ → 关于」**：显示版本号、构建号、发布仓库与许可。12 项菜单，`⋯` 菜单截图随之重拍。
- **应用内升级**：只在按下「检查更新」时向 `api.github.com/repos/<repo>/releases` 发一次请求；取回列表后只保留带 `app-v` 前缀、非 draft、非 prerelease、且带 https APK 附件的条目（`/releases/latest` 按发布时间回答，本仓库同时发 Web，所以不能用它）。选出版本号最大、且确实高于本机的一项再下载。
- **校验后才交给安装器**：流式写入 `PackageInstaller` 会话（不落共享存储、不需要 FileProvider），边写边算 SHA-256，与该发布公开的 `digest` 对不上就地抛 `ChecksumMismatch` 丢弃；安装器拒绝、Android 8 未授权、版本比较无效率等情形各有确定结果，不会“显示已更新但实际什么都没装”。比较规则放在 `:core`（新增 `AppRelease.kt`）：逐段按数字比、短尾补零、`-dev` 之类后缀忽略。
- 新增两条权限 `INTERNET`、`REQUEST_INSTALL_PACKAGES`，**只服务于这个页面**；扫描、遥测、录制、导出仍然只走 USB，没有任何后台联网或开机自启。
- 首次升级需要两个动作：先在系统设置里允许本应用「安装未知应用」；拨开关的瞬间系统会杀掉本进程，回到应用后重新进入「关于」再按一次「安装」。
- 内置图文教程加第 16 节「关于与升级」（现 18 节 21 图）；`docs/MANUAL.zh-CN.md` 加 9.10 并改写第 16 节；两份 README 不再声称“没有申请任何权限”。
- 字符串各 333 条（中英）。

### 桌面版 `web-v0.9` — 第一次有自己的版本号与发布说明

> 桌面产物其实先上过 `web-v0.8`：0.8.0 的 dmg 与 win zip 在 2026-10-27 被追加挂进那个 release，但文件名里的版本位是 `0.8.0`、发布说明只是事后补的一段，而且那份 `widscan58-web-0.8.zip` 与它们不是同一代构建。`web-v0.9` 才是三形态按同一个版本号、同一次 `npm run build` 发布的第一轮，`web-v0.8` 的说明里已加了这条更正。

- `web-desktop/`：**Electron 45 壳 + electron-builder 26**，把同一个 `dist/` 变成 macOS Apple Silicon 的 `.app`（dmg）与 Windows x64 的免安装绿色版（zip）。零运行时依赖，主进程只有一个 `main.mjs`。
- 用自注册的 `app://widscan58/` standard+secure 协议加载页面，这样才是安全上下文、`navigator.serial`/`navigator.usb` 才存在——这是不用 Tauri 的唯一原因：WKWebView 与 WebView2 都没有 Web Serial/WebUSB。
- 补上 Electron 没有的串口/USB 设备选择对话框（session 事件 `select-serial-port`、`select-usb-device`）。
- `webSecurity: false` 放行局域网 `http://` 的 RotorHazard 叠加；RH 侧本来就带 `Access-Control-Allow-Origin: *`，因此「连板子」和「读 http 的 RH」第一次在同一份产物里同时可用，不需要隧道。
- 烧录走 Web Serial + esptool-js，与网页版完全一致。产物**未签名**：macOS 首次打开要右键 → 打开。
- 已知未定案：Mac 上出现过两次进程消失，一次确证是 macOS TAL（已用 `NSSupportsAutomaticTermination`/`NSSupportsSuddenTermination`/`NSAppSleepDisabled` 三键关掉），一次疑似渲染进程崩溃；主进程已加 `child-process-gone` 日志与崩溃重载。

### Web 页面 `web-v0.9`

- **烧录清单改为优先读线上站点**：先取 `https://onebody.github.io/widscan58/firmware/index.json`（5 秒超时、`no-cache`），成功则每个版本的固件部件都从线上绝对地址下载、SHA-256 校验不变；拿不到就整单回落到页面自带的副本。`localStorage["widscan58.fwUrl"]` 可覆盖该地址（镜像/离线测试用）。这一改动专治打包产物的固有问题：桌面壳与下载 zip 里的固件是构建时快照，而 Pages 每次 push 都会重建线上清单。
- 页面功能与 0.8 一致（中文界面与切换、8 通道卡片、全景/瀑布/zoom、RSSI 历史、RAW I/Q、协议控制台、GPIO、录制与回放、混频段频道方案、计圈页、功率计页、PWA 离线、浏览器内烧录）。
- 版本号 `0.9.0`（与 App 0.9 对齐）。

### 固件

- **本轮无代码改动**，三个页面的改动不涉及固件。
- Pages 继续按 `firmware/versions.json` 的两个 ref 构建 `esp32c5`：`official` = `upstream/main`（`9999b07`）、`rh-uart` = `origin/main`（fork main）。桌面版/网页版这次能同步到线上清单，因此刷出来的就是这两个当前版本，不必等下一份下载包。
- ⚠️ **`official` 仍不含 C5 boot 信道修复**：`esp_wifi_start()` 之后 C5-Zero 停在 5G-only + chan 36，再 `esp_wifi_set_channel(1)` 会让 boot 失败并循环报 `ERR boot`。修复在 `firmware/src/main.c` 的 `wifi_boot_channel()`（提交 `ceb5a36`），目前只在 `rh-uart` 里。要用官方版本，等上游合入之后。

### 发布物与校验

| 发布物 | 标签 | 文件 |
|---|---|---|
| Android App | `app-v0.9` | `widscan58-app-0.9-release.apk` |
| 网页版（静态目录） | `web-v0.9` | `widscan58-web-0.9.zip` |
| macOS 桌面版 | `web-v0.9` | `widscan58-web-0.9.0-mac-arm64.dmg` |
| Windows 桌面版 | `web-v0.9` | `widscan58-web-0.9.0-win-x64.zip` |
| 文档 | — | `PROJECT-REPORT.zh-CN.md`、本文件挂在两个 release 上 |

---

## 0.8 — 2026-10-27

三端第一次一起固化为可发布产物，并把“页面放在哪里才能既连板子又读 RH”这个问题答清楚。

### Web 页面 `web-v0.8`（首个独立 Web 发布包）

- 首个独立发布包 `widscan58-web-0.8.zip`：`npm run build` 的 `dist/` 原样压缩（内容在压缩包根目录），可解压到任意静态目录托管，也可本地运行。
- `web/serve.mjs`：零依赖静态服务（默认 `dist/`、`0.0.0.0:8080`）。
- **部署矩阵写进文档**：https 页面（GitHub Pages）能连板子但被混合内容拦掉局域网 `http://` 的 RH；局域网 `http://` 页面能读 RH 却拿不到 Web Serial/WebUSB；只有「本机 `http://localhost:8080/`」与「https 页面 + https 的 RH（隧道）」两种组合可用。页面在这两种受限情况下给出中英双语提示。
- RotorHazard 4.5 赛事数据叠加（heat、飞手名、profile）、混频段频道方案、固件版本选择、中文界面与切换、计圈模式改名沿用上游说法。

### Android App `app-v0.8`（`versionCode 8`）

- 频道计划支持**混频段**：从不同 band 混搭最多 8 个频道，总数不变；满额时给黄色提示而不是禁用按钮。
- README 四处与代码不符已修正（测试数 209、瀑布行高、增益上限、语言行为），中英两份同步；`MANUAL.zh-CN.md` 适用版本改为 0.8。

### 工具与文档

- 新增 `tools/rh_diag.py`：不启动 RotorHazard 服务端，直接按 4.x 发现流程逐字节诊断串口。
- `tools/check_phy_abi.sh`：`objdump` 改为“遍历候选、取第一个可运行版本”，兼容更多本机环境。
- `CLAUDE.md`、`PLAN.md` 同步（`CONFIG SAVE` 覆盖 GROUP/SINGLE、IDF 版本与目录树重写）。
- 新增 `docs/PROJECT-REPORT.zh-CN.md`——随 0.8 首次发布的项目总结报告，之后每个版本在其后续写。
- 同日追加：`widscan58-web-0.8.0-mac-arm64.dmg` 与 `widscan58-web-0.8.0-win-x64.zip` 挂进 `web-v0.8`（未签名）。它们和那个 zip 附件不是同一次构建，这个问题在 0.9 修掉。

---

## 0.7 — 2026-10-26

Android 侧从“能看的原型”变成“可以发出去的包”，并第一次带签名。

### Android App `app-v0.7`（`versionCode 7`）

- 签名 release 构建：证书 `CN=widscan58, OU=app, O=onebody`，APK Signature Scheme v2；密钥在仓库外，仓库里只留被 gitignore 的 `keystore.properties`，缺文件时照常构建、只是不签名，因此干净检出不会失败。
- **⚠️ 换签名密钥的代价**：装过 debug 包的必须先卸载（`INSTALL_FAILED_UPDATE_INCOMPATIBLE`），应用内设置随之清空，板卡设置靠重连握手读回来。
- Web 的功能补齐到手机上：功率计（衰减器表、削顶提示、校准偏移、退出时还原 `MODE`/`GAIN`）、RAW I/Q（星座、时域、1024 点 FFT）、录制与导出（`.widscan58`、频道 CSV、频谱 CSV）、计圈 4 页签、GPIO、协议控制台、扫描器遥测、RSSI 历史、自动缩放、频道计划编辑器。
- 应用内语言切换（中文 / English / 跟随系统），入口在「⋯ → 常规设置」。
- 内置图文使用教程 `⋯ → 使用帮助`：纯本地、不联网、只用系统 WebView，随 APK 打包。
- `docs/MANUAL.zh-CN.md` 操作手册。

### Web 页面

- 混频段频道方案、功率计与 RAW I/Q 视图、METRICS 三种模式与固件行为对齐（LEVELS 的读数与 SNR 语义、杂散显示方式）。

---

## 0.1 – 0.6 — 2026-10-25 ~ 10-26

### Android App `app-v0.1`（debug 包，`versionName 0.1-dev`）

- 手机端第一版：全景频谱 + 瀑布图 + 8 信道磁贴 + 频段芯片 + 增益与启停。
- `:core` 是无 `android.*` 依赖的纯 JVM 代码（线协议、重同步流解析、全景/存储数学），`:mobile` 只做 USB 认领、Canvas 绘制与字符串。
- 默认中文，Android 13+ 可按应用切英文。
- 真机验证：USB CDC 枚举、权限弹窗、`USB_DEVICE_ATTACHED` 拉起，板子以 `0x303A:0x1001` 出现。

### 0.2-dev – 0.6-dev（未单独发布标签，功能随 0.7 一起公开）

- 0.2-dev：单频道 zoom 与逐计划步进、RSSI 历史（两分钟）、扫描器遥测面板。
- 0.3-dev：磁贴上的五项读数、瀑布图深度对齐 Web、自动缩放入口移到拇指可及处。
- 0.4-dev：按 RotorHazard 板子的方式计圈、录制与回放、GPIO、协议控制台。
- 0.5/0.6-dev：功率计与功率标定进 `:core`、每次读数所依据的采样数、METRICS 精简模式实际上报的电平、应用内图文教程与手册、语言切换、签名说明。

---

## 更早（固件与 Web 的上游基线）

- 上游 `CapnBry/widscan58`：把 ESP32-C5 的 Wi-Fi 射频前端当 SDR 用的 5.8 GHz 扫描器本体——promiscuous I/Q 抓取、逐 hop 调谐/捕获/DSP/上报、METRICS/LEVELS/SPECTRUM/TELEMETRY/RAWIQ 帧、Web 全景与瀑布。
- 本地 fork 在其之上加的主线：**RotorHazard UART 双端口桥**（UART0 GP11/GP12、每端口命令与回复、8 KB 输出队列、为 C5 堆直接把回复写进驱动）、**C5-Zero 板载 WS2812 状态灯**（听到信号即亮）、**boot 信道按 band 选择**（`wifi_boot_channel()`）、计圈模式、低频段 L1–L8、GPIO 输出与配置页、旋转节流与 WFI 时钟门控。
