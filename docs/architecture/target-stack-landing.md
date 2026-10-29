# RF Track 技术栈落地设计（复用 fastapi-vue-fullstack-dev 基线）

> 日期：2026-10-28。状态：**设计待用户确认**，本文档只是阶段 0 的设计产物，未创建任何后端目录、未新增任何运行时依赖、未改动原 `web/` 与 `firmware/`。
> 决策依据：用户指令「复用我常用的框架来进行开发落地，包括数据库」，并在两个分歧点上选定：① 前端 = **Vue3 CDN 无构建**；② 数据库范围 = **配置 + 事件入库，采样只留摘要**。
> 大模型（LLM）接入为本设计的增补能力，详细方案见 `docs/architecture/llm-integration.md`，本文只在目录/表/API/阶段上挂接。
> 结论标注：**[已由源码确认]** / **[已由本机实测]** / **[根据源码推断]** / **[需要实机验证]** / **[待确认]**。

## 1. 基线与本机环境

技术栈基线取自 `~/.qoder-cn/skills/fastapi-vue-fullstack-dev/SKILL.md`：FastAPI + SQLAlchemy + Pydantic / SQLite(WAL) + Alembic / Vue3 CDN 无构建 / Docker + Nginx 子路径反代 / 自签 JWT。

本机实测（`python3 -c import`）：**[已由本机实测]**

| 组件 | 版本 | 状态 |
|---|---|---|
| Python | 3.14.5 | 可用（注意：PlatformIO 的 pioarduino 平台拒 Python 3.14，固件构建须用另一解释器，两条链路不得共用） |
| FastAPI / SQLAlchemy / Alembic / Pydantic | 0.133.1 / 2.0.47 / 1.18.4 / 2.12.5 | 已装 |
| uvicorn / httpx / pytest | 0.41.0 / 0.28.1 / 8.4.2 | 已装（满足「TestClient 全绿即门槛」） |
| pyserial / websockets | 3.5 / 16.0 | 已装（`tools/serial_bridge.py` 现依赖） |
| SQLite | 3.53.1 | 支持 WAL |

## 2. 与原实施计划的偏差记录（改判）

| 原计划条款 | 原文 | 改判 | 理由（源码证据） |
|---|---|---|---|
| §4.3 | 不为形式强行增加数据库或独立服务 | 后端服务与数据库**有实质必要** | 多节点聚合浏览器侧做不到（`App.device` 单例、Web Serial 单连接）；后台任务须不依赖前台组件可见 |
| §6.5 | 不假设一定需要 Python/Node.js/MQTT/Redis | Python 确定为运行时语言，**仍不引入 MQTT/Redis** | 仓库已有 Python 网关先例 `tools/serial_bridge.py`（asyncio + pyserial + websockets），复用同一条链路最小侵入 |
| §8 阶段 2 | 依审计选拖拽网格方案 | 自研 12 列栅格 + pointer 事件（不引第三方网格库） | 保持「无构建」基线；栅格需求固定，引库反而增加版本与样式耦合。**[待确认]** |
| §8 阶段 3 | 优先兼容原配置系统 | 原 localStorage 作为**导入源**，服务端成为唯一真源 | 现状 7 个 localStorage 键无版本无 Schema（见 `current-state.md` §6），无法承载布局/实例级配置 |
| §6.4 首批后台插件 | `anomaly-detector`/`interference-correlator`/`alert-engine`/`signal-associator`/`track-engine`/`report-engine` | 增补 `llm-analyst`（P2），**只做解释与摘要，不参与规则判定**；确定性规则插件仍是告警唯一来源 | 用户要求接入大模型；同时规范 §7.4/§10.5 要求可信度与定位不得由推测生成，故限定其输出域 |
| §2.3 数据真实性 | 全部约束保留 | **不变** | `MOCK/DEMO` 标记、`unsupported` 状态、禁止离散 RSSI 冒充连续频谱、禁止凭 RSSI 造坐标；LLM 产物另立 `llm_note` 表并强制「AI 推导」标记 |

