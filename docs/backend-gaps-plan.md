# Backend Production Evolution Plan (V3+)

> 起源：前端缺口文档 `docs/backend-gaps.md`。
> 但本计划的**基准已从 V1 Slim 切到 V3+**：
> - V3 系统定位：[`docs/plans/2026-05-05-v3-style-learning-wardrobe-insights.md`](plans/2026-05-05-v3-style-learning-wardrobe-insights.md)
> - V4 系统定位：[`docs/plans/2026-05-05-v4-personal-wardrobe-os.md`](plans/2026-05-05-v4-personal-wardrobe-os.md)
>
> 因此本计划不是"补字段"，而是 V3+ 生产系统在 5 个维度的演进：
> 解释层、执行层、长期记忆层、异步资产层、AI 成本/隐私层。
>
> 最后更新：2026-05-06

---

## 0. 总原则

1. **Strict additive contract**：所有新增 API 字段 optional + 默认值；OpenAPI diff 必须在 PR 描述里标 add-only / breaking。
2. **Feature flag 兜底**：规则未稳定的能力（评分、obstacles、prep、ask、try-on）一律挂 `ENABLE_*`，线上误判直接关。
3. **静态/动态分层**：依赖 `item.status` / `fit_profile` / `weather` 的衍生数据不冻结到生成时刻，detail/today 请求时实时计算；可缓存的部分用版本化 key。
4. **i18n key + params + fallback_text**：服务端不返回纯 key（调试/灰度/旧客户端不友好），返回 `{key, params, fallback_text}`，并 structlog 记录 resolved key + rule version。
5. **Observability over schema**：每条业务规则触发、每次降级、每次 LLM/embedding/OCR 调用必有 structlog 事件 + correlation_id；trace 优先于字段。
6. **Async first**：V3+ 核心风险是异步管线（embedding / outfit board / LLM / OCR / PDF / try-on），不是 HTTP schema。job 幂等、重试、死信、并发锁、recovery 是 P0，不是后置。
7. **Recommendation versioning**：daily_recommendation 已存在 wear/feedback 互动后，**禁止 in-place 覆盖**。引入 `revision` 字段，强刷生成新版本，旧版本及其 outfit/wear/feedback 引用永久保留。
8. **Privacy & cost guardrails 是 P0**：V4 之后 Ask history、OOTD、自身照、try-on body image、shopping intent 都是敏感数据。retention、删除链路、对象存储级联删除、LLM 输入脱敏、token/cost 限额，必须先于功能上线。
9. **Migration 不是零风险**：加列也要管锁超时、批量回填、索引并发建、JSONB 增长、回滚向前兼容。删 / 收紧枚举要灰度 + dual-write。
10. **配置外置**：评分、obstacles 阈值、ask 限额、retention 天数全部 yaml + 落库 `rules_version` / `policy_version`。

---

## 1. 现状再判定：哪些是真问题、哪些是噪音

| 前端缺口 | V3+ 视角下的真实重要性 | 处理 phase |
|---------|-----------------------|-----------|
| #1 scores 四维 | 解释层基本契约 | B |
| #2 obstacles | 解释层 + 执行层 | B（轻量）→ D（替换品 ranking） |
| #3 prep_steps | 执行层；i18n 是关键 | B |
| #4 hero / display_role | V3 outfit board 必需 | A |
| #5 单件热力图 | 长期记忆层 read 通道 | A |
| #6 last_worn | 一致性已偏离（wear_count 已 denormalize） | A |
| #7 purchase_date | 长期记忆层补完 | A |
| #8 today refresh | **真正风险点 — 不能 bypass 互动** | A（versioning）+ E（cost）|
| #9 prep_minutes | 与 #1 合并 | B |
| #10 weather/scene context | 解释一致性 | D |
| #11 feedback bundle | 事务一致性 + 减请求 | A |
| #12 4 档 feedback | 风格学习信号粒度 | A |
| #13 reason_tags 字典 | 配置外置 | A |
| #14 ConfirmImport 扩字段 | won't do | — |
| #15 status 枚举收紧 | **wear → dirty 自动推断要否决** | A |

**新增的非前端可见问题**（V3+ 必做但 backend-gaps.md 没提）：
- N1：embedding worker 没有死信 / 重试上限 / 任务去重
- N2：outfit-visual-assets 异步生成无并发锁，重复触发会重复扣 LLM/CLIP 成本
- N3：LLM 调用没有 token / cost log，没有用户日限额
- N4：OOTD / 自身照 / try-on body image 没有专用 bucket、没有删除链路
- N5：用户删除（账号注销）级联未实现
- N6：每次推荐生成会读全量 wardrobe + style + fit + weather，无缓存层

