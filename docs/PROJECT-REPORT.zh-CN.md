# Widscan58 本地版本项目总结报告

- 报告日期：2026-10-27（0.9 版更新）
- 分析对象：`widscan58-clone/` 下当前已有的全部代码、文档与构建产物
- 报告性质：随版本发布提交到 `repo/docs/PROJECT-REPORT.zh-CN.md`，同时作为 Release 的说明底稿；逐版变更列表见 [`CHANGELOG.zh-CN.md`](CHANGELOG.zh-CN.md)
- 分析方式：逐模块读源码与文档；实际运行固件 host 测试、Web 单元测试、Android core 单元测试；检查 APK 元数据与签名
- 未做：烧板、射频测量、真机 UI 操作、Web e2e、固件完整构建（固件由 Pages CI 在发布提交上构建）

---

## 0. 摘要

Widscan58 是一个把 **ESP32-C5 的 Wi-Fi 射频前端当作 SDR** 使用的 5.8 GHz FPV 频谱/RSSI 扫描器。它通过 esp-sdr 找到的原始 I/Q 抓取路径读取采样，轮流扫描最多 8 个 FPV 频道，输出 RSSI、噪声底、SNR、信号中心频率、99% 占用带宽和整段频谱；同时还能作为 **RotorHazard 计时节点**、**浏览器单机计圈器** 和 **VTX 功率计**。

当前本地版本由三块组成：

| 固件 + Web + 桌面壳 + 工具 + 项目文档 | `repo/`，git 仓库，分支 `rh-uart-bridge` @ `63ec23c`（已合入 fork main） | 领先上游 `CapnBry/widscan58` 43 个提交、落后 4 个 | 固件源码（`firmware/src`）约 5,900 行；Web 约 10,200 行（TS 9,800 + 脚本 420）；桌面壳主进程 142 行；工具 1,441 行；项目 Markdown 4,645 行 |
| Android 原生 App | `app/`，独立 git 仓库，`master` @ `b03b8cc` | `0.9`（versionCode 9） | Kotlin/Java 15,825 行；`:core` 30 个主源文件、27 个测试类；App 侧文档 942 行 |
| Android 发布包 | `widscan58-app-0.9-release.apk` | 约 1.5 MB，已用 v2 方案签名 | 包名 `com.onebody.widscan58`，minSdk 26 / targetSdk 36 |
| Web 发布包 | `widscan58-web-0.9.zip` | 构建产物 `dist/` 的压缩包，48 个文件 / 54 条目 | 版本 `0.9.0`，解压到任意静态目录；`serve.mjs` 在源码目录里，不随 zip 分发 |
| 桌面版 | `repo/web-desktop/`，产物在其 `release/` 下 | 版本 `0.9.0`，macOS Apple Silicon dmg + Windows x64 免安装 zip，**未签名** | Electron 45 壳，零运行时依赖；约 120 MB / 154 MB |

自动测试共 **282 项**（0.8 发布时实跑；0.9 这轮的复跑见 7.1）：

| 层 | 测试数 | 本次结果 |
|---|---:|---|
| 固件 host 测试（Unity） | 17 | 17/17 通过，2.76 s |
| Web 单元测试（Vitest） | 56 | 56/56 通过，10 个测试文件 |
| Android `:core`（JUnit） | 209 | 26 个测试类，0 失败、0 错误 |

**总体判断**：三端功能已经闭环，工程完成度高；固件在真机上做过大量时序与射频测量，Web 有浏览器内烧录/PWA/模拟器，Android 已到可发布的 0.8。当前最主要的不是“缺功能”，而是**工程治理问题**：私有 PHY ABI 与 C5 内存余量脆弱、固件发布不可复现、UART/真机飞行验收缺少正式记录、Android 独立仓库没有 remote（依赖手工镜像）、若干文档已滞后。

---

## 1. 本地版本与仓库状态

### 1.1 工作区的真实拓扑

工作区里不是“一个仓库”，而是两个 git 仓库加一个发布包：

```text
widscan58-clone/
├── repo/                  # 主仓库：固件、Web、桌面壳、工具、文档；Android 的只读镜像
│   ├── app/               # 根目录 app/ 的内容镜像（无 .git）
│   ├── firmware/          # ESP32-C5 / ESP32-S3 / native host tests
│   ├── web/               # Vite + TypeScript（含 serve.mjs 本地静态服务）
│   ├── web-desktop/       # Electron 45 壳：main.mjs + electron-builder 配置 + release/ 产物
│   ├── tools/             # Python/shell 工具
│   ├── docs/              # RotorHazard 集成文档、更新日志、本报告、内部设计/计划
│   ├── ref/RotorHazard/   # 参考检出，未跟踪
│   ├── PLAN.md CLAUDE.md README.md
│   └── .github/workflows/pages.yml
├── app/                   # Android 独立仓库（有 .git、keystore.properties、mipmap 图标）
├── PROJECT-REPORT.md      # 本报告的本地副本
├── widscan58-app-0.9-release.apk
├── widscan58-web-0.9.zip
└── widscan58-web-0.9.0-{mac-arm64.dmg,win-x64.zip}
```

`repo/app` 与根目录 `app/` 的源码、资源、README、MANUAL **内容一致**；差异只在 `.git`、构建产物、`keystore.properties`、根目录多出的空 `res/mipmap/`，以及 `gradlew.bat` 的文件差异（`diff -rq` 核对）。这意味着 Android 代码在总仓库里是一份**手工镜像**，不是 submodule，也不是构建时生成。

`repo/web-desktop/dist/` 与 `repo/web/dist/` 是同一份产物（`npm run sync-dist` 拷贝），两者都被 gitignore；`web/public/firmware/` 同样被忽略，是发布前从线上 Pages 取回的固件副本（见 11.2）。

### 1.2 分支与提交

`repo/`：

- 发布分支 `rh-uart-bridge`，HEAD `63ec23c`（把 fork main 合回来：README 的 Pages 链接改成本站、App 0.9 的镜像）。
- 相对 `origin/main`（上游 `CapnBry/widscan58`）领先 **43** 个提交、落后 **4** 个——上游这 4 个是 `fa948b2`（扫描任务每秒让出一次）、`b5b2f6a`（遥测按整轮统计旋转率）、`09c5905`（扫描任务最后启动）、`9999b07`（计圈传 PLATEAU 容差）。**上游仍不含 C5 的 boot 信道修复**，见 11.4。
- 相对 `fork/main`（`onebody/widscan58`）内容一致：`repo/` 侧 `firmware/`、`web/`、`web-desktop/`、`tools/`、`docs/`、`app/` 六个目录的 tree hash 与 fork main 逐一相同（`git rev-parse` 对比）。发布时用 merge 合入，不重写历史、不 force push。
- 提交作者：上游 Bryan Mayland 55 个，fork 作者 onebody 43 个（`git shortlog`）。
- 历史里有同标题重复提交（如 `24b05e7` / `4d72509`，均为 “web: open the lap timer page for settings saved as 'rh'”），是合并/拣选留下的痕迹，不影响代码但会让审计变吵。

相对上游的本地增量按主题分：

| 主题 | 代表提交 | 内容 |
|---|---|---|
| Android App | `7327c1f` … `a00627c`、`434e899`、`3c21764`、`ac736e6` | 引入手机端、补到 0.7、中文手册、签名说明、频道方案混频段、关于页与应用内升级 |
| 桌面版 | `27b5447` | Electron 壳（`app://` 安全上下文、串口/USB 选择器、局域网 http 的 RH）、产物打包 |
| RotorHazard UART | `82c9a6c` … `ae4319b` | 设计/计划、第二 sink、UART0 GP11/GP12、每端口命令与 RH 状态、堆内存修复 |
| 计圈改名 | `74b3c34`、`3b97626`、`24b05e7`/`4d72509` | “RotorHazard mode” → “lap timer mode”，NVS blob 从 `rh` 改为 `timer` |
| 混频段 | `61aa3e2`、`3c21764` | Web/App 允许从不同 band 混合选最多 8 个频道 |
| 状态灯与 boot 信道 | `ceb5a36` | C5-Zero 板载 WS2812 指示“听到信号”；`wifi_boot_channel()` 修 5G-only 启动 |
| 固件版本选择 | `423e007`、`19a56af`、`482e6ce`、`39d7339` | Pages 按 `versions.json` 构建多个固件版本并在烧录对话框中选择 |
| 烧录清单线上同步 | `27b5447` | 烧录器优先读线上 `firmware/index.json`，离线回落内置副本 |
| Web 中文与 RH 数据 | `e3206ed` | Web UI 中文、RotorHazard 4.5 赛事数据、固件版本选择 |
| Web 本地读 RH | `1c43903` | https 页面的混合内容提示、`serve.mjs` 本地静态服务、两种可用部署组合写入 README |

`app/`（独立仓库，`master`）：HEAD `b03b8cc` “Release 0.9”，功能提交 `e0f3557`；工作区干净。这轮 fork 侧的镜像提交是 `ac736e6`，标签 `app-v0.9`。

### 1.3 各轮发布固化的本地改动

0.9 这轮两个仓库的工作区都是干净的（没有未提交内容），改动即下表；逐条变更列表在 [`CHANGELOG.zh-CN.md`](CHANGELOG.zh-CN.md)，发布细节在 §11。

0.8 轮次固化的内容：

| 文件 | 变更 |
|---|---|
| `tools/check_phy_abi.sh` | `objdump` 选择改为“遍历候选、取第一个可运行版本”，提高本机兼容性 |
| `tools/rh_diag.py` | 新增 185 行 RH 串口诊断工具：不走 RotorHazard 服务端，直接按 4.x 的发现流程逐字节校验 |
| `web/package-lock.json` | `npm install` 造成的依赖元数据更新（删除过期的 `libc` 字段） |
| `CLAUDE.md`、`PLAN.md` | 文档同步：`CONFIG SAVE` 覆盖 GROUP/SINGLE、PHY ABI 参照说明、源码目录树重写 |
| `app/*.md` | Android 文档镜像（与独立仓库同步） |
| `web/package.json`、`web/serve.mjs`、`web/README.md` | Web 版本号 `0.8.0`、本地静态服务与部署说明（`1c43903` 已提交） |
| `docs/PROJECT-REPORT.zh-CN.md` | 本报告 |

`app/` 侧：`mobile/build.gradle.kts` 升至 `versionCode 8` / `versionName "0.8"`，`README.md`、`README.zh-CN.md`、`docs/MANUAL.zh-CN.md` 的修正与版本号同步，随 `app-v0.8` 发布。

### 1.4 构建产物

0.9 的四个发布包（都已在根目录，SHA-256 由 `shasum -a 256` 实算）：

