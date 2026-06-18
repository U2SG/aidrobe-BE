# V4 UI 落地 — 后端最小改动清单

为支撑 V4 原型（见本目录 `ui.pdf` / `*.html` 及 01–07 plan），前端对照后端 schema/路由发现以下点：UI 画了数据、但当前契约提供不了。以下仅列**需要动后端**的；纯前端处理的见末尾。

> 约定：沿用现有分层（router/schema/store）、snake_case、`ResponseModel` 信封（style 路由已用）。改动都很小。

---

## 1. 读取当前风格标签 `GET /v1/style/tags`（建议必做）

**缺口**：只有 `PATCH /v1/style/tags`（`update_style_tags` → `repo.upsert_style_tags`）写入，**没有读取端点**，settings 也不暴露。原型「风格灵感」页顶部的「你的风格」标签 chips（极简/米咖/法式）无数据来源 → 不补则该 UI 只能写、无法回显。

**建议改动**
- 新增 `GET /v1/style/tags` → `ResponseModel[StyleTagsResponse]`。
- `class StyleTagsResponse(BaseModel): tags: list[str]`（`app/.../schemas/style.py`）。
- store 加 `get_style_tags(user_id) -> list[str]`（读 `upsert_style_tags` 写入的同一处）。
- 前端：`style.ts` 加 `getStyleTags()`，`useQuery(['styleTags'])`。

**影响 UI**：05 风格灵感（标签回显 + 编辑）。**工作量**：极小（一个读端点）。

---

## 2. 报告环比所需的上一周期穿着数（建议做，否则砍数字）

**缺口**：`WardrobeReportResponse` 有 `total_wears` 和 `style_trend.{current,previous}`（标签计数），但**没有上一周期的 total_wears**。原型报告页「比上半年多 18%」算不出来。

**建议改动（二选一）**
- A：`WardrobeReportResponse` 加 `prev_total_wears: int`（聚合层已算上一周期用于 style_trend，顺带返回 total 成本低）。前端自算百分比。
- B：直接返回 `wears_delta_pct: float | None`（无上一周期数据时 null）。

**影响 UI**：07 衣橱报告的环比数字。**不做的话**：前端去掉「+18%」这句。**工作量**：极小。

---

## 3. 打包清单单件「已打包」状态（看产品决策）

**缺口**：`PackingItem = {item_id, day, scene, notes}`，无单件级 `packed/checked`；`status` 是清单级。原型 PackingListDetail 的逐项勾选 +「已打包 4/9」进度无处持久化。

**选项**
- A（不动后端）：v1 勾选做**纯前端本地态**（不跨设备/不持久）。最省。
- B（动后端）：`PackingItem` 加 `packed: bool = False`（schema + DB 列 + update 路径）。`update_packing_list` 已接收整份 `items[]`，加字段即可流转。跨设备保留勾选。

**影响 UI**：02 打包清单。**建议**：v1 先走 A，确有需求再上 B。**工作量**：B 为小（含一条 migration）。

---

## 不需要后端改（前端处理，列此避免误派）

- **配色分布百分比**：`color_palette` / report `color_palette` 返回 `{颜色: 次数}`，原型的 % 由**前端按总数换算**。
- **id-only 引用**：sense-check `similar_items`、ask `item_refs`、packing items 均只给 `item_id`；缩略图+名称前端用 `getWardrobeItem` 批量取（`useQueries`）。当前契约够用。

---

## 已知可用性（非契约缺口，记录）
- `POST /v1/ask`：本环境后端 LLM 未接 → 503/5001。原型已画 503 态；联调前需后端把 ask 的 LLM 接好。
- `POST /v1/shopping/sense-check`：无效 URL 当前返**裸 500**，建议改为友好错误（422/带 message），前端也会兜底提示。

---

## 汇总
| # | 改动 | 必要性 | 后端工作量 |
|---|---|---|---|
| 1 | `GET /v1/style/tags` | 建议必做（否则标签无法回显） | 极小 |
| 2 | report 加 `prev_total_wears` 或 `wears_delta_pct` | 建议做（否则砍环比数字） | 极小 |
| 3 | `PackingItem.packed` 字段 | 产品决策（v1 可前端本地绕过） | 小（含 migration） |
| — | sense-check 500→友好错误 | 体验优化 | 极小 |
