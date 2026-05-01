# V1 Slim 之后：阶段路线图

> 前置条件：`docs/plans/2026-05-01-v1-slim-backend-mvp.md` 全部 16 个 Task 完成，后端闭环跑通。

---

## 阶段总览

```
V1 Slim Backend MVP (当前，4~6 周)
  ↓ 完成
Phase 1: 生产部署 + iOS 客户端 MVP (3~4 周，与后端收尾并行)
  ↓ 完成
Phase 2: 内测验证 M4 (2 周，10~20 种子用户)
  ↓ 验证通过
Phase 3: V1.1 功能补齐 (2~3 周)
  ↓
Phase 4: V1.2 智能增强 (2~3 周)
  ↓
Phase 5: V2 条件感知推荐 (4~6 周)
```

---

## Phase 1: 生产部署 + iOS 客户端 MVP

### 1A: 后端生产化 (1~2 周)

V1 Slim Backend MVP 是功能完整但未生产就绪的状态。上线前需要补齐以下基础设施：

#### 1A-1: 真实认证替换 X-User-Id

**技术选型：** Apple Sign In + JWT

V1 Slim 用 `X-User-Id` 临时方案，内测前必须替换。考虑到目标用户是 iOS 用户，最小路径：

| 方案 | 实现量 | 推荐 |
|---|---|---|
| Apple Sign In Only | 小 | **V1 Slim 内测首选** |
| Apple + 手机号 | 中 | V1.1 补 |
| Apple + Google + Email | 大 | V2 再说 |

**实现要点：**
- 参考 meeting-assistant 的 auth 架构：`application/auth/ports/` + `infrastructure/security/jwt/`
- Apple ID Token 验证：用 `python-jose` 或 `PyJWT` 验证 Apple OIDC id_token
- JWT 签发：短期 access token (1h) + 长期 refresh token (30d)
- Session 管理：PostgreSQL 存 session，access token 可选 Redis 缓存（V1 Slim 先不引入 Redis，直接查 DB）
- 在 `app/presentation/api/v1/deps.py` 中替换 `get_current_user`，其他代码零改动

**不做：**
- 多设备 session 管理（V1.1）
- Refresh token rotation（V1.1）
- 通用 API 限流（V1.1）— Vision 成本保护限流已在 1A-2 单独处理，内测期其余端点用户少不需要额外限流

#### 1A-2: 真实 Vision API 接入

**技术选型建议（按优先级）：**

| Provider | 优点 | 缺点 | 单次成本 |
|---|---|---|---|
| **通义千问 VL (qwen-vl-max)** | 国内延迟低、中文好、便宜 | 需阿里云账号 | ~¥0.01-0.03/次 |
| 豆包视觉 (Doubao) | 字节系、延迟低 | API 成熟度不如通义 | ~¥0.01-0.03/次 |
| GPT-4o-mini Vision | 识别质量最稳 | 海外延迟、成本略高 | ~$0.01/次 |

**推荐方案：** 通义千问 VL 作为主 provider，GPT-4o-mini 作为 fallback。

**实现要点：**
- 在 `app/application/ports/vision.py` 已有 `VisionPort` Protocol
- 新建 `app/infrastructure/vision/qwen_vision.py`，实现真实调用
- Prompt 设计：要求模型返回结构化 JSON `{category, color_main, title, confidence_score}`
- 设置每用户每小时调用上限（60 次），用内存计数器即可
- `app/bootstrap/container.py` 根据 `settings.VISION_PROVIDER` 选择 adapter

#### 1A-3: 真实对象存储接入

**技术选型：**

| 方案 | 适合阶段 | 月成本 (10GB) |
|---|---|---|
| **MinIO (自部署)** | 开发/内测 | 免费 |
| **阿里云 OSS** | 生产 | ~¥1 |
| Cloudflare R2 | 生产（无出口费） | ~$0.015/GB |
| AWS S3 | 生产 | ~$0.023/GB |