| 文件 | 字节 | SHA-256 |
|---|---:|---|
| `widscan58-app-0.9-release.apk` | 1,576,961 | `1bd2ea03f8d0c8a4cd77d829f3adaf70da6c17e6e0d7474b24fff780405ad0ee` |
| `widscan58-web-0.9.zip` | 1,163,822 | `31e147a7cc3c1a3bb9cfdfdbda47d85a7628120f77476cdc01ed1476a04eeebf` |
| `widscan58-web-0.9.0-mac-arm64.dmg` | 121,504,090 | `708643914bd90e6b9f5137ec840258d9957c630adaefbed997060ce440ebee3a` |
| `widscan58-web-0.9.0-win-x64.zip` | 154,266,380 | `8f424e08864bf3f07dbb74d18ff674871a6a2bc0de9bd7efc8e1026954a5e2fc` |

APK（`aapt2 dump badging` + `apksigner verify` 实跑）：

- `package: com.onebody.widscan58`，`versionCode 9` / `versionName 0.9`，minSdk 26 / targetSdk 36 / compileSdk 36
- v2 签名有效，证书 DN `CN=widscan58, OU=app, O=onebody, C=CN`，证书指纹 `4b68a113…b4a37d`（与 0.7、0.8 同一密钥，可覆盖安装）
- `uses-permission` 含 `INTERNET`、`REQUEST_INSTALL_PACKAGES`
- 签名密钥在仓库外，`app/keystore.properties` 只保存路径与口令，且被 `.gitignore` 排除

Web zip 与两个桌面包**是同一次 `dist/` 构建**：入口 bundle 都是 `assets/index-DSC5v4fL.js`，包内固件副本都是 `official 9999b07` + `rh-uart app-v0.9-1-g5bf32a5`（发布前从线上 Pages 取回并逐件按 manifest 的 SHA-256/字节数校验，见 4.10）。桌面包版本由 `plutil` 读 `Info.plist` 与 asar 内的 `package.json` 确认为 `0.9.0`。

对比 0.8 的教训：`widscan58-web-0.8.zip` 里是上一代 bundle `index-rlcnjTLh.js`（还没有固件清单线上同步），而同一 release 后来补挂的桌面包已经是新一代——附件不同代是这次把 Web/桌面一起 bump 到 0.9 的直接原因。

`widscan58-app-0.8-release.apk`（1,540,474 字节，`24c799865d3b428c1ebe5f87c32ddcecf702e7724ec05eb637c5dbd94fefed1e`）与 `widscan58-web-0.8.zip`（1,159,362 字节，48 个文件 / 54 条目，`aa64461a47c3b30a05062d034caeb285d6cbaf9c8991107e74b3f985acf2ec82`）留在 `app-v0.8` / `web-v0.8` 两个 release 上，仍可下载。

---

## 2. 系统架构

### 2.1 总体拓扑

```text
                5.8 GHz VTX / 环境信号
                          |
                 [ESP32-C5 Wi-Fi PHY]
                 原始 I/Q（adctrig 调试通路）
                          |
                +---------+----------+
                |  固件 scanner 任务  |
                |  profile/planner   |
                |  tune→capture→DSP  |
                |  指标 / 计圈 / 组帧 |
                +----+----------+----+
                     |          |
              USB CDC/JTAG    UART0 GP11/GP12
              （帧 + 命令）    （RH 字节流，可选）
                     |          |
        +------------+--+       +--> RotorHazard 服务端（树莓派）
        |               |
   Web Serial        Android USB Host
   （浏览器）          （原生 App）
        |
   WebSocket 桥 / 模拟器 / 回放
```

固件是唯一的“真相源”；Web 与 Android 都只是同一套协议的宿主。三端共享的核心语义：

- 频道计划：最多 8 个频道，可跨 band 混合，按频率排序。
- 扫描模式：`GROUP`（40 MHz 内的频道共享 LO）或 `SINGLE`（每频道独立 LO）。
- 指标模式：`FULL`（完整 METRICS 帧）、`RSSI`、`SNR`（紧凑 LEVELS 帧）。
- 宿主协议：`MONITOR`（自定义命令 + 二进制帧）或 `RH`（RotorHazard node 协议，API level 36）。
- 设备开机默认 `RH` 且静默，直到主机发 `PROTOCOL MONITOR`；RotorHazard 探针可把端口重新拿回。

### 2.2 统一线协议

主机 → 设备：ASCII 命令行，以 `\n` 结尾。
设备 → 主机：ASCII 回复行与二进制帧交错。

二进制帧（`firmware/src/proto/frames.h:7`，Web/Android/Python 各自镜像）：

```text
u8 0xA5 | u8 'R' | u8 type | u8 version | u16 len | payload[len] | u32 crc32(zlib)
```

| type | 名称 | 用途 | 固定头/记录 |
|---:|---|---|---|
| 1 | METRICS | 每 hop 的 8 通道完整指标 | 34 B 头 + 26 B/通道 |
| 2 | SPECTRUM | 抽取后的显示频谱 | 16 B 头 + bins |
| 3 | TELEMETRY | 250 ms 遥测 | 59 B |
| 4 | RAWIQ | 原始 I/Q 分块 | 14 B 头 + 1024 样本/块 |
| 5 | LOG | 已声明但未发送 | — |
| 6 | LEVELS | RSSI/SNR 模式的紧凑电平 | 32 B 头 + 4 B/通道 |

`frames.h:118-137` 用 `_Static_assert` 锁定了这些尺寸，Web、Android、`widscan58cli.py` 三处硬编码因此有编译期约束。

### 2.3 关键设计决策

| 决策 | 体现 | 评价 |
|---|---|---|
| 频段参数全部进 profile | `c5-race` / `s3-race24`，DSP 与 UI 只认 Hz | 让 C5 与 S3 共用扫描、协议、UI；新增 band 只需改 profile |
| 自定义二进制帧 + ASCII 命令 | 帧紧凑、命令可人工调试 | 录制的帧格式随固件版本变化，回放天然不跨版本 |
| 开机静默在 RH 模式 | 符合 RotorHazard 对节点的期待 | 代价是宿主必须先切协议；Web/App/CLI 都已自动处理 |
| 计圈在板端完成 | 255 ms 中值/EMA，过门检测不依赖轮询 | RH 100 ms 轮询不影响过门时间精度 |
| Web 无框架、原生 DOM/Canvas | 依赖极少，构建产物小 | 视图代码量大，但可预测、无运行时框架开销 |
| Android `core` 纯 JVM | 帧解析、Store、RH 模型可在主机测试 | 209 个测试的主要基础；`mobile` 层没有自动测试 |
| 浏览器内烧录 + PWA | esptool-js + service worker 缓存固件 | 现场无需工具链；代价是发布流程更复杂 |

---

## 3. 固件

### 3.1 构建与资源约束

- 构建系统：PlatformIO + pioarduino `55.03.312-1`，固定 ESP-IDF **5.5.5**（`firmware/platformio.ini:3,13`）。
- 三个环境：`esp32c5`（默认、真实 5.8 GHz）、`esp32s3`（2.4 GHz 缩放模拟目标）、`native`（host 测试）。
- Slim build：只编译 `src` 与 PlatformIO 的 `__pio_env`，其余 IDF 组件按 `REQUIRES` 拉取（`firmware/CMakeLists.txt:24-29`、`firmware/src/CMakeLists.txt:35-39`）。文档称 C5 只构建 108 个组件中的 47 个（`CLAUDE.md:85-95`）。
- Flash 4 MB，单 `factory` 分区，应用从 `0x10000` 起约 1.94 MiB，**没有 OTA/回滚**（`firmware/partitions.csv`）。
- C5 把 `0x40820000–0x40840000` 的 128 KiB 留给 RF dump，链接器断言静态数据不得越界（`firmware/src/radio/c5/sram_guard.ld`、`radio_c5.c:27-35`）。静态 RAM 只剩几百字节，启动后堆约 5 KB（`CLAUDE.md:107-109,293`）。
- 为省 RAM/IRAM：Wi-Fi 只以 `WIFI_MODE_NULL` 启动 PHY；关闭控制台、日志、SoftAP/WPA3/OWE；扫描热路径放 IRAM；RMT/LED 驱动留在 flash。

### 3.2 启动与任务

`app_main`（`firmware/src/main.c:66-104`）顺序：NVS → GPIO 输出 → 状态灯 → link → event loop → Wi-Fi `WIFI_MODE_NULL` + promiscuous → 固定启动信道 → radio → RH → scanner → commands。

| 任务/定时器 | 栈/优先级/核 | 作用 |
|---|---|---|
| `scan` | 8192 / 4 / core0（`scanner.c:729`） | 扫描循环、RAWIQ、FFTEVAL、节流 |
| `link` | 4096 / 5 / 末核（`link.c:62`） | USB 输出队列 |
| `cmd`、`cmd_uart` | 各 6144 / 6 / 末核（`commands.c:721-724`） | 每端口一条命令任务 |
| 状态灯定时器 | 50 ms | WS2812 状态 |
| 遥测 | 250 ms | TELEMETRY 帧 |
| 节流定时器 | 一次性 | 旋转速率上限 |

启动失败不会 reboot-loop，而是在主机发话后每秒打印 `ERR boot <step> <error> heap=… largest=…`（`main.c:37-50`），便于现场诊断。

### 3.3 扫描流水线

一次 hop 的顺序（`scanner.c:369-406`）：

1. 若 LO 变化，`radio_tune(lo)`。
2. `radio_capture()`，等待剩余的 `SETTLE`（默认 100 µs）。
3. **立刻 tune 下一个 hop 的 LO**，让稳定时间与本次 DSP 重叠。
4. `psd_compute(..., conjugate=true)`：Hann 窗、Welch 平均、IQ 校正。
5. 应用 spur mask，计算 RSSI/噪声/SNR/峰值/质心/OBW/EMA。
6. 计圈模式更新每 seat 的电平与过门状态。
7. 组 METRICS 或 LEVELS 帧；每 N 个 hop 组一个 SPECTRUM 帧。
8. 按 `THROTTLE` 等到本旋转的截止时间。

planner 的规则（`scan/planner.c:21-95`）：

- 频道按频率排序，以 `hop_max_span_khz` 贪心成组。
- LO 候选包括单通道偏移、相邻频道间隙中点、可达频段端点。
- 选择“离最近频道最远”的候选，尽量避开频道中心。
- C5 Raceband 默认 4 个 LO：5677 / 5751 / 5825 / 5899 MHz，频道在 −19 / +18 MHz（`scan/profile.c:61`）。

默认扫描参数（`scanner.c:94-129`）：