---

## 2. 关键架构决策

### 2.1 Recommendation Versioning（替代"refresh bypass"）

**问题**：原 plan 的 `force=True` 会覆盖已有 `daily_recommendations` 行，破坏 wear/feedback 历史的引用语义（feedback 仍指向旧 outfit_id，但解释 / 评分已被新版本覆盖）。

**做法**：
- `daily_recommendations` 加列 `revision SMALLINT NOT NULL DEFAULT 1`、`is_current BOOLEAN NOT NULL DEFAULT TRUE`
- 唯一约束改为 `(user_id, recommendation_date, revision)`；额外 partial unique `(user_id, recommendation_date) WHERE is_current`
- 强刷流程：写新行 `revision = max+1, is_current=TRUE`，旧行 `is_current=FALSE`
- 旧 outfit / wear_history / feedback 保留，detail 接口能按 outfit_id 永久访问
- `GET /today` 永远返回 `is_current=TRUE` 那一行
- `POST /today/refresh` 限频（默认 3 次/天，flag 可调）

### 2.2 评分静态/动态分层（同前次审查，保留）

- 静态层落 `outfits.score_breakdown_json` + `score_rules_version`：`aesthetic`、`prep_minutes_base`
- 动态层运行时算：`feasibility`（依赖 item.status）、`fit`（依赖最新 fit_profile）、`prep_minutes` 修正
- 公式落 `app/config/scoring_rules.yaml`

### 2.3 last_worn_at denormalize（保留）

- `wardrobe_items.last_worn_at DATE NULL` + `wear` 路径同事务更新
- 索引 `(user_id, last_worn_at DESC NULLS LAST)`

### 2.4 wardrobe 状态机（修订）

5 值仍是 `wearable / unavailable / dirty / lost / donated`，**但**：

**否决"穿过自动 dirty"**。理由：穿一次不必然脏（衬衫 vs 内衣、季节、个人习惯）；自动改写会破坏 obstacles 解释一致性。

替代：
- `freshness` 由 `wear_history` 派生（最近一次 worn_date + per-category 耐穿天数表 `app/config/freshness_rules.yaml`），运行时计算
- `dirty` 仅当用户显式置 dirty，或未来"洗衣模块"成熟后由该模块写入
- `obstacles.kind=stale`（取代部分 laundry 语义）由 freshness 触发；`obstacles.kind=laundry` 仅当 status 显式 dirty
- 状态机文档落 `docs/wardrobe-status-machine.md`，区分"用户触发 / 系统触发 / 模块触发"三类

### 2.5 obstacles 替换品降级链 + 预计算（保留）

- 候选 top-3 落 `outfits.obstacles_json[*].replacement_candidate_ids`
- 请求时过滤仍 `wearable` 取首个
- 降级：embedding → 同 category+color → 同 category → null
- 每级降级触发 structlog `replacement.fallback`

### 2.6 i18n contract（修订）

```json
{
  "title": {
    "key": "obstacle.laundry",
    "params": { "item_title": "黑色西裤" },
    "fallback_text": "黑色西裤在脏衣篮"
  }
}
```
- `fallback_text` 强制要求服务端有合理 zh-CN 默认值
- structlog 记录 `i18n.rendered { key, rule_version }` 便于 A/B 与回归

### 2.7 4 档 feedback 兼容性（保留）

- pattern 扩 `^(love|like|neutral|dislike)$`，老 like/dislike 继续接受
- `build_style_profile` 4 分支显式覆盖，禁止默认 fallback
- 历史数据不迁移；分析侧 `like := like + love` 合并桶

### 2.8 Async job reliability framework（新增，C 阶段）

V3+ 的所有异步管线（embedding / outfit-board / LLM / OCR / PDF / try-on）共用这一套：

**任务表 `async_jobs`**（统一）：
```
id           uuid PK
job_type     varchar(32)   -- embedding | outfit_board | llm_ask | ocr_receipt | pdf_ingest | tryon
idempotency_key varchar(128) UNIQUE  -- 业务侧去重 key
user_id      uuid
target_id    uuid          -- item_id / outfit_id / ask_session_id 等
status       varchar(16)   -- queued | running | succeeded | failed | dead
attempts     int default 0
max_attempts int default 3
last_error   text
correlation_id uuid        -- 跨服务追踪
cost_tokens  int           -- LLM/embedding 成本
cost_cents   int
payload      jsonb
result       jsonb
created_at   timestamptz
updated_at   timestamptz
next_run_at  timestamptz   -- 重试退避
```

