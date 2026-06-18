# V4 UI 后端缺口 — 第 2 轮

接 [`08-backend-gaps.md`](./08-backend-gaps.md)。第 1 轮 4 项里 **3 项已落地、前端已对齐消费**，1 项实际未落地。

## 第 1 轮结果核对（对照后端真实代码）

| 项 | 状态 | 前端 |
|---|---|---|
| `GET /v1/style/tags`（`ResponseModel[StyleTagsResponse{tags}]`） | ✅ 已落地 | 已接：`getStyleTags()` + 风格灵感页「你的风格」标签回显/增删（PATCH） |
| `WardrobeReportResponse.prev_total_wears: int` | ✅ 已落地 | 已接：报告页 hero 卡显示「较上期多/少 X%」（prev=0 时不显示） |
| sense-check httpx 异常 → 422 | ✅ 已落地 | 已兼容：onError 对 422/500 弹「无法解析该链接」 |
| `PackingItem.packed` | ❌ **未落地** | schema 仍无该字段 → 前端打包勾选仍为**纯本地态** |

> 注：以上 3 项后端改动当前在工作区**未提交**（`git status` 显示 style/insights/sense_check 等已改未 commit），记得提交。

---

## 仍需后端的缺口（第 2 轮）

### 1. `PackingItem.packed`（carryover · 建议必做）
**现状**：`PackingItem = {item_id, day, scene, notes}`，无单件级 `packed`。`PackingListDetail` 的逐项勾选 +「已打包 X/N」进度仍是**前端本地态**，刷新/换设备即丢。
**建议**：`PackingItem` 加 `packed: bool = False`（schema + DB 列 + 一条 migration）。`update_packing_list` 已接收整份 `items[]`，加字段即可流转。落地后前端把勾选写回 `updatePackingList`。
**前端等待点**：`src/screens/discover/PackingListDetailScreen.tsx` 的 `checked: Set<number>`。

### 2. Ask LLM provider 未接（建议必做）
**现状**：`POST /v1/ask` 在当前环境返 **503/5001「Ask is not available」**（`ENABLE_ASK_AIDROBE` 已开，但 LLM client 未接）。前端对话 UI + 历史 + 503 内联兜底已就绪，但拿不到真实回答。
**建议**：接入 LLM provider，让 `/v1/ask` 返回真实 `answer` + `item_refs`。
**前端等待点**：`src/screens/discover/AskAidrobeScreen.tsx`（已就绪，无需改动）。

### 3. OOTD 海报生成链路在 dev 是否产出（需确认）
**现状**：`POST /v1/ootd-posters` 返 **202 queued**；前端轮询 `GET /v1/outfit-visual-assets/{id}` 直到 `image_url` 非空或 `error_code`。但 dev 环境是否有 worker 真正生成并回填 `image_url` 未确认——若永不产出，前端会一直轮询到用户离开。
**建议**：确认 dev 下生成 worker 可跑通；或在不可用时让 asset 返回明确的 `generation_status=failed` / `error_code`，前端已能据此显示失败态。

### 不需要后端（前端 TODO，记此免混淆）
- **sense-check / 灵感 图片输入**：前端 v1 仅支持粘贴链接（`source_url`）。图片上传路径（presign → `image_url`）是前端待补，后端 `image_url` 入参已支持，不阻塞后端。

---

## 汇总
| # | 改动 | 必要性 | 后端工作量 |
|---|---|---|---|
| 1 | `PackingItem.packed` 字段（+migration） | 建议必做（第 1 轮漏做） | 小 |
| 2 | Ask 接入 LLM provider | 建议必做（否则 Ask 无真实回答） | 中（看 provider） |
| 3 | 确认 OOTD 生成 worker 在 dev 产出 / 失败态 | 需确认 | 视现状 |