| 参数 | 默认 |
|---|---|
| 采样率 | C5 80 MS/s，S3 40 MS/s |
| 捕获/FFT/段数 | 4096 样本 / 1024 点 / 4 段 Welch |
| 增益 | 手动 index 45 |
| 指标 | `FULL` |
| RSSI / 搜索窗 | C5 ±5 MHz / ±12 MHz |
| EMA | RSSI 100 ms，空信道底 2000 ms |
| 信号门限 | 6 dB |
| 占用平均 | `AVG 7`（2⁻⁷/跳，约 2.3 s） |
| 显示频谱 | 每 4 跳，256 bin，MAX |
| FFT 实现 | S16R（16 位舍入，块浮点） |
| 旋转上限 | 1050 次/s |

### 3.4 DSP 与指标

- Welch：`floor(n/nfft)` 段、不重叠、每段去均值、最多 64 段，ENBW 按 Hann 窗计算（`dsp/psd.c:399-450`）。
- FFT 类型：F32-R2、F32-R4、S16、S16R。C5 无 FPU，浮点 FFT 约 68 ms/hop；默认 S16R 约 3.65 ms DSP。
- IQ 校正：按 LO 保存 I/Q 二阶矩并做指数更新，修正 `q' = c1·q + c2·i`，对增益比和相关系数设门限（`psd.h:49-59`、`psd.c:319-339`）。
- 噪声底：通带内有效 bin 的 20% 分位，再用百分位偏差校正为均值；RSSI 是窗口功率和/ENBW；SNR = RSSI − floor（`metrics.c:55-69`、`scanner.c:191-192`）。
- 峰值/中心/99% OBW 先按 hop 求，再做时间平均（权重 2⁻shift）；无信号时不上报中心（`metrics.c:101-141,187-220`）。
- 固件**没有全局 max-hold**；`SPEC` 的 MAX 只是显示抽取时每个 bin 取最大。

### 3.5 命令与协议模式

命令分族（`proto/commands.c`，约 40+ 条，含参数化调试命令）：

| 类别 | 代表命令 |
|---|---|
| 身份/协议 | `INFO`、`PROFILE?`、`STATUS?` / `CONFIG?`、`PROTOCOL MONITOR|RH` |
| 计划 | `CHANNELS`、`CHANNELS DEFAULT`、`ENABLE`、`MODE GROUP|SINGLE` |
| 扫描 | `SCAN START|STOP`、`THROTTLE`、`SETTLE`、`TUNESKIP` |
| 射频 | `GAIN`、`FILTER`、`RATE`、`RECAL`、`RECALMODE`、`REGWATCH` |
| 采集 | `FFT`、`CAPN`、`SEGMENTS`、`IQCORR` |
| 测量 | `METRICS`、`EMA`、`AVG`、`SPEC`、`SPURS` |
| 请求 | `RAWIQ`、`FFTEVAL`、`TIMING?` |
| 板级 | `GPIO`、`GPIO SAVE`、`REBOOT`、`BOOTLOADER` |

两种模式各自保存扫描配置，切换时恢复：

- `PROTOCOL MONITOR`：应用 `METRICS FULL`，开始发二进制帧。
- `PROTOCOL RH`：应用 `CONFIG SAVE` 保存的计圈默认值（增益、FFT、指标、GROUP/SINGLE、滤波）。未保存时：`FFT 64`（256 样本）、`METRICS RSSI`、`MODE GROUP`、`GAIN MANUAL 45`、`LEVELFILTER MEDIAN`（`commands.c:120-137`）。
- RH 模式只放行白名单命令，其余返回 `ERR rh_mode`（`commands.c:244-256`）。

### 3.6 RotorHazard 节点与 UART

- 实现 RotorHazard 串口节点协议 **API level 36**，单端口 8 个 seat，等同 S32_BPill 的 8 接收机（`proto/rh.c`）。
- 读命令 ≤ `0x50`、写命令 > `0x50`；大端；sum8 校验；写超时 100 ms。
- 两个物理端口：USB CDC 与 C5-Zero 的 UART0（GP11 TX / GP12 RX，115200 8N1）。两个端口有独立的行解析、写状态与 seat 选择，seat 电平共享；帧与遥测**只走 USB**，不污染 RH 的字节流（`proto/link.h:8-9`、`uart_link.c`）。
- 射频归属：UART 最近 2 s 有流量时，USB 的 `PROTOCOL MONITOR` 返回 `ERR uart_rh`，UART 自己返回 `ERR uart_port`；静默 2 s 后 Web 可拿回。
- 电平：RSSI/SNR 编码为 1–254，0.25 dB 步进；默认 EnterAt/ExitAt = 125/105（−32.5/−37.5 dBFS）。
- 过门：默认 255 ms 运行中值（RotorHazard 节点同款）或 EMA（3 点中值 + τ=2.72 ms）；过门时间取“最高平台”的中点（`scan/lap.c:96-108`）。
- UART sink 没有环形队列：C5 启动后只剩约 5 KB 堆，8 KB ring + writer task 曾让 `commands_task_start` 的 3 KB 回复缓冲分配失败，板子“活着但不回话”；现在回复直接在 `uart_write_bytes` 上阻塞写出（`CLAUDE.md:292-296`、提交 `ffc2490`）。

### 3.7 芯片与硬件适配

| 项 | ESP32-C5 | ESP32-S3 |
|---|---|---|
| 频段 | Raceband 5.8 GHz，可扩到 A/B/E/F/L | 2.4 GHz 线性缩放模拟（k = 83.5/296） |
| LO 步进 | 1 MHz | 1 kHz |
| 采样率 | 80/40/20/10/8/4 MS/s | 80/40/16 MS/s |
| 校准 | 每 LO 缓存（`RECALMODE CACHE` 默认），16 槽 | `ONCE`，`CACHE` 等同 `EVERY` |
| 重调 | PLL-only（`TUNESKIP 3`），全 channel setup 只在重新校准后 | 每 200 ms 强制全调（`TUNESKIP 7`） |
| ADC 路径 | 每次 tune 后强制 ADC rate 1 + RX filter mode 8 | 不适用 |
| 杂散 | 5600/5712/5760/5920 MHz | 2400/2440/2448/2480 MHz |
| 特殊外设 | 状态灯、UART、REGWATCH、PHYIDLE | 无 |

- **状态灯**：C5-Zero 的 GP27 通过 RMT 驱动 WS2812；首次听到信号亮绿 1 s，持续时 100 ms 亮/300 ms 灭，最后一次信号后 2 s 熄灭；XIAO 的同脚黄色 LED 同步（`status_led.c`）。
- **GPIO 输出**：`GPIO <n> LOW|HIGH|NONE` 立即生效，`GPIO SAVE` 存 NVS，启动时在 radio 之前应用；GP11/12、GP27、USB、flash 引脚标记为不可用（`gpio_out.c`）。
- **R8**：文档实测 5917 MHz 可用，但由于 5831 MHz 以上的 ADC rate/数字滤波切换，R8 有约 1.3–1.7 dB 的倾斜（`PLAN.md:1122`）。
- **S3**：仅开发目标；文档称已合并共享改动但近期未重新烧写/复跑（`PLAN.md` 状态段）。

### 3.8 性能（文档实测）

以 C5、8 通道、4 个 pair LO、4096 样本 @ 80 MS/s、4×1024 Welch 为基准：

| 配置 | Hop | 每通道更新率 |
|---|---:|---:|
| 默认 S16R / FFT 1024 | ~4.0 ms | ~60 /s |
| S16R / FFT 256 | ~2.1 ms | ~122 /s |
| S16R / FFT 128 + METRICS SNR | ~0.73 ms | ~318 /s |
| METRICS RSSI / FFT 64（4×64） | ~0.51 ms | ~464 /s |
| RH 8 seats / 4 LO | ~1.96 ms/seat | ~510 /s |

其他已记录的实测：

- C5 单次 PLL-only 重调约 225–234 µs，其中约 2/3 是模拟 I2C 与固定延时。
- 旋转节流后 USB 电流约 92–93 mA；`PHYIDLE 4` 在 1100 /s 时约 80.5 mA。
- RH 8 seats、60 s、同频测试：圈时差中位数 2 ms、最大 2 ms（`CLAUDE.md:327-335`）。

这些数字来自 `CLAUDE.md` / `PLAN.md` 的历史测量，本次未复测。

### 3.9 固件测试

`cd firmware && pio test -e native`，Unity，17 项：

- R4 FFT 对 DFT 的置换、精度、满幅不溢出。
- `metrics_db`、`code_q8`、单音 RSSI/峰值/质心/OBW、占用平均、LEVELS 编码。
- 计圈过门、滤波、窗口、历史、reset、EMA。
- planner 分组、默认 Raceband 计划。
- S3 缩放频段映射。

**未覆盖**（重要空白）：`psd_compute` 整体、Welch/S16R/S16/F32、IQ 校正、scanner 任务与节流、EMA/频谱/遥测、C5/S3 射频驱动、帧/CRC/link、命令解析、RH 状态机、UART、LED/GPIO、FFTEVAL。

---

## 4. Web 前端

### 4.1 技术栈与构建

- Vite 8.3.3 + TypeScript 7.0.2（strict、ES2022），**无 UI 框架**，原生 DOM + Canvas。
- 运行依赖只有 `esptool-js` 与 `spark-md5`；测试用 Vitest 5，e2e 用 puppeteer-core 25。
- `npm run build` = `tsc --noEmit && vite build`；`base: "./"`，可部署在 GitHub Pages 子路径。
- `vite.config.ts` 内的 `pwaPlugin` 在构建时生成 `dist/sw.js`，把页面文件、烧录代码、固件索引和缓存名哈希写进去。

### 4.2 模块划分

| 层 | 文件 | 职责 |
|---|---|---|
| `serial/` | `parser.ts`、`device.ts`、`webusb.ts`、`rhLink.ts`、`flasher.ts`、`recorder.ts` | 流解析/CRC、串口或 WS 传输、Android WebUSB、RH 客户端、esptool-js、录制 |
| `proto/` | `frames.ts`、`rh.ts` | 二进制帧与 RH 协议常量 |
| `model/` | `store.ts`、`app.ts`、`timer.ts`、`laps.ts`、`power.ts`、`rhServer.ts`、`channelPlan.ts`、`gpio.ts` | 设备状态、模式、计圈、圈规则、功率、RH HTTP 赛事数据、频道方案 |
| `views/` | 14 个视图 | 全景、瀑布、zoom、历史、RAW IQ、调试、控制、频道网格/编辑、GPIO、烧录、计时、功率等 |
| `mock/` | `mockDevice.ts`、`dsp.ts`、`lap.ts` | C5/S3 模拟器与合成信号 |
| 其他 | `i18n.ts`、`util/canvas.ts`、`main.ts` | 中英切换、Canvas 工具、装配 |

`main.ts`（618 行）负责所有视图的装配、模式切换、连接/重连和 rAF 渲染循环。

### 4.3 数据流

