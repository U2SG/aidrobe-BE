# iOS App 前端页面设计与 React Native 技术方案（V1 Slim）

## 1. 文档目标

本文用于对齐 Aidrobe V1 Slim 的 iOS 前端范围，明确需要设计多少页、每页承载哪些功能、页面之间如何跳转，以及使用 React Native 落地时的技术实现方案。

本文以 `docs/12_V1范围精简方案.md` 为范围基准。V1 Slim 的核心验证目标不是“完整 AI 衣橱”，而是：

> 用户是否愿意在早晨打开 App 看一眼今天穿什么，并采纳其中一套。

## 2. 页面数量结论

V1 Slim 建议交付 **12 个页面/页面单元**：

| 类型 | 数量 | 说明 |
|---|---:|---|
| App 内核心页面 | 10 | 覆盖 onboarding、今日推荐、衣橱、导入、设置 |
| App 内弹层 | 1 | 推荐反馈弹层 |
| Web/外部落地页 | 1 | 用于产品介绍、内测招募或 App 下载承接 |

如果只统计 App 内页面，则为 **11 个页面单元**。如果把 Web 落地页也纳入设计交付，则总计 **12 个**。

## 3. 信息架构

```text
外部入口
└── Web 落地页
    └── App Store / TestFlight / 下载二维码

iOS App
├── Onboarding
│   ├── 欢迎页
│   ├── 权限申请页
│   └── 出门时间设置页
├── 今日
│   ├── 今日推荐页
│   ├── 推荐详情页
│   └── 推荐反馈弹层
├── 衣橱
│   ├── 衣橱列表页
│   ├── 衣物详情页
│   ├── 添加衣物入口页
│   ├── 导入确认页
│   └── 导入失败兜底页
└── 设置
    └── 设置页
```

底部 Tab 建议使用 3 栏：

1. 今日
2. 衣橱
3. 设置

“添加衣物”不单独做一级 Tab，从衣橱页主按钮进入；今日页在衣物不足时也可以跳转到添加入口。

## 4. 页面设计说明

### 4.1 Web 落地页

页面目标：承接外部访问、讲清产品价值、引导用户下载或加入内测。

核心内容：
- 首屏标题：每天出门前，直接看到 3 套可穿搭配
- 副文案：基于你已经录入的真实衣物，减少早晨纠结
- 主 CTA：加入 TestFlight / 下载 App
- 次 CTA：查看产品说明
- 产品截图区：展示今日推荐、衣橱、添加衣物 3 个关键画面
- 内测说明：适合通勤人群，先支持拍照/相册导入
- 隐私说明：衣物图片仅用于识别与推荐，不做公开展示

不做：
- 长篇品牌故事
- 复杂价格页
- 博客/社区模块

### 4.2 欢迎页

页面目标：App 首次打开时，用一屏说明价值并进入设置。

核心功能：
- 展示产品一句话价值
- 展示 2 个利益点：省时间、少纠结
- 主按钮：开始使用
- 次入口：稍后设置

设计要点：
- 画面重点放在“今日 3 套推荐”的结果感
- 不做多页 onboarding 轮播
- 不申请权限，权限放到下一页

### 4.3 权限申请页

页面目标：解释并申请相机、相册权限。

核心功能：
- 相机权限：用于拍摄衣物
- 相册权限：用于从已有照片导入衣物
- 权限状态展示：未授权 / 已授权 / 被拒绝
- 主按钮：允许访问
- 次按钮：暂时跳过

V1 Slim 不申请：
- 推送权限
- 定位权限
- 通讯录权限

### 4.4 出门时间设置页

页面目标：采集 `departure_time`，用于今日页倒计时和后端预生成推荐。

核心功能：
- 时间选择器
- 默认说明：用于提前准备今日推荐，不会发送推送
- 主按钮：保存并继续
- 次按钮：跳过

接口依赖：
- `GET /v1/user/settings`
- `PATCH /v1/user/settings`

### 4.5 今日推荐页

页面目标：用户打开 App 后立即看到今天可穿的推荐，不出现 loading 空屏。

核心功能：
- 顶部展示日期和“距离出门 XX 分钟”
- 展示 1 到 3 套推荐卡片
- 推荐类型：稳妥、快速、有变化
- 每套展示衣物缩略图、推荐理由、核心单品
- 操作：查看详情、今天穿这套
- 衣物不足时展示补录引导
- 下拉刷新触发重新拉取推荐

