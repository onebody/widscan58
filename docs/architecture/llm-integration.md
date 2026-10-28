# RF Track 大模型（LLM）接入方案

> 编写日期：2026-10-28。技术栈前提：FastAPI + SQLAlchemy + Pydantic + SQLite(WAL) + Alembic 后端、Vue3 CDN 无构建前端（用户已选定基线）。
> 本文是 `docs/architecture/target-stack-landing.md`（落地方案主文）的**能力增补**：目录、表命名、API 前缀、阶段编号一律与主文一致——后端在 `rftrack/backend/app/`，全局设置唯一真源是 `app_config` 单行表，插件放在 `app/services/plugins/`。
> 关联规范：`docs/rf-track/RF_Track_Widscan58_Qoder_Implementation_Plan.md` §2.3 数据真实性、§2.4 性能可靠性、§6 插件体系、§7.2/§7.4 目标边界、§10.5 验收。
> 标注：**[已由源码确认]** / **[已由本机实测]** / **[根据源码推断]** / **[需要实机验证]** / **[当前无法确认]**。

## 0. 定位与硬约束

LLM 在本项目是**可选增强能力**，不是采集链路的一部分。三条不可让渡的边界：

1. **不进关键路径**：LLM 超时、断网、被禁用时，扫描、事件流、告警、布局必须全部正常。符合规范 §2.4「插件异常不得拖垮主采集服务」。**[已由源码确认：规范约束]**
2. **不产生观测量**：LLM 不得写入 RSSI/SNR/频率/坐标/轨迹/频谱数据，不得把 `CandidateTarget` 提升为可信定位（规范 §7.4）。它的输出只能是**文字解释、归因假设、摘要、建议下一步操作**。**[已由源码确认：规范约束]**
3. **推导内容与测量内容物理隔离**：LLM 产物一律落库为独立记录并带 `producer='llm:<plugin_id>@<version>'`、`kind='llm_note'`，前端必须显示「AI 推导」徽标与模型名，绝不混入真实观测时间线（规范 §5.2、§10.5）。**[已由源码确认：规范约束]**

## 1. 两种接入方式：分层关系，不是二选一

| | 方式 A：底层接入（平台级配置） | 方式 B：插件接入（工作流编排） |
|---|---|---|
| 是什么 | 后端提供一个**模型网关层**：provider 配置、密钥、配额、超时、并发闸门、审计日志、脱敏过滤器，统一对外一个 `complete()` 契约 | 后台分析插件 + 可配置工作流：触发 → 取数 → 提示模板 → 调用网关 → 结构化校验 → 落库/联动 |
| 谁使用 | 平台内建能力（告警归因、事件摘要、报告生成）与所有插件 | 用户/运维在前台编排与手动触发 |
| 配置位置 | `app_config.llm_json`（全局设置段）+ `llm_provider` 档案表 | `workflow` / `workflow_step` 表 + 既有 `plugin_registry` 启停（规范 §6.3） |
| 单独使用的代价 | 只有 API 没有流程沉淀：每处调用各自拼 prompt，不可观测、不可复用 | 没有统一出口：每个插件各持密钥、各自重试，配额与熔断失控，审计困难 |

**设计判断（推荐）**：A 是**唯一出口**，B 是 A 的**消费者**。任何需要调用模型的代码（内建服务或插件）都只能通过 `rftrack/backend/app/services/llm_gateway.py`，禁止插件自行 `httpx.post` 到模型端点。理由：密钥、每日预算、并发闸门、脱敏、调用审计这五件事只有集中在一处才能真正生效。**[根据源码推断：规范 §6.2/§6.5 要求「插件异常不得拖垮主采集服务」「队列有界」，集中出口是该要求的最小实现]**

落地顺序：**先 A 后 B**。A（网关 + provider 配置 + 手动「测试调用」按钮 + 审计表）约 1–1.5 天；B（`llm-analyst` 插件 + 工作流编排）约 2–3 天，可与规范阶段 5/6 并行。

