# RF Track 落地实施方案与计划 v1.0

> 编写：2026-10-28。依据：`docs/rf-track/RF_Track_Widscan58_Qoder_Implementation_Plan.md`（总规范，逐阶段验收纪律不变）+ 阶段 0 审计（`docs/architecture/current-state.md`、`feature-inventory.md`、`data-flow.md`、`risk-register.md`）+ 技术栈决策（`docs/architecture/target-stack-landing.md`）+ LLM 增补（`docs/architecture/llm-integration.md`）。
> 技术栈（用户拍板）：**FastAPI + SQLAlchemy + Pydantic + SQLite(WAL) + Alembic** 后端，**Vue3 CDN 无构建**前端，**配置与事件入库、原始采样只留摘要**。
> 部署与层级（用户 2026-10-28 追加定调）：**支持多场地，每场地下多台中继（网关）与多个节点；整套系统本地化部署为主，不进行云端部署**（原 F3「Pi→云上行钩子」作废，跨机器只走局域网 WS 与本地归档包）。
> 分支拓扑（用户 2026-10-29 定调）：**`rf-track` 线永不合入 `main`，两条线隔离、各自向下迭代**。`main` 继续承载单节点 web/固件的发布线（Pages 只从 `main` 构建，线上 rh-uart 固件因此永远停留在 F1 之前）；`rf-track` 线的产品分发走"每场地一套本地实例"（serve.mjs + `rftrack/backend`），不经 GitHub Pages。两线之间如需吸收更新，只允许 `main` → `rf-track` 单向，反向永不合入。
> 标注：**[已由源码确认]** / **[已由本机实测]** / **[已由真机实测]** / **[根据源码推断]** / **[需要实机验证]** / **[当前无法确认]**。

---

## 1. 业务规则与状态机

| 对象 | 状态机 | 触发者 |
|---|---|---|
| 场地 Site | `active → archived`（archived 后停止拨号、保留数据、禁止再挂接入机） | 用户在场地管理页操作；**删除只允许 archived 且不静默清数据** |
| 中继（物理位置）Relay | `active → archived` | 用户管理；archived 位置下的接入机必须先行迁走或停用，**不允许出现"位置没了但还在拨号"** |
| 接入机 Gateway | `unprobed → dialing → online → unreachable → disabled` | 后端 dialer 拨号（`base_url` 可达性 + `/api/endpoints` 版本协商）；`unreachable` 由连续失败/静默判定，**不得**因为一次超时就把整台机器判死 |
| 节点 Node | `offline → connecting → online → degraded → lost` | 后端 ingest（WS 连接结果 / 静默超时 / CRC 错误率）**[已由源码确认：serve.mjs 45s 心跳语义可沿用]**；**接入机 `unreachable` 时其下所有节点必须连带转 `lost`，不能各自超时** |
| 汇聚外场 RemoteSite（仅汇聚实例） | `syncing → fresh → stale → unreachable` | `aggregate.py` 定时拉取；`lag_s > AGGREGATE_STALE_S` → `stale`，**UI 必须置灰并显示最近同步时刻，禁止把上次值当实时值** |
| 信号事件 SignalEvent | `new → acked → silenced → resolved` | alert-engine 产生；人工 ack/silence |
| 候选目标 CandidateTarget | `unreviewed → confirmed \| rejected` | 人工复核（算法只能改置信度，不能改复核态）**[规范 §7.3]** |
| 工作台布局 Layout | `draft → active → archived` | 用户保存/切换；**`is_active` 唯一性按场地成立**（各场地各自的当前布局） |
| 后台插件 Plugin | `stopped → starting → running → failing → stopped` | 用户在插件管理页启停；supervisor 捕获异常转 `failing` |
| 板卡归属 BoardLink | `rh ↔ scan`（同一块板二选一） | 页面按钮 → `POST /api/mode`，**不得绕过** **[已由真机实测]** |

层级与配置优先级（2026-10-28 用户两轮定调：**多场地 / 场地内多中继多节点 / 不做云端 / 每场地一套实例 + 局域网只读汇聚**）：

```text
Site 场地 ──1:N── Relay 中继（物理位置：起点塔/看台/计时区…）
                       ──1:N── Gateway 接入机（一台跑 serve.mjs 的机器，一个位置可多台）
                                     ──1:N── Node 节点（一块 C5）──1:N── slot 频道
生效值优先级：内置默认 < 全局(app_config) < 场地(site) < 布局(layout) < 实例(widget)
节点全局唯一键：(gateway_id, transport_addr)  ← 不是裸名字；两个场地都可能有 /dev/widscan58-2
实例角色：instance_role ∈ {site(拨本场地的接入机) | aggregator(只读汇聚，不拨号不透传)}
```

硬边界（继承规范 §2.3，本方案不放松）：离散频道 RSSI **不得**渲染为连续频谱；无真实频率轴帧时只显示「频道活动热图」；LLM/算法推导内容必须带 `producer` 与「AI 推导」徽标，物理隔离于观测时间线；模拟源必须带 `source='mock'|'demo'`。

---

## 2. 目标拓扑与已核实的环境事实

```text
【场地实例：每个场地一套后端 + 一个 SQLite；下面就是场地 A 的那一套】
        ┌──────────── 接入机 G1 = Pi4b 203.0.113.183（位置 R1 起点塔）────────────┐
 C5 ×N ─┤ /dev/ttyAMA0 ← RotorHazard 4.5 (8 节点)   ← C5 #1 (UART GP11/12)        │
        │ /dev/widscan58-<SN> → web/serve.mjs :8080 ← C5 #2..N (USB，已实测)      │
        │ ws://…:8080/node?id=<name>                ← S3 桥/远程节点（F2，后置）   │
        └──────────────┬──────────────────────────────────────────────────────────┘
        ┌─ 接入机 G2（同位置 R1 的第二台机器）┐  ┌─ 接入机 G3（位置 R2 看台）┐
        │ /dev/widscan58-* → serve.mjs :8080  │  │ …                        │   ← 同一套 serve.mjs，零改动
        └──────────────┬──────────────────────┘  └──────┬───────────────────┘
                       │ 每端点一条 WS 客户端（serve.mjs 单客户端语义）
   ┌───────────────────▼──────────────────────────────────────────────────────────────┐
   │ rftrack FastAPI  instance_role='site'  (:9000, uvicorn 单 worker, 只听局域网)     │
   │  dialer(只拨本场地 relay/gateway) → ingest(每端点单写者锁) → store(环形,键=(gw,addr,slot))│
   │  有界队列(每接入机一条) → anomaly / interference / alert 插件 → SQLite(site_id 贯穿)│
   │  REST /api/*  ·  WS /ws/live  ·  WS /ws/bridge/{ep}(透传，一条连接只属一个接入机)  │
   │  只读出口 GET /api/aggregate/summary|events（供汇聚实例拉，不含原始字节/写能力）    │
   └───────┬──────────────────────────────────────────────────────────────────────────┘
           │
   Vue3 工作台（状态栏常驻 场地 A · R1 起点塔 · G1 · widscan58-2）      原 web/ SPA（零改动，改指 ?bridge=ws://…:9000）

【汇聚实例：同一份代码，另一台本地机器，只读】
   rftrack aggregator (instance_role='aggregator')
     定时 GET 各场地实例 /api/aggregate/*（只读 token）→ site(origin='aggregated', last_sync_at, lag_s)
     不提供 /ws/bridge、不建 dialer 任务、拒绝一切写；**UI 常驻「滞后 X 秒」，超阈值置灰为「陈旧」**
```

**部署边界（2026-10-28 用户两轮定调，覆盖原 F3 设想）**：本地化部署为主，**不进行云端部署**。跨机器只有三条通道——场地内局域网 WS（实时）、汇聚实例的只读拉取（近似、滞后）、`GET /api/export/site/{id}` 归档 ZIP 在另一台机器导入（完整搬家）。层级定为 **场地 → 中继（物理位置）→ 接入机（一台跑 serve.mjs 的机器，一个位置可多台）→ 节点**：用户明确"场地下面既有多个物理位置，也可能有多台同位置机器"，所以位置与机器不能合并成一张表。

**环境事实（本轮实测）**

- Pi：Python **3.13.5**、pip 25.1.1、node 20.19.2、SQLite 3.46.1、内存 905 MB（约 460 MB 可用）、`widscan58-web` 与 `rotorhazard` 均 active。**[已由真机实测]**
- Mac：Python 3.14.5 + FastAPI 0.133.1 / SQLAlchemy 2.0.47 / Alembic 1.18.4 / Pydantic 2.12.5 / uvicorn / httpx / pytest / pyserial / websockets 齐备。**[已由本机实测]**（注意 pioarduino 拒 3.14，固件与后端两条链路不得共用解释器）
- 双 C5 USB 板并跑实测：单端点 ~36–37 KB/s、60 圈/s、CRC 错误 0，RH 同时保持 8 节点；第二客户端连同一端点被 4096 拒绝。**[已由真机实测]**
- UART 上的板子**不能扫频**（`commands.c` 对 UART 回 `ERR uart_port`）→ 拓扑唯一可行解：RH 用 UART 板，扫频用 USB 板。**[已由真机实测]**
- Pi 数据落在 SD 卡 → 高频写会磨损，故 `sample_summary` 桶粒度 ≥1 s、批量事务、WAL，并保留「只内存不落库」开关。**[根据源码推断]**