```text
Transport（Web Serial / WebUSB / WS / 模拟 / 回放）
  → Device 读循环
  → StreamParser：0xA5 'R' 同步、CRC 校验、文本/二进制分流
  → store.ingest：PROFILE/STATUS/INFO/SPURS 文本；METRICS/SPECTRUM/TELEMETRY/RAWIQ/LEVELS
  → Series / EMA / max-hold / waterfallVersion
  → rAF + 各视图 minMs 节流，按 store.version 重绘
```

容错细节：

- 串口读缓冲 1 MiB，解析缓冲可扩容，遇到垃圾数据超过 4096 字节后丢弃重新同步。
- Break/Framing/Parity/Overrun 视为可恢复；NetworkError 提示重新插拔。
- 3 s 没有数据就重发模式切换（`main.ts:591-599`），处理板子重启后回到 RH 静默模式的情况。
- 模式切换用 Promise 串行化，避免“切换过程中又切一次”。

### 4.4 功能

- 8 通道卡片：开关、RSSI/EMA、SNR、噪声底、标记、10 s sparkline、RH 选手名。
- 全景：按“最近 LO”拼接，live/max/avg/floor 四条迹线，悬停与自动量程。
- 瀑布：每完成一次旋转压一行，按平滑噪声底定标。
- Zoom：峰值、质心、标称频率、99% OBW 标记。
- RSSI 历史：10–120 s，多通道。
- RAW IQ：星座图 + 1024 点 FFT。
- 调试：遥测、逐跳时序、协议控制台。
- 频道编辑器：SVG 频率图，可整段选 band，最多 8 个，支持混频段。
- GPIO 对话框、烧录对话框、关于/固件版本。
- 计圈页：8 seats、EnterAt/ExitAt、电平图、事件、圈规则、语音播报、最快连续圈。
- 功率计页：衰减器表、削顶提示、校准偏移、平滑时间。

### 4.5 三种模式

`startMode()`（`main.ts:185-222`）：

1. 先问 `PROTOCOL?`（1.5 s 超时），老固件没有该命令则按 monitor-only 处理。
2. 计圈模式：`PROTOCOL RH` → `INFO` → `TimerModel.start()`；不恢复设备扫描设置。
3. Monitor/功率：若不是 monitor 则 `PROTOCOL MONITOR` → `syncDevice()` 恢复计划/增益/窗/频谱；功率模式再额外保存并替换 `WIN`/`SPEC`。
4. 离开功率模式时恢复 monitor 的窗口与频谱；频道计划、LO 模式、增益由 stored device settings 恢复（`main.ts:386-417`）。
5. 断开时发 `PROTOCOL RH`，让板子回到静默状态，方便 RotorHazard 接管。

### 4.6 PWA、离线与烧录

- Service worker：导航请求 network-first，3 s 超时后回退缓存；其余资源 cache-first；只清理 `widscan58-*` 旧缓存。
- 已安装应用可把选定固件标记为“离线可用”，按版本 id 保存在 `…-fw` 缓存。
- 烧录：读 `firmware/index.json` → SHA-256 + 大小校验 → 动态加载 esptool-js → `no_reset` 连接 → 关闭 C5 看门狗 → 保留 flash 参数 → watchdog reset。
- `firmware/versions.json` 当前提供两个版本：`official`（`upstream/main`）与 `rh-uart`（`origin/main`，带 UART 与 LED）。
- **0.9 起清单优先读线上**：`loadVersions()` 先取 `https://onebody.github.io/widscan58/firmware/index.json`（5 s `AbortSignal.timeout`、`no-cache`），成功后每个版本的 `base` 指向线上绝对地址，部件从线上下载，SHA-256 校验不变；失败（无网络、超时、JSON 不合法、清单为空）整单回落到页面内置的 `./firmware/`。`localStorage["widscan58.fwUrl"]` 可覆盖该地址，用于镜像或离线测试。动机见 11.3：Pages 每次 push 都重建线上清单，而下载包与桌面壳里的副本会过期。

### 4.7 测试与代码问题

Vitest 10 个文件、56 项：parser（CRC/粘包/坏帧恢复/RAWIQ）、store（新旧 INFO 格式）、RH 协议、GPIO、功率（校准/削顶/3 dB）、RH HTTP 快照、圈规则、频道方案、语音、模拟计圈。

e2e 两个脚本：`headless.mjs` 覆盖连接、帧率、模式往返、计时、RAWIQ、REBOOT、390 px；`layout-stability.mjs` 冻结数据测 700–1800 px 高度。它们需要本机 Chrome 与可选的真板/桥，本次未运行。

代码层可见问题：

- `views/debug.ts:235` 日志满 500 条后每帧重建控制台。
- `serial/parser.ts:26,58` 的 `length > MAX_PAYLOAD` 是死分支。
- 历史缓冲按 250 次/s 分配，8 通道 120 s 约 11.5 MB（`model/store.ts:145`）。
- 回放一次性排完所有 `setTimeout`，没有按时间节流（`serial/recorder.ts:55`）。
- 频道计划/掩码逻辑在 `views/channelGrid.ts:63-74` 与 `views/power.ts:153-164` 重复。
- i18n 以“英文原文”为 key，改英文文案会静默丢中文；`views/power.ts:140` 的 “Power” 未翻译。
- `main.ts:360` 注释写的是 `firmware/versions.json`，实际读的是 `index.json`。
- 烧录只发布 C5；S3 逻辑被注释保留（`serial/flasher.ts:69-81`）。

### 4.8 本地副本与 RotorHazard 数据源（0.8 的部署结论）

云端页面（GitHub Pages，https）读不了局域网里的 `http://` RotorHazard，这是浏览器规则而不是代码缺陷；同时“把页面架在局域网 http 地址上”也不可行。两条规则一起看：

| 页面地址 | 读 http RH | 连板子（Web Serial / WebUSB） |
|---|---|---|
| `https://`（GitHub Pages） | 被混合内容拦截，请求发不出去 | 可以（安全上下文） |
| `http://192.168.x.x` | 可以 | **不行**：非安全上下文，`navigator.serial`/`navigator.usb` 不存在 |
| `http://localhost`（本机 `npm run serve`） | 可以 | 可以 |

因此只有两种可用组合：

1. **插着板子的电脑上跑本地副本**：`npm run serve`（新增 `web/serve.mjs`，零依赖，默认 `dist/`、`0.0.0.0:8080`），浏览器打开 `http://localhost:8080/`，板子链路和 RH 的 http 地址都可用。
2. **https 页面 + https 的 RH**：GitHub Pages 照常部署，RH 通过 cloudflared / Tailscale Funnel 之类的隧道暴露成 https 地址；代价是比赛 API 对公网可见。

RotorHazard 4.5 的 `/api/status`、`/api/heat/{id}`、`/api/pilot/all`、`/api/profile/{id}` 都返回 `Access-Control-Allow-Origin: *`，所以同网跨源读取不需要代理或 CORS 配置。页面在 https 下填入 `http://` 地址时，会在发起请求前给出中英双语提示，指向本机副本或 https 地址方案。

### 4.9 桌面版（0.9 起，`repo/web-desktop/`）

同一个 `dist/` 打成 Windows/macOS 本地应用：Electron 45 + electron-builder 26，运行时零依赖（主进程只有 `main.mjs` 142 行，`package.json` 的 `files` 只带 `main.mjs`、`package.json`、`dist/**`）。

为什么不是 Tauri：页面依赖 Web Serial（连板子、烧录）与 Web USB，只有 Chromium 系支持；Tauri 的 WKWebView / WebView2 都没有这两个 API。

关键实现：

| 点 | 做法 | 原因 |
|---|---|---|
| 页面加载 | 自注册 `app://widscan58/` 协议（`standard` + `secure`）提供 `dist/` | 必须让页面处于安全上下文，否则 `navigator.serial`/`navigator.usb` 不存在；用 `file://` 或普通 http 都不行 |
| 局域网 RH | `webPreferences.webSecurity: false` | 放行 `app://` 页面对 `http://192.168.x.x:7572` 的请求（否则混合内容拦截）；RH 侧本就带 CORS |
| 设备选择 | 主进程处理 session 事件 `select-serial-port`、`select-usb-device`，用 `dialog` 出选择框 | Electron 不提供内置选择 UI；不实现则 `requestPort()` 永远失败 |
| 事件签名 | `select-serial-port` 实参是 `(event, portList, webContents, callback)`；`select-usb-device` 是 `(event, details, callback)`，列表在 `details.deviceList` | 按直觉写成 `(event, webContents, portList)` 会让 portList 实际是 webContents，静默出一个空列表——带这个 bug 的第一版 Windows zip 已经流出过，重打才修掉 |
| 进程存活 | `extendInfo` 里 `NSSupportsAutomaticTermination`/`NSSupportsSuddenTermination` = false、`NSAppSleepDisabled` = true；主进程加 `child-process-gone` 日志与渲染进程崩溃重载窗口 | 曾两次“应用自己消失”：第一次确证是 macOS TAL（unified log 有 `_kLSApplicationWouldBeTerminatedByTALKey`）；第二次在关掉 TAL 的包里仍发生（点烧录后约 70 s，无 crash report），最可能是渲染进程崩溃 → `window-all-closed` → quit，**死因未定案** |
| 测试钩子 | `WIDSCAN58_AUTO_SERIAL=<子串>` 让主进程自动选中匹配串口，跳过对话框 | 配合 CDP `Input.dispatchMouseEvent` 可以做全自动真机链路测试 |

产物与验证（macOS Apple Silicon 本机）：

- `widscan58-web-<版本>-mac-arm64.dmg`（约 120 MB）与 `widscan58-web-<版本>-win-x64.zip`（约 154 MB，免安装绿色版，解压运行 `widscan58.exe`）。**两者都未签名**，macOS 首次打开要右键 → 打开。
- Windows 只出 zip 不出安装包：NSIS 的 `makensis` 只有 x86 版，本机没有 Rosetta，`spawn error -86`。
- 真机链路已验证（0.8.0 那版，代码与本轮一致）：连接 `cu.usbmodem21401`（Espressif USB JTAG/serial）→ Web Serial 状态正常 → 频谱 canvas 像素持续变化；RH 4.5 叠加用本地服务端验到「Heat 1 · 4 位飞手」与 R1 频点上的 PILOT-A；**烧录验到实物**——esptool-js 经 USB-JTAG 写入，`esptool read-flash 0x10000` 读回 app 分区头的版本串确认写入，板子重启后扫描仪自动重连。
- 烧录排障事实（值得记住）：C5 的 flash 布局里 bootloader 在 **0x2000**（不是 0x0），官方 manifest 三段是 bootloader 0x2000 / partitions 0x8000 / firmware 0x10000；写错偏移 ROM 会 `invalid header: 0xff8006b7` 循环，但 esptool 逐字节读回却是“对的”。串口 115200 收到 `ERR boot esp_wifi_set_channel(1...)` 循环说明板子在 boot_fail 重试，是固件问题而不是桌面壳问题。