## 2. 方式 A：平台层模型网关

### 2.1 Provider 配置（`config.py` 收口默认值 + `app_config.llm_json` 承载运行时值）

全局设置的唯一真源是主文 §5 的 `app_config` 单行表，LLM 段作为 `llm_json` 列存入（不新开一套设置表）；`config.py` 只提供可被环境变量覆盖的**默认值**，符合技能基线「config.py 收口所有环境变量」。

```
llm.enabled            bool    默认 false（本地优先、默认关闭，杜绝意外计费）
llm.provider           enum    openai_compatible | anthropic | ollama | disabled
llm.base_url           str     例：http://127.0.0.1:11434/v1 或 https://<host>/v1
llm.model              str
llm.api_key_ref        str     只存引用名，真实密钥不落库、不入 git、不入日志
llm.timeout_s          int     默认 30
llm.max_concurrency    int     默认 1（串行，保护网关主机）
llm.daily_token_budget   int   默认 200000
llm.per_run_token_limit  int   默认 8000
llm.redact             bool    默认 true
```

- 密钥来源与技能基线一致：环境变量注入 + `.secret_key` 式落盘文件（`chmod 600`，`.gitignore` 覆盖 `*.db`/`.secret*`/`.env`）。DB 只存 `api_key_ref`。**[已由源码确认：fastapi-vue-fullstack-dev 技能阶段6 基线]**
- 界面提供「测试连接」按钮，返回真实结果（成功/失败/HTTP 状态/耗时/用量），**失败必须如实报错，不得假报成功**（规范 §4.3）。

### 2.2 统一调用契约

```python
LlmRequest(messages, json_schema, timeout_s, run_id, purpose)
  -> LlmResult(ok, text, structured, model, prompt_tokens, completion_tokens,
               latency_ms, status: ok|unavailable|timeout|budget_exceeded|
               schema_invalid|provider_error, error)
```

网关职责（顺序固定，任一失败即短路返回，不抛异常给调用方）：
1. `enabled` 与熔断状态检查 → 2. 预算检查（当日累计 token/成本）→ 3. 并发闸门（信号量）→ 4. **脱敏过滤** → 5. 单次请求（无自动重试；重试策略归编排层，见 §3.2）→ 6. JSON schema 校验 → 7. **引用 ID 白名单校验**（§4）→ 8. 写 `llm_call_log` 审计。

### 2.3 为什么不自建重试/队列
采集侧已有真实背压点（固件 16KB 环形缓冲、`telemetry.drops` 可观测；`docs/rf-track/web-data-flow.md`）。LLM 属低优先级旁路，熔断 + 预算上限足够；在网关里再加重试层会让「谁在耗资源」变得不可归因。**[根据源码推断，YAGNI]**

## 3. 方式 B：`llm-analyst` 插件与工作流编排

### 3.1 插件元数据（对齐规范 §6.3）

插件实现文件 `rftrack/backend/app/services/plugins/llm_analyst.py`，与主文既有的 `anomaly_detector.py` / `interference_correlator.py` / `alert_engine.py` 同目录同级；启停与配置走同一张 `plugin_registry` / `plugin_state` 表，不引入第二套插件机制。

```
plugin_id: llm-analyst          kind: backend      version: 0.1.0
capabilities: [llm.complete]    requires: [model_gateway]
inputs:  signal_event[] | alert | candidate_target | channel_summary[]
outputs: llm_note[]             config: workflow 默认绑定、语言、最大上下文条数、模板版本
lifecycle: start/stop/health/last_error   queue: 有界 256，满载丢弃并计数（不阻塞）
```

插件不直接依赖任何前台组件是否显示（规范 §6.2）；停用插件不影响采集与告警主链。

### 3.2 工作流模型（`workflow` / `workflow_step`）

步骤类型（第一版只做这 5 类，足够覆盖前两个场景）：