**接入层的复用决定（本方案的关键修正）**：后端不自建串口层，而是 `serve.mjs` 的下游消费者。理由：`serve.mjs:101,102,177,244,292,494-499` 的端点枚举、busy/replaced、掉线心跳、`/node?id=` 中继、`/api/mode` 停启 RH 均已在真板闭环验证；Python 重写等于把这些坑重踩一遍，且违背规范 §2.1「保护原有功能、增量扩展」。**[已由源码确认]**

---

## 3. 数据模型（Alembic 增量，严禁重置库）

表清单与字段见 `target-stack-landing.md` §5。多场地定案后对表结构的改动是：**新增 `relay`（物理位置）与 `gateway`（接入机）两级**、`site` 从"预留"升为一等实体、`workbench_layout.site_id` 由可空改必填、`signal_event`/`sample_summary` 增冗余 `site_id`；汇聚形态另加 `app_config.instance_role` 与 `site.origin/last_sync_at/lag_s`。本节只补落地纪律：

| 迁移 | 内容 | 判据 |
|---|---|---|
| `001_baseline` | `app_config` + **`site`** + **`relay`** + **`gateway`** + `node` + `channel_plan(_item)` | 空库可建、重复执行幂等；`node` 的唯一键是 `(gateway_id, transport_addr)`；`gateway.relay_id`、`relay.site_id` 外键与级联限制生效；**首版就建三级层级，不留"后补场地列"的迁移债**（`rftrack/` 尚未创建，此刻改模零代价） |
| `002_layout` | `workbench_layout`(**site_id 必填**, relay_id?) + `layout_widget` | 实例 id 唯一约束生效；**`is_active` 唯一索引是 `(site_id)` 部分唯一**——A 场地切布局不得影响 B 场地的当前布局 |
| `003_events` | `signal_event` + `sample_summary`（`(node_id,slot,ts_bucket)` 唯一 + `(site_id,node_id,slot,ts_bucket)` 查询索引） | 10 万行批量写 < 2 s（Pi 实测）**[需要实机验证]**；按 `site_id` 过滤不得走全表扫 |
| `004_plugins` | `plugin_registry` + `plugin_state` + `plugin_log` | dropped_count 可写；规则/插件的 `scope` 能锁到场地或位置 |
| `005_targets` | `alert_rule` + `alert` + `candidate_target` + `track_observation` | 复核字段与算法字段分列；`alert_rule.scope ∈ {global,site,relay,gateway,node}` |
| `006_import` | `local_settings_import` | 导入可回滚（保留原 payload） |
| `007_aggregate` | `app_config.instance_role` + `site.origin/last_sync_at/lag_s` | **汇聚是独立一支迁移**：场地实例升上来后 `instance_role='site'`，字段全空即"本场地数据"；新增列全部可空，符合"新增列一律可空"纪律 |

保留策略（`config.py` 全部可 env 覆盖，默认值即落地值）：`RETENTION_EVENTS_DAYS=7`、`RETENTION_SUMMARY_DAYS=30`、`RETENTION_LOG_ROWS=5000`、`SUMMARY_BUCKET_S=1`、`RAWIQ_PERSIST=false`。时间统一 aware UTC。**[规范 §2.4]**

**多场地带来的三项新增强制项**（原设计里没有，现在必须有，否则"多场地"只是界面上多个名字）：

1. **按场地覆写保留**：`site.retention_events_days/summary_days` 为空则继承全局——SD 磨损与库体积按场地分级，大场地可以只留 3 天摘要。**[根据 R11 推断]**
2. **按场地清理**：retention 任务必须逐场地跑，并把 `site_id`/删除行数打进结构化日志（**不为此新开表**），不能一条 `DELETE WHERE ts<…` 把两个场地的数据一起删。
3. **场地归档**：`GET /api/export/site/{id}` 用 `sqlite3.Connection.backup()` 出完整库再按 `site_id` 过滤打包（禁止 `VACUUM INTO` 后手工裁剪），导入侧校验清单与 `schema_version`。

---

## 4. API 契约

前缀 `/api/*`，健康检查 `/api/health`，端口基线 9xxx（后端 9000，各场地网关的 serve.mjs 保持 8080 不动）。

| 端点 | 说明 | 关键校验 |
|---|---|---|
| `GET/POST/PATCH/DELETE /api/sites[/{id}]` | 场地 CRUD | 删除仅允许 `archived` 且无在线接入机→否则 409；**删场地不清数据**（要清走归档导出后人工） |
| `GET/POST/PATCH/DELETE /api/relays[/{id}]` | 中继（物理位置）CRUD，挂 `site` 下 | 位置 `archived` 前必须先迁走/停用其下接入机，**不允许"位置没了但还在拨号"** |
| `GET/POST/PATCH/DELETE /api/gateways[/{id}]` | 接入机 CRUD，挂 `relay` 下（`name`+`kind`+`base_url`+`serial_prefix`） | `base_url` 必须是局域网地址；**拒绝任何公网/云地址**（本地化部署定调） |
| `POST /api/gateways/{id}/probe` | 连通性**实测**：拨 `base_url`、取 `/api/endpoints`、报真实延迟与失败原因 | 不得只回"已保存"；失败原因要分「DNS/拒绝/超时/版本不符/端点为空」 |
| `GET/POST/DELETE /api/aggregate/remotes` `POST /api/aggregate/sync` `GET /api/aggregate/status` | **仅汇聚实例**：登记远端场地、手动触发、看 `last_sync_at/lag_s/失败原因` | 同步失败必须显式呈现，不得静默沿用上次数值冒充新鲜 |
| `GET /api/aggregate/summary\|events` | **仅场地实例**：对外只读出口（供汇聚拉），只给摘要与事件 | 只读 token 鉴权；**不含原始字节、不含写能力、不含 `/ws/bridge` 升级路径** |
| `GET/PUT /api/settings/global` | 实例级全局设置，`version` 乐观并发 | 422 逐字段错误；409 版本冲突 |
| `GET/PUT /api/settings/site/{id}` | 场地级设置（保留天数/时区/告警默认值） | 返回**生效值 + 来源链**（默认<全局<场地） |
| `GET/POST/PATCH/DELETE /api/layouts[/{id}]` | 保存/切换/重命名/删除（均在 `?site=` 内） | 版本不兼容→降级并返回原因，不 500 |
| `POST /api/layouts/{id}/clone` `POST …/activate` | 另存为 / 切换 | `is_active` 唯一**按场地**成立 |
| `PUT /api/layouts/{id}/widgets/{instance_id}` | 实例局部配置 | 返回**生效值 + 来源链**（默认<全局<场地<布局<实例） |
| `GET /api/plugins` `POST /api/plugins/{id}/enable\|disable` `GET …/state\|logs` | 启停幂等、状态与日志 | 停用的插件队列必须排空且不丢计数；`scope` 能限定到单场地 |
| `GET /api/nodes?site=&gateway=` `PUT /api/nodes/{id}` | 节点/能力（capabilities 显式声明） | 缺能力字段→`unsupported`，不得补造；**节点身份键 `(gateway_id, transport_addr)`，禁止按显示名全局去重** |
| `GET /api/endpoints?gateway=` `POST /api/board/mode` | 代理**指定网关**的 serve.mjs 端点表与 RH/scan 切换 | 只转发不改语义 **[已由真机实测]**；多网关后必须带 `gateway` 参数，否则不知道该切哪台 |
| `GET /api/events[/{id}]` `GET /api/targets[/{id}]` `POST /api/targets/{id}/review` | 事件流 / 候选目标 / 人工复核 | 详情必须返回证据链与局限说明；`?site=` 缺省=当前场地，**不默认跨场地全表扫** |
| `GET /api/alerts?site=` `POST /api/alerts/{id}/ack\|silence\|resolve` `/api/alert-rules` CRUD | 告警闭环 | 去重键唯一；规则必须能锁到场地，A 场地阈值不得惊动 B 场地 |
| `GET /api/samples/summary?site&node&slot&from&to&bucket` | 趋势图取数 | 桶粒度参数有白名单，禁止任意全表扫 |
| `GET /api/export/site/{id}` · `POST /api/import/site-package` | **本地归档**（ZIP：库快照+配置+清单）——跨机器/跨场地的唯一数据流动通道，取代任何云上传 | 导入前校验 `schema_version`；`backup()` 取快照，禁止热库直接 copy |
| `WS /ws/live?site=` | 当前值 + 事件推送 | 客户端断开必须清理订阅（无泄漏）；**订阅必须按场地隔离，A 场地推送不得混入 B 场地事件** |
| `WS /ws/bridge/{ep}` | **字节透传**，让原 SPA 经后端连板 | 只读扇出 + 写命令单写者锁；**一条连接只属一个 gateway** |

