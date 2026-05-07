# Backend 接口缺口（前端联调发现）

记录前端在按 `docs/openapi.json` 全量接入 MVP 闭环屏幕（Today / OutfitDetail / FeedbackSheet / WardrobeList / ItemDetail）后，**当前只能用前端静态数据兜底**的字段与端点。按优先级降序排列。

> 现状已接入：`GET /v1/recommendations/today`、`GET /v1/recommendations/outfits/{outfit_id}`、`POST .../wear`、`POST .../feedback`、`POST /v1/fit-feedback`、`GET/PATCH /v1/wardrobe-items[/:id]`、`PATCH /:id/fit`、`GET /v1/import-tasks/...`、`POST /v1/import-tasks/.../confirm`、`GET /v1/insights/{summary,top-items,idle-items,color-palette}`、`GET /v1/recommendations/outfit-history`、`GET/PATCH /v1/user/settings`。

---

## P0 · 阻塞 OutfitDetail 体验

### 1. `OutfitResponse` 缺评分细分

**屏幕**：`OutfitDetailScreen` 顶部四宫格（美感 / 执行性 / 合身度 / 准备时间）。

**当前情况**：服务端 `OutfitResponse` 只有 `outfit_id / type / title / reason / time_sensitivity / items`，无评分字段。前端写死 `SCORES = [88, 74, 90, 12']`。

**建议字段**（加在 `OutfitResponse` 上）：

```json
"scores": {
  "aesthetic": 88,           // 0-100，美感/搭配协调
  "feasibility": 74,         // 0-100，可执行性（衣物在手、状态可穿等综合）
  "fit": 90,                 // 0-100，合身度（基于历史 fit-feedback）
  "prep_minutes": 12         // int，预计准备时间分钟
}
```

> 与 `time_sensitivity` 枚举正交：枚举给出"标签语义"，`prep_minutes` 给出具体值。前端用 `prep_minutes` 渲染时间预算条。

### 2. `OutfitResponse` 缺障碍/替换建议

**屏幕**：`OutfitDetailScreen` 顶部黄色 banner（"黑色西裤在脏衣篮 · 换一件 30 秒解决"）。

**当前情况**：前端用 `item.status !== 'wearable'` 简单判断显示警告，但**无法给出具体替换建议**。`OutfitResponse` 也无 `obstacles` 字段。

**建议字段**：

```json
"obstacles": [
  {
    "kind": "laundry" | "missing" | "weather_mismatch" | "seasonal",
    "blocked_item_id": "uuid",
    "title": "黑色西裤在脏衣篮",
    "description": "你昨天穿过、还没洗",
    "suggested_replacement_item_id": "uuid",
    "auto_resolvable": true
  }
]
```

### 3. `OutfitResponse` 缺取衣指引

**屏幕**：`OutfitDetailScreen` 底部"取衣与准备"卡片。

**当前情况**：前端临时用 `items[].location` 拼接 `主衣柜 取白衬衫`。但缺顺序、熨烫提示、用时分解。

**建议字段**：

```json
"prep_steps": [
  { "order": 1, "action": "主衣柜抽屉 2 取衬衫", "minutes": 1 },
  { "order": 2, "action": "挂区取西装", "minutes": 1 }
],
"prep_notes": ["无需熨烫", "鞋已在玄关"]
```

### 4. `OutfitResponse` 缺 hero 视觉布局/主图

**屏幕**：`OutfitDetailScreen` / `TodayScreen` 中央插画。

**当前情况**：前端用静态坐标 `HERO_LAYOUT[i]` 摆放每件 item 的图片或 SVG。

**建议方案 A**（轻量）：服务端为每件 item 加一个 `display_role: "top" | "outer" | "bottom" | "shoes" | "accessory"`，前端按角色定位。

**建议方案 B**（重量级）：服务端预渲染整套 OOTD 拼图，返回 `outfit_image_url`。优先级低。

---

## P1 · 影响 ItemDetail / 历史展示

### 5. 单件按天的穿着热力图无法重建

**屏幕**：`ItemDetailScreen` 近 60 天热力图（4 级强度 × 60 格）。

**当前情况**：
- `WardrobeItemResponse.wear_count` 给的是总数，无时间序列。
- `GET /v1/recommendations/outfit-history` 列出按天的 outfit，但**不带 item-level 展开**，且没有 `item_id` 过滤参数。
- 用现有接口要 N+1 调用：先列 60 天的 outfit，再对每个 outfit 查 detail 看 items 是否包含目标 item — 不可行。

**建议任一方案**：

**A. 给 `outfit-history` 加 `item_id` 过滤**：

```
GET /v1/recommendations/outfit-history?item_id=<uuid>&start_date=...&end_date=...
```

**B. 新增按物品查穿着记录端点**：

```
GET /v1/wardrobe-items/{item_id}/wear-history?days=60
→ {
  "entries": [
    { "worn_date": "2026-04-22", "outfit_id": "uuid", "scene": "commute" }
  ]
}
```

