# V2 开发计划：异步多来源摄取 + 可执行推荐

> 版本：V2  
> 前置条件：V1 Slim Backend MVP 全部任务完成，iOS 客户端 MVP 上线，Phase 2 内测验证通过  
> 目标：推荐真实可执行；导入低摩擦、多来源、不阻塞主线程  
> 预计工期：6–8 周

---

## 版本定位

V1 验证了"用户愿意录入衣服并看推荐"。V2 要解决的核心问题：

```
导入：同步阻塞 → 异步任务化（推荐名符其实）
来源：仅拍照/相册 → 链接/截图/多图/社媒平台
推荐：搭配合理 → 现在能穿、找得到、来得及拿
```

---

## 模块清单

### M1 — 异步导入任务系统

**问题**：`POST /v1/import-tasks` 同步等 Vision API（5–15s）；链接解析/LLM 超时时用户无反馈；无法支撑批量、PDF、复杂链接等场景。

**目标状态**：
```
POST /v1/import-tasks → 202 Accepted，立即返回 task_id + status: queued
后台 worker 异步跑：link_fetch → image_extract → vision_analyze
GET /v1/import-tasks/{task_id} → 客户端轮询
Push Notification → 识别完成后推送（依赖 V1.1 APNs）
```

**状态机扩展**：
```
queued → processing → waiting_confirm → completed
                    ↘ failed
```

**涉及变更**：

| 文件 | 变更内容 |
|------|---------|
| `app/common/enums.py` | `ImportStatus` 加 `QUEUED = "queued"`、`PROCESSING = "processing"` |
| `app/application/import_tasks/create_import.py` | 拆分：创建任务记录（同步）vs 执行任务（异步）；`execute()` 只建 DB 记录，不调用 Vision |
| `app/infrastructure/workers/import_worker.py` | 新增：arq worker，异步执行 vision 分析、更新 task 状态 |
| `app/jobs/scheduler.py` | 增加 arq worker 启动入口或保留 BackgroundTasks 降级模式 |
| `alembic/versions/0007_import_task_status` | 无 breaking change，`task_status` 已是 varchar |

**降级方案**：若 arq 未启用（开发环境），用 `BackgroundTasks` 模拟异步，保持接口语义一致。

---

### M2 — 多来源导入引擎

**问题**：`HtmlLinkParser` 只解析 og:image/twitter:image，无法处理淘宝/小红书/抖音等 SPA 或短链平台；`source_url` 是二等字段而非一等流程。

**目标状态**：
```
input_type: image | link | screenshot | image_batch
source_type: camera | album | taobao | jd | xhs | douyin | web | screenshot
```

**解析链路**：
```
source_url
  → 短链展开（跟随 30x，每跳 re-check SSRF 防护）
  → 域名路由 → Platform Adapter
      ├── taobao/tmall → og:image / 商品主图 meta / json-ld
      ├── xhs（小红书）→ og:image / content meta
      ├── douyin      → og:image / 视频封面 meta
      └── generic     → META_IMAGE_RE / IMG_RE（现有逻辑）
  → Adapter 失败 → 返回 failed（Playwright 截图兜底留 V3）
  → image_url → Vision 识别（走 M1 异步 worker）
```

**参考架构**：gallery-dl 的 extractor 模式，每个平台一个 adapter class，统一实现 `extract_image_url(url) → str | None` 接口。现有 `LinkParserPort` Protocol 无需改动，只扩展实现层。

**新增文件**：

| 文件 | 内容 |
|------|------|
| `app/infrastructure/importers/adapters/__init__.py` | adapter 注册表 |
| `app/infrastructure/importers/adapters/taobao.py` | 淘宝/天猫 adapter |
| `app/infrastructure/importers/adapters/xhs.py` | 小红书 adapter |
| `app/infrastructure/importers/adapters/douyin.py` | 抖音 adapter |
| `app/infrastructure/importers/router.py` | 按域名路由到对应 adapter |

**`import_tasks` 表扩展**：
```sql
ALTER TABLE import_tasks
  ADD COLUMN input_type varchar(32) DEFAULT 'image',
  ADD COLUMN source_type varchar(32);
```

---

