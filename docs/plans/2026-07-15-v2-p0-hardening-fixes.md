# V2 P0 Hardening Fixes Implementation Plan

**Goal:** 以 V2 产品体验方案为准，使用户本地日期、First Outfit、推荐重算、Context Override 和推荐归因达到可内部测试状态。

**Architecture:** 在现有 FastAPI/SQLAlchemy 分层内增加用户 IANA timezone，并让所有日界线逻辑复用同一纯函数。复用现有 async_jobs worker runtime 执行推荐重算；不引入完整 Recommendation Session，而以 daily recommendation id/revision 和持久化规范化 constraints 完成 P0 归因。

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, async SQLAlchemy, PostgreSQL, Alembic, arq, pytest, GitHub Actions.

**Verification policy:** 测试、Ruff、Mypy、构建和迁移回环只在 GitHub CI 执行；本机仅做静态 diff 与文件检查。

---

### Task 1: 用户本地时区

**Files:**
- Create: `app/common/local_time.py`
- Modify: `app/config/settings.py`
- Modify: `.env.example`
- Modify: `app/infrastructure/persistence/models/user.py`
- Modify: `app/presentation/api/v1/schemas/settings.py`
- Modify: `app/presentation/api/v1/routers/settings.py`
- Modify: `app/infrastructure/persistence/repositories/user_repo.py`
- Modify: `app/presentation/api/v1/routers/recommendations.py`
- Modify: `app/application/recommendations/refresh_today.py`
- Modify: `app/jobs/recommendation_jobs.py`
- Modify: `app/infrastructure/persistence/repositories/recommendation_repo.py`
- Create: `alembic/versions/0044_add_user_timezone.py`
- Test: `tests/unit/test_local_time.py`
- Test: `tests/api/test_settings.py`
- Test: `tests/api/test_today_tomorrow.py`

**Steps:**
1. 写 timezone 校验、本地日期、下一本地午夜秒数和跨午夜失败测试。
2. 将聚焦测试纳入 GitHub CI，用于证明旧 UTC 实现失败。
3. 增加 nullable IANA timezone 与 `DEFAULT_TIMEZONE` 兼容旧用户。
4. 将 Today、First Outfit、refresh、countdown、cache TTL 和 scheduler 切到用户本地时间。
5. 由 GitHub CI 运行聚焦测试和迁移 round-trip。

### Task 2: First Outfit 可行性与可信 Gate

**Files:**
- Create: `app/application/recommendations/feasibility.py`
- Modify: `app/application/recommendations/engine.py`
- Modify: `app/application/recommendations/first_outfit_gate.py`
- Test: `tests/unit/test_recommendation_engine.py`
- Test: `tests/api/test_first_outfit_gate.py`

**Steps:**
1. 写 `dress + shoes` 必须生成、缺鞋必须失败、图片不可用不得 ready 的测试。
2. 将失败用例纳入 GitHub CI。
3. 让 Gate 与 Engine 复用类别完整性规则，移除固定 `<3` 判断。
4. 将阻塞性 gate failure 转成 `no_outfit`，不返回伪可信首套。
5. 由 GitHub CI 运行 First Outfit 和推荐引擎测试。

### Task 3: 真正异步的推荐重算

**Files:**
- Modify: `app/application/recommendations/background.py`
- Create: `app/infrastructure/workers/recommendation_recompute_worker.py`
- Modify: `app/infrastructure/workers/worker_main.py`
- Modify: `app/presentation/api/v1/routers/recommendations.py`
- Test: `tests/services/test_recommendation_background.py`
- Test: `tests/services/test_recommendation_recompute_worker.py`
- Test: `tests/api/test_recommendations_today.py`

**Steps:**
1. 写衣橱变更只入队、不调用生成器以及 Today fallback 入队的测试。
2. 将旧同步行为的失败用例纳入 GitHub CI。
3. 用 async_jobs enqueue 实现按用户/本地日期合并的 recompute job。
4. 注册 worker drain 与短周期 cron，生成失败走现有 retry/dead-letter。
5. 让 Today 缺当日结果时返回 fallback 并入队。
6. 由 GitHub CI 运行 background/worker/Today 测试。

### Task 4: V2 flag 与 Context Override 诚实语义

**Files:**
- Modify: `.env.example`
- Modify: `app/presentation/api/v1/routers/recommendations.py`
- Test: `tests/api/test_today_context.py`
- Test: `tests/unit/test_context_override.py`

**Steps:**
1. 写无 LLM free-text、未知 chip 不得返回 applied success 的测试。
2. 实现结构化 422/503 失败，不持久化原始文本。
3. 文档化 V1 flag-off 和 V2 internal-test 两套完整快照及依赖。
4. 由 GitHub CI 运行 override/context 测试。

### Task 5: 最小 Recommendation 归因

**Files:**
- Modify: `app/presentation/api/v1/schemas/recommendations.py`
- Modify: `app/presentation/api/v1/routers/recommendations.py`
- Modify: `app/application/recommendations/generator.py`
- Test: `tests/api/test_recommendations_today.py`
- Test: `tests/api/test_recommendation_events.py`
- Test: `tests/api/test_today_context.py`

**Steps:**
1. 写 Today 返回 recommendation id/revision、refresh constraints 持久化和 server event 带 attribution 的测试。
2. 将归因失败用例纳入 GitHub CI。
3. 暴露 optional `recommendation_id` / `revision`，兼容未持久化 fallback。
4. 将 normalized constraints 和 context version 写入 `context_json`，不写 raw text。
5. 在生成完成后记录 override event 并关联 daily recommendation。
6. 由 GitHub CI 运行契约和事件测试。

### Task 6: Golden Path 与全量验证

**Files:**
- Modify: `tests/api/test_v1_slim_flow.py`
- Modify: `AGENTS.md`
- Modify: `docs/aidrobe-product-experience-optimization-plan-v1.0.md`

**Steps:**
1. 移除 full-flow `xfail`，让测试覆盖 V2 First Outfit/Today 主路径。
2. 将 AGENTS 权威关系更新为 V2 当前真源、V1 flag-off 回归真源。
3. 修正文档版本、flag matrix 和已验证限制。
4. 由 GitHub CI 运行所有聚焦测试。
5. 由 GitHub CI 运行 `uv run pytest -q`、`uv run ruff check .`、`uv run mypy app`。
6. 由 GitHub CI 执行 Alembic upgrade/downgrade/upgrade。
7. 检查 `git diff`，不 commit、不 push。