**推荐方案：** 内测用 MinIO Docker，上线切阿里云 OSS。两者都兼容 S3 协议。

**实现要点：**
- 新建 `app/infrastructure/storage/s3_storage.py`，用 `aiobotocore` 或 `boto3` 生成 presigned URL
- Presigned PUT URL 带 `Content-Length` 限制 (10MB) 和过期时间 (15 min)
- `cover_image_url` = `original_image_url`（V1 Slim 不做图片处理）
- CDN 可暂不接，内测阶段直连 OSS

#### 1A-4: 部署方案

**技术选型：**

| 方案 | 适合阶段 | 月成本 |
|---|---|---|
| **单 VPS + Docker Compose** | 内测 | ~¥50-100 |
| 阿里云 ECS + RDS | 生产 | ~¥200-500 |
| K8s (ACK) | 规模化 | V2 再考虑 |

**推荐方案：** 内测期用单台 VPS + Docker Compose 部署全栈。

**Docker Compose 服务编排：**
```yaml
services:
  api:          # FastAPI (Uvicorn, 单 worker)
  scheduler:    # 独立进程跑 APScheduler (同一 image, 不同 entrypoint)
  postgres:     # PostgreSQL 16
  minio:        # MinIO (内测阶段)
  nginx:        # 反向代理 + HTTPS (Let's Encrypt)
```

**关键决策：**
- API 和 Scheduler **必须分两个容器**（解决 APScheduler 多 worker 问题）
- Scheduler 容器：`python -m app.jobs.scheduler` 作为独立进程常驻；需在 `app/jobs/scheduler.py` 新增 `__main__` 入口，初始化 engine、运行 `AsyncIOScheduler` 并 `asyncio.run()` 挂起事件循环
- API 容器强制 `ENABLE_SCHEDULER=false`，确保 scheduler 只由独立容器驱动
- PostgreSQL 数据 volume 挂载宿主机，定期 pg_dump 备份
- `.env` 不进镜像，通过 `env_file` 注入

**Dockerfile 要点：**
- 多阶段构建：builder stage 用 `uv sync`，runtime stage 只复制 `.venv`
- `uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1`
- 健康检查：`HEALTHCHECK CMD curl -f http://localhost:8000/healthz`

#### 1A-5: 可观测性

**最小可观测性栈（内测期）：**

| 层 | 工具 | 理由 |
|---|---|---|
| **日志** | structlog → JSON → Docker logs | 已有 |
| **错误追踪** | Sentry (免费额度) | 必须，生产发现错误的唯一途径 |
| **指标** | 暂不引入 Prometheus | 内测 <20 用户不需要 |

**Sentry 接入要点：**
- `pip install sentry-sdk[fastapi]`
- 在 `create_app()` lifespan 中 `sentry_sdk.init(dsn=settings.SENTRY_DSN)`
- 过滤 4xx `FriendlyException`，只上报 5xx 和未捕获异常
- 绑定 `user_id` 到 Sentry scope

---

### 1B: iOS 客户端 MVP (3~4 周，与 1A 并行)

#### 技术栈

| 选项 | 推荐 |
|---|---|
| **SwiftUI** | **是** — 与 V1 Slim 源文档一致，目标 iOS 16+ |
| UIKit | 否 — 新项目没必要 |
| Flutter / RN | 否 — V1 Slim 明确 iOS 优先，跨端成本反而更高 |

#### 客户端架构

```
AidrobeApp/
├── App/
│   ├── AidrobeApp.swift          # @main entry
│   └── AppState.swift            # 全局状态 (auth, user)
├── Core/
│   ├── Network/
│   │   ├── APIClient.swift       # URLSession + async/await
│   │   ├── APIEndpoints.swift    # 枚举所有端点
│   │   └── AuthInterceptor.swift # JWT 注入 + token refresh
│   ├── Storage/
│   │   └── KeychainHelper.swift  # Token 安全存储
│   └── Models/
│       ├── WardrobeItem.swift
│       ├── Recommendation.swift
│       └── ImportTask.swift
├── Features/
│   ├── Onboarding/               # 欢迎页 + 出门时间设置
│   ├── Import/                   # 拍照/相册 + 确认页
│   ├── Wardrobe/                 # 衣橱列表 + 详情
│   ├── Recommendation/           # 今日推荐 + 方案详情
│   ├── Settings/                 # 设置页
│   └── Auth/                     # Apple Sign In
├── Shared/
│   ├── Components/               # 通用 UI 组件
│   └── Extensions/
└── Resources/
```

