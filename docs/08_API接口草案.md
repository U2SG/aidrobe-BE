# API 接口草案（V1）

## 1. 文档目标

定义 V1 阶段前后端需要的核心接口，便于 iOS、后端与算法服务并行开发。本文为草案，后续可补为 OpenAPI 规范。

---

## 2. 设计原则

1. App 与图片解析、推荐计算解耦
2. 长耗时处理统一走异步任务
3. 导入链路与推荐链路都尽量可重试
4. 推荐结果尽量提前生成，避免晨间等待
5. 所有可被用户纠正的 AI 字段，都要支持 patch 更新

---

## 3. 通用约定

### 3.1 鉴权
- `Authorization: Bearer <token>`

### 3.2 响应结构建议

```json
{
  "code": 0,
  "message": "ok",
  "data": {}
}
```

### 3.3 错误码建议
- `0`：成功
- `4001`：参数错误
- `4002`：未登录
- `4003`：无权限
- `4041`：资源不存在
- `4091`：状态冲突
- `5001`：服务内部错误
- `5002`：异步任务失败

---

## 4. 用户与设置

### 4.1 获取用户设置
`GET /v1/user/settings`

#### 返回字段
```json
{
  "departure_time": "08:15",
  "push_enabled": true,
  "timezone": "Asia/Shanghai"
}
```

---

### 4.2 更新用户设置
`PATCH /v1/user/settings`

#### 请求体
```json
{
  "departure_time": "08:30",
  "push_enabled": true
}
```

#### 说明
- 支持局部更新
- `departure_time` 为本地日常时间，不含日期

---

## 5. 导入链路

### 5.1 获取上传凭证
`POST /v1/uploads/presign`

#### 请求体
```json
{
  "file_name": "top_001.jpg",
  "content_type": "image/jpeg",
  "biz_type": "wardrobe_item"
}
```

#### 返回字段
```json
{
  "upload_url": "https://...",
  "file_url": "https://cdn.../top_001.jpg",
  "object_key": "wardrobe/2026/04/top_001.jpg"
}
```

#### 用途
- iOS 端直传对象存储，减轻应用服务器压力

---

### 5.2 创建导入任务
`POST /v1/import-tasks`

#### 请求体示例 1：图片导入
```json
{
  "input_type": "image",
  "source_type": "camera",
  "image_url": "https://cdn.../top_001.jpg"
}
```

#### 请求体示例 2：链接导入
```json
{
  "input_type": "url",
  "source_type": "link",
  "source_url": "https://item.taobao.com/..."
}
```

#### 请求体示例 3：分享导入
```json
{
  "input_type": "shared_content",
  "source_type": "share",
  "shared_payload": {
    "url": "https://...",
    "text": "法式通勤衬衫",
    "image_urls": ["https://..."]
  }
}
```

#### 返回
```json
{
  "task_id": "it_123",
  "status": "pending"
}
```

---

### 5.3 查询导入任务状态
`GET /v1/import-tasks/{task_id}`

#### 返回字段
```json
{
  "task_id": "it_123",
  "status": "waiting_confirm",
  "candidate_item": {
    "title": "白色通勤衬衫",
    "cover_image_url": "https://cdn...",
    "category": "top",
    "sub_category": "shirt",
    "color_main": "white",
    "season_tags": ["spring", "autumn"],
    "style_tags": ["commuting", "minimal"],
    "confidence_score": 0.82,
    "source_platform": "taobao"
  }
}
```

#### 状态枚举
- `pending`
- `parsing`
- `waiting_confirm`
- `completed`
- `failed`

---

### 5.4 确认导入并生成衣物
`POST /v1/import-tasks/{task_id}/confirm`

#### 请求体
```json
{
  "title": "白衬衫",
  "category": "top",
  "sub_category": "shirt",
  "color_main": "white",
  "status": "wearable",
  "location": "main_closet"
}
```

#### 返回
```json
{
  "item_id": "wi_001"
}
```

---

### 5.5 重试导入任务
`POST /v1/import-tasks/{task_id}/retry`

#### 用途
- 链接解析失败后人工重试
- 模型识别失败后再次触发

---

## 6. 衣橱模块

### 6.1 获取衣橱列表
`GET /v1/wardrobe-items`

#### Query 参数
- `category`
- `status`
- `location`
- `page`
- `page_size`

#### 返回字段
```json
{
  "items": [
    {
      "item_id": "wi_001",
      "title": "白衬衫",
      "cover_image_url": "https://cdn...",
      "category": "top",
      "sub_category": "shirt",
      "color_main": "white",
      "status": "wearable",
      "location": "main_closet"
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 56
}
```

---

### 6.2 获取衣物详情
`GET /v1/wardrobe-items/{item_id}`