构建命令（本机环境坑）：

```sh
cd repo/web && npm run build          # 先出 dist/
cd ../web-desktop && npm run sync-dist # dist/ 拷进桌面壳
# Qoder 的 shell 继承 ELECTRON_RUN_AS_NODE=1，必须清掉，否则 electron 变成纯 Node
env -u ELECTRON_RUN_AS_NODE npm run dist:mac
env -u ELECTRON_RUN_AS_NODE ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/ \
    ELECTRON_BUILDER_BINARIES_MIRROR=https://npmmirror.com/mirrors/electron-builder-binaries/ \
    npm run dist:win
```

`@electron/get` 只认 http 代理，不认 `socks5://`；GitHub 直连被墙时走 npmmirror 或 `HTTPS_PROXY=http://127.0.0.1:7897`。Windows 交叉构建不能用 `electronDist` 指向 mac 二进制（缺 `electron.exe`），只能重新下载。

### 4.10 发布包里的固件副本

`web/public/firmware/`（gitignore，不进仓库）是构建时放进 `dist/` 的固件快照，桌面壳与下载 zip 离线兜底用的就是它。它**会过期**，所以 0.9 的发布流程固定加一步：构建前从线上 Pages 取回 `firmware/index.json` 与全部部件，逐个按 manifest 的 SHA-256 与字节数校验后再 `npm run build`。这样即使完全断网，下载包烧出来的也是当轮线上那两个版本。

**离线副本必然比线上落后一代，这是构造性的**。Pages 用 fork main 的提交号给 `rh-uart` 命名，而那次构建正是由发布镜像推送触发的——副本却在推送之前就要 staging 完。0.9 发布后实测：线上 `firmware/index.json` 是 `app-v0.9-2-g2a25967`，包内 `dist/sw.js` 与线上 `sw.js` 字节数相同、只差这一个字符串（`app-v0.9-1-g5bf32a5`）。结论有两点：把副本刷到“当时线上最新”是能做到也有做头的（能烧、有校验），但别指望它等于推送之后的那一版；能联网时优先走线上清单（4.6）才是正解。

---

## 5. Android App

### 5.1 工程与发布

- 两个模块：`:core` 纯 Kotlin/JVM（无 `android.*` 依赖），`:mobile` Android 应用。
- AGP 8.11.1、Kotlin 2.2.20、JDK 17、Gradle 8.14；compile/targetSdk 36，minSdk 26。
- 唯一测试依赖是 JUnit 4.13.2；没有 AndroidX、Compose 或第三方 UI 库，界面全部用程序化 View/Canvas 构建。
- Release 读取仓库根 `keystore.properties`；缺失时仍可构建，只是未签名（`mobile/build.gradle.kts:10-14,39-43`）。
- 版本 `0.9` / versionCode 9；minify 关闭。发布仓库地址走 `BuildConfig.RELEASE_REPO`，关于页把它显示出来，升级请求也打向它。
- **出包前必须先 commit**：AGP 把当时的 git revision 写进 APK 的 `META-INF/version-control-info.textproto`，同一棵树在 commit 前后构建会得到不同 SHA-256 的包（0.9 就为此重传过一次）。

### 5.2 架构与数据流

```text
USB CDC reader 线程
  → feedBytes
  → StreamParser（0xA5 'R' 同步 / CRC / 文本分流）
  → CommandRunner（FIFO、单飞、3 s 超时）
  → Store（120 s 环、峰值保持、逐 LO 噪声底、全景/瀑布/卡片）
  → 主线程 33 ms 重绘
```

- 单 Activity + 多个 Sheet/自定义 View；`Widscan58Activity.kt` 1006 行，负责连接、握手、菜单、模式切换。
- 连接握手：`PROTOCOL MONITOR` → `SCAN STOP` → 计划/模式/增益/`SPEC` → `PROFILE?`/`STATUS?`/`SPURS?` → `SCAN START`。
- 3 s 没有任何帧就重新握手（`Widscan58Activity.kt:940-946`），区分“板子重启”与“只是停扫但仍有 TELEMETRY”。
- USB 只拉 DTR、绝不拉 RTS：rev ≤1.0 的 C5 上 RTS 复位可能让 USB 直到重新上电才恢复（`usb/UsbSerialLink.kt:107-109`）。
- `SimFeed` / `RhSimLink` 通过 `--ez sim/rh` 注入同一 sink，完全不碰 USB 协议栈。

### 5.3 功能矩阵

`⋯` 菜单 12 个入口（`Widscan58Activity.kt:432-445`，`openMore()`）：

| 入口 | 能力 |
|---|---|
| 常规设置 | 扫描参数、显示、设备设置 |
| 自动缩放 | dB 量程自适应并记住 |
| RSSI 历史 | 30 s+ 多通道历史 |
| 扫描器遥测 | hop/tune/capture/DSP、heap、drops |
| 协议控制台 | 双向命令与回复 |
| GPIO 配置 | 三态、保存、回读 |
| 录制与导出 | `.widscan58`、频道 CSV、频谱 CSV |
| 计圈（RotorHazard） | 4 个页签：seats / levels / events / receiver |
| 功率计 | 衰减器表、量程、校准、退出还原 |
| RAW I/Q | 星座、FFT、采样参数 |
| 使用帮助 | 内置 WebView 教程，离线、中文 |
| **关于与升级**（0.9） | 版本号、构建号、发布仓库、检查更新、应用内安装 |

此外还有频道方案编辑器（6 个 band、可混排、去重升序、最多 8 个）、中英切换、CSV 导出、会话回放、USB 探测屏。

### 5.4 协议与 RH

- 监控帧与固件 `frames.h` 对齐：METRICS 34+26/通道、LEVELS 32+4、RAWIQ 14 B 头 + 1024 样本/块。
- RawIQ 按 offset 拼装，断档丢弃；未发送可选 `rate_code`，固件回退当前档。
- RH：API `0x25`（level 36）、大端、读 ≤`0x50` 有应答、写无应答；`RhDevice`/`RhModel`/`RhLapSeat`/`RhLapTracker`/`RhSerialLink`/`RhSimLink` 分工明确。
- 录制格式：`u32 ms | u32 len | bytes` 小端，上限 32 MiB。

### 5.5 本地化与文档

- `values/` 中文、`values-en/` 英文，各 **333** 条字符串；`localeConfig` 注册两种语言。
- 默认语言是中文（`Prefs.kt:75-77`，`LANGUAGE_ZH`），可选 English 或 system。
- 内置帮助：`assets/help/index.html` 548 行、18 节、21 张图，仅中文；不联网。0.9 加第 16 节「关于与升级」，`⋯` 菜单截图重拍为 12 项。
- `docs/MANUAL.zh-CN.md` 552 行，覆盖全部视图、命令、CSV/录制格式、排障、术语、隐私；0.9 加 9.10 并改写第 16 节。

### 5.6 测试

`core/src/test` 共 27 个测试类、**216 项**（0.9 新增 `AppReleaseTest`），覆盖：

- 帧/流解析、CRC、重同步。
- Store、Series、全景、瀑布、频道卡片、历史、hop 时序。
- CommandRunner、DeviceCommands、Session、Export。
- Power、RawIq、Bands、Gpio。
- RH 全链路：Device、Model、LapSeat、LapTracker、Gate、Poller、Protocol、SerialLink、SimLink。
- **0.9 新增** `AppReleaseTest`：`app-v` 标签解析、逐段数字版本比较（0.10 > 0.9、`0.7-dev` == `0.7`）、候选筛选与“本机已最新”判定、`sha256:<hex>` 摘要清洗。比较与筛选规则放在 `:core`，所以升级逻辑的排序部分不需要网络也不需要 Robolectric。

`mobile/` **没有任何自动测试**；USB 枚举/权限、CDC 吞吐、DTR、TTS、RH 串口、不同固件版本差异只能真机验证。`AboutActivity`/`ApkInstaller`/`GitHubReleases` 三个新类同样没有测试，`PackageInstaller` 会话与系统安装器只能在真机/模拟器上验。

### 5.7 与总仓库镜像的关系

- `repo/app` 与根 `app/` 源码一致，但前者没有 `.git`、没有签名配置、没有图标目录内容。
- 根 `app/` **没有配置任何 git remote**（`git remote -v` 为空），目前只存在于本机；对外发布靠“`git archive` + `rsync --delete` 镜像到 fork main”（做法见 §11.4）。
- 这带来两个治理风险：一是仓库丢失/未备份；二是总仓库镜像与独立仓库之间靠手工同步，容易漂移。0.9 这轮镜像提交是 `ac736e6`，与 `app/` HEAD `b03b8cc` 的 142 个 blob 逐条一致。

### 5.8 关于与应用内升级（0.9 新增）

```text
「⋯ → 关于」AboutActivity
  → GitHubReleases.fetch()：GET api.github.com/repos/<RELEASE_REPO>/releases?per_page=N
  → 过滤：tag 以 `app-v` 开头 + 非 draft + 非 prerelease + 有 https 的 .apk 附件
  → AppRelease.kt（:core）：newestRelease / upgradeFrom 选出版本高于本机的那一项
  → ApkInstaller：canRequestPackageInstalls() 判定 → PackageInstaller 流式会话
      → 边写边算 SHA-256 → 与附件公开的 digest 比对 → 不一致抛 ChecksumMismatch 丢弃
      → COMMIT 后收 RESULT_OK/PENDING/CUCCESS 广播，失败显示原因并给「打开发布页」兜底
```

设计上的三个刻意选择：

- **不用 `/releases/latest`**：那个端点按发布时间回答，而本仓库同时发 Web 与固件，`latest` 可能不是手机包；因此拉列表再按 `app-v` 前缀收窄。
- **校验在写盘路径上完成**：不把 APK 落到共享存储、不引入 FileProvider，摘要对不上就不交给安装器；这样“显示已更新”与“实际装上”不会被拆成两件事。
- **无网络也照常使用**：除了这个页面，应用其余部分不发起任何请求；扫描、遥测、录制、导出仍只走 USB，没有后台服务、没有开机自启、不读 IMEI/序列号/位置。

真机行为（模拟器实测）：`adb uninstall` 会连「安装未知应用」授权一起清掉，所以关于页先回“需要先在设置里允许本应用安装应用”并出「去设置」；拨开关的瞬间系统杀掉本进程，回来后要再按一次「安装」。1.5 MB 的下载在模拟器上走了近 60 秒且长时间停在 99%——那是进度回调的粒度，不是卡死。

---

## 6. 工具链与交付

### 6.1 Python / shell 工具