#### 客户端核心页面 (11 个，与 V1 Slim 对齐)

| 页面 | 优先级 | 对应 API |
|---|---|---|
| 欢迎页 | P0 | — |
| Apple 登录 | P0 | `POST /v1/auth/login/apple` |
| 出门时间设置 | P0 | `PATCH /v1/user/settings` |
| 添加衣物入口 | P0 | `POST /v1/uploads/presign` |
| 导入确认页 | P0 | `POST /v1/import-tasks`, `POST .../confirm` |
| 今日推荐页 | P0 | `GET /v1/recommendations/today` |
| 推荐详情页 | P0 | `GET /v1/recommendations/outfits/{id}` |
| 反馈弹层 | P0 | `POST .../wear`, `POST .../feedback` |
| 衣橱列表页 | P0 | `GET /v1/wardrobe-items` |
| 衣物详情页 | P1 | `GET /v1/wardrobe-items/{id}`, `PATCH ...` |
| 设置页 | P1 | `GET/PATCH /v1/user/settings` |

#### 客户端关键设计决策

1. **网络层：** 纯 `URLSession` + `async/await`，不引入 Alamofire（V1 Slim 只有 14 个 REST API，不需要重框架）
2. **图片上传：** 系统相机/相册 → presign 获取上传 URL → 直传 OSS → 回传 `file_url` 给 import-tasks
3. **本地缓存：** 今日推荐用 `UserDefaults` 简单缓存，保证冷启动不白屏。衣橱列表用内存缓存，不做本地数据库（V1.1 再考虑 SwiftData）
4. **下拉刷新：** 推荐页支持下拉刷新，触发 `GET /v1/recommendations/today`（后端会返回最新版或昨日版）
5. **倒计时：** 首屏展示"距离出门 XX 分钟"，纯客户端计算（`departure_time - now`）

---

## Phase 2: 内测验证 M4 (2 周)

### 目标

10~20 种子用户实际使用，验证核心假设：**用户是否愿意在早晨打开 App 看一眼今天穿什么。**

### 埋点方案

**技术选型：** 后端埋点优先（不依赖客户端 SDK）

`operation_logs` 表在 Phase 1 上线前建好（不等到 Phase 2），保证第一批用户数据完整。

**表结构：**
```sql
operation_logs (
  id         uuid primary key,
  user_id    uuid not null,
  event      varchar not null,
  properties jsonb,
  created_at timestamptz default now()
)
-- 索引：(user_id, event, created_at)
```

**埋点事件清单：**

| 事件 | 触发点 | properties 关键字段 |
|---|---|---|
| `user_created` | 认证成功首次写 users 表 | — |
| `user_login` | 每次登录 / token refresh | device_token, platform |
| `app_open` | `GET /v1/recommendations/today` | hour_of_day, had_cache |
| `item_imported` | `POST /v1/import-tasks/{id}/confirm` | category, color_main |
| `recommendation_viewed` | `GET /v1/recommendations/outfits/{id}` | outfit_type |
| `outfit_worn` | `POST .../wear` | outfit_id, worn_date |
| `feedback_given` | `POST .../feedback` | feedback_type |
| `item_deleted` | `DELETE /v1/wardrobe-items/{id}` | — |

> `user_created` 和 `user_login` 是留存计算的必须分母，Phase 1 未落则 D1/D7 无法计算。