## 3. 目录形态（阶段 1+ 才创建）

```text
repo/
├── web/            # 原无框架 SPA —— 只读保护，不动一行
├── firmware/       # 原固件 —— 本设计不涉及
├── rftrack/
│   ├── backend/
│   │   ├── app/
│   │   │   ├── config.py            # 全部环境变量收口（DB_PATH/BIND/QUEUE_LIMIT/RETENTION…）
│   │   │   ├── database.py          # engine + SessionLocal + Base（WAL）
│   │   │   ├── models.py            # SQLAlchemy 模型，单文件（技能基线）
│   │   │   ├── schemas.py           # Pydantic 入/出参 + 配置 Schema 校验
│   │   │   ├── deps.py              # 依赖注入（预留 require_role）
│   │   │   ├── routers/             # settings.py layouts.py plugins.py nodes.py events.py targets.py alerts.py samples.py llm.py workflow.py ws.py
│   │   │   └── services/
│   │   │       ├── frame_parser.py  # 0xA5'R' 帧解析（镜像 web/src/proto/frames.ts 语义，不改协议）
│   │   │       ├── ingest.py        # NodeLink：单节点串口/TCP 读循环 + 重连 + 命令单写者
│   │   │       ├── store.py         # 内存环形缓冲（当前值/短历史），不入库
│   │   │       ├── config_layer.py  # 平台默认 < 全局 < 布局 < 实例 的生效值计算
│   │   │       ├── retention.py     # 摘要落库、事件/日志清理
│   │   │       ├── llm_gateway.py   # 大模型唯一出口：密钥引用/预算/并发闸门/熔断/脱敏/调用审计
│   │   │       ├── workflow.py      # 编排引擎：trigger → gather → prompt → call_model → emit
│   │   │       └── plugins/         # anomaly_detector.py interference_correlator.py alert_engine.py llm_analyst.py
│   │   ├── alembic/versions/        # 迁移命名 00N_描述.py，防御式、严禁重置库
│   │   ├── migrate.py               # 备份 → 三场景迁移 → 回填 → 幂等种子
│   │   ├── seed.py
│   │   └── tests/                   # test_frame_parity.py test_settings_isolation.py test_layouts.py test_plugins.py test_llm_guardrails.py …（含 run_all.py）
│   ├── frontend/
│   │   └── workbench/               # index.html + app.js + components/*.js + styles.css（相对路径 ./，?v=YYYYMMDD）
│   ├── requirements.txt             # 钉版
│   ├── Dockerfile / docker-entrypoint.sh / compose 片段（9xxx 端口，数据卷永不删）
│   └── fixtures/                    # 真机录制帧样本（不入库 git 的二进制另议）
```

前端实时图仍用手绘 canvas（沿用 `web/src/util/canvas.ts` 的思路与 8 色分类板），Vue 只负责组件库、栅格、属性面板与设置界面。

## 4. 数据流（目标态）

> **2026-10-28 自我修正**：本节初稿写「后端用 pyserial 直接持有串口」，与本轮核实的事实冲突——树莓派上已有一层**真板实测通过**的 Node 接入层 `web/serve.mjs`（端点枚举 + busy/4096 replaced + 掉线心跳 + `/node?id=` 中继节点 + `/api/mode` 停/启 RH，见 serve.mjs:101,102,177,244,292,494-499），已部署为 Pi 上的 `widscan58-web.service`。后端若自己抢串口，等于把那些踩过的坑（CLOSE_WAIT 僵尸连接导致端点永久 busy、udev 别名 realpath 去重、45s 心跳判死）在 Python 里重踩一遍。**结论：接入层复用 serve.mjs，FastAPI 只做它的下游消费者。** pyserial 直连只保留为「Mac 本机单节点开发」的退化路径。