> **S2 已落地部分与前两条口径差**（其余行仍属 S3+）：三级 CRUD、`probe`、`GET /api/nodes`（+ `/api/nodes/{id}`）、`GET /api/health` 已在。①**查询参数名按列名直出**——实建是 `?site_id=`/`?relay_id=`/`?gateway_id=`，本表草稿写的是 `?site=`/`?gateway=`；S3 接前端时以本表为准统一，别让两套名字同时存在。②`PUT /api/nodes/{id}` **未做**：节点行由 S3 的 ingest 产生，S2 只有读。③`probe` 失败分类里的"版本不符"暂时以 `unexpected_response`（响应结构不符）体现，因为 serve.mjs 不吐版本字段——见 §7 S2 行偏差①。④`DELETE` 一律 204 且**不级联**，比草稿多写的 `GET /api/health` 的 `problems` 数组是为了让"未迁移库"给出可执行的下一步而不是 500。

> **S3 已落地部分与 §4 表的口径差**（其余行仍属 S4+）：`GET /api/endpoints?gateway=`、`POST /api/board/mode?gateway=`、`WS /ws/bridge/{ep}` 三行已在，读侧筛选参数已统一回本表的短名（`?site=`/`?gateway=`，S2 那套 `?site_id=` 不再存在）。四条差异：①**`/ws/bridge/{ep}` 的 `gateway` 是必填查询参数**——本表只写了路径，但"一条连接只属一个 gateway"必须有个地方说清是谁，缺参直接以 `1008 gateway_required` 拒绝而不是猜一台。②**上游就是桥自己的 `/serial?ep=<id>`**，后端不另发明路径、也不预校验 id 存不存在（桥会用 `unknown endpoint` 回答，那个理由比我们的猜测可信）；`serial:`/`uart:`/`node:` 三种 id 由桥自己分发，`uart:` 会连带停 RotorHazard。③**汇聚角色下这条 WS 以 `1008 aggregator_role` 拒绝**，比 S11 行写的"写端点 405"提前发生——WS 没有 405 语义，关闭码是唯一能把理由带到页面上的通道（所以先 `accept` 再 `close`）。④`PUT /api/nodes/{id}` **仍未做**：节点行今天只有 ingest 这一条写路径，"显式声明能力"这条对客入口按阶段边界留给需要它的那一端（`fw_version`/`protocol_version`/`capabilities_json` 三列一律 NULL，读侧渲染 `unsupported`，不代填）。

---

## 5. 后端业务逻辑分层（router 薄 / service 厚）

```text
rftrack/backend/app/
├── config.py        DB_PATH/BIND/PORT、QUEUE_LIMIT、RETENTION_*、SUMMARY_BUCKET_S、BRIDGE_BASE_URL(ws://pi:8080)
│                    新增：INSTANCE_ROLE(site|aggregator)、MAX_GATEWAYS、MAX_NODES_PER_GATEWAY、DIAL_CONCURRENCY、
│                    AGGREGATE_INTERVAL_S、AGGREGATE_STALE_S、AGGREGATE_READ_TOKEN
├── database.py      engine(WAL)+SessionLocal+Base
├── models.py        单文件（技能基线）：Site / Relay / Gateway / Node / …
├── schemas.py       Pydantic：全部配置 Schema、Layout v1、事件/目标出参、Site/Relay/Gateway
├── deps.py          get_db、require_read_token（汇聚出口与 /api/aggregate/* 用；比 RBAC 轻得多）
├── routers/         settings.py layouts.py plugins.py nodes.py events.py targets.py alerts.py samples.py bridge.py ws.py
│                    新增 sites.py relays.py gateways.py archive.py aggregate.py
└── services/
    ├── frame_parser.py    0xA5'R' + CRC32 + 失步重同步（镜像 web/src/proto/frames.ts）
    ├── dialer.py          **只拨本场地的 relay→gateway**（配置里出现远端场地地址直接报错，不静默接受）；
    │                      建 WS → 枚举端点 → 交给 ingest；退避重连、`unreachable` 判定、其下节点连带转 lost
    ├── ingest.py          端点表 → 节点行对账（身份键 `(gateway_id, transport_addr)`、消失只转 lost 不删行、
    │                      超限在任何一行动手之前就 409 并点名是哪些端点）；~~重连退避/单写者锁~~：这两件事
    │                      实建分属 `dialer.py`（退避与 `unreachable`）与 `ws_bridge.py`（单写者锁），不塞在 ingest 里
    ├── ws_bridge.py       **S3 实建，草稿里没有这个名字**：`/ws/bridge/{ep}` 的调度核心——按端点开一条上游、
    │                      只读扇出给 N 个页面、写命令单写者锁（抢不到回桥自己的那个词 `busy`）、上游死把
    │                      code/reason 原样传出去、最后一页走把端点关回 1000 交还给桥
    ├── store.py           内存环形缓冲（每节点每 slot 上限点数；总点数受 MAX_GATEWAYS×MAX_NODES 约束）
    ├── retention.py       桶聚合批量写；按场地覆写保留天数；逐场地清理并打结构化日志
    ├── config_layer.py    优先级生效值计算 + 来源标注（默认<全局<场地<布局<实例）
    ├── registry.py        内置插件注册表（无动态代码加载）
    ├── archive.py         场地归档导出/导入（backup() + 清单校验 + schema_version）
    ├── aggregate.py       **仅 aggregator 角色**：定时 GET 远端 /api/aggregate/*，写 site(origin='aggregated',
    │                      last_sync_at, lag_s)；失败保留旧数据但标记 stale，绝不"沿用即新鲜"
    └── plugins/{anomaly_detector,interference_correlator,alert_engine}.py
```

- 队列：`asyncio.Queue(maxsize=QUEUE_LIMIT)`，满载丢最旧并 `dropped_count++`，禁止阻塞 ingest 读循环。**[规范 §2.4/§6.5]**
- **多接入机的队列形态必须定死**：采用「每 gateway 一条有界队列 + 一个共享消费池」，而不是每节点一条。理由：`serve.mjs` 端点已经是单客户端，每台接入机的字节速率有上限（实测 ~37 KiB/s/板），按接入机排队能天然隔离"起点塔打满不影响看台"；按节点排队则队列数随节点数线性、且每队列都得重复一次帧解析。**[根据真机带宽实测推断，队列数上限仍需压测确认]**
- **总并发上有硬闸**：`DIAL_CONCURRENCY` 限制同时拨号的接入机数，`MAX_NODES_PER_GATEWAY` 限制单机订阅端点数；超限返回明确 429/409 文案，不得静默丢弃新接入机（否则用户会以为"加了机器怎么没数据"）。默认值待 S9 在 Pi 上压测定 **[需要实机验证]**。
- **角色约束要写成断言而不是约定**：`instance_role='aggregator'` 时，启动即拒绝创建 dialer 任务、路由表里摘掉 `/ws/bridge`、所有写端点返回 405。**[根据"汇聚数据可能被误当实时"的风险设计]**
- 插件异常：supervisor `try/except` 捕获 → `plugin_state.status='failing'` + 退避重启（上限 3 次），采集链路不受影响。**[规范 §10.4]**
- 布局保存/切换**不触碰** ingest 与插件生命周期；**切位置/切接入机同样不触碰**（只换视图与订阅，后台拨号与插件继续跑）。**[规范 §10.4]**
- 帧解析必须过 §9 的对拍测试才允许接线（三份协议镜像的唯一防漂移手段）。

---

## 6. 前端工作台（Vue3 CDN 无构建）

```text
rftrack/frontend/workbench/
├── index.html            相对路径 ./，CDN 用国内镜像 + 回退，?v=YYYYMMDD
├── app.js                根实例：**场地上下文** / 工具栏 / 组件库 / 栅格 / 属性面板 状态编排
├── api.js                运行时探测子路径前缀，不硬编码；所有请求带当前 site 上下文
├── context.js            **位置/接入机选择器 + 当前场地上下文 + 跨场地不得串数据**
├── grid.js               自研 12 列栅格 + pointer 拖拽/缩放（不引第三方网格库）
├── chart.js              手绘 canvas（沿用 web/src/util/canvas.ts 的 8 色分类板与度量画法）
├── components/*.js       site-tree(场地→位置→接入机→节点) · channel-matrix · channel-detail · rssi-trend ·
│                         signal-events · channel-activity-heatmap · target-inspector · node-status
└── styles.css            CSS 变量主题（与 app_config.theme 同步）
```