关键状态：
- `ready`：展示推荐
- `insufficient_items`：展示还缺哪些衣物
- `selected`：当天已选择穿搭
- `stale_fallback`：展示昨日或兜底推荐，并用轻提示说明已在后台更新

接口依赖：
- `GET /v1/recommendations/today`
- `POST /v1/recommendations/outfits/{outfit_id}/wear`

设计约束：
- 首屏不能只显示“正在生成”
- 不能把推荐生成进度作为主体验
- 空状态必须给出明确的下一步，例如“再添加一件下装”

### 4.6 推荐详情页

页面目标：让用户确认这套搭配由哪些真实衣物组成，并完成采纳或反馈。

核心功能：
- 展示搭配名称和推荐类型
- 展示单品大图/缩略图列表
- 展示每件衣物名称、品类、颜色
- 展示推荐理由
- 主按钮：今天穿这套
- 次操作：喜欢 / 不喜欢 / 返回看其他方案

接口依赖：
- `GET /v1/recommendations/outfits/{outfit_id}`
- `POST /v1/recommendations/outfits/{outfit_id}/wear`
- `POST /v1/recommendations/outfits/{outfit_id}/feedback`

### 4.7 推荐反馈弹层

页面目标：低成本收集偏好信号。

核心功能：
- 喜欢
- 不喜欢
- 关闭

V1 Slim 不做：
- 不喜欢原因标签
- 多步问卷
- 文字备注

接口依赖：
- `POST /v1/recommendations/outfits/{outfit_id}/feedback`

### 4.8 衣橱列表页

页面目标：展示用户已录入衣物，并提供添加入口。

核心功能：
- 双列卡片展示衣物
- 品类 Tab：全部、上衣、下装、鞋、外套、配饰
- 卡片展示图片、名称、颜色、状态
- 状态标识：可穿 / 暂不可用
- 添加衣物按钮
- 点击卡片进入衣物详情

接口依赖：
- `GET /v1/wardrobe-items`

V1 Slim 不做：
- 复杂筛选器
- 搜索
- 批量编辑
- 季节/风格筛选

### 4.9 衣物详情页

页面目标：查看并轻量编辑单件衣物。

核心功能：
- 展示衣物主图
- 内联编辑名称
- 内联编辑品类
- 内联编辑颜色
- 切换状态：可穿 / 暂不可用
- 删除衣物

接口依赖：
- `GET /v1/wardrobe-items/{item_id}`
- `PATCH /v1/wardrobe-items/{item_id}`
- `DELETE /v1/wardrobe-items/{item_id}`

设计约束：
- 不单独做编辑页
- 编辑动作直接在详情页完成
- 删除需要二次确认

### 4.10 添加衣物入口页

页面目标：提供唯一导入入口，降低选择成本。

核心功能：
- 拍照添加
- 从相册选择
- 展示导入说明：识别后只需确认品类、颜色、名称

系统能力：
- 调用系统相机
- 调用系统相册

V1 Slim 不做：
- 链接导入
- 分享扩展
- 截图导入
- 手动从零创建衣物

接口依赖：
- `POST /v1/uploads/presign`
- `POST /v1/import-tasks`

### 4.11 导入确认页

页面目标：让用户确认视觉识别结果并保存到衣橱。

核心功能：
- 展示上传图片
- 展示识别结果
- 可编辑字段：名称、品类、颜色
- 主按钮：保存到衣橱
- 次按钮：放弃

接口依赖：
- `GET /v1/import-tasks/{task_id}`
- `POST /v1/import-tasks/{task_id}/confirm`

设计约束：
- 只露出 3 个字段
- 不要求用户补季节、风格、正式度
- 识别错误要能快速修正

### 4.12 导入失败兜底页

页面目标：识别或上传失败时保留用户继续导入的路径。

核心功能：
- 展示失败原因的用户可理解文案
- 主按钮：重新选择图片
- 次按钮：返回衣橱

失败场景：
- 上传失败
- 图片格式不支持
- 图片超过 10MB
- 视觉识别失败
- 网络异常

接口依赖：
- 复用上传和导入接口

### 4.13 设置页

页面目标：承载 V1 Slim 必要设置。

