# Widscan58 本地版本项目总结报告

- 报告日期：2026-10-27
- 分析对象：`widscan58-clone/` 下当前已有的全部代码、文档与构建产物
- 报告性质：随 0.8 版本发布提交到 `repo/docs/PROJECT-REPORT.zh-CN.md`，同时作为本次 Release 的说明底稿
- 分析方式：逐模块读源码与文档；实际运行固件 host 测试、Web 单元测试、Android core 单元测试；检查 APK 元数据与签名
- 未做：烧板、射频测量、真机 UI 操作、Web e2e、固件完整构建（固件由 Pages CI 在发布提交上构建）

---

## 0. 摘要

Widscan58 是一个把 **ESP32-C5 的 Wi-Fi 射频前端当作 SDR** 使用的 5.8 GHz FPV 频谱/RSSI 扫描器。它通过 esp-sdr 找到的原始 I/Q 抓取路径读取采样，轮流扫描最多 8 个 FPV 频道，输出 RSSI、噪声底、SNR、信号中心频率、99% 占用带宽和整段频谱；同时还能作为 **RotorHazard 计时节点**、**浏览器单机计圈器** 和 **VTX 功率计**。

当前本地版本由三块组成：

| 组成 | 位置 | 版本状态 | 规模 |
|---|---|---|---|
| 固件 + Web + 工具 + 项目文档 | `repo/`，git 仓库，分支 `rh-uart-bridge` @ `1c43903`（发布时合入 fork main） | 相对上游 `CapnBry/widscan58` 领先 35 个提交 | 固件源码约 6,700 行；Web 约 11,300 行；工具 1,441 行；Markdown 文档约 4,600 行 |
| Android 原生 App | `app/`，独立 git 仓库，`master` @ `4a45b93` | `0.8`（versionCode 8） | Kotlin/Java 15,097 行；core 29 个文件；文档约 1,400 行 |
| Android 发布包 | `widscan58-app-0.8-release.apk` | 约 1.5 MB，已用 v2 方案签名 | 包名 `com.onebody.widscan58`，minSdk 26 / targetSdk 36 |
| Web 发布包 | `widscan58-web-0.8.zip` | 构建产物 `dist/` 的压缩包 | 版本 `0.8.0`，可静态托管或 `npm run serve` 本地运行 |