**留存 SQL（示例）：**
```sql
-- D1 留存：注册后次日有 app_open 的用户占比
WITH cohort AS (
  SELECT user_id, date(created_at) AS reg_date
  FROM operation_logs WHERE event = 'user_created'
),
active_d1 AS (
  SELECT DISTINCT user_id, date(created_at) AS active_date
  FROM operation_logs WHERE event = 'app_open'
)
SELECT
  c.reg_date,
  count(DISTINCT c.user_id)                                      AS total,
  count(DISTINCT a.user_id)                                      AS retained_d1,
  round(count(DISTINCT a.user_id)::numeric / count(DISTINCT c.user_id), 2) AS d1_rate
FROM cohort c
LEFT JOIN active_d1 a
  ON c.user_id = a.user_id AND a.active_date = c.reg_date + 1
GROUP BY c.reg_date;
```

**分析：** 内测期直接 SQL 查询 `operation_logs`，不需要 BI 工具。

### 观测指标

三档分级：**健康信号 → 预警线 → 终止线**，AND/OR 逻辑来自 `docs/11` §4.2。

| 指标 | 计算分母 | 健康信号 | 预警线 | 备注 |
|---|---|---|---|---|
| D1 留存 | `user_created` cohort | > 40% | < 20% | 需 `user_created` 事件作分母 |
| D7 留存 | `user_created` cohort | > 20% | < 10% | 需 `user_created` 事件作分母 |
| 人均录入件数中位数 | 全量用户 | > 6 件 | < 6 件 | `item_imported` 聚合；低于终止线即进入预警 |
| 晨间采纳率 | `app_open` (6:30-9:30) | > 15% | < 5% | `outfit_worn` / `app_open` |
| 喜欢率 | `feedback_given` | > 50% | < 30% | `like` / 全反馈 |

### 终止条件（来自 `docs/11` §4.2）

> **同时满足以下两条**，连续 2 周，暂停 V2 回到 PMF 探索：
> 1. 晨间采纳率 < 5%
> 2. 人均录入件数中位数 < 6 件
>
> 单条预警不触发终止，但需要本周内修复。

### 内测期间重点修复方向

1. **识别准确率：** 如果品类/颜色错误率 > 30%，调整 vision prompt 或换 provider
2. **推荐重复感：** 如果用户反馈"每天一样"，检查 freshness scoring 权重
3. **建库阻力：** 如果人均录入中位数 < 6 件，简化确认页（一键确认，不展开编辑）

---

## Phase 3: V1.1 功能补齐 (2~3 周)

> 前置：Phase 2 验证通过（不触发终止条件）

### 3A: 后端新增

#### 3A-1: Push 通知

**技术选型：** APNs (Apple Push Notification service) 直连

| 方案 | 推荐 | 理由 |
|---|---|---|
| **APNs 直连 (aioapns)** | **是** | 只有 iOS，不需要跨平台推送服务 |
| Firebase Cloud Messaging | 否 | V1 只做 iOS |
| 极光/个推 | 否 | 国内服务，但 V1 用户少不需要 |

**实现要点：**
- 新建 `app/infrastructure/push/apns_provider.py`
- 用户表增加 `device_token` 字段
- 推送时机：推荐生成成功后（在 scheduler job 中触发）
- 推送文案：模板化，如"今天有 3 套穿搭方案，出门前看一下"
- 用户可在设置页关闭推送 (`push_enabled` 字段，V1 Slim 推迟了，现在补)

#### 3A-2: 链接导入 / 分享扩展

**技术评估（来自 `docs/11` §3.4 的风险）：**

链接解析（淘宝/京东/小红书）稳定性 < 70%，V1.1 **优先做分享扩展 + 截图识别**，链接导入降级为 best-effort。

| 导入方式 | 实现方案 | 稳定性 |
|---|---|---|
| **iOS Share Extension** | 系统级分享 → 截取图片 → 走 vision 识别 | 高 |
| 截图识别 | 从相册选截图 → vision 识别 | 高 |
| 链接导入 | URL → 服务端抓取主图 → vision | 中低 |