```text
【场地实例：每个场地一套，各自一个 SQLite，只拨本场地的网关】
场地 Site A ─┬─ 位置 R1(起点塔) ─ 接入机 G1: Pi serve.mjs（已实测，不动）── C5 节点 ×N ──USB──┐
             ├─ 位置 R1         ─ 接入机 G2: 另一台本地机器（同构 serve.mjs，零改动）── C5 ×M ─┤
             └─ 位置 R2(看台)   ─ 接入机 G3 …                                                    │
                        每台网关都提供 /serial?ep= 字节透传(单端点单客户端) · /api/endpoints · /api/mode
                                    ↑ 每个在用端点一条 WS 客户端；**每网关一路 dialer（只限本场地的网关）**
                        rftrack FastAPI 后端（本场地机器，uvicorn 单 worker，**只监听局域网，不对外网**）
                          ├─ services/dialer.py    按 relay/gateway 表拨号、退避重连、网关态→其节点连带 lost
                          ├─ services/ingest.py    端点订阅 + 重连 + 帧解析（frame_parser）
                          ├─ services/store.py     内存环形缓冲：当前值 + 短历史，键=(gateway_id,addr,slot)
                          ├─ services/retention.py 1s 桶降采样 → sample_summary；按场地保留覆写
                          ├─ 事件化 → signal_event（带 site_id，有界保留）
                          └─ asyncio.Queue(maxsize, drop=oldest + dropped_count) → 插件 worker（每网关一条队列）
                                   └─> alert / candidate_target / track_observation
                                   └─> **对外只读出口 /api/aggregate/summary|events（供汇聚实例拉，不含原始字节）**

【汇聚实例：局域网内一台只读机，定时拉各场地实例的摘要与事件】
rftrack aggregator（instance_role='aggregator'，另装一台本地机器）
  ├─ services/aggregate.py  逐 remote site 定时 GET → 写本站 site(origin='aggregated', last_sync_at, lag_s)
  ├─ 只读：拒绝一切写命令、拒绝 /ws/bridge、不建 relay/gateway 的拨号任务
  └─ UI 必须常驻「滞后 X 秒 / 最近同步 HH:MM」，超过阈值转「陈旧」并禁止当作当前值

【两个实例共用的前端形态】
Vue3 工作台 ──HTTP /api/*── 场地、位置、接入机、配置、布局、事件、目标、摘要（汇聚实例多一个滞后徽标）
            └─WS /ws/live── 实时当前值与事件推送
            └─归档：GET /api/export/site/{id} ⇄ POST /api/import/site-package（跨机器的第二条通道，非实时、无云）
原 web/ SPA ─┬─ Web Serial 直连（Mac 本机单节点）
             └─ WS /ws/bridge/{ep}  ← 场地实例的字节透传（一条连接只属一个网关；**汇聚实例不提供此端点**）
```

**必须解决的一个冲突**：serve.mjs 的端点是**单客户端**（第二客户端连同一端点被 4096 `replaced`/`busy` 拒绝，已实测）。后端一旦成为持有者，浏览器直连 `/serial?ep=` 就进不去。因此后端对外提供**字节透传端点** `WS /ws/bridge/{ep}`：后端是该端点在 serve.mjs 上的唯一客户端，向任意多个浏览器扇出原始字节（只读扇出；写命令经单写者锁串行化）。原 SPA 的连接方式因此不变，只是把 `?bridge=ws://pi:8080` 换成 `?bridge=ws://pi:9000`——**不改 SPA 一行代码**。命令写入必须由后端每端点一把 `asyncio.Lock` 串行，且转发 `PROTOCOL RH` 这类会改变无线电归属的命令时，必须走 `/api/mode` 的既有语义，不得自己发明。**[已由源码确认]**

RAW IQ/SPECTRUM 大帧只经内存转发、默认不落库（`commands.c:505` 注明这类帧只由 USB 承载）。**[已由源码确认]**


## 5. 数据模型（阶段 3 落表，字段先定契约）