核心功能：
- 出门时间设置
- 清空衣橱
- 隐私说明
- 版本号

接口依赖：
- `GET /v1/user/settings`
- `PATCH /v1/user/settings`

设计约束：
- 不放推送设置
- 不放账号体系
- 不放风格偏好配置

## 5. 主要用户流程

### 5.1 首次使用流程

```text
欢迎页
  -> 权限申请页
  -> 出门时间设置页
  -> 衣橱列表页空状态
  -> 添加衣物入口页
  -> 系统相机/相册
  -> 导入确认页
  -> 衣橱列表页
  -> 今日推荐页
```

### 5.2 每日推荐流程

```text
打开 App
  -> 今日推荐页
  -> 推荐详情页
  -> 今天穿这套
  -> 推荐反馈弹层
```

### 5.3 衣物维护流程

```text
衣橱列表页
  -> 衣物详情页
  -> 修改状态/名称/品类/颜色
  -> 后端后台重算今日推荐
```

## 6. React Native 技术方案

### 6.1 技术选型结论

V1 Slim 可以采用 **React Native + Expo Dev Client** 落地。

原因：
- 页面和交互复杂度适中，React Native 足够覆盖
- 相机、相册、上传、表单、列表等能力都有成熟库
- 后续如需 Android 版本，复用成本更低
- 小团队可以用 TypeScript 统一前端类型和 API 契约

如果产品只做 iOS 且强依赖系统级体验，SwiftUI 是更原生的选择；但在希望快速迭代、未来兼容 Android 的前提下，React Native 更适合 V1 Slim。

### 6.2 推荐技术栈

| 模块 | 推荐方案 |
|---|---|
| 框架 | React Native + Expo Dev Client |
| 语言 | TypeScript |
| 路由 | React Navigation Native Stack + Bottom Tabs |
| 请求 | Axios 或 Fetch 封装 |
| 服务端状态 | TanStack Query |
| 本地状态 | Zustand |
| 表单 | React Hook Form |
| 图片选择 | expo-image-picker |
| 相机 | expo-camera 或系统 ImagePicker camera mode |
| 图片压缩 | expo-image-manipulator |
| 本地存储 | AsyncStorage |
| 安全存储 | expo-secure-store |
| UI 组件 | React Native Paper / Tamagui / NativeWind 三选一 |
| 埋点 | PostHog / Firebase Analytics / 自研事件接口 |
| 构建 | EAS Build |

V1 建议 UI 层优先选择 **React Native Paper** 或 **NativeWind**：
- 如果希望组件开箱即用，选 React Native Paper
- 如果希望样式更贴近 Web/Tailwind 团队习惯，选 NativeWind

### 6.3 前端目录建议

```text
src/
├── app/
│   ├── navigation/
│   └── providers/
├── screens/
│   ├── onboarding/
│   ├── today/
│   ├── wardrobe/
│   └── settings/
├── components/
│   ├── cards/
│   ├── forms/
│   ├── feedback/
│   └── layout/
├── api/
│   ├── client.ts
│   ├── settings.ts
│   ├── uploads.ts
│   ├── importTasks.ts
│   ├── wardrobe.ts
│   └── recommendations.ts
├── hooks/
├── store/
├── types/
├── utils/
└── assets/
```

### 6.4 API 对接方式

前端统一封装 API Client：
- 自动注入 `X-User-Id`，仅用于开发环境
- 统一处理 `{ code, message, data }` 响应结构
- 统一处理 401、403、422、500 错误
- TanStack Query 负责缓存、重试、刷新

关键接口映射：

| 页面 | 接口 |
|---|---|
| 出门时间设置页 | `GET/PATCH /v1/user/settings` |
| 今日推荐页 | `GET /v1/recommendations/today` |
| 推荐详情页 | `GET /v1/recommendations/outfits/{id}` |
| 穿这套 | `POST /v1/recommendations/outfits/{id}/wear` |
| 推荐反馈 | `POST /v1/recommendations/outfits/{id}/feedback` |
| 衣橱列表页 | `GET /v1/wardrobe-items` |
| 衣物详情页 | `GET/PATCH/DELETE /v1/wardrobe-items/{id}` |
| 添加衣物 | `POST /v1/uploads/presign` + `POST /v1/import-tasks` |
| 导入确认页 | `GET /v1/import-tasks/{id}` + `POST /v1/import-tasks/{id}/confirm` |