#### 返回字段
```json
{
  "item_id": "wi_001",
  "title": "白衬衫",
  "cover_image_url": "https://cdn...",
  "original_image_url": "https://origin...",
  "category": "top",
  "sub_category": "shirt",
  "color_main": "white",
  "season_tags": ["spring", "autumn"],
  "style_tags": ["commuting"],
  "status": "wearable",
  "location": "main_closet",
  "source_type": "camera",
  "source_platform": "unknown"
}
```

---

### 6.3 更新衣物
`PATCH /v1/wardrobe-items/{item_id}`

#### 请求体
```json
{
  "title": "白色通勤衬衫",
  "status": "laundry",
  "location": "drawer"
}
```

#### 说明
- 允许局部更新
- AI 字段与用户字段共用 patch

---

### 6.4 删除衣物
`DELETE /v1/wardrobe-items/{item_id}`

#### 返回
```json
{
  "deleted": true
}
```

---

## 7. 推荐模块

### 7.1 获取今日推荐
`GET /v1/recommendations/today`

#### 返回字段
```json
{
  "recommendation_date": "2026-04-21",
  "status": "ready",
  "outfits": [
    {
      "outfit_id": "of_001",
      "type": "safe",
      "title": "最稳妥",
      "reason": "适合普通通勤场景，且主衣柜可直接拿取",
      "items": [
        {
          "item_id": "wi_001",
          "title": "白衬衫",
          "cover_image_url": "https://cdn...",
          "category": "top"
        },
        {
          "item_id": "wi_010",
          "title": "黑色西裤",
          "cover_image_url": "https://cdn...",
          "category": "bottom"
        }
      ]
    }
  ]
}
```

#### 状态枚举
- `ready`
- `generating`
- `insufficient_items`
- `failed`

---

### 7.2 获取推荐详情
`GET /v1/recommendations/outfits/{outfit_id}`

#### 返回字段
```json
{
  "outfit_id": "of_001",
  "type": "safe",
  "title": "最稳妥",
  "reason": "适合普通通勤场景，且不需要熨烫",
  "items": [
    {
      "item_id": "wi_001",
      "title": "白衬衫",
      "location": "main_closet"
    },
    {
      "item_id": "wi_010",
      "title": "黑色西裤",
      "location": "main_closet"
    }
  ]
}
```

---

### 7.3 选择“今天穿这套”
`POST /v1/recommendations/outfits/{outfit_id}/wear`

#### 请求体
```json
{
  "selected_at": "2026-04-21T07:05:00+08:00"
}
```

#### 返回
```json
{
  "success": true
}
```

---

### 7.4 提交推荐反馈
`POST /v1/recommendations/outfits/{outfit_id}/feedback`

#### 请求体
```json
{
  "feedback_type": "dislike",
  "reason_tags": ["too_formal", "not_me"]
}
```

#### 反馈类型
- `like`
- `dislike`
- `worn`
- `skipped`

#### 原因标签建议
- `too_formal`
- `too_complex`
- `not_me`
- `not_for_today`

---

## 8. 历史与行为记录

### 8.1 获取穿搭历史
`GET /v1/outfit-history`

#### Query 参数
- `start_date`
- `end_date`
- `page`
- `page_size`

#### 返回字段
```json
{
  "records": [
    {
      "date": "2026-04-20",
      "outfit_id": "of_002",
      "selected": true,
      "feedback_type": "like"
    }
  ],
  "total": 12
}
```

---

## 9. 推送与后台任务

### 9.1 触发当日推荐重算（内部 / 管理接口）
`POST /internal/v1/recommendations/generate`

#### 请求体
```json
{
  "user_id": "u_001",
  "target_date": "2026-04-22"
}
```

#### 说明
- 由定时任务或事件系统调用
- 不直接暴露给客户端

---

### 9.2 导入任务消费完成回调（算法服务 -> 应用服务）
`POST /internal/v1/import-tasks/{task_id}/callback`

#### 请求体
```json
{
  "status": "waiting_confirm",
  "candidate_item": {
    "title": "灰色针织上衣",
    "category": "top",
    "sub_category": "knitwear",
    "color_main": "gray",
    "confidence_score": 0.78
  }
}
```

---

## 10. 接口实现优先级

### P0
- 获取 / 更新设置
- 获取上传凭证
- 创建 / 查询 / 确认导入任务
- 获取衣橱列表 / 详情 / 更新 / 删除
- 获取今日推荐 / 详情
- 选择穿搭
- 提交反馈

### P1
- 导入任务重试
- 历史记录
- 内部推荐生成接口
- 算法回调接口

### P2
- 搜索
- 批量编辑
- 收藏推荐
- 推荐解释增强接口