| 步骤类型 | 职责 | 关键参数 |
|---|---|---|
| `trigger` | 何时运行：事件数阈值 / 告警产生 / 定时 / 前台手动按钮 | 去重键、冷却秒 |
| `gather` | 从 SQLite 取窗口数据（摘要表 + 事件，非原始全量采样） | 时间窗、节点/频道过滤、max_rows |
| `prompt` | 模板渲染（存 DB，带 `template_version`） | 模板 ID、语言 |
| `call_model` | 调 §2.2 网关 | schema_id、token 上限 |
| `emit` | 落库 + 发事件总线 | 目标对象类型（alert/target/none）、是否联动选中 |

运行记录 `workflow_run(run_id, workflow_id, trigger, status, steps_json, tokens, latency_ms, error, created_at)`：每步的状态与耗时可观测，失败步可单独重跑（幂等：`emit` 按 `run_id + dedup_key` 去重）。

**示例工作流 1：告警归因**（P1，接规范 §6.4 `alert-engine`）
`trigger: alert.created` → `gather: 该告警 + 同时间窗 ±60s 的多频道 RSSI 摘要与关联事件` → `prompt: 解释可能成因，按给定 event_id 引用证据，输出 {hypotheses:[{text, evidence_ids[], confidence_label}], next_actions[]}` → `call_model` → `emit: llm_note 挂到该 alert，人工复核状态不变（仍 unreviewed）`。

**示例工作流 2：监测报告**（P2，接规范 §6.4 `report-engine`）
`trigger: 手动（前台按钮）/定时` → `gather: 时段内事件统计、Top 活动频道、告警计数` → `prompt` → `call_model` → `emit: 报告文本存 llm_note(kind=report)`，前端事件流组件可查看。

### 3.3 与现有后台插件的关系
`anomaly-detector` / `interference-correlator` / `alert-engine` 是**确定性规则**，先产出结构化事件与告警；`llm-analyst` 只消费它们的输出。不允许 LLM 参与规则判定本身（否则告警不可复现，违反规范 §6.5「任务超时、异常退出和重试可观测」与 §10.4）。**[根据源码推断]**

## 4. 输出真实性护栏（本方案最重要的实现细节）

1. **引用白名单**：模型输出的 `evidence_ids[]` 必须逐个存在于 `gather` 步骤实际取回的 ID 集合；出现未知 ID → 该条假设标记 `rejected_fabricated_reference`，不落库为证据。这是对「幻觉编造观测」唯一可靠的机器校验。**[根据源码推断]**
2. **置信度只能是枚举标签**（`low|medium|high` + 文字理由），不得输出数值置信度写入 `CandidateTarget.confidence`（规范 §7.3 要求置信度由明确算法计算）。**[已由源码确认：规范约束]**
3. **禁止生成的字段**：坐标、方位、距离、速度、轨迹点、频谱幅度、频率值。schema 里不出现这些字段名，且落库前做一次字段黑名单断言。**[根据源码推断]**
4. **呈现规范**：`llm_note` 前端必须并列展示 `model`、`prompt_template_version`、`created_at`、数据窗口（而非当前时刻）与「AI 推导，未经人工复核」标记；人工复核字段独立保存。**[已由源码确认：规范 §7.1 第 8 条]**

## 5. 数据模型增量（Alembic 迁移，遵循主文「新增列一律可空、严禁重置库」纪律）

`app_config.llm_json` 只存**运行时治理参数 + 当前生效的 provider_id**；多套 provider 档案单独成表，便于在本地/云端之间切换而不改治理参数。