**后端变更：**
- `import_tasks.input_type` 增加 `screenshot`, `shared_content`
- `import_tasks.source_type` 增加 `share`, `screenshot`
- 链接导入新增 `app/infrastructure/import/link_parser.py`：HTTP 抓取 + HTML 解析主图
- 新建 `app/application/ports/link_parser.py` Protocol

#### 3A-3: 穿搭历史页

- 新增 `GET /v1/outfit-history`，按日期倒序
- 支持 `start_date`, `end_date`, 分页
- 返回每日穿了什么、给了什么反馈

#### 3A-4: 反馈原因标签

- `POST .../feedback` 增加可选 `reason_tags: ["too_formal", "too_complex", "not_me", "not_for_today"]`
- `outfit_feedback` 表增加 `reason_tags: JSONB`
- 反馈标签用于 V1.2 的偏好学习

#### 3A-5: 通用 API Rate Limiting

> Vision 成本保护限流（60 次/user/hour）已在 Phase 1 的 1A-2 实现，本节只补通用端点限流。

**技术选型：**

| 方案 | 推荐 | 理由 |
|---|---|---|
| **内存计数器 (dict + sliding window)** | **V1.1 用这个** | 单进程，简单，不需要 Redis |
| Redis + sliding window | V1.2 升级 | 多 worker 时需要 |

**V1.1 新增限流规则：**
- 全局端点: 100 req/min/user（防爬）
- `POST /v1/auth/login`: 10 req/min/IP（防暴力破解）

#### 3A-6: 时间富裕度分档

来自 `docs/12` §3.2 的明确 V1.1 补齐项（V1 Slim 全员标准档，V1.1 补分档逻辑）。

| 剩余时间 | 档位 | 推荐策略 |
|---|---|---|
| > 90 min | 宽裕 | 可推需要准备的方案（叠穿、稍复杂） |
| 30-90 min | 标准 | 标准三套（与 V1 Slim 一致） |
| 15-30 min | 紧张 | 三套偏 fast，过滤状态不佳的单品 |
| < 15 min | 极速 | 只给 1 套"开门即穿"，去掉 stylish 档 |

**实现要点：**
- 预生成时生成全量候选池（不只取 top3），每套标记 `time_sensitivity` 档位
- `GET /v1/recommendations/today` 计算 `departure_time - now` 剩余时间，从候选池动态筛选返回
- `daily_recommendations` 存完整候选池（JSONB），API 层动态切片，不重新生成
- V1 Slim 单档逻辑保持兼容：若 `departure_time` 未设置，默认返回标准档

### 3B: iOS 客户端补齐

- Push notification 权限申请 + token 上报
- Share Extension（系统级，拦截"分享到 Aidrobe"）
- 穿搭历史页
- 反馈弹层增加原因标签
- 衣物详情页内联编辑
- 首屏倒计时联动：剩余时间进入"紧张"或"极速"档时，UI 给出视觉提示

---

## Phase 4: V1.2 智能增强 (2~3 周)

### 4A: Embedding + 向量召回

**技术选型：**

| 方案 | 推荐 | 理由 |
|---|---|---|
| **pgvector** | **是** | 已用 PostgreSQL，不引入新基础设施 |
| Qdrant / Milvus | 否 | V1.2 数据量不需要独立向量库 |
| Pinecone | 否 | 托管服务，成本不必要 |

**实现要点：**
- PostgreSQL 安装 `pgvector` 扩展
- 新建 `item_embeddings` 表：`item_id, model_name, vector(512), created_at`
- 导入确认时异步生成 embedding（后台任务）
- Embedding 模型：用 `CLIP ViT-B/32`（通过 `transformers` 库本地推理，或调用通义千问的 embedding API）
- 推荐引擎新增 **embedding 相似度召回** 作为候选扩展（不替代规则生成）

### 4B: LLM 推荐文案

**技术选型：** 通义千问-turbo（最便宜的中文 LLM）

