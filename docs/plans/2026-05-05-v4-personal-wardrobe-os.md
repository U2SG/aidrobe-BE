# V4 开发计划：个人衣橱操作系统

> 版本：V4  
> 前置条件：V3 全部模块完成；embedding 推荐稳定运行 > 4 周；衣橱洞察 DAU 占比 > 30%  
> 目标：从"推荐工具"升级为"个人衣橱与风格生活的长期操作系统"  
> 预计工期：8–12 周

---

## 版本定位

V3 验证了"系统越来越像我"。V4 的目标是：

```
从单次出门决策 → 全生命周期衣橱管理
从"今天穿什么" → "我的衣橱帮我做决定"
从工具 → 操作系统
```

核心升级：
```
导入：现有多来源 → 加 receipt/订单截图/购物邮件（全渠道建库）
风格：异步学习 → PDF/杂志/多文件批量摄取
推荐：场景推荐 → Ask Aidrobe 主动问答
展示：抠图拼搭板 → 可选虚拟试穿
长期：衣橱洞察 → 购物前 sense-check + 出差 packing list
```

---

## 模块清单

### M15 — Ask Aidrobe（AI 问答助手）

**目标**：以对话方式回答穿搭相关的开放性问题，作为每日推荐的探索和解释入口。

**典型问题**：
```
今天有客户会，穿哪套更稳？
这件衬衫还能怎么搭？
我下周出差 3 天，帮我打包。
我的衣柜缺什么基础款？
最近我是不是总穿同一种颜色？
```

**架构**：
```
POST /v1/ask   { question: str, context?: { date, scene, time_left_minutes } }
→ LLM（通义千问 / GPT-4o-mini）
→ 系统 prompt 注入用户衣橱数据（轻量化结构化数据，不传图片）
→ 流式或非流式回答
→ 引用具体单品 item_id（可跳转衣物详情）
```

**成本控制**：
- 每用户每日限额：5 次 Ask（订阅用户）/ 1 次（免费用户）
- LLM 输入仅传结构化数据（category/color/status/location），不传图片
- 超出限额返回 429 + 提示升级

**新增接口**：
```
POST /v1/ask                    发起问答（支持 streaming: bool 参数）
GET  /v1/ask/history            问答历史
```

**新增表** `ask_sessions`：
```sql
CREATE TABLE ask_sessions (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      uuid NOT NULL REFERENCES users(id),
  question     text NOT NULL,
  answer       text,
  item_refs    jsonb,           -- 引用的 wardrobe_item_id 列表
  context      jsonb,
  tokens_used  int,
  created_at   timestamptz DEFAULT now()
);
```

**新增文件**：
- `app/application/ports/llm.py`：`LLMPort` Protocol（含 streaming 支持）
- `app/infrastructure/llm/qwen_llm.py`：通义千问实现
- `app/infrastructure/llm/openai_llm.py`：GPT-4o-mini fallback
- `app/application/ask/answer_question.py`：用例，负责组装上下文
- `app/presentation/api/v1/routers/ask.py`

---

### M16 — 出差旅行 Packing List

**目标**：用户输入出行天数 + 场景，系统从真实衣橱生成可打包清单。

**新增接口**：
```
POST /v1/packing-lists          生成清单
GET  /v1/packing-lists          历史清单
GET  /v1/packing-lists/{id}     清单详情
PATCH /v1/packing-lists/{id}    用户修改（勾选/替换单品）
```

**`POST /v1/packing-lists` Request**：
```json
{
  "destination": "上海",
  "days": 3,
  "scenes": ["business", "dinner"],
  "weather_hint": "cool",
  "departure_date": "2026-05-10"
}
```

**生成逻辑**：
- 按天数 × 场景，从衣橱中按规则选取（核心逻辑，不调 LLM）
- Ask Aidrobe 作为可选解释入口：用户可追问"为什么选这件"
- 优先 `status=wearable` + `fit_status=fits_well`
- 避免最近 7 天重复穿过的组合

**新增表** `packing_lists`：
```sql
CREATE TABLE packing_lists (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         uuid NOT NULL REFERENCES users(id),
  destination     varchar(120),
  days            smallint,
  scenes          jsonb,
  departure_date  date,
  items           jsonb,   -- [{item_id, day, scene, notes}]
  status          varchar(32) DEFAULT 'draft',  -- draft / confirmed
  created_at      timestamptz DEFAULT now(),
  updated_at      timestamptz DEFAULT now()
);
```

---

### M17 — 购物前 Sense-Check

**目标**：用户粘贴商品链接或上传图片，系统评估"买了会和现有衣橱重复吗？还缺什么？值得买吗？"

**新增接口**：
```
POST /v1/shopping/sense-check   { image_url? / source_url? }
GET  /v1/shopping/history       历史 sense-check
```

