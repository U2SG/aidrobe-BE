# V3 开发计划：风格学习闭环 + 衣橱洞察

> 版本：V3  
> 前置条件：V2 全部模块完成；用户有足够穿着记录（推荐采纳率稳定）；fit_feedback 数据积累 > 2 周  
> 目标：系统越来越"像我"；推荐展示升级为可信拼搭板；用户长期留存由衣橱洞察驱动  
> 预计工期：6–8 周

---

## 版本定位

V2 解决了"推荐能不能执行"。V3 要解决：

```
推荐：可执行 → 越来越像我（风格学习真正生效）
展示：单品卡片 → 真实抠图拼搭板（不生图，不改衣服）
留存：每日推荐 → 穿搭日历 + 衣橱洞察（长期价值锚）
合身：手动输入 → Fit Profile 自动学习
```

---

## 模块清单

### M8 — pgvector + Embedding 基础设施

**前置**：V2 M4 图片流水线已生成 `cutout_image_url`，图片质量稳定。

**目标**：启用 pgvector，为衣物和 style sources 生成真实 embedding，替换 V2 的 JSON 近似向量。

**迁移**：
```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE item_embeddings (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  item_id     uuid NOT NULL REFERENCES wardrobe_items(id) ON DELETE CASCADE,
  model_name  varchar(64) NOT NULL,          -- clip-vit-b32 / qwen-embedding
  vector      vector(512) NOT NULL,
  created_at  timestamptz DEFAULT now()
);
CREATE INDEX ON item_embeddings USING hnsw (vector vector_cosine_ops);
```

**Embedding 生成**（异步 worker，入库/图片处理完成后触发）：
```
cutout_image_url
  → CLIP ViT-B/32 本地推理（transformers 库）
  → 写入 item_embeddings
  → 更新 user_style_profiles.style_vector（真实 CLIP embedding 替换 JSON 近似值）
```

**推荐引擎升级**：`aesthetic_score` 从稀疏余弦相似度升级为真实 CLIP embedding 余弦相似度。

**新增依赖**：
```toml
pgvector = ">=0.3"
transformers = ">=4.40"   # CLIP 本地推理
torch = ">=2.2"           # CPU only，仅用于 embedding，不做训练
```

**新增文件**：
- `app/application/ports/embedding.py`：`EmbeddingPort` Protocol
- `app/infrastructure/embedding/clip_embedding.py`：本地 CLIP 实现
- `app/infrastructure/persistence/models/embedding.py`：`ItemEmbedding` model
- `app/infrastructure/persistence/repositories/embedding_repo.py`
- `app/infrastructure/workers/embedding_worker.py`

---

### M9 — Fit Profile 自动学习

**前置**：V2 M7 的 `fit_feedback` 数据积累 > 2 周，有足够样本。

**目标**：从用户穿后反馈中自动学习版型偏好，推荐理由可引用"你喜欢宽松上衣"。

**新增表** `user_fit_profiles`：
```sql
CREATE TABLE user_fit_profiles (
  user_id                uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  preferred_top_fit      varchar(32),    -- fitted / regular / relaxed / oversized
  preferred_bottom_fit   varchar(32),    -- slim / straight / wide_leg / loose
  preferred_waist_rise   varchar(32),    -- high / mid / low
  preferred_length_tags  jsonb,          -- ["cropped", "regular", "longline"]
  avoid_fit_tags         jsonb,          -- ["too_tight", "too_short", "low_rise"]
  comfort_priority       varchar(16),    -- low / medium / high
  updated_at             timestamptz DEFAULT now()
);
```

**学习规则**（周期性任务，每周运行一次）：
```python
# 从 fit_feedback 聚合
tight_items → fit_intent 提取 → 更新 avoid_fit_tags
fits_well + like → fit_intent 提取 → 更新 preferred_* 字段
3+ 件 tight 同类 fit_intent → comfort_priority 升为 high
```

**推荐引擎集成**：
- `fit_score` 增加 `preferred_fit_match_score`：用户穿着偏好 vs 推荐单品 `fit_intent` 的匹配度
- 推荐理由文案可引用 Fit Profile："这件直筒裤符合你喜欢的下装版型"

**新增接口**：
```
GET  /v1/user/fit-profile     查看当前 Fit Profile
PATCH /v1/user/fit-profile    用户主动修改偏好
```

---

### M10 — 真实抠图拼搭板（Outfit Board V1）

**前置**：V2 M4 已为多数衣物生成 `cutout_image_url`。

**目标**：推荐展示从"单品缩略图列表"升级为"真实衣物拼搭板"，不生图，不改变衣服。

