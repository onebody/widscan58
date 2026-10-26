# Widscan58 安卓版

一台装在手机上的 5.8 GHz 频谱测试仪。把 ESP32-C5 板子插进手机的 USB-C 口，应用
直接把它当串口读：全景谱、瀑布图、八个信道格子、频段与增益控制。不需要 Wi-Fi，
不需要中转电脑，不需要 root。

它是 [`../web`](../web) 浏览器界面的搭档，不是替代品。这里的帧编解码、重同步流解析
和显示引擎，都是从 `../web/src/serial/parser.ts` 与 `../web/src/model/store.ts`
移植过来的，所以两边同一个数字指的是同一件事。原生应用多出来的部分是：插上板子
应用自己就起来了，场边不需要浏览器也不需要网络，而且屏幕布局是手机浏览器标签页
给不了的完整尺寸。如果你手机上那个浏览器不支持 Web Serial，这条路照样能走。

## 运行条件

- Android 8.0（API 26）及以上，手机要支持 USB host / OTG。
- 刷了 widscan58 固件的板子。它的原生 USB 是乐鑫 USB-Serial/JTAG 口
  （VID `0x303A`、PID `0x1001`），枚举成 CDC-ACM 设备。刷固件请用电脑或网页界面
  的烧写功能先做掉——本应用不负责烧录。
- 一根能走数据、不是只能充电的线。

## 构建与安装

```sh
cd app
./gradlew :core:test :mobile:assembleDebug
adb install -r mobile/build/outputs/apk/debug/mobile-debug.apk
```

工具链：Gradle 8.14、AGP 8.11、Kotlin 2.2、JDK 17。`local.properties` 里要写
`sdk.dir=…` 指向本机的 Android SDK。`:core:test` 是纯 JVM 测试，不需要设备。

然后插上板子，在弹出的 USB 访问授权里点允许。应用注册了 `USB_DEVICE_ATTACHED`
过滤器，所以插上板子这一步本身就能把应用拉起来。

## 文档

- **图文教程在应用里**：`⋯ → 使用帮助`。它是打包进 APK 的本机页面
  （`mobile/src/main/assets/help/`），不联网、也没有为它申请任何权限，板子没插也能看。
  目前只有中文。
- **操作手册**是 [`docs/MANUAL.zh-CN.md`](docs/MANUAL.zh-CN.md)：按功能逐项列控件取值
  和它对应的固件命令，另有帧格式与 `.widscan58` / CSV 文件结构。同样只有中文。

## 屏幕上都有什么

| 区域 | 内容 |
| --- | --- |
| 状态行 | profile、每秒旋转数、hop 数、增益、链路占用、链路名 |
| 频段按钮 | 一键把整个频段（R/A/B/E/F/L）下发到设备；`Max`/`Avg` 开关两条保持迹线 |
| 全景谱 | 所有 LO 频段按"最近 LO"拼起来：实时迹线、最大值保持、平均、逐格噪声底、每信道的 RSSI/搜索窗、规划 LO 标记、板子自身的杂散 |
| 瀑布图 | 每完成一次扫描旋转压一行，最新在顶上，按平滑后的噪声底定标；每行 2 dp，所以一部手机上大约留 45 次旋转的历史 |
| 信道格子 | 电平（上报时用 EMA）、以 floor−10…floor+40 dB 为量程的条、SNR、噪声底、徽标、10 秒小折线 |
| 底栏 | 开始/停止、手动增益 0…63、清空保持 |

点图表或点格子都能选信道。长按图表会把 dB 量程自适应到当前画面（这个设置会记住）。
规划还没下发之前格子是隐藏的，图表显示"等待频谱帧…"，而不是画一条假的空扫。

只支持竖屏是故意的：没有横屏布局，横过来会把全景谱压成一条细缝。

## 和板子怎么说话

先 `PROTOCOL MONITOR`，因为板子开机时处在 RotorHazard 模式，没人问就一个字节都不发（见
[`../docs/RotorHazard.md`](../docs/RotorHazard.md)）。接着 `SCAN STOP`、存好的规划
（`CHANNELS R1=5658 … R8=5917`、`MODE GROUP`、`GAIN MANUAL 45`）、
`SPEC 4 256 40000000 MAX`、`PROFILE?`、`STATUS?`、`SPURS?`、`SCAN START`。

帧是二进制的 `0xA5 'R' type ver | u16 len | payload | u32 crc32`；`PROFILE` 和
`STATUS` 回 JSON。所有解析都在 `:core` 里完成，跑在 USB 读线程上，回到主线程只做
应用。控制线只拉 DTR、绝不拉 RTS：rev 1.0 及更早的 C5 上，一次 RTS 复位可能让 USB
一直到重新上电才活过来。

