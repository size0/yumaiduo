# Wanda Plugin Cockpit UI feature mapping

Visual structure is owned by the cockpit reference; data and actions remain owned by the existing API contracts. No backend business semantics were changed in this UI pass.

| OLD_PAGE | OLD_FEATURE | API | NEW_PAGE | NEW_LOCATION | STATUS |
|---|---|---|---|---|---|
| 客服工作台 | 图片识别、文字回复、报价结果展示 | `POST /api/chat/image-messages`, `POST /api/chat/text-messages`, `GET /api/diagnostics/recent` | 概览 / 识别报价入口 | Existing `chat` workspace, reachable from existing model/chat affordance; not a first-level menu | ACTIVE |
| 店铺开关 | 自动化开关 | `GET /api/plugin/shops`, `PUT /api/plugin/shops/{shop_id}` | 店铺开关 | Shop list | ACTIVE |
| 店铺开关 | Canonical 报价开关 | `GET /api/plugin/shops?include_canonical=true`, `PUT /api/plugin/shops/{shop_id}` | 店铺开关 | Shop list | ACTIVE |
| 店铺开关 | 鱼麦多店铺同步 | `POST ui/api/shops/sync` | 店铺开关 | Refresh shops | ACTIVE |
| 运营报价 | 良票报价规则 | `GET/PUT /api/settings/operations` | 运营报价 | 报价规则左侧 | ACTIVE |
| 运营报价 | 万达/W+报价规则、取整、启用开关 | `GET/PUT /api/settings/operations` | 运营报价 | 报价规则右侧及报价预览 | ACTIVE |
| 报价记录 | 核价列表、成功/失败、价格事实 | `GET /api/plugin/quote-records` | 报价记录 | Filter/list + right detail panel | ACTIVE |
| 订单管理 | Wanda 订单列表 | `GET ui/api/orders` | 订单管理 | Filters/status tabs/table + right detail panel | ACTIVE |
| 订单管理 | 良票订单列表 | `GET /api/plugin/liangpiao-orders` | 订单管理 | Unified order list | ACTIVE |
| 订单管理 | 订单详情、取票信息 | `GET ui/api/orders/{order_id}`, `GET /api/plugin/liangpiao-orders/{order_no}` | 订单管理 | Detail panel and existing detail dialog | ACTIVE |
| 回复话术 | 流程文案、关键词规则、图片资产 | `GET/PUT /api/settings/reply-templates`, `POST /api/settings/reply-keyword-images` | 回复话术 | Left template groups + right editor | ACTIVE |
| 回复话术 | 观影提醒配置与任务摘要 | `GET/PUT /api/settings/reminders`, `GET /api/plugin/reminders` | 回复话术 | Reminder section | ACTIVE |
| 会话策略 | AI 回复、记忆、业务区间、人工接管 | `GET/PUT /api/settings/conversation-policy` | 设置 | 通用配置 | ACTIVE |
| 模型接口 | GLOBAL/TENANT/SHOP scope model settings | `GET/PUT /api/settings/vision`, `POST /api/settings/vision/models` | 模型接口 | Scoped model connection card + form | ACTIVE |
| 知识库 | 搜索/分类、编辑、启停、删除 | `GET/POST/PUT/DELETE /api/settings/knowledge` | 知识库 | Search/category/list editor | ACTIVE |
| Agent 审计 | run/context/tool/reply/command/failure projection | `GET /api/rules-first/agent-runs` | Agent 审计 | Left run list + right detail visual language | ACTIVE |
| 人工任务 | list/claim/complete/resume | `GET /api/rules-first/manual-tasks`, `POST .../{task_id}/claim`, `POST .../{task_id}/complete` | 人工任务 | Task list/detail/actions | ACTIVE |
| 系统设置 | Existing general settings | `GET/PUT /api/settings/conversation-policy` | 设置 | 通用配置 | ACTIVE |
| 概览 | Plugin health/config summary | `GET ui/api/overview`, existing read APIs | 概览 | KPI, current config, recent runs, quick actions | ACTIVE |

## Protection result

- `ACTIVE_FEATURE_LOST = 0`
- `UNMAPPED_FEATURES = none`
- No first-level `会话` menu was added.
- No new chat workbench, pricing workflow, recognition authority, transaction authority, payment flow, order binding, provider behavior, or RulesFirst semantics were added.
- API-backed controls remain real controls; unavailable data renders an empty/error state rather than a fake action.

## Page API contracts

| PAGE | LOAD_API | SAVE_API | ACTION_API | BACKEND_OWNER |
|---|---|---|---|---|
| 概览 | `ui/api/overview`, shops, quote-records, agent-runs, manual-tasks, settings/vision | — | Existing page navigation | Plugin gateway + existing V4 backend projections |
| 店铺开关 | `GET /api/plugin/shops?include_canonical=true` | `PUT /api/plugin/shops/{shop_id}` | `POST ui/api/shops/sync` | Shop automation store |
| 运营报价 | `GET /api/settings/operations` | `PUT /api/settings/operations` | — | Pricing settings store |
| 报价记录 | `GET /api/plugin/quote-records` | — | — | Quote record store |
| 订单管理 | `ui/api/orders`, quote-records, liangpiao-orders | — | Existing read-only detail endpoints | FishMore/Wanda and Liangpiao adapters |
| 回复话术 | `GET /api/settings/reply-templates`, reminders | `PUT /api/settings/reply-templates`, `PUT /api/settings/reminders` | `POST /api/settings/reply-keyword-images` | Reply template and reminder stores |
| 模型接口 | `GET /api/settings/vision` | `PUT /api/settings/vision` | `POST /api/settings/vision/models` | Scoped model settings store |
| 知识库 | `GET /api/settings/knowledge` | `POST/PUT/DELETE /api/settings/knowledge` | — | Knowledge store |
| Agent 审计 | `GET /api/rules-first/agent-runs` | — | — | Redacted RulesFirst audit projection |
| 人工任务 | `GET /api/rules-first/manual-tasks` | — | `POST .../{task_id}/claim`, `POST .../{task_id}/complete` | RulesFirst manual-task store |
| 设置 | `GET /api/settings/conversation-policy` | `PUT /api/settings/conversation-policy` | — | Conversation policy store |