| 表 | 关键字段 | 说明 |
|---|---|---|
| `app_config` | id=1 单行, version, **instance_role(site·aggregator)**, theme, lang, tz, units, default_refresh_ms, alert_defaults_json, updated_by/at | **实例级**全局设置唯一真源。`instance_role='aggregator'` 时本库只装**别的场地**汇聚来的摘要与事件，不接受本地拨号，且行级只读 |
| `site` 场地 | site_id, name, slug, timezone, description, status(active·archived), retention_events_days?, retention_summary_days?, origin(local·aggregated), last_sync_at?, lag_s?, created/updated | **一等实体（2026-10-28 用户定：系统须支持多场地）**。保留天数为空=继承 `app_config`。**每场地一套本地实例 ⇒ 场地实例里 `site` 通常只有一行；`origin='aggregated'` 的多行只出现在汇聚实例**，此时 `last_sync_at/lag_s` 必须有值，UI 据此显示陈旧度 |
| `relay` 中继（物理位置） | relay_id, site_id FK, name, note(位置：看台/起点塔/计时区…), status | **用户明确要求的中间层**：一个场地内多个物理位置/中继站。位置是归属与管理单位，本身不承载连接 |
| `gateway` 接入机 | gateway_id, **relay_id FK**, name, kind(usb-bridge·mcu-relay·local-dev), base_url(ws://<局域网IP>:8080), serial_prefix(/dev/widscan58), status, last_seen, dial_state | **一个位置可挂多台机器**（用户 2026-10-28 确认）。一台=一个跑 `serve.mjs` 的进程；`mcu-relay` 留给方式③ F2，第一版只建 enum 不实现 |
| `node` | node_id, **gateway_id FK**, display_name, transport(kind+addr), capabilities_json(rssi/snr/spectrum/rawiq/position), fw_version, protocol_version, status, last_seen | 场地/位置经 `gateway→relay` 推导（不存 `node.site_id`，避免三处双写不一致）；能力显式声明，缺失即 `unsupported`；**唯一键 `(gateway_id, transport_addr)`**，禁止按显示名全局去重 |
| `channel_plan` / `channel_plan_item` | plan_id, **relay_id? (NULL=全场共享)**, name, band; item(slot_index, name, freq_khz) | 频道方案服务端化；`freq_khz` 统一 kHz 整数（解决 web 侧 MHz/固件 kHz 口径分裂）；绑到位置后可表达"起点塔用 L 段、看台用 R 段" |
| `workbench_layout` | layout_id, **site_id FK(必填)**, relay_id?, name, version, is_active, defaults_json, created/updated | 布局版本化；**`is_active` 唯一性按场地成立**（各场地有各自的当前布局）；不兼容版本安全降级 |
| `layout_widget` | instance_id PK, layout_id FK, plugin_id, pos_x/y/w/h, config_json, sort | 实例级局部配置隔离于此 |
| `plugin_registry` | plugin_id, kind(front/backend), name, version, enabled, config_schema_json, config_json, queue_limit, rate_limit | 内置注册表 + 配置启停，不开放第三方代码 |
| `plugin_state` | plugin_id, status, started_at, last_error, dropped_count, stats_json, health_at | 可观测：启停幂等、溢出计数 |
| `plugin_log` | ts, plugin_id, level, msg | 上限保留（默认 5000 行）**[待确认]** |
| `signal_event` | event_id, ts, **site_id + gateway_id（冗余列）**, node_id, freq_khz/slot, kind(activity/anomaly/node_offline/alert…), metric, value, quality, source(real/mock/demo), payload_json | **source 字段是 MOCK/DEMO 强制标记的落点**；冗余 `site_id/gateway_id` 是为"按场地过滤 + 按场地分场保留/归档"不必穿三表 join，写入时由 ingest 从 gateway 推导。**[根据设计权衡，见 §7.9]** |
| `sample_summary` | ts_bucket, **site_id**, node_id, slot, metric, mean/min/max/last, sample_count | 唯一入库的采样形态（摘要），默认 30 天；索引 `(site_id, node_id, slot, ts_bucket)` |
| `alert_rule` / `alert` | rule(**scope: global·site·gateway·node** / expr/threshold/cooldown/silence) / alert(rule_id, **site_id**, level, event_id, status active·acked·silenced·resolved, dedup_key, created/recovered) | 去重与恢复状态；规则作用域必须能锁到单个场地，否则 A 场地的阈值改动会惊动 B 场地 |
| `candidate_target` | target_id, first/last_seen, confidence, algo_id+algo_version, review_status(unreviewed·confirmed·rejected), evidence_json, limitations | 置信度必须由算法产生并带算法版本；人工复核与算法推测分列 |
| `track_observation` | obs_id, target_id, event_id, ts, node_id, slot, rssi, snr, quality | 目标时间线的证据链 |
| `local_settings_import` | imported_at, payload_json | 一次性导入原 localStorage 的留痕与回滚依据 |
| `app_config.llm_json`（列） | provider_id, enabled, timeout_s, max_concurrency, daily_token_budget, per_run_token_limit, redact | LLM 治理参数随全局设置走，不新开设置表 |
| `llm_provider` / `llm_call_log` | 档案（base_url/model/api_key_ref）/ 审计（status, tokens, latency, cost_est, 不存正文） | 大模型接入，详见 `llm-integration.md` §5 |
| `workflow` / `workflow_step` / `workflow_run` | 编排定义 / 步骤(seq+type+config_json) / 运行观测(status, steps_json, tokens, error) | `llm-analyst` 等插件的流程载体 |
| `llm_note` | producer, kind(hypothesis/report/note), text, structured_json, target_ref, evidence_ids_json, review_status, rejected_reason | AI 文本独立表；**不进 `signal_event`**（其 `source` 只有 real/mock/demo 三个观测量取值） |

写入纪律（技能基线）：新增列一律可空、迁移前 inspector 查重、严禁重置库、WAL 备份用 `sqlite3.Connection.backup()`、时间统一一种（全部 aware UTC）。**[已由源码确认，技能规范]**

## 6. API 契约（阶段 3+）

| 端点 | 用途 |
|---|---|
| `GET /api/health` | 存活 + 后端任务概览 + **各 gateway 拨号状态** |
| `GET/POST/PATCH/DELETE /api/sites[/{id}]` | 场地 CRUD；删除只允许 `archived` 且无在线网关（否则 409），**数据永不因删场地而静默清除** |
| `GET/POST/PATCH/DELETE /api/relays[/{id}]` | 中继（物理位置）CRUD，挂在 `site` 下 |
| `GET/POST/PATCH/DELETE /api/gateways[/{id}]` | 接入机 CRUD，挂在 `relay` 下（`base_url`+`serial_prefix`+`kind`） |
| `POST /api/gateways/{id}/probe` | **连通性实测**：拨通 `base_url`，取 `/api/endpoints` 与版本，返回真实延迟/失败原因；不得只回"已保存" |
| `GET/POST/DELETE /api/aggregate/remotes` | **仅汇聚实例**：登记要拉取的场地实例（`base_url`+token+间隔） |
| `POST /api/aggregate/sync` `GET /api/aggregate/status` | 手动触发/查看逐场地同步态（`last_sync_at`、`lag_s`、失败原因） |
| `GET /api/aggregate/summary|events` | **仅场地实例**：对外只读出口，供汇聚实例拉摘要与事件（**不含原始字节、不含写能力**） |
| `GET/PUT /api/settings/global` | 实例级全局设置，带 `version` 乐观并发；非法配置 422 并返回逐字段错误 |
| `GET/PUT /api/settings/site/{id}` | 场地级设置（保留天数、告警默认值、时区显示）；返回**生效值 + 来源链**（默认<全局<场地） |
| `GET/POST/PUT/DELETE /api/layouts?site=`, `POST /api/layouts/{id}/clone`, `PATCH /api/layouts/{id}/rename`, `POST /api/layouts/{id}/activate` | 布局保存/另存为/重命名/切换（均在指定场地内） |
| `PUT /api/layouts/{id}/widgets/{instance_id}` | 实例局部配置（服务端按优先级算生效值并回显来源） |
| `GET /api/plugins`，`POST /api/plugins/{id}/enable|disable`，`GET /api/plugins/{id}/state|logs` | 插件启停（幂等）与可观测；`?site=` 可把插件产出限定到场地 |
| `GET /api/nodes?site=&gateway=`, `PUT /api/nodes/{id}` | 节点/能力/连接配置 |
| `GET /api/events?site&node&slot&kind&since&limit`，`GET /api/events/{id}` | 事件流与详情（证据链）；**`site` 缺省=当前场地，不默认跨场地全表扫** |
| `GET /api/targets`，`GET /api/targets/{id}`，`POST /api/targets/{id}/review` | 候选目标与人工复核（复核走审计字段） |
| `GET /api/alerts?site=`，`POST /api/alerts/{id}/ack|silence|resolve`，`/api/alert-rules` CRUD | 告警闭环 |
| `GET /api/samples/summary?site&node&slot&from&to&bucket` | 趋势图摘要取数 |
| `GET/POST /api/providers/llm`，`POST /api/settings/global/llm/test`，`GET /api/llm/calls` | 模型档案、连通性实测、用量审计（`llm-integration.md` §6） |
| `GET/POST/PUT/DELETE /api/workflows`，`POST /api/workflows/{id}/runs`，`GET /api/workflows/runs` | 编排定义与手动触发、运行历史 |
| `GET /api/insights?target=&kind=`，`POST /api/insights/{id}/review` | 读取 AI 推导笔记；人工复核与算法产出分列（规范 §10.5） |
| `GET /api/export/site/{id}` · `POST /api/import/site-package` | **本地归档导出/导入**（ZIP：SQLite 快照 + 配置 JSON + 清单）。这是"跨场地看历史"与"备份"的唯一通道——**没有任何上传云端的路径**（2026-10-28 用户定：本地化部署为主，不做云端部署） |
| `WS /ws/live?site=`、`WS /ws/bridge/{ep}` | 实时推送 / 原 SPA 透传兼容；**一条 bridge 连接只属于一个 gateway** |

配置写入涉及硬件参数（波特率、ENABLE 掩码、采样率）的端点必须返回影响说明并要求确认字段，不得静默重启采集。**[根据源码推断，计划 §4.2 要求]**

## 7. 关键工程取舍与代价（显式）

1. **三份帧协议镜像**（C `frames.h` / TS `frames.ts` / Python `frame_parser.py`）——新增第三方实现即新增漂移风险。对策：`tests/test_frame_parity.py` 用真机录制字节（`web/src/serial/recorder.ts` 能产样本）做输入，断言 Python 输出与 TS 输出的 golden JSON 逐字段相等；无 golden 文件时测试标记跳过而不假绿。**[需要实机验证]**
2. **多进程/单进程**：默认单进程（uvicorn 单 worker）持有串口与插件任务；多 worker 会抢串口，故禁用。**[根据源码推断]**
3. **高频写入**：40+ 频道 ×100ms ≈ 400 行/秒，直接落库不可持续。对策：原始采样只进内存环形缓冲，入库走 `sample_summary` 桶（默认 1s），`signal_event` 仅事件化数据。**[根据源码推断，需在阶段 7 压测实测]**
4. **Vue3 CDN 无构建的代价**：无 TS 类型检查、组件靠约定分层、CDN 需国内镜像回退。换取的是与技能基线一致、改完即生效、无第二条构建链路。
5. **鉴权**：第一版默认绑定 `127.0.0.1`，暴露局域网须显式 `RFTRACK_BIND` 并可选 token。**曾以为"多场地成正式需求"会逼上租户级 RBAC，但按定案的"每场地一套实例"，每套实例只装一个场地的数据，RBAC 的需求随之回落**；反而多出一个更小的硬需求：场地实例的 `/api/aggregate/*` 只读出口必须能被汇聚实例认证（只读 token），否则"任何人都能拉走全部观测数据"。**[待确认形式，见 §9.3]**
6. **不引入** MQTT / Redis / 插件市场 / 消息代理，与计划 §6.5 一致。**2026-10-28 追加：同样不引入任何云部署形态**（不做 SaaS、不做设备直连云、不做云备份）；跨机器只走局域网 WS 与本地归档包。
7. 原 `web/` 的 7 个 localStorage 键继续由原 SPA 使用，新工作台首次运行一次性导入到服务端并留 `local_settings_import` 痕迹；不做双向同步（避免两套真源互相打架）。
8. **大模型是旁路，不是链路**：默认 `llm.enabled=false`，超时/断网/预算耗尽只影响 AI 笔记生成，不影响采集、事件、告警与布局。所有模型调用必须经 `services/llm_gateway.py` 单一出口（密钥引用、并发默认 1、每日 token 预算、熔断、脱敏、调用审计）；`llm-analyst` 只解释规则插件的产物，不参与判定。护栏的关键实现是**引用 ID 白名单**：模型输出的 `evidence_ids` 必须存在于本次实际取回的数据集内，否则整条假设以 `rejected_fabricated_reference` 拒落。依赖上**不引入 openai SDK**，用已装 httpx 直连 OpenAI 兼容接口，一套代码覆盖云端/ollama/vLLM 三类 provider。**[已由本机实测 httpx 0.28.1 可用]** **[需要实机验证：本地模型时延与内存占用]**
   - **本条随"不做云端部署"的定调收紧（2026-10-28）**：`llm_provider` 的默认取值改为**本地（ollama/vLLM）**；选择云端 provider 等于让观测数据出网，必须是显式勾选 + 页面常驻"数据发往 `<base_url>`"提示 + 脱敏开关不可关闭。原 `llm-integration.md` 待决项"云端优先还是本地优先"就此定案：**本地优先**。
9. **多场地的部署形态**（**已于 2026-10-28 定案：每场地一套本地实例 + 局域网只读汇聚实例**）。三种形态的数据模型相同（都带 `site`/`relay`/`gateway`），所以先建模不返工；定案带来的具体后果：
   - **场地实例的拨号范围被收紧**：dialer 只允许拨"本场地的 relay 下的 gateway"，配置里出现远端场地的地址应当报错而不是照做——这把"跨场地网段打通"这个未解问题（原 R16）从关键路径上移走了。
   - **汇聚实例是第二个角色，不是第二套代码**：同一份 `rftrack/backend`，靠 `instance_role='aggregator'` 关掉 dialer/bridge/写路径，多出一个 `aggregate.py` 定时拉取。**代价**：多一套部署、多一层"数据新鲜度"语义，且汇聚视图永远是滞后的。
   - **归档包与汇聚并存不冲突**：汇聚给"现在的近似"，归档给"完整历史搬家"；两者都不经互联网。
10. **冗余 `site_id` 到事实表**：`signal_event`/`sample_summary` 上冗余场地列，代价是写入路径必须从 gateway→relay→site 推导（一处推导、两处使用），收益是按场地过滤与分场保留/归档不必三表 join，且**汇聚实例导入的外场数据可以天然按 `origin='aggregated'` 与本地数据隔开**。**[根据设计权衡，若压测显示 join 足够快可回退]**
11. **汇聚数据的"陈旧"必须显式化**（新增硬约束）：`site.last_sync_at/lag_s` 为空或超阈值时，UI 必须显示「陈旧 / 未同步」并把该场地的当前值区域置灰，**禁止把上次同步的值渲染成实时值**——这是"不得用插值或复制上一帧补出来"这条纪律在汇聚场景下的直接延伸，也是最容易被前端偷懒违反的一处。**[规范 §2.3 精神 + 本次定案]**

## 8. 阶段重映射（对原计划 §8 的影响）

| 原阶段 | 原设想载体 | 新载体（本设计） | 验收增量 |
|---|---|---|---|
| 0 只读审计 | — | 已完成，见 `docs/architecture/` 四文 | — |
| 1 产品开发线 | git 分支 | 不变（`develop/rf-monitor-platform`），另加 `rftrack/` 空目录规划 | 分支与工作区洁净确认 |
| 2 工作台布局 | 手写 TS 栅格 | **Vue3 CDN 工作台 + 12 列自研栅格**（纯前端，可先不接后端） | 布局持久化改为服务端 API（无后端时以内存 + 导出 JSON 兜底） |
| 3 设置系统与注册表 | 前端为主 | **FastAPI + SQLite + Alembic**，`config_layer.py` 优先级计算 | 局部/全局隔离测试改为后端 pytest（含 409/422 路径） |
| 4 统一数据模型 | TS 模型 | **Pydantic 模型 + ingest/NodeLink/frame_parser** | 帧对拍测试 + MOCK/DEMO 字段 |
| 5 插件运行时 | 前端 worker/定时器 | **asyncio 有界队列 + plugin_registry/state** | 满载 drop 计数、插件异常不拖垮采集 |
| 6 多流探索与候选目标 | TS 视图 | `/api/events|targets` + Vue 组件联动 | 无真频谱→频道活动热图（明确标注） |
| 7 真机与稳定性 | 手动 | 多节点 ingest 实机 + 40 频道压测 + 保留策略实测 + Docker 部署 | 真实 CPU/内存/延迟/丢帧数字，禁编造 |
| 增补：LLM 接入 | — | 阶段 3 落 `app_config.llm_json` + `llm_provider`（+0.5 天）；阶段 5 落 `llm_gateway.py` + 审计 + 测试连接（+1–1.5 天）；阶段 6 落 `llm-analyst` + workflow 四表 + `llm-insight`/`workflow-runner` 组件（+2–3 天）；阶段 7 补断网/禁用态回归与成本实测（+0.5 天） | `test_llm_guardrails.py`：伪造引用必须被拒；禁用态三套测试与告警链路零回退。详见 `llm-integration.md` §9/§10 |

每阶段仍遵守原计划 §11 报告模板与「完成即停止等待确认」纪律，测试基线门槛保持 M0 的 56/56（web 单测）+ 28/28（e2e）+ 17/17（固件宿主测试）不回退，并新增 `pytest rftrack/backend/tests`。

## 9. 待用户决定

1. 目录名 `rftrack/`（vs `gateway/`、vs 复用 `web/` 子目录）是否接受。
2. 栅格是否同意自研（不引 gridstack）。
3. 鉴权：首版仅本机绑定 + 可选 token，还是现在就上技能基线的 JWT。**定案"每场地一套实例"后这条权重回落**——每套实例只服务一个场地，场地内权限等价于"这台机器给谁用"，不再需要跨场地的租户隔离；但**汇聚实例要能读场地实例的 `/api/aggregate/*`，所以需要一档"只读拉取 token"**（新的最小需求，比 RBAC 轻得多）。**[待确认：只读 token 的形式（静态 token vs 技能基线 JWT）]**
4. 阶段 1 是否立即开始（建 `develop/rf-monitor-platform`、写 `requirements.txt` 与 `rftrack/backend/` 骨架）——当前仍未动任何代码。
5. ~~多场地部署形态~~ **已定案（2026-10-28）：每场地一套本地实例 + 局域网只读汇聚实例**，后果见 §7.9–7.11。
6. **MCU 无线中继（方式③ F2）落在哪一层**：现设计它是 `gateway.kind='mcu-relay'` 的一行（挂在某个 `relay` 位置下）。但如果一台 MCU 中继复用多块扫频板，那"板子"其实是它的子节点，`relay/gateway/node` 三层仍然够用；反之若 MCU 中继只是"一条链路介质"而不拥有节点，则应记在 `node.transport` 里而不是建一行。**F2 已后置，此条不阻塞 S1–S9，留到 F2 启动前定。** **[待确认]**
7. LLM 接入剩余两个决定（第一版是否只做方式 A 网关、`llm_note` 是否进事件流合并视图）见 `llm-integration.md` §12；"云端优先还是本地优先"**已于 2026-10-28 定案：本地优先**。