- **当前对象必须写在状态栏**：状态栏常驻 `场地 A · R1 起点塔 · G1 · widscan58-2 · online`，不能只靠 URL 参数、下拉选中项或悬浮提示——四级层级之后"我现在看的是哪块板"是最容易出错事的歧义点。**[用户既往明确偏好]**
- **汇聚实例的滞后必须显式**：`origin='aggregated'` 的场地在页面常驻「滞后 X 秒 · 最近同步 HH:MM」，`lag_s` 超阈值整块置灰为「陈旧」，**不得把上次同步值画成实时曲线**。**[规范 §2.3 在汇聚场景的延伸]**
- 位置/接入机选择器在**顶部工具栏**（不是设置页里）；切换只换视图与订阅，**不重启后台拨号与插件**。
- DEMO 标识：模拟源数据必须走 `source='mock'|'demo'` 字段并在 UI 角标显示。**[规范 §2.3]**
- 实例语义：`instance_id` 唯一、同类可多实例、销毁必须 `unsubscribe` + 清定时器。**[规范 §3.3/§10.1]**
- 与原 SPA 共存：工作台顶部保留原页面入口链接（`?bridge=ws://…:9000/ws/bridge/{ep}` 或 Mac 本机 Web Serial），原页面在验收前不移除。**[规范 §2.1]**
- **没有任何云相关入口**：不做登录云账号、不做"同步到服务器"、不做远程分享链接；需要跨机器就给"导出场地归档包"按钮（指向 `GET /api/export/site/{id}`）。**[用户决定]**
- 移动端适配（S6 之后）：≤640px 卡片化、底部 Tab、按钮 ≥44px、输入字号 ≥16px。**[技能基线]**

---

## 7. 阶段计划（每阶段做完即停，等用户确认）