**实现要点：**
- 新建 `app/application/ports/llm.py` Protocol
- 新建 `app/infrastructure/llm/qwen_llm.py`
- 推荐生成后，异步调用 LLM 润色 `reason_text`
- **降级策略：** 如果 LLM 调用失败或超时 (3s)，保留模板文案
- **成本控制：** 每用户每日最多 3 次 LLM 调用（正好 3 套方案）
- V1.2 不做 streaming，直接返回完整文案

### 4C: 基础偏好学习

**不做复杂推荐模型**，只做简单的统计偏好：

```
用户偏好 = {
    preferred_colors: Counter(worn outfit main colors),
    disliked_colors: Counter(disliked outfit main colors),
    preferred_categories: Counter(worn outfit main item categories),
    wear_frequency: {item_id: count},
}
```

**实现：**
- 新建 `app/application/recommendations/preferences.py`
- 从 `wear_history` + `outfit_feedback` 聚合
- 推荐引擎 `preference_score` 维度从固定值变为动态计算
- 缓存在 `user_settings` 的 JSONB 字段中，每次推荐生成前刷新

### 4E: 客制化审美

> 目标：让用户通过图片、链接、文件等方式传达"我喜欢这种风格"，推荐引擎基于此做个性化匹配，而不是只靠规则。

**前置条件：**
- Phase 3 的灵感图/分享扩展导入链路已跑通（图片 → Vision API → 结构化）
- Phase 4A 的 `item_embeddings` 基础设施已落地

#### 用户输入方式（按实现优先级）

| 方式 | 实现依赖 | 优先级 |
|---|---|---|
| **穿搭灵感图片**（相册/拍照） | Vision API + Embedding | P0，V1.2 做 |
| **图片链接**（小红书/Pinterest URL） | Phase 3 链接导入 + Embedding | P1，V1.2 做 |
| **风格标签自选**（快速冷启动） | 纯前端 + user_settings | P0，V1.2 做 |
| **文件导入**（截图/PDF 穿搭手册） | Vision API OCR + Embedding | P2，V2 再考虑 |

#### 风格档案（Style Profile）

新建 `user_style_profiles` 表，存储用户的审美向量：

```sql
user_style_profiles (
  id            uuid primary key,
  user_id       uuid not null references users(id),
  source_type   varchar not null,  -- 'inspiration_image' | 'style_tag' | 'wear_history' | 'feedback'
  source_id     uuid,              -- 关联的 wardrobe_item_id 或 import_task_id
  style_vector  vector(512),       -- CLIP embedding
  weight        numeric(4,2),      -- 用户主动输入 > 穿着历史 > 点赞
  created_at    timestamptz default now()
)
-- 索引：(user_id, source_type)
```

**权重策略：**

| 来源 | 权重 | 理由 |
|---|---|---|
| 灵感图片（用户主动上传） | 1.0 | 最强意图信号 |
| 已穿搭 + 点赞 | 0.8 | 实际行为验证 |
| 已穿搭（无反馈） | 0.5 | 行为信号，弱于点赞 |
| 不喜欢 | -0.6 | 负向信号，降权用 |
| 风格标签（用户选择） | 0.7 | 意图明确但粒度粗 |

#### 灵感图导入流程

```
用户上传灵感图
  → POST /v1/uploads/presign → 直传 OSS
  → POST /v1/style/inspirations { image_url }
    → Vision API 提取风格特征（颜色、品类、风格词）
    → CLIP 生成 embedding → 存 user_style_profiles
    → 异步触发今日推荐重算
  → GET /v1/style/inspirations  列出已上传的灵感图
  → DELETE /v1/style/inspirations/{id}  删除某张
```

**新增 API：**
- `POST /v1/style/inspirations` — 上传灵感图
- `GET /v1/style/inspirations` — 列出灵感图
- `DELETE /v1/style/inspirations/{id}` — 删除灵感图
- `PATCH /v1/user/settings` 扩展 `style_tags: list[str]`（快速冷启动时用）

#### 推荐引擎集成

在 Phase 4A 的 embedding 召回基础上，新增 `aesthetic_score` 维度：

