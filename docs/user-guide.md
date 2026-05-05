# Aidrobe 后端 API 使用指南

> 面向 iOS 客户端开发者和内测调试人员。

---

## 目录

1. [通用约定](#1-通用约定)
2. [认证](#2-认证)
3. [用户设置](#3-用户设置)
4. [图片上传](#4-图片上传)
5. [导入任务（衣物识别）](#5-导入任务衣物识别)
6. [衣橱管理](#6-衣橱管理)
7. [今日推荐](#7-今日推荐)
8. [穿搭操作（穿着 / 反馈 / 历史）](#8-穿搭操作)
9. [风格灵感（Style Profile）](#9-风格灵感style-profile)
10. [健康检查](#10-健康检查)
11. [错误码速查](#11-错误码速查)
12. [完整用户流程示例](#12-完整用户流程示例)

---

## 1. 通用约定

### Base URL

```
http://<host>/
```

开发环境默认：`http://localhost:8000`

### 响应格式

所有接口统一返回 `ResponseModel` 包装：

```json
{
  "code": 0,
  "message": "ok",
  "data": { ... }
}
```

| 字段 | 说明 |
|---|---|
| `code` | `0` 表示成功，非 `0` 表示业务错误（见[错误码速查](#11-错误码速查)） |
| `message` | 成功时为 `"ok"`，失败时为可读描述 |
| `data` | 业务数据，失败时为 `null` |

### 认证方式

- **生产 / 内测**：`Authorization: Bearer <access_token>`（JWT）
- **开发调试**（`APP_ENV=dev/test`）：`X-User-Id: <uuid>` 头（无需登录，会自动创建用户）

---

## 2. 认证

### 2.1 Apple Sign In 登录

```
POST /v1/auth/login/apple
```

**Request Body**

```json
{
  "id_token": "eyJhbGci..."
}
```

**Response**

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "user_id": "550e8400-e29b-41d4-a716-446655440000",
    "access_token": "eyJhbGci...",
    "refresh_token": "dGhpcyBpc...",
    "token_type": "bearer",
    "expires_in": 3600
  }
}
```

| 字段 | 说明 |
|---|---|
| `access_token` | Bearer Token，有效期 60 分钟，用于后续所有请求 |
| `refresh_token` | 刷新令牌，有效期 30 天，存入 Keychain |
| `expires_in` | access_token 有效秒数 |

**限流**：同一 IP 每分钟最多 10 次（防暴力破解）。

---

### 2.2 刷新 Token

```
POST /v1/auth/refresh
```

**Request Body**

```json
{
  "refresh_token": "dGhpcyBpc..."
}
```

**Response**：同 2.1，返回新的 `access_token`（refresh_token 不变）。

---

## 3. 用户设置

### 3.1 获取设置

```
GET /v1/user/settings
Authorization: Bearer <token>
```

**Response**

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "departure_time": "08:00:00",
    "push_enabled": true,
    "device_token": "abc123...",
    "location": "北京"
  }
}
```

> `null` 字段在响应中会被省略（`exclude_none=true`）。

---

### 3.2 更新设置

```
PATCH /v1/user/settings
Authorization: Bearer <token>
```

**Request Body**（所有字段可选，只传需要修改的）

```json
{
  "departure_time": "08:30:00",
  "push_enabled": true,
  "device_token": "APNs设备Token字符串",
  "location": "上海"
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `departure_time` | `"HH:MM:SS"` | 出门时间，影响推荐预生成时机；传 `null` 清除 |
| `push_enabled` | `bool` | 是否开启推送通知 |
| `device_token` | `string` ≤255 | APNs 设备 Token，首次启动 / 权限变更时上报 |
| `location` | `string` ≤120 | 城市名，为未来天气功能预留，当前不影响推荐 |

---

## 4. 图片上传

衣物图片需先获取预签名上传地址，再由客户端直传 OSS，**不经过 API 服务器**。

### 4.1 获取预签名上传地址

```
POST /v1/uploads/presign
Authorization: Bearer <token>
```

**Request Body**

```json
{
  "file_name": "shirt.jpg",
  "content_type": "image/jpeg",
  "biz_type": "wardrobe_item",
  "max_content_length": 10485760
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `file_name` | string | ✓ | 原始文件名，1–255 字符 |
| `content_type` | string | ✓ | MIME 类型，如 `image/jpeg`、`image/png`、`image/heic` |
| `biz_type` | string | — | 默认 `"wardrobe_item"` |
| `max_content_length` | int | — | 最大字节数，默认 10MB，不可超过 10MB |

**Response**

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "upload_url": "https://minio.example.com/...",
    "file_url": "https://cdn.example.com/wardrobe/uuid/shirt.jpg",
    "object_key": "wardrobe/uuid/shirt.jpg"
  }
}
```

**客户端上传步骤**

1. 用 `PUT` 方法将图片 body 直传 `upload_url`，Header 包含 `Content-Type: image/jpeg`
2. 上传成功后，将 `file_url` 带入下一步创建导入任务

---

## 5. 导入任务（衣物识别）

### 5.1 创建导入任务

调用后后台立即进行 Vision API 识别，同步返回识别结果（`waiting_confirm` 状态）。

```
POST /v1/import-tasks
Authorization: Bearer <token>
```

**Request Body**

```json
{
  "input_type": "image",
  "source_type": "camera",
  "image_url": "https://cdn.example.com/wardrobe/uuid/shirt.jpg"
}
```

| 字段 | 可选值 | 说明 |
|---|---|---|
| `input_type` | `image` / `screenshot` / `shared_content` | 当前主路径用 `image` |
| `source_type` | `camera` / `album` / `share` / `screenshot` / `link` | 来源类型，用于埋点 |
| `image_url` | URL | 上一步获得的 `file_url` |
| `source_url` | URL | `input_type=link` 时传商品链接，服务端自动抓取主图 |

**Response**

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "task_id": "uuid",
    "status": "waiting_confirm",
    "candidate_item": {
      "title": "白色棉质衬衫",
      "category": "top",
      "color_main": "white",
      "confidence_score": 0.92,
      "original_image_url": "https://cdn.example.com/..."
    }
  }
}
```

| `status` | 含义 |
|---|---|
| `waiting_confirm` | 识别成功，等待用户确认 |
| `failed` | 识别失败，`error_code`/`error_message` 有详情 |

**`category` 枚举值**：`top`（上装）/ `bottom`（下装）/ `outerwear`（外套）/ `dress`（连衣裙）/ `shoes`（鞋子）

---

### 5.2 查询导入任务

```
GET /v1/import-tasks/{task_id}
Authorization: Bearer <token>
```

---

### 5.3 确认导入到衣橱

用户可在确认页修改识别结果后提交。

```
POST /v1/import-tasks/{task_id}/confirm
Authorization: Bearer <token>
```

**Request Body**

```json
{
  "title": "白色棉质衬衫",
  "category": "top",
  "color_main": "white"
}
```

**Response**

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "item_id": "uuid"
  }
}
```

> 确认后立即触发今日推荐后台重算（有 30 秒防抖，若当日推荐已有穿着/反馈记录则跳过）。

**错误**：重复确认同一任务返回 `409`，错误码 `4091`。

---

## 6. 衣橱管理

### 6.1 获取衣橱列表

```
GET /v1/wardrobe-items?category=top&limit=20&offset=0
Authorization: Bearer <token>
```

**Query 参数**

| 参数 | 类型 | 说明 |
|---|---|---|
| `category` | string | 按品类筛选，可选 |
| `limit` | int | 每页数量，1–100，默认 20 |
| `offset` | int | 偏移量，默认 0 |

**Response**

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "items": [
      {
        "item_id": "uuid",
        "title": "白色棉质衬衫",
        "category": "top",
        "color_main": "white",
        "status": "wearable",
        "cover_image_url": "https://...",
        "original_image_url": "https://...",
        "location": null
      }
    ],
    "limit": 20,
    "offset": 0,
    "total": 15
  }
}
```

---

### 6.2 获取单件衣物详情

```
GET /v1/wardrobe-items/{item_id}
Authorization: Bearer <token>
```

---

### 6.3 更新衣物信息

```
PATCH /v1/wardrobe-items/{item_id}
Authorization: Bearer <token>
```

**Request Body**（所有字段可选）

```json
{
  "title": "修改后的名称",
  "category": "top",
  "color_main": "blue",
  "status": "unavailable",
  "location": "主衣柜"
}
```

| `status` | 含义 |
|---|---|
| `wearable` | 可穿（默认，会被纳入推荐） |
| `unavailable` | 不可穿（如洗涤中，不参与推荐） |

> 更新 `status` 后立即触发今日推荐后台重算。

---

### 6.4 删除衣物

```
DELETE /v1/wardrobe-items/{item_id}
Authorization: Bearer <token>
```

**Response**

```json
{
  "code": 0,
  "message": "ok",
  "data": { "deleted": true }
}
```

> 软删除，不物理删除数据。删除后立即触发今日推荐后台重算。

---

## 7. 今日推荐

### 7.1 获取今日推荐

```
GET /v1/recommendations/today?scene=commute
Authorization: Bearer <token>
```

**Query 参数**

| 参数 | 可选值 | 说明 |
|---|---|---|
| `scene` | `commute` / `formal` / `casual` | 场景快切（可选），不传则按默认策略 |

**Response**

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "recommendation_date": "2026-05-04",
    "status": "ready",
    "guidance_text": null,
    "outfits": [
      {
        "outfit_id": "uuid",
        "type": "safe",
        "title": "最稳妥",
        "reason": "经典颜色搭配，适合日常通勤",
        "time_sensitivity": "standard",
        "items": [
          { "item_id": "uuid", "title": "白色棉质衬衫", "category": "top" },
          { "item_id": "uuid", "title": "深蓝直筒裤", "category": "bottom" },
          { "item_id": "uuid", "title": "白色小白鞋", "category": "shoes" }
        ]
      }
    ]
  }
}
```

| `status` | 含义 |
|---|---|
| `ready` | 推荐生成成功，`outfits` 有数据 |
| `insufficient_items` | 衣物不足无法生成，`guidance_text` 有提示文字 |

| `type`（穿搭类型） | 含义 |
|---|---|
| `safe` | 最稳妥 — 颜色经典，日常通勤首选 |
| `fast` | 最省时间 — 近期常穿单品快速搭配 |
| `stylish` | 更有风格感 — 有一定个性，适合宽裕时 |

| `time_sensitivity`（时间敏感度） | 场景 |
|---|---|
| `relaxed` | 宽裕（> 90 分钟），stylish 类方案 |
| `standard` | 标准（30–90 分钟），safe 类方案 |
| `rushed` | 紧张（15–30 分钟），fast 类方案 |
| `instant` | 极速（< 15 分钟），只返回 1 套 |

> **保证**：此接口永远不返回加载中状态，无推荐时返回 `insufficient_items` + 引导文案。结果基于 Redis 缓存，有效至当天 UTC 23:59:59。

---

### 7.2 获取穿搭详情

```
GET /v1/recommendations/outfits/{outfit_id}
Authorization: Bearer <token>
```

---

## 8. 穿搭操作

### 8.1 记录穿着

```
POST /v1/recommendations/outfits/{outfit_id}/wear
Authorization: Bearer <token>
```

**Request Body**（可选）

```json
{
  "worn_date": "2026-05-04"
}
```

> 不传 `worn_date` 默认为今天（UTC）。同一 outfit + 同一日期重复调用幂等，不重复记录。

---

### 8.2 提交反馈

```
POST /v1/recommendations/outfits/{outfit_id}/feedback
Authorization: Bearer <token>
```

**Request Body**

```json
{
  "feedback_type": "like",
  "reason_tags": ["comfortable", "classic"]
}
```

| 字段 | 说明 |
|---|---|
| `feedback_type` | `like` 或 `dislike` |
| `reason_tags` | 可选，原因标签数组（用于 V1.2 偏好学习，当前可不传） |

---

### 8.3 穿搭历史

```
GET /v1/recommendations/outfit-history?start_date=2026-05-01&end_date=2026-05-04&limit=20&offset=0
Authorization: Bearer <token>
```

**Response**

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "items": [
      {
        "outfit_id": "uuid",
        "worn_date": "2026-05-04",
        "outfit_type": "safe",
        "title": "最稳妥",
        "feedback_type": "like",
        "reason_tags": []
      }
    ],
    "limit": 20,
    "offset": 0
  }
}
```

---

## 9. 风格灵感（Style Profile）

用户可上传灵感图片或选择风格标签，系统基于此调整推荐引擎的 `aesthetic_score` 权重，使推荐更符合个人审美。上传后自动触发今日推荐缓存失效。

### 9.1 上传灵感图

> 先调 [4.1 获取预签名上传地址](#41-获取预签名上传地址) 将图片上传到 OSS，再将 `file_url` 提交此接口。

```
POST /v1/style/inspirations
Authorization: Bearer <token>
```

**Request Body**

```json
{
  "image_url": "https://cdn.example.com/wardrobe/uuid/inspo.jpg"
}
```

**Response** `201`

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "id": "uuid",
    "image_url": "https://cdn.example.com/...",
    "source_type": "inspiration_image",
    "created_at": "2026-05-04T10:00:00Z"
  }
}
```

---

### 9.2 获取灵感图列表

```
GET /v1/style/inspirations?limit=20&offset=0
Authorization: Bearer <token>
```

---

### 9.3 删除灵感图

```
DELETE /v1/style/inspirations/{inspiration_id}
Authorization: Bearer <token>
```

---

### 9.4 更新风格标签（冷启动快速设置）

新用户无历史数据时，可直接选择风格标签作为初始偏好信号。

```
PATCH /v1/style/tags
Authorization: Bearer <token>
```

**Request Body**

```json
{
  "tags": ["minimal", "classic", "casual"]
}
```

| 约束 | 说明 |
|---|---|
| 列表长度 | 1–20 个标签 |
| 单个标签长度 | 1–50 字符 |

> 每次调用覆盖全量标签（非追加）。

---

## 10. 健康检查

```
GET /healthz
```

**Response**（无需认证）

```json
{ "status": "ok" }
```

---

## 11. 错误码速查

| HTTP 状态码 | 业务 `code` | 含义 | 常见原因 |
|---|---|---|---|
| 401 | `4002` | 未认证 | 缺少 / 过期 / 无效 Token |
| 403 | `4003` | 无权限 | 访问他人资源 |
| 404 | `4041` | 资源不存在 | item_id / task_id / outfit_id 错误或属于他人 |
| 409 | `4091` | 状态冲突 | 重复确认导入任务；推荐重算限流 |
| 422 | `4001` | 请求参数错误 | 字段格式不符、枚举值不合法 |
| 429 | `4091` | 限流 | Vision API 超 60 次/小时；通用 API 超 100 次/分钟 |
| 500 | `5001` | 服务器内部错误 | 联系后端排查日志 |

---

## 12. 完整用户流程示例

以下是一个新用户从登录到获取推荐的完整流程：

```
1. 登录
   POST /v1/auth/login/apple  →  获得 access_token + refresh_token

2. 设置出门时间 + 上报设备 Token
   PATCH /v1/user/settings
   { "departure_time": "08:30:00", "device_token": "APNs Token" }

3. 上传衣物图片
   POST /v1/uploads/presign  →  获得 upload_url + file_url
   PUT <upload_url>  （客户端直传图片二进制）

4. 创建识别任务
   POST /v1/import-tasks
   { "input_type": "image", "source_type": "camera", "image_url": "<file_url>" }
   →  返回 task_id + 识别结果（category / color_main / title）

5. 确认导入衣橱（可修改识别结果）
   POST /v1/import-tasks/{task_id}/confirm
   { "title": "白色衬衫", "category": "top", "color_main": "white" }
   →  返回 item_id
   （重复步骤 3–5 直到衣橱有足够单品）

6. 获取今日推荐
   GET /v1/recommendations/today
   →  返回最多 3 套穿搭方案

7. 查看穿搭详情
   GET /v1/recommendations/outfits/{outfit_id}

8. 记录穿着 + 提交反馈
   POST /v1/recommendations/outfits/{outfit_id}/wear
   POST /v1/recommendations/outfits/{outfit_id}/feedback
   { "feedback_type": "like" }

9. （可选）上传风格灵感图
   POST /v1/uploads/presign  →  上传图片
   POST /v1/style/inspirations  { "image_url": "..." }
   →  下次推荐自动融入审美偏好
```

---

## 附录：推荐触发机制

| 触发条件 | 方式 | 防抖 |
|---|---|---|
| 确认导入衣物 | `BackgroundTasks` 异步执行 | 30 秒内已更新则跳过 |
| 更新 / 删除衣物 | `BackgroundTasks` 异步执行 | 同上 |
| 出门前 90 分钟 | APScheduler 每 5 分钟轮询 | 当日已有穿着/反馈则跳过 |
| 每天凌晨 03:00 | APScheduler cron | 仅针对未设出门时间的用户 |
| 添加灵感图 / 标签 | Redis 缓存失效（非重新生成） | — |