| 阶段 | 目标 | 新增 | 改动（原代码） | 出口判据 | 回滚 |
|---|---|---|---|---|---|
| **S0** 前置清障（0.5 天）✅ **已完成于 2026-10-28 23:58，但"两批分开提交"未执行** | 工作区洁净 + 基线入库 | — | 并行会话把当时悬空的 15 个文件捆成了**一个提交** `7bec675 "Allow spectrum frames on the UART and pace them for 115200 baud"`（`git show --numstat` 实测 **1253/38**：`web/` 8 文件 +`serve.mjs`450/`main.ts`385/`package-lock.json`278/`i18n.ts`35/`package.json`3/`model/app.ts`7/`serial/device.ts`5/`styles.css`6，`firmware/` 5 文件 **+108/−14**）。**原判据"S0 提交里不含 `firmware/` 改动"落空**，且这个提交同时打了 `web-v0.10`/`Web-v0.10` 两个 tag——F1 固件与 web 网关能力从此在历史上不可分拆 revert。**这不是要返工的理由**（返工意味着改写已发布历史，代价远大于收益），而是要改变后续做法：F1 的结论只能**向前**修正（真机首轮已测过：UART 6.80 KiB/s、558 帧 CRC 全对；长跑堆/drops 另开新提交补），不能再靠"拆提交"隔离；同理，任何 UART 侧回归失败都**无法只回滚固件**而不带上 web。 | ✅ `git status` 只剩刻意不提交的 4 个 untracked（3 份采集记录 + 任务书）；✅ 四套基线在 `d53651b` 上亲手复跑全绿（见 S1 tag 消息）；❌ web/firmware 未分开（已不可挽回，记录为事实） | 该提交可单独 revert（但会把 web 网关能力一起带回退，见上） |
| **S1** 产品开发线（0.5 天）✅ **已完成 2026-10-29** | `develop/rf-monitor-platform` + tag | — | 无 | 实建：分支 `develop/rf-monitor-platform` 与**注释 tag `rftrack-baseline-v0.1.0`** 同指 `d53651b`。**tag 名与草稿不同**——仓库既有惯例是 `rftrack-` 前缀（`rftrack-m0` 指向审计态 `015018b`），草稿里的 `rf-track-baseline-v0.1.0` 带连字符不合规矩，故按实建名回写；tag 消息里写明了四道闸的绿结果，并写清两件事：`7bec675` 的 F1 **已由写它的那路会话烧板并实测**（UART 6.80 KiB/s、558 帧 CRC 全对，本审计未复测、长跑未测），以及 **M0-3 真机 60 s 录制仍欠**——防止这个 tag 被误读成硬件签收。分支拓扑已确认：`origin`=CapnBry/widscan58（上游只读）、`fork`=onebody/widscan58。**注意分支基线落在 `d53651b`，而更正 F1 状态的那个纯文档提交 `6f6fe76` 只在 `rf-track` 上**——S2 开工第一步是 `git checkout develop/rf-monitor-platform && git merge --ff-only rf-track`（tag 不动，它钉的就是那份"四道闸全绿的代码树"）。 **（这条 ff 已于 2026-10-29 随 S3 收尾补做，做法与结果见 S2 行末"分支事实"。）** | 删分支 + 删 tag 即可（两者均未推送） |
| **S2** 后端骨架（2–2.5 天）✅ **已完成 2026-10-29** | FastAPI 分层 + `site`/**`relay`**/`gateway`/`node` 建模 + Alembic `001` + `migrate.py` + `/api/health`（含各接入机拨号态） | `rftrack/backend/**`（实建 21 个非测试 `.py` 共 1772 行 + 7 个测试 `.py` 共 1068 行；另 `alembic.ini`/`pytest.ini`/`script.py.mako`）, `rftrack/requirements.txt`（钉版 fastapi 0.133.1 / uvicorn 0.41.0 / SQLAlchemy 2.0.47 / alembic 1.18.4 / httpx 0.28.1 / pytest 8.4.2）, `.gitignore`（本地库与 `.backups/`） | 无 | 实建：`pytest rftrack/backend/tests` → **104 passed**（TestClient，未起临时 dev server）＝ health 8 / 迁移 10 / 三级 CRUD 29 / base_url 校验 28（参数化）/ probe 16 / 节点读取 13。**出口判据逐条**：✅ 三级 CRUD 全在（`GET/POST/PATCH/DELETE`，删除只认"已归档 + 下级真空"，11 个 409 reason 词表（`duplicate_slug`/`duplicate_relay_name`/`duplicate_gateway_name`/`site_archived`/`relay_archived`/`site_not_archived`/`gateways_online`/`site_has_relays`/`relay_has_gateways`/`gateway_has_nodes`/`no_base_url`），绝不级联删数据）；✅ `probe` 的失败分支可测（`dns`/`refused`/`timeout`/`http_status`/`unexpected_response`/`empty_endpoints`/`no_base_url` 七类，用注入的假 transport 打，不碰网络）；✅ `instance_role` 缺行、NULL、甚至**表都不存在**三种情况一律回 `site` 且 200；✅ 迁移三场景各验一次（空库 `create_all`+stamp / 有数据无版本 stamp_only / 正常 upgrade head），**另加一条草稿没有的防线**：两条建库路径产出的结构必须逐表比 `PRAGMA table_info`/`foreign_key_list`/`index_list` 语义事实相等（不比 DDL 文本，SQLite 不记 FK 名）。**三条如实申报的偏差与补充**：①**"版本不符"目前落不了地**——实测 serve.mjs 的 `/api/endpoints` 只回 `{endpoints:[…]}`，**没有任何版本字段**（另有 `/api/mode`），所以 R20 那条只能在结构不符时以 `unexpected_response` 形式体现；真要版本协商得改 `web/serve.mjs`，那是 S3 的选择题，不在 S2 偷做。②§1"不得因为一次超时就把整台机器判死"在草稿里没有落点，实现补了 `gateway.dial_failures` 连续失败计数 + `dial_fails_to_unreachable=2` 阈值（首次失败只写 `dial_reason`，`dial_state` 保持不动）——这是把既有规则变成数据，不是新功能。③`/api/health` 多了 `problems` 数组，且**库里读不到时 `gateways` 如实回 `null` 而不是报一堆 0**。显式未做（按阶段边界留给 S3+）：节点写路径（`ingest`）、后台拨号循环（health 里 `dialer.running` 硬编码 `false`，如实报未运行）、`channel_plan`/`channel_plan_item` 只建表无接口。**分支事实**：S1 行写的"S2 开工第一步 ff `develop/rf-monitor-platform`"未执行，代码全程留在 `rf-track` 上；已核实 `d53651b` 是 `rf-track` 的祖先，那条 ff 无损、随时可补。**已于 2026-10-29 补做**：用 `git fetch . rf-track:develop/rf-monitor-platform` 而不是"先 checkout 再 merge"——这条命令只对 fast-forward 生效（非 ff 直接拒绝），且**一次都不翻工作树**，避免动到 `rf-track` 上正在跑的另一路会话。实测 `develop/rf-monitor-platform` → `1898743`，两分支 `^{tree}` 相同；注释 tag `rftrack-baseline-v0.1.0` 按计划**不动**，仍钉 `d53651b`（它就是 R13 留的那个锚点），所以 develop 领先这个 tag 若干个提交——**别记具体数字，它每次 ff 都变**（写这行时是 6，落库时已是 8）；要判"两分支是否对齐"只看 `git rev-parse rf-track^{tree} develop/rf-monitor-platform^{tree}` 是否相同。**副作用要知道**：`rf-track` 每前进一个提交，develop 就又落后一个，需要重跑同一条命令——它不是自动跟踪，别把它当 CI 分支。 | 删 `rftrack/`（未碰任何原代码；`docs/` 只有本文件回写） |
| **S3** 接入层打通（2–3 天）✅ **已完成 2026-10-29，但出口判据第二条未成立（见下）** | `dialer.py`（每接入机一路，**只拨本场地**）+ frame_parser + ingest + `/ws/bridge/{ep}` + `/api/endpoints?gateway=` 代理；~~顺带做 Z0~~（**2026-10-29 随②b 取消**：适配器不再接；`uart:` 端点的枚举/独占/心跳复用同一套代码且已在 Pi 上实测） | 实建新增：**5 个 service**（`services/{frame_parser,ingest,store,ws_bridge}.py` 全新 + `services/dialer.py` 从"只探一台"扩成 `DialCoordinator` 后台循环）、**2 个 router**（`routers/bridge.py`、`routers/ws.py`）、**7 个测试文件**（`test_frame_parity`/`test_ingest_nodes`/`test_bridge_proxy`/`test_store`/`test_dial_loop`/`test_ws_bridge`/`test_multi_gateway`）、`tests/golden/frames-v1.json`、`rftrack/tools/export_golden_frames.mjs`（TS 侧权威导出，需 `node --experimental-strip-types`）、`requirements.txt` 钉版 **`websockets==16.0`**。改动既有（全部只加不减）：`app/main.py`（`create_app(transport=, upstream_factory=)` 两个注入口 + 挂 hub 与两个 router）、`app/routers/health.py`（新增 `bridge` 节）、`app/config.py`（+70 行：轮询/并发/退避/容量与各 `RFTRACK_*` 环境变量）、`app/routers/nodes.py`（查询参数按 §4 表统一为 `?site=`/`?gateway=`）、`tests/conftest.py`（测试实例默认 `dialer_enabled=False`）。规模：非测试 `.py` 21→**25 个 / 1772→3079 行**，测试 `.py` 7→**13 个 / 3218 行** | 无（原 SPA 仅改 `?bridge=` 目标 URL，运行时参数不是代码改动；`web/`、`firmware/` 一行未碰） | 实建：`pytest rftrack/backend/tests` → **235 passed in 12 s**（S2 的 104 + S3 的 131 ＝ 对拍 37 / ingest 17 / 桥代理 14 / 两只桶 16 / 拨号循环 19 / WS 透传 17 / 多接入机并跑 11）。TestClient + 自写 ASGI 驱动，**未起任何临时 dev server**。**出口判据逐条**：✅ **对拍逐字段相等**＝37 项，期望值全部由 `export_golden_frames.mjs` 现场调 `web/src/serial/parser.ts` + `web/src/proto/frames.ts` 导出，Python 侧只认这份权威；golden 缺失时 `pytest.skip`（今天存在，未触发），且第一条测试就断言"夹具必须写明来源，否则与手写期望值没有区别"。✅ **多接入机并跑三条**＝`test_multi_gateway.py` 用三台假网关（分处两个 `relay`）跑完整链路：三台都报 `/dev/widscan58-2` 落库是**三行**且显示名完全相同（证明去重真的没看名字）、`?gateway=` 各回各的一块板、`relay_id` 分得开两个位置、第二轮不挪位不合并；A 判死只带走 A 的节点（B/C 仍 `active`、行不删）；`max_nodes_per_gateway` 是每台的闸；队列与环形缓冲每接入机一份；`/api/endpoints`/`/api/board/mode` 各打各的 `base_url`；health 逐台报自己的态。WS 侧同名判据在 `test_ws_bridge.py`（两网关两条上游、写权各自独立、一台桥没了只关自己的页）。✅ **`medium=uart-pl011` 必带 `stops_rh=true`**＝`test_bridge_proxy.py` 与 `test_ingest_nodes.py` 两侧各断言一次（代理出的表 + 落库的那一列），R21 前半成立。⏸ **断线/热拔插**按判据标为**真机待验**（§8 真机行仍欠）。**❌ 第二条"单节点数值与 M0 基线一致"在 S3 内无法判定，不假装通过**：M0-3 的 60 s 真机录制从未执行（任务 #7 挂着），`m0-baseline.md` 里只有模拟器（c5-race）读数；golden 夹具自带的 `caveat` 字段写的就是"合成帧：证明两份解析器口径一致；不证明固件实际字节"。**这一条的输入不存在，不是解析器有问题**——补法见"下一动作"：真机录制 → 同一批字节分别喂 TS 与 `frame_parser` → 逐帧 diff。**四条如实申报的偏差与补充**：①**`frame_parser` 与两只桶今天没有运行时调用方**（除测试外无人 import）——WS 透传的是**原始字节**，"解析→入桶→`/ws/live` 出曲线"按 §7 属 S6（`routers/samples.py` + `003` 迁移），不在 S3 偷接；所以 S3 交付的是**能力齐备但未接线的采集层**，不是说"data path 已通"。②`/ws/bridge/{ep}` 的 **`gateway` 是必填查询参数**（§4 表里只写了路径）：一条连接必须绑死一台接入机，上游地址只可能从它的 `base_url` 拼出来——这是"一条连接只属一个 gateway"那条校验的落地形式。③**汇聚角色下 WS 直接以 `1008 aggregator_role` 拒绝**，比 §7 S11 行写的"写端点 405"提前发生（WS 没有 405 语义，关闭码是唯一能带理由的通道，且先 accept 再 close，页面上才看得到那句话）。④**修了一条 S2 遗留的真挂死**：`DialCoordinator.stop()` 原来只 `gather` 不 `cancel`，`while True` 的循环永不结束——实测 8 s 内一台机器被拨 1000+ 轮、`stop()` 再也回不来；现在"先摘规划任务、再收接入机任务"，并顺手关掉"规划在 stop 快照之后又建任务"的孤儿竞态。**取证手法**：`sample <pid>` 拿原生栈 + 任务 `print_stack()`，不是靠读代码猜出来的。**未落地的既有对策**：R14（多 worker 会各开一套 hub/循环）的"启动断言 `workers==1`"还没有落点，因为 S3 没有部署入口（`uvicorn` 命令行归 S9 的启动脚本）；今天的等价事实是 hub 与 dialer 都挂在 `app.state` 上，单进程内共享。 | 关掉后端、SPA 直连 8080 即恢复原状（原代码零改动，本阶段可整段删 `rftrack/backend/app/services/{frame_parser,ingest,store,ws_bridge}.py` + 两个 router + 7 个测试文件回退） |
| **S4** 设置系统（2–3 天） | `app_config`/**场地级**/布局/实例四级 + `config_layer` + localStorage 一次性导入 | `routers/{settings,sites,layouts}.py`, `models`, `002/006` 迁移, `tests/test_settings_isolation.py`, `tests/test_site_isolation.py` | 无 | 规范 §4.4 十条全部 pytest 化通过（A 阈值不影响 B、同型两实例不同配置、非法配置不崩、局部不绕过硬件约束…）**+ 新增两条：A 场地的设置与保留不得影响 B 场地；场地级覆写为空时正确继承全局并回显来源链** | Alembic downgrade 一步 + `local_settings_import` 留痕可回退 |
| **S5** 工作台壳 + 5 个 DEMO 组件（3–4 天） | 栅格 + 组件库 + 属性面板 + **场地/网关选择器与 `site-gateway-tree`** + 矩阵/详情/趋势/事件流 | `rftrack/frontend/workbench/**` | 无 | 规范 §10.1 交互清单：增删/拖缩/复制/保存/另存为/重命名/切换/刷新恢复/坏布局降级不崩；资源清理有断言；**切场地后各场地保留自己的当前布局，状态栏显示当前场地/网关/板** | 独立目录，删除即回滚 |
| **S6** 真实数据组件（2–3 天） | 组件接 `/ws/live?site=`，无数据/过期/断线/unsupported 状态 | `components/{channel-matrix,…}.js` 接线, `routers/samples.py`, `003` 迁移 | 无 | 用真机录制回放做数据源；MOCK 与真实可区分；历史有容量边界；**A 场地的 `/ws/live` 订阅收不到 B 场地的事件（pytest + 双场地夹具）** | 同上 |
| **S7** 插件运行时（3–5 天） | 有界队列（**每网关一条**）+ supervisor + `anomaly-detector`/`interference-correlator`/`alert-engine` 第一版 + **规则作用域可锁到场地** | `services/{registry,plugins/*}.py`, `004/005` 迁移 | 无 | 规范 §10.4：插件启停/状态/日志可观测、队列有界、插件崩溃不拖垮采集、切布局不停任务（pytest 断言）；**切场地也不停任务**；A 场地队列打满不得吃掉 B 场地配额 | 全部 `enabled=0` 即等效不存在 |
| **S8** 探索与目标（1–2 周） | 选择联动 + `target-inspector` + 候选目标/复核 + 频道活动热图 | `routers/{events,targets,alerts}.py`, components | 无 | 规范 §10.5：结果含来源/证据/不确定性；不生成无来源坐标；复核与推测分列 | 同上 |
| **S9** 稳定性与部署（1–2 周） | 本地部署（Docker 或 venv+systemd）+ 多场地/多网关/多组件/长跑压测（**按现有 8 频道；40+ 频道属固件 M3，不在本阶段**）+ **`MAX_GATEWAYS`/`DIAL_CONCURRENCY` 定值** + 归档导出/导入往返实测 + SD 磨损评估 + 文档交付 | `Dockerfile`/`docker-entrypoint.sh`/compose（9xxx、数据卷永不删） | 无 | 真实 CPU/内存/延迟/队列/dropped/CRC 数字在案；禁编造；未测项如实标注；**归档包在另一台机器导入后事件与配置可读回（往返测试）**；**代码与文档里 grep 不到任何云上传端点** | 回滚 = 停服务 + 保留数据卷 |
| （可选）S10 LLM 旁路 | 见 `docs/architecture/llm-integration.md`（方式 A 网关先行，不进关键路径；**provider 默认本地 ollama/vLLM**） | `services/llm_gateway.py` 等 | 无 | 断网/禁用时 S1–S9 全部照常；**选云端 provider 时页面必须常驻"数据发往 `<base_url>`"提示且脱敏不可关** | 关闭 `llm.enabled` |
| **S11 局域网只读汇聚（2–3 天，用户 2026-10-28 定案的形态）** | 同一份代码第二个角色：`aggregator` 定时拉各场地实例的 `/api/aggregate/*` → 本站只读库 + 滞后徽标 + 陈旧置灰；场地实例侧补上只读出口与 `require_read_token` | `services/aggregate.py`, `routers/aggregate.py`, `007_aggregate` 迁移, `tests/test_aggregate_staleness.py` | 无（新增只读端点，不改既有端点语义） | 拔掉头某个场地的网线 → 该场地在汇聚视图里转 `stale` 并显示最近同步时刻，**其余场地不受影响**；`lag_s` 阈值可配；aggregator 角色下 `POST`/`/ws/bridge` 一律 405（pytest 断言）；**不得把陈旧值画成实时曲线** | 停用汇聚实例即回到"每场地各看各的"，场地实例零影响 |