**契约**：
- 入队前必须给 `idempotency_key`（如 `embedding:item_id:model_version`），重复入队 → 直接返回已有 job
- 重试退避：1m / 5m / 30m，超 `max_attempts` 进 `dead`
- `dead` 状态触发 Sentry alert
- 所有 worker 持有 `SELECT ... FOR UPDATE SKIP LOCKED` 拿任务 → 天然并发安全
- 关键日志事件：`job.enqueued / job.started / job.retried / job.succeeded / job.failed / job.dead`

**结果**：N1 / N2 / N3 全部解决；任务可观测、可恢复、不重复扣成本。

### 2.9 Privacy & deletion（新增，E 阶段）

**对象存储分桶**：
- `aidrobe-items`（衣物 cutout / original）
- `aidrobe-ootd`（OOTD 自拍）
- `aidrobe-private`（try-on body image / 购物截图）
- `aidrobe-derived`（outfit board 拼图）

**每桶独立 retention policy** 落 `app/config/retention.yaml`（如 try-on 30 天自动 purge，OOTD 永久但用户可删）。

**删除链路（账号注销）**：
- 同步：DB `ON DELETE CASCADE`（已部分覆盖）
- 异步：入队 `job_type=user_purge` 任务 → 遍历各 bucket 删对象 → LLM 提供商侧 `ask_sessions` content 删除（如有 retention API）→ 写 `audit_log`
- SLA：72h 内完成；失败进死信触发人工

**LLM 输入脱敏**：
- Ask / sense-check / packing 注入用户数据时只传 `category / color / status / location / season_tags`，**不传图片、不传 user_id 原值**（用 hashed user_token）
- token / cost 写 `async_jobs.cost_*`，按 user-day 聚合限额

---

## 3. Phase 排期（重排）

### Phase A — Contract + Consistency（3–4 d）

> 解锁前端 MVP 闭环屏幕；不引入新规则风险；零向后破坏。

- [ ] `wardrobe_items` 加 `last_worn_at DATE`、`purchase_date DATE`；批量回填迁移（分批 LIMIT 5000，避免长事务锁表）
- [ ] `wear` 路径事务内更新 `last_worn_at`
- [ ] `wardrobe_items.last_worn_at` 索引并发建（`CREATE INDEX CONCURRENTLY`）
- [ ] `daily_recommendations` 加 `revision`、`is_current`，重写唯一约束（partial unique）；旧数据迁移 `revision=1, is_current=TRUE`
- [ ] `WardrobeItemStatus` 枚举 + 状态机文档；**否决 wear→dirty 自动推断**
- [ ] `UpdateWardrobeItemRequest.status` / `WardrobeItemResponse.status` 改 Literal
- [ ] `RecommendationItemResponse.display_role`：category → role 映射 + default `accessory`
- [ ] `FeedbackRequest.feedback_type` 扩 4 档；`build_style_profile` 4 分支显式覆盖；老值继续接受
- [ ] `POST /outfits/{id}/feedback/bundle`：单事务 all-or-nothing
- [ ] 防并发重复 wear：`wear_history (user_id, outfit_id, worn_date)` 唯一约束 + `ON CONFLICT DO NOTHING`
- [ ] 新端点 `GET /v1/wardrobe-items/{item_id}/wear-history?days=60`：预聚合 `daily_counts`
- [ ] 配置 `app/config/feedback_tags.yaml` + `GET /v1/recommendations/feedback-tags?scene=`
- [ ] `docs/api-changelog.md`、`docs/wardrobe-status-machine.md` 起头
- [ ] 测试：迁移 upgrade/downgrade/upgrade、bundle partial failure rollback、并发 wear 幂等、status 非法转换 422

**Feature flag**：无（全 additive）。
**迁移检查表**：lock_timeout=3s、`CREATE INDEX CONCURRENTLY`、回填分批、回滚 SQL、生产回滚向前兼容（新列 nullable）。

### Phase B — Explainability Runtime（2–3 d）

> 让 OutfitDetail 顶部 4 宫格、obstacles banner、prep 卡片可用；规则全部 lightweight，可关。