前端会把 entries 聚合成 60 格的强度热力图（同日多次穿 = 高强度）。

### 6. `WardrobeItemResponse.last_worn` 缺失

**屏幕**：`ItemDetailScreen` "最近穿着 · 3 天前"。

**当前情况**：`IdleItemResponse` 上有 `last_worn`，但 `WardrobeItemResponse` 没有。前端只能显示 `wear_count`。

**建议**：在 `WardrobeItemResponse` 加 `last_worn: string | null` (date)。

### 7. `WardrobeItemResponse.purchase_date` 缺失

**屏幕**：`ItemDetailScreen` 信息行"购于 2025-09"。

**当前情况**：有 `purchase_price` 但无 `purchase_date`。前端已移除该静态文案。

**建议**（可选）：在 `WardrobeItemResponse` 加 `purchase_date: string | null` (date)。

---

## P2 · Today / 推荐策略增强

### 8. 单次今日推荐没有"换一套"语义

**屏幕**：`TodayScreen` 底部"换一套"按钮。

**当前情况**：前端调用 `query.refetch()` 触发 `GET /v1/recommendations/today`，但服务端是否每次返回不同结果未知（缓存策略不透明）。

**建议**：要么文档化今日推荐的稳定性，要么新增：

```
POST /v1/recommendations/today/refresh
→ TodayRecommendationResponse
```

强制重新生成。

### 9. `OutfitResponse` 缺 `prep_minutes`

见 §1 — `prep_minutes` 应作为评分对象的一部分或独立字段。前端的 TimeBudgetBar 只能用静态 `PRESETS[strategy].prepMinutes`。

### 10. 推荐没有天气/场景上下文回显

**屏幕**：`TodayScreen` "多云 18° · 通勤"。

**当前情况**：前端写死。`StyleContextResponse` 只回 `template_id / active_tags / confidence`。

**建议**：`TodayRecommendationResponse` 加：

```json
"context": {
  "weather": { "condition": "cloudy", "temp_c": 18, "feels_like_c": 16 },
  "scene": "commute"
}
```

---

## P3 · Feedback 体验增强（非阻塞）

### 11. Feedback 没有批量提交

**屏幕**：`FeedbackSheetScreen`。

**当前情况**：前端必须并发调用 1 次 outfit feedback + N 次 fit-feedback。

**建议**（可选，节省请求数）：

```
POST /v1/recommendations/outfits/{outfit_id}/feedback/bundle
{
  "overall": { "feedback_type": "like", "reason_tags": [...] },
  "items": [{ "item_id": "...", "feedback_type": "tight", "scene": "commute" }]
}
```

### 12. Overall feedback 只支持 like/dislike

**屏幕**：`FeedbackSheetScreen` 4 档（很喜欢 / 不错 / 一般 / 不喜欢）。

**当前情况**：前端把 `love|good → like`、`meh|bad → dislike`，丢失了细粒度信号。

**建议**：把 `feedback_type` pattern 从 `^(like|dislike)$` 扩为：

```
^(love|like|neutral|dislike)$
```

或加 `score: int 1..5`。

### 13. 没有 reason_tags 候选目录

**屏幕**：`FeedbackSheetScreen` 底部 6 个标签（"被夸利落"等）。

**当前情况**：服务端接受任意字符串数组，前端 hardcode 候选。不同用户/不同场景看不到自定义。

**建议**（可选）：

```
GET /v1/recommendations/feedback-tags?scene=commute
→ { "tags": [{ "key": "complimented", "label": "被夸利落" }, ...] }
```

---

## P4 · 入参收紧

### 14. `ConfirmImportRequest` 缺 wearable 状态/颜色辅助等

确认时只能设置 `title / category / color_main / location / size_label / fit_status`。前端 `ImportConfirmScreen` 没有暴露 sub_category / material / season_tags 编辑入口 — 当前版本可以接受，因为这些是非必填，先在 ItemDetail 编辑。**无需修改。**

### 15. `UpdateWardrobeItemRequest` 缺 `status` 校验

`status` 当前是 free-form string。前端 ItemDetailScreen 的 Switch 只切 `wearable` ↔ `unavailable`，但实际上还有 `dirty` / `lost` / `donated` 等业务状态时无法清晰区分。

**建议**：明确枚举 `status: "wearable" | "unavailable" | "dirty" | "lost" | "donated"`。

---

## 已经匹配良好的部分（无需改动）

- `WardrobeItemResponse` 字段集合完整（fit / size / season_tags 等）
- `UpdateFitRequest`、`FitFeedbackRequest`、`FitFeedbackType`、`FitScene` 枚举语义清楚
- Import 流程（presign → import-task → poll → confirm）三段一致
- Insights summary / top-items / idle-items / color-palette 已能覆盖衣橱列表的洞察 banner

---

## 联系
前端联调环境：`http://localhost:8000`（`apiClient` baseURL）。如需开发环境 mock，请提供 staging URL 与 X-User-Id 测试值。

> 文件位置：`D:\project\Python\aidrobe-frontend\docs\backend-gaps.md`