> **M3（40+ 频道）与本计划的关系**：频道数扩展是**固件分支**工作（`PROFILE_MAX_CHANNELS 8→N` + ENABLE u32 掩码改位集，见 `docs/rf-track/channel-limit-audit.md`；静态 RAM 红线须实机链接验证 **[需要实机验证]**）。本计划 S1–S9 全部在**不改固件**前提下把软件侧做完，S9 压测按现有 8 频道/多节点做；40+ 频道另立固件阶段，验收前工作台必须显示 `unsupported` 而不是假装能跑。**[规范 §2.3]**

> **三种硬件接入方式与本计划的关系**（详见 `docs/architecture/topology-and-access-methods.md`；2026-10-28 用户已就②③拍板）：
> ①**USB × N → Pi** 是**产品线验收用的路线**，S1–S9 全部按它验收（双板并跑已实测，~37 KiB/s/板、0 CRC 错）。**②今天也已可用**（F1 已提交并烧板实测，见下条），但②与 RH 互斥、PL011 只有一路、长跑未测，所以**不作为 S 阶段的验收基线**——这是取舍，不是能力缺失（形态已按 ②a 定案，见下条）。
> ②**C5 UART → Pi**：**接入形态已裁定（2026-10-29）：走 ②a「Pi 板载 PL011 `ttyAMA0` 直连 + 桥接时停/复 RotorHazard」，②b「USB-UART 适配器」取消。** 基线上固件一行都省不掉：帧只从 USB 出（`link.c:110`/`link.h:20-23`）、UART 上拒 `PROTOCOL MONITOR`（`commands.c:397`）、UART 收字节即强制 RH（`:677`）——**所以它必需固件阶段 F1**（**已提交为 `7bec675`、三块板已烧、UART 桥已真机实测**：6.80 KiB/s、558 帧 CRC 全对、断桥后 RH 自动恢复 8 节点；数字出自写固件的那路会话，本审计未复测；见 [`topology-and-access-methods.md` §1 时效声明与 §3.3](topology-and-access-methods.md)）。这三个行号是**基线 `015018b`** 的，F1 之后已变（新行号 `link.c:100-116`、`commands.c:400,407-417,701-715` 见 §1 时效声明）。**两件事不随裁定消失**：长跑 `heap_free` 与切转速时的 `drops` 未测；UART 上 `PROTOCOL MONITOR` 的对外语义变了而 `docs/PROJECT-REPORT.zh-CN.md:306`、`docs/superpowers/plans/2026-10-26-rh-uart-bridge.md:1312` 两处旧文档未同步（②a 成为正式形态后，这条从"待裁定附带项"升为交付项）。
>
> **选定的②a 带来两条硬约束**（不是免费的）：① **UART 扫频与 RotorHazard 计圈在同一接入机上互斥**——接这个端点就会 `systemctl stop rotorhazard`，场地正在计圈时不能扫；这条必须做成能力位 `stops_rh=true` 并在 UI 显式确认，不能藏在实现里。② **PL011 只有一路 ⇒ 每台接入机一块 UART 板**；要多块就得多台接入机，而唯一的替代形态②b 已取消，所以"单台接入机多块 UART 板"这个拓扑**今天不支持**，别再当成可选项提。
>
> **改判依据（为什么没选②b）**：②a 已实现、已提交、已部署到 Pi，并已真机跑通一次 UART 扫频（`serve.mjs:41,219-225,486-504`：`kind:"uart"` 端点、`--rh-stop-cmd`、等 2.5 s 覆盖固件 2 s `UART_HOLD`）；②b 至今未实现、硬件未买、零次测量。**②b 本来买的是"与 RH 并存 + 可多块并列"，不是带宽**——线速与固件改造两条完全相同。另有一条与选型无关的教训保留：早期把②b 描述成"不动固件"是错的，适配器换的是"线另一头是谁"，不是"板子愿不愿意在 UART 上发扫频帧"，**②b 同样需要 F1**。详见 [`topology-and-access-methods.md` §3.1 与 §3.4](topology-and-access-methods.md)。**S1–S9 的验收仍不押在②上**：②a 与 RH 互斥，用它验收会把计时服务反复打断；S 阶段按①（USB×N）走，②只在真机待验清单里单独验。
> ③**GPIO 中继（S3/另一块 C5）无线上行**：本仓库**没有中继固件**（无 websocket/HTTP/TLS 客户端，S3 连 UART link 都是空实现），Pi 侧接收端 `/node?id=` 已就绪并实测过。**排期决定：S 阶段优先，F2 后置**。原「节点直连云 / F3=Pi→云上行钩子」**已于 2026-10-28 整体作废**：用户定"整套系统本地化部署为主，不进行云端部署"，跨机器只保留局域网 WS 与场地归档包导出/导入。
> 软件侧把三者收敛成同一个 `AccessSource.kind` 抽象（`bridge-ws`（含 `usb-native` / `uart-pl011` 两种 medium）/`node-ws`/`local-serial`；**`cloud-relay` 因"不做云"而删除**；早期那个独立的 `uart-direct` kind 不再需要——②a 已实现的就是 `bridge-ws` 下 `medium=uart-pl011` 的一条端点，**不额外开一种 kind**，②b 的 `uart-adapter` medium 随裁定从抽象里删除），**每个接入源必须带 `site_id` + `relay_id` + `gateway_id`**（后两者经 `gateway → relay → site` 推导，节点上不存 `site_id`），**节点能力由声明渲染，未声明即 `unsupported`**；②a 形态必须声明能力位：**"接入这个端点会停掉 RotorHazard 计时"**（`stops_rh=true`），这是运营者必须先看到的信息，不能藏在实现里。**[规范 §2.3]**


---

## 8. 测试与验收矩阵