| 工具 | 作用 |
|---|---|
| `tools/widscan58cli.py` | 主机客户端：`info`、`cmd`、`monitor`、`rawiq`、`spectrum`、`bootloader` |
| `tools/serial_bridge.py` | 串口 ↔ WebSocket，供无 Web Serial 的浏览器/远程查看使用 |
| `tools/rh_check.py` | 调用 RotorHazard 自己的服务端接口代码，对板子做发现、座位、轮询、过门、通信错误检查 |
| `tools/rh_diag.py` | 不启动 RH 服务端，按 4.x 发现流程逐字节诊断串口 |
| `tools/pages_firmware.py` | 把 PlatformIO 构建产物整理成 Web 烧录器需要的 manifest/index |
| `tools/powercal.py` | 通过 Betaflight MSP 扫 VTX，对功率计做 dBFS→dBm 标定 |
| `tools/flash.sh` | 先停流、再 esptool 进入下载模式、watchdog reset 退出；支持 `--manual` |
| `tools/check_phy_abi.sh` | 对比两个 IDF 版本的私有 PHY 函数指令流 |
| `tools/ttyfix.py` | 把 tty 留在 `VMIN=1`，避免 Chrome 误报 “device has been lost” |
| `tools/gen_twiddle.py` | 生成 radix-4 FFT 的 Q15 旋转因子表 |

### 6.2 CI 与 Pages

`.github/workflows/pages.yml`：

1. checkout（`fetch-depth: 0`，为了 `git describe`）。
2. 拉取 fork 的所有分支与 `upstream/main`。
3. 用 PlatformIO 缓存安装工具链。
4. 按 `firmware/versions.json` 的每个 ref 创建 worktree，构建 `esp32c5`，调用 `pages_firmware.py` 暂存产物，最后生成 `index.json`。
5. `npm ci` → `npm test` → `npm run build`。
6. 非 PR 时部署到 GitHub Pages。

关键事实：

- **CI 只跑 Web 测试，不跑固件 host 测试**。
- 只发布 `esp32c5`；S3 因 USB 烧录可靠性问题被 `FW_ENVS` 排除。
- `versions.json` 引用的是**分支**（`upstream/main`、`origin/main`），不是 tag 或 commit SHA，因此同一版本 id 的内容会随分支移动。

### 6.3 构建命令速查

```sh
# 固件
cd repo/firmware
pio run -e esp32c5
pio run -e esp32c5 -t upload
pio test -e native

# Web
cd repo/web
npm install
npm run dev          # http://localhost:5173
npm test
npm run build

# Android
cd app
./gradlew :core:test :mobile:assembleDebug
./gradlew :mobile:assembleRelease   # 需仓库外 keystore.properties
adb install -r mobile/build/outputs/apk/debug/mobile-debug.apk
```

### 6.4 文档体系

| 文档 | 行数 | 受众 | 新鲜度与问题 |
|---|---:|---|---|
| `repo/README.md` | 105 | 新用户 | 功能描述与当前实现基本一致 |
| `repo/CLAUDE.md` | 496 | 开发者 | 信息密度高，是实际开发手册；`CONFIG SAVE` 的 “LO plan” 已注明是 GROUP/SINGLE（2026-10-27） |
| `repo/PLAN.md` | 1,294 | 设计/历史 | 开头设计章节已过时，`## Status` 之后的日志才是当前事实；IDF 版本与目录树已于 2026-10-27 修正 |
| `repo/docs/RotorHazard.md` | 213 | 部署/操作 | 接线、Pi 配置、增益、性能、平滑都很完整；明确写了“未用真机四轴过门验证” |
| `repo/docs/superpowers/specs/2026-10-26-rh-uart-bridge-design.md` | 207 | 内部设计 | 决策表清晰；状态“已确认” |
| `repo/docs/superpowers/plans/2026-10-26-rh-uart-bridge.md` | 1,369 | 实施计划 | 任务拆解细；**Task 7 验证步骤全部未勾选** |
| `app/README.md` | 200 | App 用户/开发者 | 已与代码对齐（瀑布行高、增益上限、语言行为、已实现功能清单于 2026-10-27 修正）；测试数与“无权限”表述需按 0.9 再看一次 |
| `app/README.zh-CN.md` | 171 | 同上 | 同上，与英文版同步修正 |
| `app/docs/MANUAL.zh-CN.md` | 552 | App 参考手册 | 内容全；适用版本 `0.9`，含 9.10「关于与升级」 |
| `repo/docs/PROJECT-REPORT.zh-CN.md` | 约 870 | 项目总结 | 架构、验证现状、风险与各轮发布内容 |
| `repo/docs/CHANGELOG.zh-CN.md` | 约 130 | 所有人 | 0.1 → 0.9 的逐版变更列表，四个发布物合并在一条时间线上 |
| `app/mobile/src/main/assets/help/index.html` | 548 | 应用内教程 | 18 节 21 图、离线、当前；仅中文 |

桌面版没有独立 README：用法与限制写在 `web-v0.9` 的发布说明与本报告 4.9 里（`web-desktop/` 目录内暂无文档，是下一份该补的）。

文档一致性问题（2026-10-27 已全部处理）：

1. `CLAUDE.md:284` 的 “LO plan” 经核对并非错误：`timer_saved_t` 保存的 `mode` 就是 GROUP/SINGLE，也就是 Web 界面 “LO plan” 控件；已在文中注明，避免与“频道计划”混淆。
2. ~~`PLAN.md:33` 固定 IDF `6.2.0-dev` / `firmware-targets.json`~~ —— 已改为 pioarduino 55.03.312-1 / ESP-IDF 5.5.5，并说明 esp-sdr 的 6.2.0-dev 只是 PHY ABI 参照。
3. ~~`PLAN.md` 目录树列出 `testgen_s3`、`calib.c`、`imd.c`、`analyze_capture.py` 等不存在的文件~~ —— 目录树已按当前源码重写（`radio/c5`、`radio/s3`、`dsp/psd.c`、`scan/lap.c`、`proto/rh.c` 等）。
4. ~~`MANUAL.zh-CN.md` 的 `0.7-dev` 与 `versionName` 的 `0.7` 不一致~~ —— 已改为 `0.7`。

App 两份 README 原先的四处不符（49 测试、`Not in this version`、瀑布 2 dp、跟随系统语言）已于 2026-10-27 全部按代码修正，英文与中文版同步，镜像 `repo/app` 已更新。同日修完 PLAN 的 IDF 版本与目录树、App 手册的适用版本；CLAUDE 的 “LO plan” 经核实是术语歧义（指 GROUP/SINGLE），已加注而非删改。

---

## 7. 验证现状

### 7.1 本次实际运行（0.9 发布前，全部实跑）

| 命令 | 结果 |
|---|---|
| `cd repo/firmware && pio test -e native` | **17/17 PASSED**，2.905 s |
| `cd repo/web && npm test` | **56/56 passed**，10 个测试文件，338 ms |
| `cd repo/web && npm run build` | `tsc --noEmit` 通过，`dist/` 48 个文件，入口 `assets/index-DSC5v4fL.js` |
| `cd app && ./gradlew :core:test --offline --rerun-tasks` | **BUILD SUCCESSFUL**，29 s；解析结果 XML：27 个套 / **216 项** / 0 失败 / 0 错误 / 0 跳过 |
| `aapt2 dump badging widscan58-app-0.9-release.apk` | 包名、`0.9`、versionCode 9、minSdk 26、targetSdk 36 正确；权限含 `INTERNET`、`REQUEST_INSTALL_PACKAGES` |
| `apksigner verify --print-certs …apk` | v2 签名有效，证书与 0.7/0.8 同一密钥（`4b68a113…b4a37d`） |
| `npm run dist:mac` / `dist:win` | dmg 与 win zip 产出；`Info.plist` 与 asar 内 `version` 均为 `0.9.0`，asar 内入口 bundle 与 `dist/` 一致 |
| 固件副本抓取（node + SHA-256 校验） | 6 个部件全部通过 manifest 的 `sha256` 与 `size` 校验（目录共 9 个文件 / 1,391,961 字节），版本 `9999b07` / `app-v0.9-1-g5bf32a5`；`public/` 与 `dist/` 两份逐条对过线上清单 |
| `git rev-parse` 子树对比 | `repo/` 的 `firmware`、`web`、`web-desktop`、`tools`、`docs`、`app` 六个 tree 与 fork main 相同 |
| 发布后线上核对 | Pages run `40481484388` success；`https://onebody.github.io/widscan58/` 的 `index.html` 与 `assets/index-DSC5v4fL.js` 与本地 `dist/` **逐字节相同**；`sw.js` 只差内嵌清单里的 `rh-uart` 提交号（见 4.10）；5 个附件的 `assets[].digest` 与本地 `shasum -a 256` 全部一致 |

合计自动测试 **289 项**（17 + 56 + 216），全部通过。桌面版**没有**任何自动化测试（`main.mjs` 无测试源集）；本轮只重打包并核对产物内容，未在真机上重跑连接与烧录链路——那条链路的证据来自 0.8.0 那一次（见 7.2），而两轮的页面产物是同一个 bundle。

### 7.2 文档记录的历史验证

- **桌面版（0.8.0 产物，真机）**：连接 `cu.usbmodem21401` → Web Serial 状态 → 频谱 canvas 像素持续变化；RH 4.5 本地服务端验到「Heat 1 · 4 位飞手」与 R1 上的 PILOT-A；**烧录实测**：esptool-js 写入后 `esptool read-flash 0x10000` 读回版本串 `21ad729`、板子重启；板子现跑 HEAD 构建 `app-v0.8`，打包版连接后 3 s 内 14/17 采样点变化，烧录后扫描仪自动重连。
- C5（XIAO rev v1.0）：hands-free 烧录、R1–R8 扫描、~4.0 ms/hop、~60 更新/s；USB 电流与 PHYIDLE 数据。
- C5-Zero：WS2812 状态灯、UART 头、GPIO26 天线切换。
- RotorHazard：`rh_check.py` 8 nodes、API 36、无通信错误；同频座位圈时差中位 2 ms。
- Android：小米 25113PN0EC / Android 16 真机，CDC 枚举、128 KiB 抓包零 CRC 错、握手全通过；模拟器 411×731 dp 与 360×640 dp。
- Android 0.9 升级链路（模拟器）：未授权时关于页提示并出「去设置」；`ManageAppExternalSourcesActivity` 拨开关后检查更新 → 安装 0.9 → 系统安装器 → `versionCode 9`。
- Web：headless e2e 对模拟器与真板（经 serial bridge）跑过连接、模式、RAWIQ、REBOOT、390 px。
- 功率计：R1–R8 约 ±1 dB（2026-10-19），但 ADC rate 改为 1 后 R1–R5 的 −0.5 dB 是估算，未重测。

### 7.3 测试覆盖空白