| 表 | 关键列 | 说明 |
|---|---|---|
| `llm_provider` | provider_id, name, base_url, model, api_key_ref, enabled, caps_json | 可多套配置，`app_config.llm_json.provider_id` 指向生效的那一套 |
| `llm_call_log` | run_id, purpose, provider, model, status, prompt_tokens, completion_tokens, latency_ms, cost_est, redacted_json_hash | 审计与预算统计；**不存 prompt 正文**（脱敏与最小化留存） |
| `workflow` | id, name, plugin_id, enabled, version, schedule_json | |
| `workflow_step` | id, workflow_id, seq, type, config_json | |
| `workflow_run` | id, workflow_id, trigger, status, steps_json, tokens, latency_ms, error | 保留 N 天（与事件流同一保留策略） |
| `llm_note` | id, run_id, producer, kind(hypothesis/report/note), text, structured_json, target_ref(alert/candidate_target/none), evidence_ids_json, review_status, rejected_reason | 独立表，**不写进 `signal_event`**：主文给 `signal_event.source` 定义的取值是 `real|mock|demo`（观测量域），AI 文本不是观测量，塞进去会污染时间线语义；需要出现在事件流时由 API 侧做「合并视图 + 类型徽标」 |

保留策略：`llm_call_log` 与 `workflow_run` 各留 90 天或 20 万行（先到为准），删除走 `migrate.py` 启动清理，与采样摘要同一 retention 模块。**[根据源码推断，具体上限待按实机磁盘压测调（规范 §10.6）]**

## 6. API 增量（`/api/` 前缀，遵循技能基线路径规范）

```
GET/PUT  /api/providers/llm               provider 档案 CRUD（密钥只写不读，返回掩码）
PUT      /api/settings/global             既有端点扩展 llm 段（乐观并发 version；非法配置 422）
POST     /api/settings/global/llm/test    连通性测试，如实返回状态与耗时
GET      /api/llm/calls?from&to&status    审计与用量
GET/POST /api/workflows                   列表/新建
PUT/DEL  /api/workflows/{id}              编辑（含 steps 数组）
POST     /api/workflows/{id}/runs         手动触发（受 enabled 与预算约束）
GET      /api/workflows/runs?status       运行历史
GET      /api/insights?target=&kind=      读取 llm_note
POST     /api/insights/{id}/review        人工复核（accept/reject），与算法产出分离
```

权限：所有 LLM 写操作与「测试连接」需管理员角色依赖（技能基线 `require_role`）；`llm_note` 读取按场地/节点权限过滤（规范 §1 多场地预留）。**[根据源码推断]**

## 7. 前端增量（Vue3 CDN 组件，无构建）

| 组件 | 用途 | 优先级 |
|---|---|---|
| `llm-settings`（全局设置分区） | provider 配置 + 测试连接 + 用量仪表 | P1 |
| `llm-insight`（前台视图插件） | 展示当前选中告警/目标的 AI 归因，含徽标与复核按钮 | P2 |
| `workflow-runner`（前台视图插件） | 触发工作流、看运行步骤状态与耗时 | P2 |
| 事件流（阶段 4 已规划） | 合并显示 `llm_note`，类型徽标区分 | P2 |

实时图仍走手绘 canvas，Vue 只管面板与列表（与已定技术栈决策一致，避免 CDN 响应式代理高频重绘）。**[根据源码推断]**

## 8. 依赖与本地部署选型

- 本机 **[已由本机实测]**：Python 3.14.5，fastapi 0.133.1 / sqlalchemy 2.0.47 / alembic 1.18.4 / pydantic 2.12.5 / httpx 0.28.1 / openai 2.29.0 / websockets 16.0，SQLite 3.53.1（与主文 §1 环境表同源）。
- **推荐：不引入 `openai` SDK**，网关用 httpx 直连 OpenAI 兼容 `/v1/chat/completions`（`response_format={"type":"json_schema",...}`）。理由：少一个会随供应商版本漂移的依赖，且 `ollama`/vLLM/网关代理都是 OpenAI 兼容面，一套代码覆盖三类 provider。**[根据源码推断，YAGNI]** 已装的 `openai 2.29.0` 只作本地实验对照，不进 `requirements.txt`。
- 本地模型路线：**[当前无法确认]** 需实测。树莓派（RH 测试机，203.0.113.183）**[已由源码确认：项目记忆]** 跑 7B 级以上模型的推理时延与内存占用预计不可用，第一版不承诺「纯本地」；把「本地可用」列为需实机验证项，不写入交付承诺。
- 云端 provider 条款：项目为 GPL-3.0-or-later **[已由源码确认：LICENSE]**；调用第三方模型 API 属独立服务交互，但其数据出境/合规限制与是否允许商用部署 **[当前无法确认，需法律与合规核实]**，规范 §13.6 的许可证核查清单需追加一行。