### M3 — 确认流程补全：location + 基础 fit 字段

**问题**：`ConfirmImportRequest` 缺 `location` 字段；`WardrobeItem.location` 已存在于 DB 但入库时无法填写；文档明确 location 是"赶时间优先主衣柜"推荐逻辑的前提。

**工期**：1–2 天，最小改动，最先落地。

**变更**：

```python
# app/presentation/api/v1/schemas/wardrobe.py
class ConfirmImportRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    category: str = Field(min_length=1, max_length=32)
    color_main: str = Field(min_length=1, max_length=32)
    location: str | None = Field(default=None, max_length=120)    # 新增
    size_label: str | None = Field(default=None, max_length=32)   # 新增，可选
    fit_status: str | None = None                                   # 新增，可选
```

**DB 迁移**（`0007_wardrobe_fit_fields`）：
```sql
ALTER TABLE wardrobe_items
  ADD COLUMN size_label varchar(32),
  ADD COLUMN fit_intent varchar(32),
  ADD COLUMN fit_status varchar(32) DEFAULT 'unknown',
  ADD COLUMN comfort_score smallint;
```

**涉及文件**：
- `app/presentation/api/v1/schemas/wardrobe.py`
- `app/application/import_tasks/confirm_import.py`：`execute()` 透传 `location`、`size_label`、`fit_status` 到 `WardrobeItem`

---

### M4 — 图片处理流水线

**目标**：衣物入库时异步生成标准化图片资产，推荐时直接使用，不重复调用 LLM。

**处理链**（异步 worker，入库后触发）：
```
原图
→ EXIF 方向修正 + 压缩（目标 < 500KB）
→ 主体检测 + 质量评分（blur / brightness / subject_area_ratio）
→ 背景移除（rembg 本地推理）→ cutout_image_url（透明 PNG）
→ 缩略图生成（256x256）→ thumbnail_url
→ 写入 wardrobe_item_images + image_processing_jobs
```

**新增字段**（`wardrobe_item_images`）：
```sql
ALTER TABLE wardrobe_item_images
  ADD COLUMN quality_score float,
  ADD COLUMN quality_flags jsonb,    -- ["low_light", "blur", "complex_background"]
  ADD COLUMN width int,
  ADD COLUMN height int,
  ADD COLUMN file_size int,
  ADD COLUMN is_primary boolean DEFAULT false;
```

**新增表** `image_processing_jobs`：
```sql
CREATE TABLE image_processing_jobs (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     uuid NOT NULL REFERENCES users(id),
  item_id     uuid REFERENCES wardrobe_items(id),
  source_image_url text,
  job_type    varchar(32),  -- normalize / segment / cutout / quality_check
  status      varchar(32) DEFAULT 'queued',
  result_payload jsonb,
  retry_count int DEFAULT 0,
  created_at  timestamptz DEFAULT now(),
  updated_at  timestamptz DEFAULT now()
);
```

**工具选型**：
- 背景移除：`rembg`（本地，无 API 成本）；可选 `remove.bg` API（质量更高，有费用），通过 `ImageProcessingPort` 注入
- 图片处理：`Pillow`

**新增文件**：
- `app/application/ports/image_processor.py`：Protocol
- `app/infrastructure/image/rembg_processor.py`：实现
- `app/infrastructure/workers/image_worker.py`：处理队列
- `app/infrastructure/persistence/models/image_job.py`：`ImageProcessingJob` model
- `app/infrastructure/persistence/repositories/image_job_repo.py`

---

### M5 — 推荐引擎升级：可执行性

**目标**：推荐不只是"搭配合理"，而是"现在拿得到、来得及穿、今天适合"。

**评分公式升级**（基于 Doc 17 §10）：
```python
# 有 style profile 时
final_score = (
    utility_score    * 0.25    # 品类完整度 + 场景适配
  + time_cost_score  * 0.20    # 新增：location + status + fit_status
  + aesthetic_score  * 0.20    # 已有（style_profile）
  + weather_score    * 0.15    # 新增：天气适配
  + freshness_score  * 0.10    # 已有
  + fit_score        * 0.10    # 新增：fit_status 信号
  - repetition_penalty         # 新增：近 7 天重复降分
)
```