| 区域 | 自动测试 | 只能人工/真机 |
|---|---|---|
| 固件 DSP 全链 | 仅 FFT/metrics/lap 局部 | PSD、Welch、IQ 校正、S16/S16R |
| 固件协议/命令/RH | 无 | 帧/CRC、命令解析、RH 状态机、UART |
| 固件射频 | 无 | 调谐、校准、杂散、R8、功率 |
| Web 视图 | 模型层有测试 | DOM 渲染、Canvas、浏览器授权、烧录 |
| Web e2e | 脚本存在 | 需 Chrome/真板，本次未跑 |
| Android mobile | **无** | USB、权限、DTR、TTS、RH 串口、系统栏 |
| 桌面壳 `web-desktop/main.mjs` | **无**（无测试源集，Electron 主进程逻辑不被任何框架覆盖） | 设备选择对话框、`app://` 协议、崩溃重载、macOS TAL |
| 跨端一致性 | 三端各自硬编码帧尺寸 | 协议漂移靠人工对照 |

桌面版的验证方式是「打出包 → 用打包产物做真机链路」，而不是自动化：0.8.0 那一轮做过连接/频谱/RH 叠加/烧录四件事的人工验证（见 7.2），0.9 这轮只重打包并核对产物内容（bundle 指纹、`Info.plist` 版本、asar 里的 `package.json`），没有再上真机。

---

## 8. 风险与技术债

按影响排序。优先级是“工程建议”，不是事故等级。

| # | 优先级 | 风险 | 证据 | 影响 |
|---:|---|---|---|---|
| 1 | 高 | 依赖私有 PHY ABI、寄存器和 `phy_param` 偏移 | `radio_c5.c`、`recal_c5.c` 大量硬编码；`check_phy_abi.sh` 手动运行 | 升级 IDF/平台可能直接失效；CI 不检查 ABI |
| 2 | 高 | C5 内存余量极薄，失败是静默的 | 静态 RAM 只剩几百字节、heap ~5 KB（`CLAUDE.md:107-109,293`）；UART 曾因 3 KB 回复缓冲分配失败导致“板子活着但不回话” | 新功能可能在运行时静默失败；`commands_task_start` 分配失败直接 return（`commands.c:704-709`） |
| 3 | 高 | 固件发布不可复现 | `firmware/versions.json` 用 `upstream/main`/`origin/main` 分支引用 | 同一版本 id 的固件内容会变；无法从发布物反查 commit |
| 4 | 高 | UART/RH 实机验收没有闭环记录 | 实施计划 Task 7 全部未勾选；`docs/RotorHazard.md:205` 明确“未用真机四轴过门验证” | UART 双端口、Pi 接线、共存仲裁、真实过门阈值缺少正式证据 |
| 5 | 中 | Android 独立仓库仍无 remote | `app/` 没有远端；`repo/app` 与 fork main 的 `app/` 都是手工镜像 | 源码已随 fork 备份，但推送仍靠本地命令，镜像与独立仓库易漂移。0.9 的 `web/`+`web-desktop/`+`docs/` 镜像同样只在发布脚本的记忆里，没有一条命令能重放 |
| 6 | 低 | 文档与代码漂移 | App 两份 README、PLAN 版本/目录树、MANUAL 版本号均已对齐（2026-10-27）；PLAN 的设计章节仍属历史，需靠 `## Status` 分辨 | 新人和后续 AI 会照抄错误事实 |
| 7 | 中 | CI 不跑固件 host 测试 | `pages.yml` 只有 `npm test` | 固件回归只能靠本地自觉 |
| 8 | 中 | 功率计在 ADC rate 1 下未重标 | R1–R5 偏移 −0.5 dB 为估算（`CLAUDE.md:136-139`） | 功率读数在 R1–R5 可能系统性偏低 |
| 9 | 中 | Web i18n 以英文原文为 key | `web/src/i18n.ts` 的 `zh[raw]` 匹配 | 改英文文案会静默丢中文；“Power” 未译 |
| 10 | 中 | 无 OTA/回滚，刷写历史上有风险 | 单 factory 分区；`CLAUDE.md` 记录过擦除后无法启动、需 BOOT+EN | 现场升级失败需要手动恢复 |
| 11 | 中 | 捕获/PSD 出错只增加计数 | `scanner.c:381-384,398-406`：捕获失败直接跳过本 hop 且不预调下一个 LO；PSD 失败时下一个 LO 已调谐，仅上报一次 `ERR psd` 后继续 | 主机只看到帧缺口，难以定位 |
| 12 | 中 | Android `mobile` 层无自动测试 | `mobile/src` 无 test 源集 | USB/权限/UI 回归只能人工 |
| 13 | 低 | Web 历史内存偏大 | 8 通道 × 120 s × 250/s ≈ 11.5 MB（`model/store.ts:145`） | 低端手机浏览器可能吃紧 |
| 14 | 低 | 回放一次性排程 | `serial/recorder.ts:55` | 长录制回放可能卡顿且时间不准 |
| 15 | 低 | 死代码/未用导出 | `FRAME_LOG`、parser 死分支、`SUPPORTED_CHIPS`、`fmtMHz` | 维护噪音 |
| 16 | 低 | RH 滤波读取无锁 | `rh.c:118-128` 读 `seat[0]` 未加 mux | 理论竞态，概率低 |
| 17 | 中 | 桌面产物未签名，且 macOS 进程消失原因未定案 | dmg/zip 无开发者证书与公证；一次点击烧录后 ~70 s 进程消失，无 crash report、无 TAL 记录，已加 `child-process-gone` 日志后未复现 | 首开需要用户在「隐私与安全」里手动放行；若真是渲染进程崩溃，用户看到的是窗口无故关闭 |
| 18 | 中 | 桌面壳与离线包里的固件副本是构建时快照 | `web/public/firmware/` 被 gitignore，随 `dist/` 打进 dmg/asar；线上 Pages 的 `index.json` 每天可能更新 | 快照过期时离线烧录会写入缺 `wifi_boot_channel()` 的旧固件，C5-Zero 直接进 `ERR boot` 循环。0.9 起烧录优先拉线上清单（4.6），副本只作兜底，但**出包前仍需重新 staging 一遍**（4.10） |

另外两个结构性观察：

- **“两份 Android 代码”**本身不是 bug，但它把发布、文档、测试和备份四件事都变成了双份工作。长期应改成 submodule、发布产物或单向生成，而不是人工镜像。
- **`PLAN.md` 的双重身份**（设计文档 + 逐日实验日志）导致它既不能当稳定设计说明，也不适合当变更日志。把历史测量留在 PLAN、把当前架构抽成一份短文档，会显著降低阅读成本。

---

## 9. 成熟度评估与建议

### 9.1 模块成熟度

| 模块 | 成熟度 | 依据 |
|---|---|---|
| 固件扫描/DSP | 高 | 真机长时间测量、性能表完整、host 测试覆盖核心算法 |
| 固件 RH/UART | 中高 | 协议与实现完整，rh_check 有 8 seat 记录；UART/Pi/真机过门验收未闭环 |
| Web | 高 | 功能全、56 项测试、e2e 脚本、PWA/烧录/模拟器齐备 |
| 桌面壳 | 中 | 功能真机验证过（连接/频谱/RH 叠加/烧录），但零自动化测试、产物未签名、一次进程消失未定案 |
| Android | 高 | 0.9 功能闭环、216 项 core 测试、真机验证过；mobile 层与文档滞后 |
| 工具链 | 高 | 覆盖客户端、桥、RH 校验、烧录、标定、ABI 检查 |
| 文档 | 中高 | 深度很好；0.8/0.9 两轮把 README、PLAN、MANUAL、版本号全部对齐，并补了 CHANGELOG |
| 发布工程 | 中 | Pages 自动化完整；固件引用分支、无 ABI/固件测试门禁、App 无远端、三形态产物靠手工串 |

### 9.2 建议的下一步（按收益排序）

1. ~~先固化当前工作区~~：已完成。`tools/check_phy_abi.sh`、`tools/rh_diag.py`、`web/package-lock.json`、文档修正和本报告都随 `app-v0.8` / `web-v0.8` 进入 fork main；`rh_diag.py` 已写入 `CLAUDE.md` 工具表。
2. **给 Android 仓库加远端**（或明确以 fork main 的 `app/` 为发布源），并决定 `repo/app` 的长期形态（submodule / 发布产物 / 生成镜像），消除人工镜像。
3. **完成 UART 验收**：按计划 Task 7 跑 USB 基线、USB-UART 适配器、Pi、RotorHazard 服务端、共存与异常；把结果写回 `docs/RotorHazard.md` 并勾选计划。
4. **补 RH 真机飞行测试**：至少一次真实四轴过门，记录 RSSI/SNR 两种指标下的阈值、过门时间与 RX5808 的偏差。
5. **把固件 host 测试加入 CI**，并考虑在 CI 中跑一次 `check_phy_abi.sh` 与 S3 构建（即使不发布）。
6. **固化发布输入**：`versions.json` 改用 tag/commit SHA；manifest 记录 commit 与构建时间；保留 `official` 的稳定语义。
7. **修文档漂移**（2026-10-27 已完成大部分）：App README、PLAN 版本与目录树、MANUAL 版本号已对齐；剩余建议给 PLAN 开头加“历史设计，当前状态见 Status”提示。
8. **让内存失败显式化**：启动时检查关键分配；失败时通过 `ERR boot`/日志明确报告，而不是静默 return；为命令回复缓冲预留静态空间或启动时一次性分配。
9. **重做 ADC rate 1 下的功率标定**，至少覆盖 R1–R5，更新 `CALIBRATION`。
10. **Web 质量小修**：给 i18n 加“未翻译字符串”测试；修 parser 死分支与回放排程；考虑把历史缓冲改为按时间裁剪而不是固定 250/s。
11. **可选**：为 `scanner.c`、`rh.c`、`link.c` 增加 native 可测的纯函数边界，把协议/状态机的关键路径纳入 host 测试。

---

## 10. 发布记录（0.8）

### 10.1 发布物

| 发布物 | 内容 | 来源 |
|---|---|---|
| 代码 | fork `onebody/widscan58` main | `rh-uart-bridge` 合入 fork main 后推送 |
| `app-v0.8` | 附件 `widscan58-app-0.8-release.apk`（1,540,474 字节） | `app/` 仓库 `master`，镜像为 fork `app/` |
| `web-v0.8` | 附件 `widscan58-web-0.8.zip`（1,159,362 字节） | `repo/web`，与 GitHub Pages 同一提交构建 |

两个附件的 SHA-256 已写入 1.4 节，发布后还会与 GitHub 回填的 `assets[].digest` 对一遍。

### 10.2 0.7 以来的变化