### 6.5 图片上传链路

```text
用户拍照/选图
  -> 前端压缩图片
  -> POST /v1/uploads/presign
  -> PUT 图片到对象存储
  -> POST /v1/import-tasks
  -> GET /v1/import-tasks/{id}
  -> 导入确认页
  -> POST /v1/import-tasks/{id}/confirm
  -> 衣橱列表刷新
```

实现要求：
- 上传前限制图片格式：JPEG、PNG、WebP
- 上传前限制大小：不超过 10MB
- 移动网络下展示上传进度
- 失败后进入导入失败兜底页

### 6.6 导航结构

```text
RootStack
├── OnboardingStack
│   ├── Welcome
│   ├── Permission
│   └── DepartureTime
└── MainTabs
    ├── TodayStack
    │   ├── Today
    │   └── OutfitDetail
    ├── WardrobeStack
    │   ├── WardrobeList
    │   ├── ItemDetail
    │   ├── AddItem
    │   ├── ImportConfirm
    │   └── ImportFailure
    └── SettingsStack
        └── Settings
```

### 6.7 状态管理

使用 TanStack Query 管服务端状态：
- `settings`
- `todayRecommendation`
- `wardrobeItems`
- `wardrobeItemDetail`
- `importTask`

使用 Zustand 管客户端 UI 状态：
- onboarding 是否完成
- 当前开发用户 id
- 本地草稿
- 全局 toast/snackbar

本地持久化：
- onboarding 完成状态存 AsyncStorage
- 开发用户 id 可存 SecureStore 或 AsyncStorage
- 不在本地持久化完整衣物数据，以服务端为准

### 6.8 页面状态规范

每个页面至少设计 5 类状态：
- 首次加载
- 有数据
- 空状态
- 错误状态
- 操作中状态

今日推荐页额外要求：
- 不能出现只有全屏 loading 的状态
- 接口返回 `insufficient_items` 时直接展示补录引导
- 接口失败时展示本地最后一次成功结果或轻量错误提示

### 6.9 埋点方案

V1 Slim 至少埋以下事件：

| 事件 | 触发时机 |
|---|---|
| `landing_viewed` | Web 落地页曝光 |
| `onboarding_started` | 点击开始使用 |
| `departure_time_saved` | 保存出门时间 |
| `add_item_started` | 点击添加衣物 |
| `image_selected` | 选择或拍摄图片 |
| `import_confirm_viewed` | 进入导入确认页 |
| `item_saved` | 保存衣物 |
| `today_viewed` | 今日推荐页曝光 |
| `outfit_detail_viewed` | 推荐详情曝光 |
| `outfit_worn` | 点击今天穿这套 |
| `outfit_feedback_sent` | 提交喜欢/不喜欢 |

## 7. 设计交付物

建议设计侧交付：
- 页面流程图
- 12 个页面/页面单元高保真稿
- 关键组件库：按钮、卡片、Tab、表单、空状态、Toast、底部弹层
- 颜色/字体/间距规范
- 今日推荐卡片 3 种类型视觉样式
- 导入链路错误状态
- iPhone 15/16 标准尺寸适配稿

## 8. 里程碑建议

### 第 1 周：设计与前端基础
- 确认页面范围
- 完成信息架构和主流程
- 搭建 React Native 项目
- 接入导航、API Client、Query Provider

### 第 2 周：衣橱与导入
- 衣橱列表页
- 衣物详情页
- 添加衣物入口页
- 图片选择和上传
- 导入确认页

### 第 3 周：今日推荐闭环
- 今日推荐页
- 推荐详情页
- 今天穿这套
- 喜欢/不喜欢反馈

### 第 4 周：设置、状态与打磨
- 出门时间设置
- 设置页
- 空状态/错误状态
- 埋点
- TestFlight 内测包

## 9. V1 Slim 不做清单

- 链接导入
- 分享扩展
- 推送通知
- 穿搭历史页
- 风格偏好问卷
- 天气接入
- 场景切换
- 虚拟试穿
- LLM 文案生成
- 复杂筛选和搜索
- 账号体系

这些能力可以作为 V1.1/V2 增量，不进入当前 iOS 前端首版设计。