**新增能力**：

1. **天气集成**：
   - `WeatherPort` Protocol 已存在
   - 新增 `app/infrastructure/weather/openweather.py`（免费层，1000次/天）
   - 用户设置加 `city`（城市级，不需要 GPS）
   - 推荐生成时按温度/降雨概率过滤外套/鞋

2. **time_cost_score**：
   - 同一 `location` 的单品集中度正向加权
   - `fit_status=tight / needs_alteration` 在赶时间时进一步降权
   - `status=unavailable` 硬过滤（已有），`status=needs_prep` 新增半可穿档位

3. **fit 过滤与降权**：
   - `fit_status=no_longer_fits` → 硬过滤
   - `fit_status=tight / needs_alteration` → 降权
   - `fit_status=fits_well` → 正向加权

4. **repetition_penalty**：
   - 近 7 天已推荐/已穿的 outfit 组合降分
   - 基于 `outfit_items` 的 Jaccard 相似度（避免"换双鞋就是新方案"的问题）

**新增配置**（`settings.py`）：
```
OPENWEATHER_API_KEY: str = ""
WEATHER_CACHE_TTL_SECONDS: int = 3600
```

**涉及文件**：
- `app/application/ports/weather.py`（已有 Protocol）
- `app/infrastructure/weather/openweather.py`（新增）
- `app/application/recommendations/engine.py`（扩展评分）
- `app/infrastructure/cache/client.py`（天气结果缓存）

---

### M6 — Style Sources 异步学习

**问题**：`AddStyleInspiration` 只存图片 URL，`style_vector` 永远为 null，学习从未发生；不支持链接输入；不支持多图。

**目标**：`style/inspirations` 升级为 `style/sources`，支持链接 + 多图 + 异步学习状态。

**新接口**：
```
POST /v1/style/sources              单张图 or source_url（链接）→ 201 + status: queued
POST /v1/style/sources/batch        多图（最多 20 条）
GET  /v1/style/sources              列表，含 learn_status
DELETE /v1/style/sources/{id}
```

**learn_status 状态机**：
```
queued → processing → learned
                    ↘ failed
```

**异步学习流程**：
```
入库 → learn_status=queued
worker 异步执行：
  1. source_url 时 → 复用 M2 adapter 提取图片
  2. 图片 → LLM 分析 → style_tags + color_palette + avoid_tags + learned_summary
  3. 写入 style_vector（JSON 格式，pgvector 未启用时）
  4. 更新 learn_status=learned，learned_at=now()
  5. 触发 cache invalidate（今日推荐）
```

**DB 迁移**（`0009_style_sources_v2`）：
```sql
ALTER TABLE user_style_profiles
  ADD COLUMN learn_status varchar(32) DEFAULT 'learned',
  ADD COLUMN learned_at timestamptz,
  ADD COLUMN source_url varchar(1024),
  ADD COLUMN learned_summary jsonb;  -- {style_tags, color_palette, avoid_tags, confidence}
```

**涉及文件**：
- `app/infrastructure/persistence/models/style.py`：加新字段
- `app/application/style/add_inspiration.py`：支持 `source_url`
- `app/application/style/add_inspiration_batch.py`：新增
- `app/infrastructure/workers/style_learn_worker.py`：新增
- `app/presentation/api/v1/schemas/style.py`：`AddInspirationRequest` 加 `source_url`，response 加 `learn_status`
- `app/presentation/api/v1/routers/style.py`：加 batch 路由，URL 从 `/inspirations` 兼容保留，新增 `/sources` 别名

---

### M7 — 合身度系统 V1（Fit System）

按 Doc 19 §14 的 V1 范围，最轻量实现，不增加用户负担。

**原则**：围绕衣服提问，不围绕身体。

**新增接口**：
```
PATCH /v1/wardrobe-items/{item_id}/fit     更新合身信息
POST  /v1/fit-feedback                      穿后合身反馈
```

**`PATCH /v1/wardrobe-items/{item_id}/fit` Request**：
```json
{
  "size_label": "M",
  "size_region": "CN",
  "fit_intent": "oversized",
  "fit_status": "fits_well",
  "comfort_score": 4
}
```