```python
# app/application/recommendations/engine.py 扩展
aesthetic_score = cosine_similarity(
    outfit_embedding,           # outfit 中主单品的 embedding 平均值
    user_style_profile_vector   # 用户风格档案的加权平均 embedding
)

# 最终评分
total = (
    color_score      * 0.35  # 原有
  + freshness_score  * 0.25  # 原有
  + aesthetic_score  * 0.35  # 新增
  + type_boost       * 0.05  # 原有
)
```

**冷启动策略（没有 style profile 的新用户）：**
- `aesthetic_score` 缺失时，权重重新分配到 `color_score` 和 `freshness_score`
- 引导用户在设置页选 3 个风格标签（简约 / 街头 / 商务 / 休闲 / 运动），用标签对应的预训练 embedding 初始化 profile

#### 实现文件

```
app/application/style/
  __init__.py
  add_inspiration.py        # 上传灵感图用例
  list_inspirations.py
  delete_inspiration.py
  build_style_profile.py    # 聚合 wear_history + feedback + inspirations → profile vector

app/infrastructure/persistence/models/style.py
  # UserStyleProfile model

app/infrastructure/persistence/repositories/style_repo.py

app/presentation/api/v1/routers/style.py
app/presentation/api/v1/schemas/style.py
```

**不做（推迟到 V2）：**
- 跨用户风格聚类（"和你审美相似的用户也穿了 X"）
- 风格 DNA 可视化页面
- 文件导入（PDF/HTML 穿搭手册解析）
- 风格漂移检测（随季节/心情的动态调整）

---

### 4D: 基础设施升级

| 组件 | V1 Slim | V1.2 升级 |
|---|---|---|
| 任务队列 | BackgroundTasks + APScheduler | 考虑引入 **arq** (轻量 Redis 队列) |
| 缓存 | 无 | Redis 缓存热数据（今日推荐、用户设置） |
| Worker | 单进程 | API 多 worker + scheduler 单进程 + arq worker |

**为什么是 arq 而不是 Celery：**
- arq 是纯 async、轻量级，只依赖 Redis
- Celery 对于 < 1000 DAU 过重
- arq 适合：embedding 生成、LLM 调用、链接解析等异步任务

---

## Phase 5: V2 条件感知推荐 (4~6 周)

> 从"今天穿什么"升级到"结合天气、场景、时间，今天怎么穿最合适"

### 5A: 天气接入

**技术选型：**

| 服务 | 免费额度 | 推荐 |
|---|---|---|
| **和风天气 (QWeather)** | 1000 次/天 | **是** — 国内最好的天气 API |
| OpenWeather | 1000 次/天 | 备选 |
| 彩云天气 | 有限 | 备选 |

**实现：**
- 用户设置增加 `location`（城市级别，不需要精确 GPS）
- 推荐生成前拉取当日天气：温度、体感温度、降雨概率、风力
- 天气影响推荐的方式：
  - 温度 < 10°C → 必须有外套
  - 降雨概率 > 50% → 降低浅色鞋权重
  - 体感温度映射到"薄/适中/厚"档位

### 5B: 场景快切

来自 `docs/11` §2.5 的建议：V1 不做，但如果 V2 前用户大量反馈"推的不对"可前置。

**实现：**
- 推荐页顶部加一行 3 个 chip：`普通通勤` / `见客户` / `轻松日`
- 选择后客户端带 `scene` 参数调 `GET /v1/recommendations/today?scene=formal`
- 后端根据 scene 调整 `formality_score` 权重阈值
- 不需要 AI 理解，纯规则权重调整

### 5C: 执行可行性增强

- `wardrobe_items` 补 `location` 字段（主衣柜/抽屉/收纳箱/鞋柜）
- 推荐引擎增加 `execution_score` 维度：位置集中度 + 状态可穿性
- "需要熨烫"等状态不在 V1 Slim 的两态中 → V2 考虑扩展为 `wearable / needs_prep / unavailable`