## 9. 阶段映射与工作量

| 规范阶段 | LLM 相关工作 | 估时 |
|---|---|---|
| 阶段 3（设置系统） | `app_config.llm_json` 段 + `llm_provider` 表、密钥引用与掩码读写 | +0.5 天 |
| 阶段 5（插件运行时） | `services/llm_gateway.py`、`llm_call_log`、`POST /api/settings/global/llm/test`、熔断与预算 | +1–1.5 天 |
| 阶段 6（多流探索） | `llm-analyst` 插件、workflow 四表、告警归因工作流、`llm-insight` 组件 | +2–3 天 |
| 阶段 7（真机与稳定性） | 长时间运行下的 token 成本/延迟/队列丢弃统计；断网与禁用态回归 | +0.5 天 |

前置条件：阶段 0 已完成；阶段 1–2 不受本方案影响。

## 10. 验收标准（追加到规范 §10.4/§10.5）

- [ ] `llm.enabled=false` 时全部原有功能与告警链路行为不变（回归 56/56 + 28/28 + 17/17 全绿）。**[可由软件测试]**
- [ ] 模型端点返回 5xx/超时/非法 JSON 时：不抛未捕获异常、不阻塞采集、`workflow_run.status` 与 `error` 可查、队列计数增长可见。
- [ ] 预算耗尽后新触发被拒绝并给出 `budget_exceeded`，已入库笔记仍可读。
- [ ] 构造一条含伪造 `event_id` 的模型输出，护栏必须拒绝落库为证据并记录 `rejected_reason`（专项单测）。
- [ ] 日志与 DB 中检索不到 API key 明文；`llm_call_log` 不存 prompt 正文。
- [ ] 前端每条 AI 结论显示模型名 + 模板版本 + 数据窗口 + 「未经人工复核」，且人工复核状态独立字段可回写。
- [ ] 停用或删除 `llm-analyst` 不影响布局保存/切换与后台其他插件运行。
- [ ] 未做真机与真实 provider 联调的条目，报告中如实标注**未执行**，不得标记通过。

## 11. 明确不做（第一版）

向量库/长期记忆 RAG、自动决策（LLM 直接触发告警或静默）、插件市场与任意第三方代码执行、用 LLM 生成频谱/轨迹/坐标、把 LLM 摘要当测量数据入历史表、模型微调。理由：与规范 §1.2 非目标、§6.3「第一版内置注册表」一致，且这些能力都不解决当前已证实的缺口（8 频道上限、多节点网关、设置持久化）。

## 12. 待用户决定的问题（续主文 `target-stack-landing.md` §9 的 1–4 项）

5. 第一版是否只做到**方式 A（网关 + provider 配置 + 测试连接 + 用量审计）**，把编排（方式 B：`llm-analyst` + workflow 四表）推到阶段 6——这能让阶段 5 保持最小可验收面。
6. ~~是否接受「云端 provider 优先」~~ **已于 2026-10-28 定案：本地优先**（用户定"整套系统本地化部署为主，不进行云端部署"）。含义：`llm.provider` 的默认取向是 `ollama`/`vLLM`；选 `openai_compatible`/`anthropic` 等于让观测数据出网，必须①显式勾选、②页面常驻"数据发往 `<base_url>`"提示、③脱敏开关不可关闭。**残留代价**：本地优先要求有一台跑得动该量级模型的主机，这一点**尚未确认**（Pi 4b 只有 905 MB，明显不行）——若暂时没有，则 S10 保持"只做方式 A 网关、provider 留空"，不假装能出笔记。
7. `llm_note` 是否允许出现在事件流合并视图（默认允许并带「AI 推导」徽标）；若要求时间线绝对纯净，则事件流只显示入口计数。