自动测试共 **282 项**，本次实跑全部通过：

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
├── repo/                  # 主仓库：固件、Web、工具、文档；Android 的只读镜像
│   ├── app/               # 根目录 app/ 的内容镜像（无 .git）
│   ├── firmware/          # ESP32-C5 / ESP32-S3 / native host tests
│   ├── web/               # Vite + TypeScript（含 serve.mjs 本地静态服务）
│   ├── tools/             # Python/shell 工具
│   ├── docs/              # RotorHazard 集成文档、内部设计/计划、本报告
│   ├── ref/RotorHazard/   # 参考检出，未跟踪
│   ├── PLAN.md CLAUDE.md README.md
│   └── .github/workflows/pages.yml
├── app/                   # Android 独立仓库（有 .git、keystore.properties、mipmap 图标）
├── PROJECT-REPORT.md      # 本报告的本地副本
└── widscan58-app-0.8-release.apk
```

`repo/app` 与根目录 `app/` 的源码、资源、README、MANUAL **内容一致**；差异只在 `.git`、构建产物、`keystore.properties`、根目录多出的空 `res/mipmap/`，以及 `gradlew.bat` 的文件差异（`diff -rq` 核对）。这意味着 Android 代码在总仓库里是一份**手工镜像**，不是 submodule，也不是构建时生成。

### 1.2 分支与提交

`repo/`：

- 发布分支 `rh-uart-bridge`，起点 `1c43903`（网页改为本地副本读 RotorHazard）。
- 发布前相对 `origin/main`（上游 `CapnBry/widscan58`）领先 **35** 个提交，落后 **0**；本次发布新增 1 个提交，再把 fork 的网页修正合入。
- 发布前相对 `fork/main`（`onebody/widscan58`）领先 **6** 个提交、落后 **1** 个（`0c9b878`，即本分支网页修正的镜像提交，内容相同、SHA 不同）。发布时用 merge 合入，不重写历史、不 force push。
- 提交作者：上游 Bryan Mayland 55 个，fork 作者 onebody 35 个（`git shortlog`）。
- 历史里有同标题重复提交（如 `24b05e7` / `4d72509`，均为 “web: open the lap timer page for settings saved as 'rh'”），是合并/拣选留下的痕迹，不影响代码但会让审计变吵。

相对上游的本地增量按主题分：

| 主题 | 代表提交 | 内容 |
|---|---|---|
| Android App | `7327c1f` … `a00627c`、`434e899`、`3c21764` | 引入手机端、补到 0.7、中文手册、签名说明、频道方案混频段 |
| RotorHazard UART | `82c9a6c` … `ae4319b` | 设计/计划、第二 sink、UART0 GP11/GP12、每端口命令与 RH 状态、堆内存修复 |
| 计圈改名 | `74b3c34`、`3b97626`、`24b05e7`/`4d72509` | “RotorHazard mode” → “lap timer mode”，NVS blob 从 `rh` 改为 `timer` |
| 混频段 | `61aa3e2`、`3c21764` | Web/App 允许从不同 band 混合选最多 8 个频道 |
| 状态灯 | `ceb5a36` | C5-Zero 板载 WS2812 指示“听到信号” |
| 固件版本选择 | `423e007`、`19a56af`、`482e6ce`、`39d7339` | Pages 按 `versions.json` 构建多个固件版本并在烧录对话框中选择 |
| Web 中文与 RH 数据 | `e3206ed` | Web UI 中文、RotorHazard 4.5 赛事数据、固件版本选择 |
| Web 本地读 RH | `1c43903` | https 页面的混合内容提示、`serve.mjs` 本地静态服务、两种可用部署组合写入 README |

### 1.3 未提交的本地改动

本次发布把两个仓库的工作区一并固化，`repo/` 侧包含：

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

根目录两个发布包已核对：

`widscan58-app-0.8-release.apk`（1,540,474 字节，SHA-256 `24c799865d3b428c1ebe5f87c32ddcecf702e7724ec05eb637c5dbd94fefed1e`）：

- `package: com.onebody.widscan58`
- `versionCode 8` / `versionName 0.8`
- `minSdk 26` / `targetSdk 36` / `compileSdk 36`
- v2 签名有效，证书 DN 为 `CN=widscan58, OU=app, O=onebody, C=CN`，指纹 `4b68a113…b4a37d`（与 0.7 相同）
- 签名密钥在仓库外，`app/keystore.properties` 只保存路径与口令，且被 `.gitignore` 排除

`widscan58-web-0.8.zip`（1,159,362 字节，54 个文件，SHA-256 `aa64461a47c3b30a05062d034caeb285d6cbaf9c8991107e74b3f985acf2ec82`）：`npm run build` 的 `dist/` 原样压缩，主 bundle 仍为 `assets/index-rlcnjTLh.js`（与线上 Pages 一致）。

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

---

## 5. Android App

### 5.1 工程与发布

- 两个模块：`:core` 纯 Kotlin/JVM（无 `android.*` 依赖），`:mobile` Android 应用。
- AGP 8.11.1、Kotlin 2.2.20、JDK 17、Gradle 8.14；compile/targetSdk 36，minSdk 26。
- 唯一测试依赖是 JUnit 4.13.2；没有 AndroidX、Compose 或第三方 UI 库，界面全部用程序化 View/Canvas 构建。
- Release 读取仓库根 `keystore.properties`；缺失时仍可构建，只是未签名（`mobile/build.gradle.kts:10-14,39-43`）。
- 版本 `0.8` / versionCode 8；minify 关闭。

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

菜单 11 个入口（`Widscan58Activity.kt:432-463`）：

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

此外还有频道方案编辑器（6 个 band、可混排、去重升序、最多 8 个）、中英切换、CSV 导出、会话回放、USB 探测屏。

### 5.4 协议与 RH

- 监控帧与固件 `frames.h` 对齐：METRICS 34+26/通道、LEVELS 32+4、RAWIQ 14 B 头 + 1024 样本/块。
- RawIQ 按 offset 拼装，断档丢弃；未发送可选 `rate_code`，固件回退当前档。
- RH：API `0x25`（level 36）、大端、读 ≤`0x50` 有应答、写无应答；`RhDevice`/`RhModel`/`RhLapSeat`/`RhLapTracker`/`RhSerialLink`/`RhSimLink` 分工明确。
- 录制格式：`u32 ms | u32 len | bytes` 小端，上限 32 MiB。

### 5.5 本地化与文档

- `values/` 中文、`values-en/` 英文，各 **305** 条字符串；`localeConfig` 注册两种语言。
- 默认语言是中文（`Prefs.kt:75-77`，`LANGUAGE_ZH`），可选 English 或 system。
- 内置帮助：`assets/help/index.html` 531 行、17 个章节、20 张图，仅中文；不联网、无权限。
- `docs/MANUAL.zh-CN.md` 517 行，覆盖全部视图、命令、CSV/录制格式、排障、术语、隐私。

### 5.6 测试

`core/src/test` 共 26 个测试类、209 项，覆盖：

- 帧/流解析、CRC、重同步。
- Store、Series、全景、瀑布、频道卡片、历史、hop 时序。
- CommandRunner、DeviceCommands、Session、Export。
- Power、RawIq、Bands、Gpio。
- RH 全链路：Device、Model、LapSeat、LapTracker、Gate、Poller、Protocol、SerialLink、SimLink。

`mobile/` **没有任何自动测试**；USB 枚举/权限、CDC 吞吐、DTR、TTS、RH 串口、不同固件版本差异只能真机验证。

### 5.7 与总仓库镜像的关系

- `repo/app` 与根 `app/` 源码一致，但前者没有 `.git`、没有签名配置、没有图标目录内容。
- 根 `app/` **没有配置任何 git remote**（`git remote -v` 为空），目前只存在于本机。
- 这带来两个治理风险：一是仓库丢失/未备份；二是总仓库镜像与独立仓库之间靠手工同步，容易漂移。

---

## 6. 工具链与交付

### 6.1 Python / shell 工具

| 工具 | 作用 |
|---|---|
| `tools/widscan58cli.py` | 主机客户端：`info`、`cmd`、`monitor`、`rawiq`、`spectrum`、`bootloader` |
| `tools/serial_bridge.py` | 串口 ↔ WebSocket，供无 Web Serial 的浏览器/远程查看使用 |
| `tools/rh_check.py` | 调用 RotorHazard 自己的服务端接口代码，对板子做发现、座位、轮询、过门、通信错误检查 |
| `tools/rh_diag.py` | 新增未跟踪：不启动 RH 服务端，按 4.x 发现流程逐字节诊断串口 |
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
| `app/README.md` | 200 | App 用户/开发者 | 已与代码对齐（测试数 209、瀑布行高、增益上限、语言行为、已实现功能清单均于 2026-10-27 修正） |
| `app/README.zh-CN.md` | 171 | 同上 | 同上，与英文版同步修正 |
| `app/docs/MANUAL.zh-CN.md` | 517 | App 参考手册 | 内容全；适用版本已改为 `0.8`（2026-10-27） |
| `repo/docs/PROJECT-REPORT.zh-CN.md` | 约 760 | 项目总结 | 本次发布新增；架构、验证现状、风险与发布内容 |
| `app/mobile/src/main/assets/help/index.html` | 531 | 应用内教程 | 17 节 20 图、离线、当前；仅中文 |

文档一致性问题（2026-10-27 已全部处理）：

1. `CLAUDE.md:284` 的 “LO plan” 经核对并非错误：`timer_saved_t` 保存的 `mode` 就是 GROUP/SINGLE，也就是 Web 界面 “LO plan” 控件；已在文中注明，避免与“频道计划”混淆。
2. ~~`PLAN.md:33` 固定 IDF `6.2.0-dev` / `firmware-targets.json`~~ —— 已改为 pioarduino 55.03.312-1 / ESP-IDF 5.5.5，并说明 esp-sdr 的 6.2.0-dev 只是 PHY ABI 参照。
3. ~~`PLAN.md` 目录树列出 `testgen_s3`、`calib.c`、`imd.c`、`analyze_capture.py` 等不存在的文件~~ —— 目录树已按当前源码重写（`radio/c5`、`radio/s3`、`dsp/psd.c`、`scan/lap.c`、`proto/rh.c` 等）。
4. ~~`MANUAL.zh-CN.md` 的 `0.7-dev` 与 `versionName` 的 `0.7` 不一致~~ —— 已改为 `0.7`。

App 两份 README 原先的四处不符（49 测试、`Not in this version`、瀑布 2 dp、跟随系统语言）已于 2026-10-27 全部按代码修正，英文与中文版同步，镜像 `repo/app` 已更新。同日修完 PLAN 的 IDF 版本与目录树、App 手册的适用版本；CLAUDE 的 “LO plan” 经核实是术语歧义（指 GROUP/SINGLE），已加注而非删改。

---

## 7. 验证现状

### 7.1 本次实际运行

| 命令 | 结果 |
|---|---|
| `cd repo/firmware && pio test -e native` | **17/17 PASSED**，2.03 s（发布前复跑） |
| `cd repo/web && npm test` | **56/56 passed**，10 个测试文件，395 ms（发布前复跑） |
| `cd app && ./gradlew :core:test --offline` | **BUILD SUCCESSFUL**；Gradle UP-TO-DATE，读取测试结果 XML：26 类 / 209 项 / 0 失败 / 0 错误（发布前复跑） |
| `aapt2 dump badging widscan58-app-0.8-release.apk` | 包名、0.8、versionCode 8、minSdk 26、targetSdk 36 均正确 |
| `apksigner verify --print-certs …apk` | v2 签名有效，证书 DN `CN=widscan58, OU=app, O=onebody, C=CN`，与 0.7 同一密钥 |
| `cd repo/web && npm run build` + `zip` | 构建成功，`dist/` 54 个文件打包为 `widscan58-web-0.8.zip` |

### 7.2 文档记录的历史验证

- C5（XIAO rev v1.0）：hands-free 烧录、R1–R8 扫描、~4.0 ms/hop、~60 更新/s；USB 电流与 PHYIDLE 数据。
- C5-Zero：WS2812 状态灯、UART 头、GPIO26 天线切换。
- RotorHazard：`rh_check.py` 8 nodes、API 36、无通信错误；同频座位圈时差中位 2 ms。
- Android：小米 25113PN0EC / Android 16 真机，CDC 枚举、128 KiB 抓包零 CRC 错、握手全通过；模拟器 411×731 dp 与 360×640 dp。
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
| 跨端一致性 | 三端各自硬编码帧尺寸 | 协议漂移靠人工对照 |

---

## 8. 风险与技术债

按影响排序。优先级是“工程建议”，不是事故等级。

| # | 优先级 | 风险 | 证据 | 影响 |
|---:|---|---|---|---|
| 1 | 高 | 依赖私有 PHY ABI、寄存器和 `phy_param` 偏移 | `radio_c5.c`、`recal_c5.c` 大量硬编码；`check_phy_abi.sh` 手动运行 | 升级 IDF/平台可能直接失效；CI 不检查 ABI |
| 2 | 高 | C5 内存余量极薄，失败是静默的 | 静态 RAM 只剩几百字节、heap ~5 KB（`CLAUDE.md:107-109,293`）；UART 曾因 3 KB 回复缓冲分配失败导致“板子活着但不回话” | 新功能可能在运行时静默失败；`commands_task_start` 分配失败直接 return（`commands.c:704-709`） |
| 3 | 高 | 固件发布不可复现 | `firmware/versions.json` 用 `upstream/main`/`origin/main` 分支引用 | 同一版本 id 的固件内容会变；无法从发布物反查 commit |
| 4 | 高 | UART/RH 实机验收没有闭环记录 | 实施计划 Task 7 全部未勾选；`docs/RotorHazard.md:205` 明确“未用真机四轴过门验证” | UART 双端口、Pi 接线、共存仲裁、真实过门阈值缺少正式证据 |
| 5 | 中 | Android 独立仓库仍无 remote | `app/` 没有远端；`repo/app` 与 fork main 的 `app/` 都是手工镜像 | 源码已随 fork 备份，但推送仍靠本地命令，镜像与独立仓库易漂移 |
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
| Android | 高 | 0.8 功能闭环、209 项 core 测试、真机验证过；mobile 层与文档滞后 |
| 工具链 | 高 | 覆盖客户端、桥、RH 校验、烧录、标定、ABI 检查 |
| 文档 | 中 | 深度很好，但新鲜度与一致性不足 |
| 发布工程 | 中 | Pages 自动化完整；固件引用分支、无 ABI/固件测试门禁、App 无远端 |

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

## 10. 本次发布（0.8）

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

APK 与 0.7 使用同一签名密钥（`~/.android/keystores/widscan58-release.jks`），已装 0.7 release 可直接覆盖安装。若手机上装的是更早的 debug 包，需先卸载（签名不同），应用内设置会被清空，板卡设置重连后会重新读取。

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
| Web 烧录 | `repo/web/src/serial/flasher.ts` |
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