### 5D: 重复穿搭降权增强

- V1 Slim 只降权"最近 3 天穿过的主单品"
- V2 增加：最近 7 天相似组合降权（基于 outfit_items 的 Jaccard 相似度）
- 避免"换双鞋就算新方案"的感知问题

---

## 技术债务与架构演进总结

| 维度 | V1 Slim | V1.1 (Phase 3) | V1.2 (Phase 4) | V2 (Phase 5) |
|---|---|---|---|---|
| 认证 | X-User-Id (dev only) | Apple Sign In + JWT | 同 | 同 |
| 推荐档位 | 全员标准档 | **时间富裕度 4 档** | 同 | + 天气/场景叠加 |
| 导入方式 | 拍照/相册 | + 分享扩展 + 截图 | 同 | 同 |
| 推送 | 无 | APNs 直连 | 同 | 同 |
| 反馈 | like/dislike | + 原因标签 | 偏好学习 | 同 |
| 限流 | Vision 60次/user/hr | + 通用 API 限流 | Redis sliding window | 同 |
| 任务队列 | BackgroundTasks | 同 | arq + Redis | 同 |
| 缓存 | 无 | 无 | Redis | 同 |
| 向量 | 无 | 无 | pgvector + item_embeddings | 同 |
| 客制化审美 | 无 | 无 | 灵感图 + style profile + aesthetic_score | + 跨用户聚类 |
| LLM | 无 | 无 | 通义千问-turbo | 同 |
| 天气 | 无 | 无 | 无 | 和风天气 |
| 场景快切 | 无 | 无 | 无 | chip 选择 |
| 对象存储 | local dev | MinIO / OSS | 同 | 同 |
| 部署 | 本地 | Docker Compose (API + Scheduler 分容器) | 同 | 考虑 K8s |
| 监控 | structlog | + Sentry + operation_logs | 同 | + Prometheus |
| 数据库 | PostgreSQL | + operation_logs 表 | + pgvector 扩展 | 同 |

---

## 风险检查点

### Phase 1 结束时检查
- [ ] Apple Sign In 流程是否顺畅
- [ ] Vision API 识别准确率是否 > 70%（品类+颜色）
- [ ] 端到端延迟是否 < 3s（import task 创建到 waiting_confirm）
- [ ] 首屏推荐加载是否 < 1s

### Phase 2 结束时检查（决定是否继续）
- [ ] 晨间采纳率与人均录入件数中位数是否**同时**低于终止线（连续 2 周）— 两条同时触发才停
- [ ] 晨间采纳率单条是否进入预警（< 5%）— 单条预警需本周内响应
- [ ] 人均录入件数中位数单条是否进入预警（< 6 件）— 单条预警需本周内响应
- [ ] 用户是否在 5 分钟内完成 5 件衣物录入（功能合格）
- [ ] "打开即有"是否做到（任何时刻不出现 loading 空屏）

### Phase 3 结束时检查
- [ ] Push 通知是否提升晨间打开率
- [ ] 分享扩展导入是否降低建库成本
- [ ] 反馈标签数据是否足够训练 V1.2 偏好模型

### Phase 4 结束时检查（客制化审美验收）
- [ ] 用户上传灵感图后，推荐结果是否可感知变化
- [ ] `aesthetic_score` 是否实际提升喜欢率（对比 Phase 3 baseline）
- [ ] embedding 生成延迟是否 < 2s（不阻塞确认流程）
- [ ] 冷启动用户（无 style profile）的推荐质量是否与 V1.1 持平
- [ ] 分享扩展导入是否降低建库成本
- [ ] 反馈标签数据是否足够训练 V1.2 偏好模型

---

## 一句话总结

V1 Slim 验证的是"用户愿不愿意看一眼"。之后的路线是：**先让它能上线（Phase 1）→ 先验证假设（Phase 2）→ 再补齐功能（Phase 3-4）→ 最后做条件感知（Phase 5）**。每个阶段都有明确的终止/继续判据，不做无验证的功能堆叠。