**展示策略**（按 Doc 18 §4）：
```
有 cutout_image_url → 透明抠图排版
无 cutout_image_url → 降级到 thumbnail_url
无 thumbnail_url    → 占位图 + 单品名
```

**Outfit Board 生成流程**：
```
outfit 确定后（推荐生成完成时）
→ 异步 worker：
  1. 加载各单品 cutout_image_url
  2. 按品类角色（top/bottom/outerwear/shoes/bag）套用布局模板
  3. 导出 outfit_board_image_url
  4. 写入 outfit_visual_assets 表
```

**布局模板**（通勤标准）：
```
      外套（可选）
  上衣          包（可选）
      下装 / 连衣裙
          鞋
```

**新增表** `outfit_visual_assets`：
```sql
CREATE TABLE outfit_visual_assets (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id           uuid NOT NULL REFERENCES users(id),
  outfit_id         uuid,
  visual_type       varchar(32),   -- card / outfit_board / style_board
  image_url         text,
  template_id       varchar(64),
  generation_status varchar(32) DEFAULT 'queued',
  input_snapshot    jsonb,         -- outfit items + style profile + 模板参数
  error_code        varchar(64),
  error_message     text,
  created_at        timestamptz DEFAULT now(),
  updated_at        timestamptz DEFAULT now()
);
```

**新增接口**：
```
GET /v1/wardrobe-items/{item_id}/images     获取衣物所有图片资产
GET /v1/outfit-visual-assets/{asset_id}    查询 board 生成状态
```

**新增文件**：
- `app/infrastructure/workers/outfit_board_worker.py`
- `app/infrastructure/persistence/models/outfit_visual.py`

**不做**：生成式 AI 图片（V4 可选）；每天三套全部生成（按需生成 + 缓存）。

---

### M11 — 穿搭日历与 OOTD 记录

**目标**：让用户能回顾"那天我穿了什么"，增强长期留存和衣橱情感价值。

**新增接口**：
```
GET  /v1/outfit-history             穿搭历史，按日期倒序，支持分页
GET  /v1/outfit-history/{date}      某天穿搭详情
POST /v1/outfit-history/{id}/ootd   上传当天 OOTD 自拍
GET  /v1/outfit-history/calendar    返回有穿搭记录的日期列表（日历视图用）
```

**穿搭历史 Response**（扩展现有 `wear_history`）：
```json
{
  "date": "2026-05-05",
  "outfit": {
    "outfit_id": "uuid",
    "outfit_type": "fast",
    "items": [...],
    "outfit_board_url": "https://...",
    "reason_text": "..."
  },
  "feedback_type": "like",
  "reason_tags": ["comfortable"],
  "ootd_image_url": null
}
```

**DB 变更**：
```sql
ALTER TABLE outfit_wear_history
  ADD COLUMN ootd_image_url text;
```

**OOTD 上传流程**：走现有 presign 上传流程（`POST /v1/uploads/presign`），然后回传 URL 到 `POST .../ootd`。

---

### M12 — 衣橱洞察（Wardrobe Insights）

**目标**：让用户看到"我最常穿什么"、"什么衣服闲置了"、"我的穿搭习惯是什么"，形成长期留存锚。

**数据来源**：`wear_history`、`outfit_feedback`、`fit_feedback`、`wardrobe_items`——全部已有，无需新建数据来源。

**新增接口**：
```
GET /v1/insights/summary              衣橱概览（件数、活跃率、闲置数）
GET /v1/insights/top-items            最常穿单品 Top 10
GET /v1/insights/idle-items           闲置超过 N 天的单品
GET /v1/insights/color-palette        常用色彩分布
GET /v1/insights/style-trend          风格标签变化趋势
GET /v1/insights/cost-per-wear        成本/穿着次数（需衣物有价格字段）
```

**`insights/summary` Response**：
```json
{
  "total_items": 48,
  "active_rate": 0.62,
  "idle_count": 12,
  "idle_threshold_days": 60,
  "most_worn_category": "top",
  "favorite_color": "black"
}
```

**DB 变更**（可选，提升查询效率）：
```sql
ALTER TABLE wardrobe_items
  ADD COLUMN purchase_price numeric(10, 2),
  ADD COLUMN wear_count int DEFAULT 0;  -- 冗余字段，由 wear_history 聚合更新
```

**闲置提醒**（push 通知，依赖 V1.1 APNs）：
- 每月运行一次：找出 `wear_count=0 AND created_at < 60 days ago` 的单品
- 推送文案："你有 3 件衣服已经 2 个月没穿了，要不要检查一下？"