- [ ] `outfits` 加 `score_breakdown_json JSONB`、`score_rules_version VARCHAR(16)`、`obstacles_json JSONB`、`prep_json JSONB`
- [ ] `app/config/scoring_rules.yaml`（v1.0）+ `freshness_rules.yaml`
- [ ] `app/application/outfits/scoring/static.py` + `scoring/dynamic.py`
- [ ] `app/application/outfits/obstacles.py`：lightweight 版本（dirty / lost / season 不匹配；先不做 weather_mismatch，留 D 阶段补；先不算替换品 ranking，仅同 category+color fallback）
- [ ] `app/application/outfits/prep.py`：按 location 分组 + material notes
- [ ] i18n 契约 §2.6（key + params + fallback_text）
- [ ] `OutfitResponse` 加 `scores`、`obstacles`、`prep_steps`、`prep_notes`
- [ ] structlog：`scoring.computed`、`obstacle.detected`、`prep.generated`、`i18n.rendered`
- [ ] 测试：dirty/lost/seasonal 各一组、freshness 边界、scoring 在 fit_profile 缺失时降级

**Feature flag**：`ENABLE_OUTFIT_SCORES`、`ENABLE_OBSTACLES`、`ENABLE_PREP_STEPS`，独立可控。

### Phase C — Async Reliability Framework（4–5 d，**核心 P0**）

> V3+ 真正的生产瓶颈。先于 D 阶段。

- [ ] 新表 `async_jobs`（§2.8 schema）
- [ ] `app/infrastructure/workers/job_runtime.py`：通用 enqueue / claim（`FOR UPDATE SKIP LOCKED`）/ retry-with-backoff / dead-letter
- [ ] 改造现有 worker：embedding（`app/infrastructure/embedding`）、outfit_visual（`app/infrastructure/workers`）、LLM（`app/infrastructure/llm`）走统一接口
- [ ] `idempotency_key` 规范：`embedding:{item_id}:{model_version}`、`outfit_board:{outfit_id}:{layout_version}`、`llm_ask:{ask_session_id}`
- [ ] Sentry 集成 `job.dead` → alert
- [ ] `correlation_id` 中间件：HTTP 入口生成 → 注入 logger context → 跨 worker 透传
- [ ] `cost_tokens / cost_cents` 字段 + 每次 LLM/embedding 调用写入
- [ ] `GET /v1/admin/jobs?status=dead`（仅 admin）观察死信
- [ ] 测试：重试退避、并发 claim 不重复、idempotency 命中、dead 状态告警 mock

**Feature flag**：通过 `ASYNC_JOBS_DUAL_WRITE=true` 灰度 — 先 dual-write 老路径 + 新表，验证一致后切单写。

### Phase D — Context + Personalization Runtime（3 d）

> 在 C 的可靠性地基上再做"越来越像我"。

- [ ] `daily_recommendations.context_json JSONB`：`{ weather, scene, fetched_at }`
- [ ] `obstacles.kind=weather_mismatch` 启用（Phase B 留的口）
- [ ] `obstacles.replacement_candidate_ids` top-3 预计算 + 降级链（§2.5）
- [ ] `app/application/recommendations/cache.py`：style_profile / fit_profile / wardrobe-snapshot 短 TTL 缓存（30s）于 redis，避开每次推荐重读全量
- [ ] `TodayRecommendationResponse.context`
- [ ] `POST /v1/recommendations/today/refresh`：调用 versioning（§2.1），写新 revision；限频 3/d（flag 可调）
- [ ] structlog：`context.weather_stale`、`replacement.fallback`、`replacement.exhausted`、`cache.hit/miss`
- [ ] 测试：weather port 失败、refresh 限频 429、refresh 后旧 outfit detail 仍可访问

**Feature flag**：`ENABLE_TODAY_CONTEXT`、`ENABLE_TODAY_REFRESH`。

### Phase E — Privacy + Cost Guardrails（3–4 d，**V4 P0**）

> Ask / OCR / PDF / try-on 上线前必须完成；不是装饰。

- [ ] 对象存储分桶 §2.9；现有 item 资源迁到 `aidrobe-items`（脚本 + 双读期）
- [ ] `app/config/retention.yaml`：每桶 retention 天数 + bucket → policy 映射
- [ ] 周期任务（cron worker）：扫过期对象 + 删 + 写 `audit_log`
- [ ] `async_jobs.job_type=user_purge`：注销时入队，遍历分桶删对象、清 ask_sessions content（保留聚合统计）、级联 DB
- [ ] LLM 输入脱敏中间件：`app/infrastructure/llm/sanitizer.py`，强制只保留 §2.9 白名单字段；测试 fixture 验证 user_id 不进 prompt
- [ ] 用户日限额：`app/application/cost_guard.py`，按 `(user_id, job_type, day)` 聚合 `async_jobs.cost_*`，超限 429 + 提示
- [ ] `GET /v1/user/cost-summary`：用户可见自己的 token / 调用次数
- [ ] `audit_log` 表：`(user_id, action, target_type, target_id, actor, occurred_at, payload)`，覆盖 delete/purge/admin 操作
- [ ] 测试：purge 失败重试、retention boundary、限额 429、sanitizer 白名单