**`POST` Response**：
```json
{
  "verdict": "consider",          -- buy / consider / skip
  "reason": "你已有 3 件相似颜色的上衣，但缺少正式外套",
  "similar_items": [              -- 已有的相似单品
    { "item_id": "uuid", "similarity": 0.87 }
  ],
  "wardrobe_gap": ["outerwear_formal"],
  "outfit_potential": 4           -- 能搭几套现有衣服
}
```

**实现**：
- 图片/链接 → Vision 识别候选单品属性
- 与现有衣橱做 embedding 相似度匹配（依赖 V3 M8）
- 按品类/颜色/场景评估"是否重复"和"是否填补缺口"
- 不依赖 LLM（规则 + embedding 足够），可选 Ask Aidrobe 解释

---

### M18 — 全渠道建库：Receipt / 订单截图 / 购物邮件

**目标**：让用户不需要重新拍照，从已有购物记录中自动建库，大幅降低建库摩擦。

**支持来源**：
```
input_type: receipt_screenshot | order_screenshot | shopping_email
```

**处理流程**：
```
用户上传截图 or 转发邮件
→ OCR 提取文字（商品名称、颜色、尺码、价格、购买日期）
→ 匹配商品链接 or 搜索补图（Bing Image Search / 平台 API）
→ Vision 识别主图
→ 候选 item 进入确认页（用户确认）
→ 同时填入 purchase_price、size_label（自动）
```

**新增 `input_type` 枚举值**：
```python
class InputType(StrEnum):
    IMAGE = "image"
    LINK = "link"
    SCREENSHOT = "screenshot"
    IMAGE_BATCH = "image_batch"
    RECEIPT_SCREENSHOT = "receipt_screenshot"    # 新增
    ORDER_SCREENSHOT = "order_screenshot"        # 新增
    SHOPPING_EMAIL = "shopping_email"            # 新增
```

**OCR 工具选型**：
- `pytesseract`（本地，免费，中文准确率中等）
- 通义千问 OCR（API，中文准确率高，有成本）
- 建议：优先通义千问，失败降级 pytesseract

**新增文件**：
- `app/infrastructure/importers/adapters/receipt.py`
- `app/infrastructure/importers/adapters/order_screenshot.py`
- `app/application/ports/ocr.py`：`OCRPort` Protocol

---

### M19 — PDF / 杂志多文件风格摄取

**目标**：用户上传 PDF 杂志或多页截图，系统批量提取风格信号写入 style profile。

**支持来源**：
```
source_type: pdf_magazine | image_batch（已有）
```

**PDF 处理流程**：
```
用户上传 PDF
→ OSS 直传（走现有 presign 流程）
→ PDF → 每页截图（pdf2image）
→ 过滤空白页 / 广告页（blur + content 检测）
→ 保留穿搭图（outfit detection 置信度 > 0.5）
→ 每张穿搭图 → Vision 分析风格特征
→ 聚合结果 → 写入 user_style_profiles（多条记录）
→ learn_status=learned，推送通知
```

**`POST /v1/style/sources` 支持 PDF**：
```json
{
  "source_url": null,
  "file_url": "https://oss.../magazine.pdf",
  "input_type": "pdf_magazine",
  "title": "ELLE 2026 春夏"
}
```

**Response**（异步）：
```json
{
  "source_id": "uuid",
  "status": "queued",
  "estimated_pages": 48
}
```

**新增依赖**：
```toml
pdf2image = ">=1.17"
poppler-utils              # pdf2image 依赖，Dockerfile 中 apt install
```

**新增文件**：
- `app/infrastructure/importers/adapters/pdf_magazine.py`
- `app/infrastructure/workers/pdf_worker.py`：PDF → 截图 → 批量入 style_learn_worker

---

### M20 — 虚拟试穿（可选，会员功能）

**目标**：用户主动触发，查看某套推荐的上身效果参考图。

**重要约束**（Doc 18 §6.2）：
- 不作为推荐主链路默认展示
- 不默认为三套全部生成
- 用户必须主动点击"看看上身效果"
- 生成图必须标注"AI 效果图仅供搭配氛围参考，版型/材质以真实衣物为准"
- 需用户主动上传或授权使用全身/半身照
- 结果缓存，不重复生成

**新增接口**：
```
POST /v1/recommendations/outfits/{outfit_id}/try-on    触发生成（需会员）
GET  /v1/outfit-visual-assets/{asset_id}               查询状态（复用现有）
```

**`POST` Request**：
```json
{
  "body_image_url": "https://oss.../user_body.jpg",
  "consent": true
}
```

**生成方案**：
- 主方案：IDM-VTON / CatVTON（开源虚拟试穿模型，本地推理）
- 备选：第三方 API（klingai / 剪映虚拟试穿 API）

**视觉资产类型**：`visual_type=virtual_try_on`（现有 `outfit_visual_assets` 表已支持）