---

### M13 — Style Profile 风格化展示

**目标**：推荐展示不仅表达"这套能穿"，还表达"这套像你"（Doc 18 §5）。

**展示模板系统**：
- Style Profile → 选择推荐页展示模板（极简通勤 / 温柔轻熟 / 成熟正式）
- 模板影响：背景色调、文字风格、推荐理由措辞

**推荐理由升级**：
```
普通理由：这套颜色协调，适合通勤。
Style Profile 增强：延续了你喜欢的黑白灰通勤风格，干净利落；主单品都在主衣柜。
```

**推荐微调入口**（客户端）：
```
[更正式] [更轻松] [更省时] [更有风格]
```
客户端带 `tone_adjustment` 参数调整，后端在候选池中重新排序（不重新生成）。

**`GET /v1/recommendations/today` 响应扩展**：
```json
{
  "outfits": [...],
  "style_context": {
    "template_id": "minimal_commute_v1",
    "active_tags": ["black", "white", "minimal"],
    "confidence": 0.74
  }
}
```

---

### M14 — 衣物子品类与扩展字段

**目标**：支持更细粒度的推荐过滤和展示（Doc 17 §6.2）。

**`wardrobe_items` 新增字段**：
```sql
ALTER TABLE wardrobe_items
  ADD COLUMN sub_category varchar(32),      -- shirt / t-shirt / blazer / etc
  ADD COLUMN color_secondary varchar(32),   -- 辅色
  ADD COLUMN material varchar(64),          -- 材质标签
  ADD COLUMN season_tags jsonb,             -- ["spring", "autumn"]
  ADD COLUMN style_tags jsonb,              -- ["minimal", "casual"]
  ADD COLUMN formality_score smallint,      -- 1-5，1=极休闲，5=正式
  ADD COLUMN time_cost_level varchar(16);   -- low / medium / high（取衣难度）
```

**导入确认页**：Vision API 已识别的字段自动填入，用户可调整；`formality_score` 由 Vision API 推断，不要求用户手填。

**DB 迁移**：`0013_wardrobe_extended_fields`

---

## 数据库迁移计划

| 迁移编号 | 文件名 | 内容 |
|---------|-------|------|
| 0012 | `0012_pgvector_embeddings` | 启用 pgvector 扩展 + `item_embeddings` 表 |
| 0013 | `0013_wardrobe_extended_fields` | `wardrobe_items` 扩展字段 |
| 0014 | `0014_user_fit_profiles` | `user_fit_profiles` 表 |
| 0015 | `0015_outfit_visual_assets` | `outfit_visual_assets` 表 |
| 0016 | `0016_ootd_field` | `outfit_wear_history.ootd_image_url` |
| 0017 | `0017_wardrobe_insights_fields` | `wardrobe_items.purchase_price`、`wear_count` |

---

## 执行顺序

```
Week 1–2    M8（pgvector + embedding 基础设施）
Week 2–3    M9（Fit Profile 自动学习）
Week 3–4    M10（Outfit Board 生成）
Week 4–5    M11（穿搭日历 + OOTD）
Week 5–6    M12（衣橱洞察接口）
Week 6–7    M13（风格化展示 + 推荐微调）
Week 7–8    M14（扩展字段）+ 集成测试 + iOS 联调
```

---

## 不进入 V3 的功能

| 功能 | 目标版本 | 原因 |
|------|---------|------|
| Ask Aidrobe 对话 | V4 | LLM 对话基础设施独立 |
| 虚拟试穿 | V4 | 用户授权 + 成本 |
| 购物前 sense-check | V4 | 需电商数据联动 |
| 旅行 packing list | V4 | 需行程理解能力 |
| 跨用户风格聚类 | V4 | 需足够用户规模 |
| 半年度/年度衣橱报告 | V4 | 需数据积累 |
| Playwright 截图兜底 | V3 后期 | 可在 M2 adapter 层稳定后补充 |

---

## 验收指标

| 指标 | 目标 |
|------|------|
| Outfit Board 生成成功率 | > 85% |
| style profile 生效用户比例（有 ≥1 个 learned source）| > 40% |
| 推荐喜欢率对比 V2 | 提升 5%+ |
| 穿搭日历页 7 日留存贡献 | 打开日历 ≥1次/周的用户 D7 留存 vs 未打开用户 |
| 衣橱洞察点击率 | > 30%（打开 App 后至少看过一次） |
| Fit Profile 覆盖率（有数据的用户占比）| > 50% |