**`POST /v1/fit-feedback` Request**：
```json
{
  "item_id": "uuid",
  "outfit_id": "uuid",
  "feedback_type": "tight",
  "scene": "commute"
}
```

**新增表** `fit_feedback`：
```sql
CREATE TABLE fit_feedback (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      uuid NOT NULL REFERENCES users(id),
  item_id      uuid REFERENCES wardrobe_items(id),
  outfit_id    uuid,
  feedback_type varchar(32),  -- fits_well / tight / loose / uncomfortable / easy_to_move / not_flattering
  scene        varchar(32),   -- commute / formal / casual / date / travel
  created_at   timestamptz DEFAULT now()
);
```

**推荐影响**（在 M5 中实现）：
- `no_longer_fits` → 硬过滤
- `tight / needs_alteration` → 降权
- `fits_well` → 正向加权

---

## 数据库迁移计划

| 迁移编号 | 文件名 | 内容 |
|---------|-------|------|
| 0007 | `0007_wardrobe_fit_fields` | `wardrobe_items` 加 fit 字段；`wardrobe_item_images` 加质量字段 |
| 0008 | `0008_image_processing_jobs` | 新表 `image_processing_jobs` |
| 0009 | `0009_style_sources_v2` | `user_style_profiles` 加异步学习字段 |
| 0010 | `0010_fit_feedback` | 新表 `fit_feedback` |
| 0011 | `0011_import_task_types` | `import_tasks` 加 `input_type`、`source_type` 字段 |

---

## 基础设施变更

### 任务队列
- 引入 `arq`（轻量 Redis 队列，纯 async，不依赖 Celery）
- Worker 类型：`import_worker`、`image_worker`、`style_learn_worker`
- `docker-compose.yml` 加 `worker` 服务（同镜像，不同 entrypoint）
- 开发环境降级：`BackgroundTasks` 模拟，不需要 arq 也能跑

### 依赖新增
```toml
# pyproject.toml
arq = ">=0.26"
rembg = ">=2.0"         # 背景移除
Pillow = ">=10.0"       # 图片处理
httpx = ">=0.27"        # 已有，link_parser 已用
```

### Docker Compose 服务扩展
```yaml
worker:
  build: .
  command: python -m app.workers.main
  environment:
    ENABLE_SCHEDULER: "false"
  depends_on:
    - postgres
    - redis
```

---

## 执行顺序

```
Week 1      M3（1–2天）+ M1 开始（3天）
Week 2      M1 完成 + M2 adapter 层（淘宝/小红书/抖音）
Week 3      M4（图片处理流水线 + rembg）
Week 4      M5（推荐升级：天气 + fit + 重复惩罚）
Week 5      M6（style sources 异步学习）
Week 6      M7（合身度 V1）+ 集成测试
Week 7–8    端到端验收 + Docker Compose 更新 + iOS 联调
```

---

## 不进入 V2 的功能

| 功能 | 目标版本 | 原因 |
|------|---------|------|
| Outfit Board 抠图拼搭板展示 | V3 | 依赖 M4 cutout 图片积累 |
| Fit Profile 自动学习 | V3 | 需足够 fit_feedback 数据 |
| 穿搭日历 / OOTD 自拍记录 | V3 | 前端主导 |
| 衣橱洞察（cost-per-wear 等）| V3 | 需穿着数据积累 |
| Ask Aidrobe | V4 | LLM 对话基础设施 |
| 虚拟试穿 | V4 | 用户授权 + 成本 |
| Playwright 截图兜底 | V3 | M2 先跑 adapter 层 |
| receipt / 订单截图导入 | V3 | OCR + 商品链接结合 |

---

## 验收指标

| 指标 | 目标 |
|------|------|
| `POST /v1/import-tasks` 响应延迟 | < 200ms（立即返回） |
| 链接导入成功率（淘宝/小红书/抖音）| > 60% |
| 异步识别完成时间（P90） | < 15s |
| 推荐采纳率对比 V1 | 持平或提升 5%+ |
| `fit_status=no_longer_fits` 误推率 | 0% |
| cutout 图片生成成功率 | > 80% |