| 层 | 命令 | 门槛 | 当前状态 |
|---|---|---|---|
| 原 Web 单测 | `cd web && npm test` | 56/56 不退步 | **[已由本机实测]**（M0，tag `rftrack-m0`） |
| 原 Web 类型 | `npx tsc --noEmit` | 0 错误 | **[已由本机实测]** |
| 原 Web e2e | `node e2e/headless.mjs --sim` | 28/28 不退步 | **[已由本机实测]** |
| 固件宿主测试 | `pio test -e native` | 17/17 不退步 | **[已由本机实测]** |
| 后端 | `pytest rftrack/backend/tests`（TestClient 全绿即门槛，不起临时 dev server） | 每功能覆盖正常/403·409/重复·400/边界 | **[已由本机实测 2026-10-29]** `104 passed`＝health 8 / 迁移 10（含"两条建库路径必须产出同一结构"的 PRAGMA 语义对拍）/ 三级 CRUD 29 / `base_url` 局域网校验 28（参数化）/ `probe` 16 / 节点读取 13；**无 403 用例**——S2 还没有认证，权限归 S4。<br>**[S3 已扩到 235 passed，2026-10-29 本机实测]** +131＝帧对拍 37 / ingest 17 / 桥代理 14 / 两只桶 16 / 拨号循环 19 / WS 透传 17 / 多接入机并跑 11。WS 那 17 条**不用 `TestClient.websocket_connect`**：实测 starlette 0.52.1 `testclient.py:671` 每调一次新起一个事件循环，扇出与单写者根本测不出来，改用自写 ASGI 驱动（`tests/test_ws_bridge.py::Page`）让两个读者落在同一循环——仍然不起临时服务 |
| **场地/接入机隔离** | `tests/test_site_isolation.py`（双场地 + 每场地多位置多接入机夹具）＋ 已建的 `tests/test_multi_gateway.py` | 设置/布局/事件/告警/保留/WS 订阅**六处**都验证"A 不影响 B"；同名端点在不同位置/不同接入机下不撞身份 | **半落地（S3 做了接入机×位置这一维）**：11 条 + `test_ws_bridge.py` 的双网关条目钉死身份键不撞名、A 掉线不影响 B、同名端点是两个节点、队列/缓冲/代理/写权各自独立。**"场地"那一维今天没有数据可隔**——`app_config`/布局/事件/告警/保留是 S4–S8 的表，`test_site_isolation.py` 归 S4 建 |
| **汇聚陈旧度** | `tests/test_aggregate_staleness.py` | 远端不可达时必须显式 `stale` + 显示 `last_sync_at`，**断言聚合视图里没有"看起来像实时"的路径**；aggregator 角色写端点 405 | 待 S11 |
| 帧对拍 | `tests/test_frame_parity.py` + TS 侧导出的 golden JSON | 逐字段相等；**无 golden 文件时标 skip，不得假绿** | **[已由本机实测 2026-10-29]** 37 passed、0 skip；`rftrack/tools/export_golden_frames.mjs` 直接 import `web/src/serial/parser.ts` + `web/src/proto/frames.ts` 生成 `tests/golden/frames-v1.json`，第一条测试断言夹具必须写明权威来源（否则与手写期望值没区别）。**局限要说清**：golden 是**合成帧**，证明"两份解析器口径一致"，**不证明固件真实字节**——所以 §7 S3 出口判据里"单节点数值与 M0 基线一致"**今天不成立**，它要的是 M0-3 那段 60 s 真机录制（未执行，任务 #7）；补法是"录制 → 同一批字节分别喂 TS 与 `frame_parser` → 逐帧 diff" |
| 归档往返 | `tests/test_archive_roundtrip.py`（导出→在临时库导入→比对） | 事件数/配置/布局逐字段一致；坏 `schema_version` 必须拒绝而不是半导入 | 待 S9 |
| 真机 | Mac 单板 / Pi 双板（`serial_bridge.py` 或 serve.mjs） | 录制 60 s 基线 + 逐帧 diff + 断线热拔插 | **仍未执行**（S3 没补做：它需要板子与一段录制时间，不在软件阶段里顺手冒充）。这一段同时卡着 §7 S3 出口判据第二条与"断线/热拔插"那条——两条都如实标为真机待验，任务 #7 |
| **②a 停 RH 的恢复** | 真机：正常关桥 + **三条异常路径**（杀掉浏览器标签、`kill -9` serve.mjs、桥开着重启 Pi）后各查一次 `systemctl is-active rotorhazard` 与 `/api/status` 节点数 | 正常关桥须回 `active`/8 节点（**已实测**）；异常路径若停在 `inactive`，则**必须有自动恢复（unit 的 `ExecStopPost`）或一条可见告警**——不允许"计时停摆且无人知晓"（R21） | 正常路径 **[已由真机实测（非本审计）]**；异常路径 **未执行** |
| 性能 | 多网关×多节点/长跑/事件增长；40+ 频道属固件 M3 就绪后再测 | 记录真实数字与丢弃率，并给出 `MAX_GATEWAYS`/`DIAL_CONCURRENCY` 的依据 | 未执行（S3 只交付了**有界的机制与它的断言**：每接入机一条队列、满载丢最旧并计数、点数预算 = 四道乘积、`dial_concurrency` 用 barrier 逼出真实并发再钉上限——**没有跑过长跑，所以一个真实 CPU/内存/丢弃率数字都没有**） |

阶段报告一律沿用规范 §11 的 11 项模板，并区分：已通过软件测试 / 已通过真机测试 / 仅模拟 / 未执行 / 无法验证 / 当前不支持。

---

## 9. 新增风险（补 `docs/architecture/risk-register.md` R1–R10 之外）

- **R11 SD 卡写磨损**（中，**多场地后升级为高**）：Pi SD 卡承载 SQLite；对策 = 桶≥1 s + 批量事务 + `RAWIQ_PERSIST=false` + `SUMMARY_BUCKET_S` 可调 + **按场地覆写保留** + S9 实测写入量。写放大随"场地×网关×节点"线性，而卡只有这一张。**[根据源码推断]**
- **R12 双网关功能重叠漂移**（中）：serve.mjs 与 FastAPI 都能「看端点」，语义必须只有一份；对策 = 后端只做代理与下游，不复制端点逻辑，`/api/endpoints?gateway=` 直通对应网关的 serve.mjs。**[已由源码确认]**
- **R13 未提交的已验证改动**（**已消解 → 降为低**，2026-10-29）：曾实测 977 → 1096 → 1234 行持续增长的那批改动，已由并行会话在 `7bec675` 全部入库（1253/38，web 8 文件 + firmware 5 文件 +108/−14），工作区不再悬空。**残余风险换了形态而不是消失**：web 网关能力与 F1 固件被捆进同一个提交，因此 UART 侧一旦回归失败，**不能只回滚固件**——回滚会连 Pi 上已真机验证过的多端点/心跳能力一起退掉。对策不是返工改写历史（风险更大），而是：F1 的遗留项（长跑堆/drops、两处旧文档的 `ERR uart_port` 语义）在 `develop/rf-monitor-platform` 上向前修正，且**任何 F1 相关回滚前先确认 web 侧能否单独承受**。`rftrack-baseline-v0.1.0` 这个注释 tag 就是为这条风险留的锚点。**[已由 git show --numstat 实测]**
- **R14 单 worker 约束**（低→中）：多 uvicorn worker 会重复订阅端点导致 busy 抢占；对策 = 启动时断言 workers==1。多网关让这个约束更难绕开（拨号器是单进程内的 asyncio 任务树）。**[根据源码推断]**
- **R15 容量边界（定案后范围收窄）**：单实例不再承载全部场地，风险降级为"**一个场地内**多位置×多接入机×多节点"的内存/队列/SD 膨胀，Pi 4b 只有 ~460 MB 可用。**对策**：`MAX_GATEWAYS`/`MAX_NODES_PER_GATEWAY`/`DIAL_CONCURRENCY` 硬闸 + 每接入机独立队列 + 超限明确文案；S9 压测给默认值。**[需要实机验证]**
- **R16 跨场地网络可达（定案后已移出关键路径）**：每场地一套实例 ⇒ 场地实例永远只拨本场地的接入机，不存在"跨场地打通网段"这件事。可达性需求只剩**汇聚实例 → 各场地实例**一条（只读、可失败、失败就显示陈旧）。若汇聚也嫌麻烦，可以完全不用 S11，退回"每场地各看各的 + 归档包"。**[当前无法确认：各场地是否在同一局域网内]**
- **R17 场地间数据隔离不足（风险位置转移）**：场地实例内多位置仍可能串（布局/告警/事件按 `site_id` 过滤，位置维度靠 `relay_id`）；更集中处是**汇聚实例里多场地同库**——那里漏一次 `site_id` 过滤就是跨场地污染，且肉眼最难发现。**对策**：`test_site_isolation.py` 做成 CI 门槛（S3 起，覆盖设置/布局/事件/告警/保留/WS 六面）+ 查询层强制 site 参数（不传=当前场地而非"全部"）+ 汇聚实例的数据一律 `origin='aggregated'` 可过滤。**[根据设计风险推断]**
- **R18 归档包成为唯一的完整数据搬家通道**（低）：云已排除，归档 ZIP 就成了跨场地/备份的唯一路径，一旦被坏包污染导入会污染现网库。**对策**：导入只允许进**新库文件**再人工切换，禁止"原地覆盖当前库"；导入前校验 `schema_version` 与清单。**[根据设计权衡]**
- **R19 汇聚数据被误读成实时**（中，S11 专属）：滞后数值一旦画进"当前值"曲线，运营者会按过期信息做现场判断——这是本系统里少数**会导致真实世界错误决策**的路径，性质与"把离散 RSSI 冒充连续频谱"同类。**对策**：`last_sync_at/lag_s` 常驻显示 + 超阈值置灰 + `test_aggregate_staleness.py` 断言 + aggregator 角色下不提供 `/ws/bridge`。**[规范 §2.3 延伸]**
- **R20 多套实例的版本漂移**（中，定案的固有代价）：N 个场地各跑一套后端与一个库，Alembic 版本、`PROTOCOL_VERSION`、serve.mjs 都可能不同代；汇聚实例要能容忍"某场地落后一个版本"而不是同步失败原因不明。**对策**：`/api/health` 与 `/api/aggregate/*` 都回 `schema_version`+`app_version`，汇聚侧把不匹配显式列为一种失败原因；升级顺序写进部署文档（先场地后汇聚）。**[根据发布拓扑经验推断，参见本项目记忆里"包内固件清单必落后一代"的同类教训]**