**成本控制**：
- 每用户每月限额：10 次（订阅用户）
- 使用 GPU 服务器单独部署推理，与主 API 分离
- 生成结果按 `outfit_id + body_image_hash + garment_hash` 缓存

---

### M21 — 半年度 / 年度衣橱报告

**目标**：每半年生成一份个性化衣橱报告，类似 Spotify Wrapped，增强情感连接和传播。

**报告内容**：
```
本期穿搭总次数
最常穿单品 Top 3（图片 + 穿着次数）
最常组合的搭配
常用色彩画像
风格变化趋势（本期 vs 上期）
最高 cost-per-wear 单品
最长闲置单品
"你的衣橱风格关键词"
推荐采用率
```

**生成方式**：
- 定时任务（每年 5 月、11 月）聚合 SQL 查询
- 生成静态 HTML 或图片卡片（可分享）
- Push 通知触达："你的春季衣橱报告来了"

**新增接口**：
```
GET /v1/insights/reports/{period}    period: "2026-H1" / "2026-annual"
```

---

## 数据库迁移计划

| 迁移编号 | 文件名 | 内容 |
|---------|-------|------|
| 0018 | `0018_ask_sessions` | `ask_sessions` 表 |
| 0019 | `0019_packing_lists` | `packing_lists` 表 |
| 0020 | `0020_shopping_sense_check` | sense-check 历史表（可选，直接返回不存储也行） |
| 0021 | `0021_import_type_extensions` | `InputType` 枚举扩展（无 schema 变更，只加枚举值） |

---

## 商业化模块配合

| 功能 | 免费用户 | 订阅用户 |
|------|---------|---------|
| 每日推荐 | 1 套 | 3 套 |
| Ask Aidrobe | 1 次/天 | 5 次/天 |
| 虚拟试穿 | 0 次 | 10 次/月 |
| Packing List | 1 个 | 无限 |
| Sense-Check | 2 次/天 | 无限 |
| 衣橱报告 | 摘要 | 完整版 |
| 多来源导入 | 拍照/相册 | 全部来源 |
| PDF 风格摄取 | 1 次/月 | 无限 |

---

## 执行顺序

```
Week 1–3    M15（Ask Aidrobe，LLM 基础设施）
Week 2–4    M16（Packing List，规则驱动，不依赖 LLM）
Week 3–5    M17（购物前 sense-check，依赖 V3 embedding）
Week 4–6    M18（Receipt/订单截图，OCR 流水线）
Week 5–7    M19（PDF/杂志，pdf2image + 批量 worker）
Week 7–9    M20（虚拟试穿，GPU 推理服务部署）
Week 9–10   M21（衣橱报告，聚合 + 生成）
Week 10–12  商业化订阅墙 + 限额控制 + iOS 联调
```

---

## 不进入 V4 的功能

| 功能 | 说明 |
|------|------|
| 社交衣橱 / 朋友衣橱分享 | 竞品证明社区功能未必是留存核心；Aidrobe 优先深耕个人 |
| 专业造型师 Lookbook | 商业化服务，需人工介入，独立运营 |
| 联盟导购 | 必须明确区分推荐和购物建议，避免破坏信任 |
| 跨平台（Android/Web）| iOS 先跑通，规模化后再考虑 |

---

## 架构演进注意事项

### LLM 成本管控
- Ask Aidrobe 系统 prompt 必须携带轻量结构化数据（category/color/status），不传图片
- 使用 prompt caching（通义千问/Anthropic 都支持）降低 token 成本
- LLM 调用全部走异步 worker，不阻塞 HTTP 响应

### 虚拟试穿 GPU 服务
- 独立部署（不在主 API 容器），避免 GPU 内存影响主服务稳定性
- 通过内部 HTTP API 调用（`app/infrastructure/tryon/tryon_service.py`）
- 容器化：`docker-compose.yml` 加 `tryon-worker` 服务（可选启动，`ENABLE_TRYON=true`）

### 数据隐私
- 用户全身照 / OOTD 自拍：单独 OSS bucket，不与其他图片混存
- 虚拟试穿所用身体图片：用户可随时删除（`DELETE /v1/user/body-images/{id}`）
- Ask Aidrobe 问答历史：用户可导出和删除

---

## 验收指标

| 指标 | 目标 |
|------|------|
| Ask Aidrobe 日活使用率（有发起问答的用户占 DAU）| > 25% |
| Packing List 生成次数（出差季）| > 30% 有效用户生成过一次 |
| Sense-Check 使用率 | > 15% 月活用户 |
| PDF 学习完成率（queued → learned）| > 90% |
| 虚拟试穿激活率（开启后点击过 try-on）| > 20% 订阅用户 |
| 年度报告分享率 | > 10%（可衡量传播效果）|
| 订阅转化率（V4 上线后 30 天）| > 5% MAU |