扫描停下来之后板子仍然以大约 4 Hz 发 `TELEMETRY`，这正是看门狗区分"链路死了"和
"只是闲着"的依据——3 秒没有任何帧就说明板子重启过，应用会自己把握手再走一遍。

## 模块划分

- `core/` —— 纯 Kotlin/JVM：帧解析、CRC、流重同步、命令器、频段表、store（分段、
  最大值保持、平均、逐 LO 噪声底）、全景拼接、瀑布行、信道格子。不 import 任何
  `android.*`，49 个测试覆盖。
- `mobile/` —— Android 部分：USB CDC host（`usb/`）、JSON 回复编解码、偏好存储、
  仪表界面和它的画布视图。

`core/` 里没有任何一处知道"USB 端点"是什么。所以这个应用里真正有意思的 bug，
全都出在 `mobile/usb/`。

## 语言

中文是默认，英文是唯一一门翻译：`res/values/` 放中文，`res/values-en/` 放英文，
所以应用跟随系统语言，系统设成其它语言时回落中文。Android 13+ 的系统级"应用语言"
选择器里两门都有（`res/xml/locale_config.xml`）。英文文档在同目录
[`README.md`](README.md)。

单位符号（`dBFS`、`MHz`、`SNR`）、频段字母，以及原始 USB 诊断信息（探测屏和
`UsbLinkException` 的文字）两门语言下保持一致，因为那些正是你要贴进 bug 报告的
内容。

## 手边没有板子时

`SimFeed` 是一个只能靠一条 intent 参数启动的设备模拟器，让这套界面在完全没有硬件
的时候也能排版和演示——和 `../web/src/mock` 给网页界面做的事一样：

```sh
adb shell am start -n com.onebody.widscan58/.Widscan58Activity --ez sim true
```

它发出的 SPECTRUM / METRICS / TELEMETRY 载荷与固件在一群 VTX 加板载杂散的情况下
一致，也会响应频段、增益和开始/停止操作，完全不碰 USB 协议栈。除了这条命令没有
别的途径能启动它。

## 这一版还没有

信道放大图、RSSI 历史页、信道规划编辑器、RotorHazard 模式、功率计模式、RAW IQ
采样、诊断控制台、录制与回放。日志/探测屏作为启动器的 **USB 探测** 入口保留着，
用于板子出现了但不认识它是 CDC 口的情况。

## 验证情况

`:core:test` —— 49 个 JVM 测试，覆盖帧编解码、流重同步、命令器、store、全景拼接、
瀑布行和信道格子。

界面在 `arm64-v8a` 模拟器（Android 11，1080x1920）上对着模拟数据源跑过 411x731 dp，
并用 `adb shell wm density 480` 跑过 360x640 dp。语言切换是按设备真正的方式验的
——`settings put system system_locales` 加重启，让 `mCurrentConfig` 里出现
`[zh_CN]`——而不是靠改资源目录名。

对着板子本体、走它的 CDC-ACM 口：128 KiB 抓包解出 150 帧 SPECTRUM、902 帧 METRICS、
22 帧 TELEMETRY，**CRC 错误为零**，码率是板子在 monitor 模式下持续的约 370 KB/s；
握手里的每一条命令都被接受，`INFO` 回答 `widscan58|esp32c5|1|78d1c81|…`。只有真板子
才能暴露的三件事：USB-Serial/JTAG 的描述符里，紧挨着 CDC 数据接口还有一个 class
`0xFF` 的厂商接口，它自己也带一对 bulk 端点，所以"取第一个有 bulk 对的接口"是错的
判据；`CHANNELS?`、`MODE?`、`SPEC?` 根本不是命令；扫描停下后仍在发遥测帧。

在真手机上——小米 25113PN0EC、Android 16、1220x2656 @ 520 dpi——板子就插在手机上
自己那个口，系统日志显示 attach 事件带的是
`/dev/bus/usb/001/010, mVendorId=12346, mProductId=4097, mClass=239`，activity 由
`USB_DEVICE_ATTACHED` 拉起，仪表正常出扫。这一趟发现了三个模拟器结构上不可能暴露的
问题：Android 15+ 会把内容画到状态栏和手势条底下，所以内容视图要按系统栏 insets
自己加内边距；各家 `DeviceDefault` 主题对按钮大小写的处理不一致；以及
`UsbInterface.getEndpoint(int)` 的参数是**数组下标**，不是方向与端点号拼起来的值，
所以端点查找必须改成遍历该接口的端点表。

## 许可

GPL-3.0-or-later，与它所移植的固件、网页界面同一许可；作者以及其依赖的 ESP32-SDR /
C5VRX 工作，在顶层 [`README.md`](../README.md) 的 Credits 一节里署名。