- 固件合入上游 0.7 之后的全部提交（EMA 改毫秒、计时保存、删除 `CONJ` 等），RotorHazard UART 双端口和 C5-Zero 状态灯保留。
- Web：中文界面与语言切换、RotorHazard 4.5 赛事数据叠加、混频段频道方案、固件版本选择，以及本地副本读 RH 的部署修正（`serve.mjs` + 中英提示）。
- Android：频道方案支持混频段、界面语言可选、文档与手册修正；版本号 0.7 → 0.8。
- 工具：新增 `tools/rh_diag.py`（RH 串口逐字节诊断），`check_phy_abi.sh` 兼容更多本机 `objdump`。
- 文档：`CLAUDE.md` / `PLAN.md` 同步，App README 与手册对齐代码，新增本报告。

### 10.3 已知限制

- https 页面读不了局域网 `http://` 的 RH；局域网 http 页面连不了板子。用法见 4.8。
- 固件无 OTA，刷写失败需要手动进入下载模式恢复。
- 固件 host 测试不在 CI 中；Android `mobile` 层没有自动测试。
- 功率计在 ADC rate 1 下的 R1–R5 标定仍是估算值。

### 10.4 安装

APK 与 0.7 使用同一签名密钥（仓库外的 Android keystore，见 5.1），已装 0.7 release 可直接覆盖安装。若手机上装的是更早的 debug 包，需先卸载（签名不同），应用内设置会被清空，板卡设置重连后会重新读取。

---

## 11. 本次发布（0.9）

0.9 分两次落地：`app-v0.9` 已经单独发过（关于页 + 应用内升级，见 5.8），本轮补的是**它的文档**，以及把 Web 与桌面壳拉齐到同一代产物。

### 11.1 三形态发布物

| 形态 | 版本 | 产物 | 字节 | SHA-256 |
|---|---|---|---:|---|
| Android App | `0.9` / versionCode 9 | `widscan58-app-0.9-release.apk` | 1,576,961 | `1bd2ea03…05ad0ee` |
| Web 页面 | `0.9.0` | `widscan58-web-0.9.zip`（48 个文件 / 54 条目） | 1,163,822 | `31e147a7…04eeebf` |
| 桌面版 macOS | `0.9.0` | `widscan58-web-0.9.0-mac-arm64.dmg`（未签名） | 121,504,090 | `70864391…0ebee3a` |
| 桌面版 Windows | `0.9.0` | `widscan58-web-0.9.0-win-x64.zip`（免安装） | 154,266,380 | `8f424e08…4a5e2fc` |

完整 64 位摘要见 1.4，这里只是缩写对照。

App 的 APK 这轮**没有重新出**：`app/` 仓库自 `b03b8cc` 之后没有代码改动，已发布的附件哈希与 GitHub 回填的 `assets[].digest` 逐一相符，重打只会得到一个字节不同（`version-control-info.textproto` 里的 commit 号）的假新版本。

三个 Web 侧产物是**同一代**：都由同一个 `dist/` 构建，入口 bundle 都是 `assets/index-DSC5v4fL.js`，离线固件副本都是 9 个文件 / 1,391,961 字节、逐条对过线上清单的 sha256。

### 11.2 这一轮做了什么

- **代码改动为零**，只有两处版本号：`web/package.json`、`web-desktop/package.json` `0.8.0` → `0.9.0`。桌面产物的文件名与 mac `Info.plist` 的版本都从 `${version}` 派生，所以这一处 bump 同时决定了发布物名字。
- **文档**：新增 `docs/CHANGELOG.zh-CN.md`（按版本倒序，覆盖 App / Web / 桌面 / 固件四条线），本报告补上 4.9（桌面壳）、4.10（发布包里的固件副本）、5.8（关于与应用内升级），并把第 0、1、5、6、7 节的数字换成实跑结果（289 项自动测试 = 17 固件 + 56 Web + 216 App）。
- **修掉一个真实的发布缺陷**：`web-v0.8` 的 zip 是比同一 release 里 dmg 更旧的**上一代构建**（`index-rlcnjTLh.js` vs `index-DSC5v4fL.js`），差的就是那轮 RotorHazard 本地读取与固件清单线上同步。0.9 起三个形态共用一次 `npm run build` 的产物，旧 release 的 notes 里追加了说明。
- **刷新一轮离线固件快照**：`web/public/firmware/` 之前还是 `21ad729`，那版缺 `wifi_boot_channel()`，离线烧到 C5-Zero 会进 `ERR boot` 循环。现在按线上清单重新下载并校验（`official` = `9999b07`、`rh-uart` = `app-v0.9-1-g5bf32a5`）。

### 11.3 为什么把 Web 和桌面一起 bump

不 bump 的话，桌面上装的、网页上下的、release 里挂的三个东西会带三个不同的版本号，而它们其实是同一份页面代码。一起 bump 的代价是重新下载 Electron 做 Windows 交叉构建（约 15–30 分钟），收益是「0.9 = 这三个文件互相一致」这条能直接对用户讲。

### 11.4 发布过程

```text
repo/（rh-uart-bridge）
  1. git merge fork/main            # 63ec23c，把 fork 侧 README 与 app 0.9 镜像回灌
  2. 改文档 + 两个 package.json     # 本轮唯一提交
  3. npm run build（web/）→ sync-dist → dist:mac / dist:win
  4. git archive HEAD web web-desktop docs | tar -x  → rsync -a --delete → fork main
  5. git tag -a web-v0.9 && push（SOCKS 代理，失败重试）
  6. gh release create web-v0.9 <4 个产物 + 报告 + CHANGELOG>   # 大文件不挂代理
  7. gh release edit --latest，并在 web-v0.8 notes 追加旧 zip 说明
  8. Pages（push 到 main 自动触发）→ curl 线上入口 bundle
```

镜像只带 `web/`、`web-desktop/`、`docs/` 三个目录，**不带 `app/`**（`app/` 由 `app/` 仓库单独镜像，这轮它没变）。

### 11.5 已知限制

- 桌面产物未签名、未公证：macOS 首次打开要在「系统设置 → 隐私与安全性」里手动放行，Windows SmartScreen 会拦一次。
- 桌面版零自动化测试；本轮只重打包并核对了产物内容，没有在真机上重跑连接与烧录链路（那条链路的证据属于 0.8.0 那次，页面 bundle 相同）。
- `official` 线上固件（`9999b07`）仍缺 boot 信道修复；用它刷 C5-Zero 会 `ERR boot` 循环，要用 `rh-uart`。
- 上游又有 4 个提交（`fa948b2`…`9999b07`）没合进本地分支，本轮没有合（合流是独立决定，与发布产物无关）。
- 报告与 CHANGELOG 里的数字来自 2026-10-27 的一次实跑；固件射频与 UART 验收仍然是空白（7.3）。
- 包内的离线固件清单按构造落后线上一次提交（4.10）：连网时烧到的是当轮最新，完全断网时烧到的是打包那一轮。两个版本都带 boot 修复与否的差别（`official` 没有），所以断网 + 选 `official` 仍可能踩 `ERR boot`。
- 网页 zip 里**不含 `serve.mjs`**（它在源码目录 `repo/web/`，不在 `dist/`），解压后想本地跑要用自己的静态服务。发布说明里已改成这个说法。

---

## 附录 A：关键文件索引

| 主题 | 文件 |
|---|---|
| 固件入口 | `repo/firmware/src/main.c` |
| 扫描循环 | `repo/firmware/src/scan/scanner.c` |
| 频道计划 | `repo/firmware/src/scan/profile.c`、`planner.c` |
| DSP | `repo/firmware/src/dsp/psd.c`、`fft_eval.c` |
| 指标 | `repo/firmware/src/scan/metrics.c` |
| 计圈 | `repo/firmware/src/scan/lap.c`、`proto/rh.c` |
| 线协议 | `repo/firmware/src/proto/frames.h`、`link.c` |
| 命令 | `repo/firmware/src/proto/commands.c` |
| UART | `repo/firmware/src/proto/uart_link.c` |
| C5 射频 | `repo/firmware/src/radio/c5/radio_c5.c`、`recal_c5.c` |
| S3 射频 | `repo/firmware/src/radio/s3/radio_s3.c` |
| 状态灯/GPIO | `repo/firmware/src/status_led.c`、`gpio_out.c` |
| Web 装配 | `repo/web/src/main.ts` |
| Web 状态 | `repo/web/src/model/store.ts` |
| Web 传输/解析 | `repo/web/src/serial/device.ts`、`parser.ts` |
| Web 烧录 | `repo/web/src/serial/flasher.ts`（线上清单同步见 `loadVersions()`、`ONLINE_FIRMWARE_DIR`） |
| 桌面壳主进程 | `repo/web-desktop/main.mjs`（`app://` 协议、串口/USB 选择器、崩溃重载） |
| 桌面壳打包配置 | `repo/web-desktop/package.json`（`build.mac`/`build.win`、`extendInfo`） |
| 发布包内的固件副本 | `repo/web/public/firmware/`（gitignored，构建前按线上清单重新 staging） |
| App 关于/升级 | `app/mobile/.../AboutActivity.kt`、`ApkInstaller.kt`、`GitHubReleases.kt`、`core/AppRelease.kt` |
| 版本变更日志 | `repo/docs/CHANGELOG.zh-CN.md` |
| Web 计圈 | `repo/web/src/model/timer.ts`、`views/timer.ts` |
| Web 功率 | `repo/web/src/model/power.ts`、`views/power.ts` |
| Android 入口 | `app/mobile/src/main/java/com/onebody/widscan58/Widscan58Activity.kt` |
| Android 核心 | `app/core/src/main/kotlin/com/onebody/widscan58/core/` |
| Android USB | `app/mobile/src/main/java/com/onebody/widscan58/usb/UsbSerialLink.kt` |
| Android RH | `app/core/src/main/kotlin/com/onebody/widscan58/core/Rh*.kt` |
| App 手册/帮助 | `app/docs/MANUAL.zh-CN.md`、`app/mobile/src/main/assets/help/index.html` |
| CI | `repo/.github/workflows/pages.yml` |
| 固件版本清单 | `repo/firmware/versions.json` |

## 附录 B：术语

| 术语 | 含义 |
|---|---|
| hop | 一次“调谐 → 捕获 → DSP → 上报”的扫描单元 |
| rotation | 所有启用频道各扫描一次 |
| LO | 本振频率；GROUP 模式下多个频道共享一个 LO |
| profile | 固定的频段参数集（频道表、窗宽、采样率、滤波、LO 计划） |
| seat | RotorHazard 的一个计时座位/飞手 |
| EnterAt / ExitAt | 过门检测的进入/退出电平阈值 |
| METRICS / LEVELS | 完整指标帧 / 紧凑电平帧 |
| S16R | 16 位舍入、块浮点的 FFT 实现 |
| OBW | 占用带宽，此处指 99% 功率带宽 |
| RF dump bank | C5 上专供原始 I/Q 抓取的 128 KiB SRAM 区域 |

---

*本报告由本地代码、文档与实跑测试归纳而成；未做硬件复测。报告为独立文件，未修改任何项目文档或代码。*