- **R21 ②a 停 RH 后异常退出，场地计时静默停摆**（中，**由 2026-10-29 的②a 裁定新引入**）：②a 的代价是"接这个端点就 `systemctl stop rotorhazard`"。正常关桥会 `resumeRh`，且这条已实测（close → `is-active=active`、8 节点）；**没测的是异常路径**——浏览器标签被杀、serve.mjs 崩溃、Pi 在桥开着的时候重启。这些情况下 `afterClose` 可能根本不触发，结果是**场地在没人扫频的时候计时一直停着，而且没人知道**。这是本系统里第二个"会导致真实世界错误决策"的路径（与 R19 同类）。**对策**：① `stops_rh=true` 能力位在 UI 确认框里显式写出"接入即停计时"；② serve.mjs 的 systemd unit 增加 `ExecStopPost`/重启后无条件 `rh-start`，让"进程死了"不等于"RH 停在死处"；③ 后端每次网关心跳顺带读 `is-active`，**"RH 已停 + 无活跃桥"直接产生一条告警**而不是静默显示旧数据。**[异常路径当前无任何实测，见拓扑文档 §7 第 7 条]**

---

## 10. 明确不做（YAGNI，第一版）

不引入 MQTT/Redis/消息代理；不做第三方插件动态代码加载与插件市场；**不做云端部署——连"网关→云上行钩子"也不做**（2026-10-28 用户定：整套系统本地化部署为主；跨机器只走局域网 WS、只读汇聚拉取与本地归档包）；不重写原 `web/`、不动 `firmware/`、不改帧协议与 `PROTO_VERSION`；不改 Android `app/`；**不做"跨场地实时透传/写穿"**——汇聚实例只读、只拉摘要与事件，绝不把命令写到别的场地的板子上；**不做多租户 RBAC**（每场地一套实例后，"这台机器给谁用"就是天然的隔离边界；只保留汇聚用的只读 token）；不在无固件扩容前宣称 40+ 频道可用。

**"不做云端部署"的边界要说清**：这条约束管的是 **RF Track 的数据与后端**（配置、事件、摘要、告警一律不出场地）。原有的 GitHub Pages 发布不受影响、也不该被这条决定牵连——Pages 上放的是静态页面与固件二进制清单，`serial/flasher.ts` 的固件清单就来自那里（见 [`data-flow.md`](../architecture/data-flow.md) 刷写行），拿掉它等于弄坏刷写流程。两者不是同一件事。

---

## 11. 待用户点头的默认值（本方案已按推荐值写死，可推翻）

| 项 | 本方案默认 | 备选 |
|---|---|---|
| 目录名 | `repo/rftrack/`（backend/frontend 同仓） | `gateway/`、独立仓库 |
| 栅格 | 自研 12 列 | gridstack.js CDN |
| 后端部署位置 | Pi（Python 3.13.5 已在，同机贴 serve.mjs 最近） | Mac 本机（开发期） |
| **层级建模** | **已定（用户 2026-10-28 明确）**：场地 → **中继（物理位置 relay）** → **接入机 gateway（一个位置可多台）** → 节点；三级都建表，从 `001_baseline` 起、`site_id` 贯穿配置/布局/事件/摘要/告警，不留后补债 | ①合并位置与机器为一张表（**已否决**：用户要"两者都要"，合并后"一个位置两台机器"无处表达）；②后补场地列（**已否决**：等于给自己留一次全表迁移） |
| **部署形态** | **已定案（用户 2026-10-28）：每场地一套本地实例 + 局域网只读汇聚实例（S11）**。汇聚只拉摘要/事件、只读、必须显示滞后 | 单实例管多场地（跨场地网段是未解项，已排除）；纯归档汇总（S11 可整体不做，退回这条也不返工） |
| **分支拓扑** | **已定（用户 2026-10-29）：`rf-track` 永不合入 `main`，两线隔离各自迭代**；`main` 保持单节点发布线（Pages 固件/页面源），`rf-track` 产品的分发靠本地实例部署；吸收更新只许 `main`→`rf-track` 单向 | 定期把 rf-track 合回 main（**已否决**：用户明确隔离；合入即隐式发布未经长跑的 F1 固件与扩展 web） |
| 鉴权 | 场地实例绑 `127.0.0.1`/局域网 + 可选 token；**新增一档"汇聚只读 token"**（`/api/aggregate/*` 与同步请求用） | 直接上技能基线 JWT |
| 原 SPA 接入 | 经后端 `/ws/bridge/{ep}` 字节透传（可多浏览器扇出） | 保持直连 8080（则后端与页面抢端点） |
| LLM | 不进 S1–S9，可选 S10 | 与 S7 并行 |
| **LLM provider 取向** | **本地优先**（ollama/vLLM）；云端 provider 属"数据出网"，须显式勾选 + 常驻提示 + 脱敏不可关 | 云端优先（**已否决**，与"不做云端部署"冲突） |

---

## 12. 一句话结论

软件侧（S0–S9）在**零固件改动、零原页面改动**的前提下即可交付一个可持久化、**可多场地/多中继/多节点**、可后台分析、可组件化拖拽、**完全本地部署**的监测工作台。**S0、S1、S2 均已完成**（2026-10-29）：未提交的改动已随 `7bec675` 入库（捆成一个提交，web/firmware 未分开，判据落空但**不返工**），审计与方案文档已随 `d53651b` 入库，注释 tag `rftrack-baseline-v0.1.0` 仍钉在 `d53651b`（"四道闸全绿"的锚点，按 S1 计划不动），`develop/rf-monitor-platform` 则已于 2026-10-29 fast-forward 到 `rf-track` 的 HEAD（做法见 §7 S2 行"分支事实"），S2 的后端骨架落在**完全独立的 `rftrack/` 目录**（一行原代码没改，删目录即回滚）。
**"零固件改动"说的是本计划的 S 阶段自身**：S2–S9 一行 `firmware/` 都不碰。历史上那段固件 F1 由并行会话实现、已在 `7bec675` 里并**烧板上真机测过**（UART 6.80 KiB/s、558 帧 CRC 全对，RH 停复回归通过）——但那些数字**不是本审计复测的**，且长跑堆/drops 未测，所以它的收尾仍挂在 F1 名下（见 R13 与 §6 方式②），不构成 S 阶段的前置。S 阶段按①（USB）验收；**②的形态已于 2026-10-29 裁定为 ②a（PL011 直连 + 桥接时停/复 RH），②b 适配器与 Z0 阶段一并取消**。②a 要过产品线验收还欠三件：长跑 `heap_free`/切转速 `drops` 实测、两处旧文档的 `ERR uart_port` 语义同步、以及**停 RH 的异常路径恢复**（浏览器标签被杀 / serve.mjs 崩溃 / Pi 重启时桥正处于 RH 已停态）；它与 RH 计圈互斥这条要做成能力位 `stops_rh=true` 并在 UI 显式确认，不是实现细节。**S2 后端骨架已于 2026-10-29 完成**（`rftrack/backend/**` 全绿 104 项，未碰一行原代码；两条如实申报的偏差见 §7 S2 行：serve.mjs 无版本字段所以"版本不符"暂时只能以 `unexpected_response` 体现、`dial_failures` 阈值是给既有"不因一次超时判死"规则补的落点）。**S3 接入层已于 2026-10-29 完成**（`pytest` → **235 passed**；拨号循环 + frame_parser + ingest + 两只桶 + `/api/endpoints`/`/api/board/mode` 代理 + `/ws/bridge/{ep}` 全在，`web/`/`firmware/` 仍是一行未碰；四条如实申报的偏差见 §7 S3 行，其中两条是要紧的：**`frame_parser` 与两只桶今天没有运行时调用方**——采集层的管道齐了但没接线，接进 live 曲线是 S6；**出口判据第二条"单节点数值与 M0 基线一致"未成立**，因为 M0-3 的 60 s 真机录制从未执行，golden 只是合成帧，拿它当"真机数值对过"就是伪造证据）。下一个动作是 S4（设置系统 + `test_site_isolation.py`，把"场地"那一维的隔离从空白做成门槛），**等用户确认后才开始**；若要把 S3 的第二条判据补成真的，则需要先安排一次真机录制（任务 #7，需要板子空闲且用户没在用网页端占串口）。
多场地这条新需求**没有改变"先做软件 S1–S9"的结论**，只改变了三件事：①`site`/`relay`/`gateway` 三级必须从 `001_baseline` 就建（不能后补）；②`test_site_isolation.py` 成为与帧对拍同级的门槛；③按定案的"每场地一套实例 + 局域网只读汇聚"，汇聚实例的唯一新风险是**把滞后数据画成实时**（R19），所以滞后徽标与陈旧置灰属于验收项而不是 polish。