**Feature flag**：`ENABLE_COST_GUARD`、`ENABLE_USER_PURGE`。

---

## 4. 性能 / 成本 baseline（Phase 0，0.5d）

不只是 wrk today。要测端到端：

| 维度 | 目标 | 工具 |
|------|------|------|
| `GET /today` p95 (含动态 score/context/obstacle) | < 200ms | wrk + APM |
| `GET /outfits/{id}` p95 | < 250ms | 同上 |
| `GET /wardrobe-items` p95 (200 items) | < 150ms | 同上 |
| `GET /wardrobe-items/{id}/wear-history?days=60` p95 | < 100ms | 同上 |
| Today payload (gzip) | < 30 KB | curl + gzip |
| embedding worker backlog (queued > 5min count) | < 10 | `async_jobs` query |
| outfit_board worker p95 e2e | < 60s | `async_jobs.duration` |
| LLM (Ask) p95 e2e | < 8s | 同上 |
| LLM 单调用平均 cost_cents | < 2 | 同上 |
| LLM 失败率 (per day) | < 2% | 同上 |
| OCR (V4) p95 e2e | < 30s | 同上 |

baseline 落 `docs/perf-baseline-2026-05.md`，每 phase 末对比，回归 > 20% 阻断 merge。

---

## 5. 不做项

- **#4 OOTD 拼图预渲染**：走 outfit-visual-assets 异步管线（C 阶段会强化其可靠性），不内联 today
- **#14 ConfirmImportRequest 扩字段**：前端在 ItemDetail 编辑

---

## 6. PR 模板（强化）

```
## Phase / Scope
- Phase: C (Async Reliability)
- Feature flag: ASYNC_JOBS_DUAL_WRITE (default true)

## OpenAPI diff
- add-only: GET /v1/admin/jobs
- breaking: 无

## Migration
- alembic revision: xxxxx_async_jobs_table
- 锁策略: CREATE TABLE 不锁旧表；新增索引 CONCURRENTLY
- 回填: 无（新表）
- 回滚验证: upgrade → downgrade → upgrade 通过；回滚后老路径仍工作（dual-write 阶段）

## Perf
- baseline (Phase 0): today p95 = ___ ms
- post-change: today p95 = ___ ms (Δ +__ ms)
- payload: ___ KB (Δ +__ KB)
- async backlog: ___

## Cost (LLM/embedding only)
- 单次平均 cost_cents 变化: ___
- 用户日限额命中率: ___

## Observability
- 新增 log 事件: job.enqueued / job.started / job.retried / job.succeeded / job.failed / job.dead
- 新增 Sentry alert: job_dead_letter

## Privacy
- 是否引入新数据类: 否
- 是否影响 retention / purge: 否
```

每个 PR merge 前：
1. `uv run pytest -q` 全绿
2. `uv run ruff check . && uv run mypy app` 全绿
3. alembic upgrade/downgrade/upgrade 通过；锁超时验证
4. `make openapi` 已重生成 + diff 已附
5. `docs/api-changelog.md` 已更新
6. C / D / E 阶段额外：dual-write 期间一致性 SQL 校验脚本通过

---

## 7. 立即下一步

1. Phase 0：开 `feat/perf-baseline` 分支，加 `X-Process-Time-Ms` 中间件 + correlation_id 中间件，跑全维度 baseline，落 `docs/perf-baseline-2026-05.md`
2. 同步前端确认 i18n key 命名空间（`obstacle.*` / `prep.*` / `feedback.tag.*` / `ask.*`）以及 `fallback_text` 是否接受
3. 对齐 V4 owner：try-on body image / Ask history 的 retention 天数；落 `app/config/retention.yaml` v0
4. C 阶段开始前先评审 `async_jobs` schema（schema review meeting），避免后续 worker 改造摇摆

---

## 8. 一句话总结

V3+ 的真问题不是"再加几个 schema 字段"，而是 **推荐版本化、异步可靠性、解释一致性、隐私删除、成本可控**。本计划按这 5 维重排为 Phase A / B / C / D / E，前端缺口作为输入而非主线。
