# 万达电影票 AI 客服 V2 执行器

本目录是鱼麦多插件 `wanda-seat-autoquote` 的 V2 **平台执行器**，不是 AI 业务后端。

## 职责边界

插件只负责：

- 使用鱼麦多官方 SDK 注册运行时并校验签名 webhook。
- 按事件 ID 幂等接收消息和订单事件，同一会话串行处理。
- 读取鱼麦多权威会话与订单状态，将事件提交到规则后端持久Inbox。
- 通过60秒租约领取并执行后端Outbox中的 `send_message`、`change_order_price` 等固定命令。
- 对改价执行所有权校验、幂等、订单回读和结果未知对账。
- 把每个动作结果可靠回传 V2 后端。

插件不得包含：

- LLM/Agent 决策、提示词、知识库或模型密钥。
- OCR/视觉识别、场次匹配、实时座位和锁座报价。
- W+ 或普通座报价公式。
- 旧插件源码、旧数据库、旧 bridge 接口或旧固定话术。

这些能力统一属于 `E:\鱼麦多\v4\backend` 后端。完整边界见 [docs/V2_ARCHITECTURE.md](docs/V2_ARCHITECTURE.md)。

## 后端契约

插件使用 `X-Wanda-AI-V2-Bridge-Key` 调用独立 V2 接口：

| 接口 | 用途 |
| --- | --- |
| `POST /api/wanda-ai-v2/plugin/shops/sync` | 同步鱼麦多店铺事实 |
| `POST /api/wanda-ai-v2/plugin/events/process` | 持久提交事件，返回 `event_id/accepted/duplicate` |
| `POST /api/wanda-ai-v2/plugin/commands/claim` | 领取带60秒租约的持久命令 |
| `POST /api/wanda-ai-v2/plugin/commands/{command_id}/result` | 回传命令结果及官方回读证据 |

## 出票系统只读订单接口

为下游出票系统提供了独立的只读 Wanda 订单接口。它只调用本插件绑定的鱼麦多订单客户端，不查询良票订单，也不执行改价、付款、出票或其他写操作。

启用方式：在运行环境设置 `WANDA_ORDER_API_KEY` 和 `WANDA_ORDER_API_TENANT_ID`。租户固定在服务端，调用方不能通过请求覆盖，避免跨租户读取。

```text
GET /__plugin__/api/wanda/orders?limit=50
Authorization: Bearer <WANDA_ORDER_API_KEY>

GET /__plugin__/api/wanda/orders/{order_id}
Authorization: Bearer <WANDA_ORDER_API_KEY>
```

列表响应示例：

```json
{
  "source": "wanda",
  "orders": [{
    "orderId": "...",
    "orderStatus": 1,
    "productTitle": "...",
    "quantity": 2,
    "payment": "0",
    "createTime": "..."
  }],
  "count": 1,
  "observedCount": 1
}
```

接口为内部服务接口，应仅通过 HTTPS 或本机回环访问；未配置密钥返回 `503`，密钥错误返回 `401`，`limit` 范围为 `1–100`。订单详情和列表均来自鱼麦多实时读取，订单事件未被插件观察到时不会被伪造或猜测。

生产不存在 `off/shadow/auto` 决策模式或legacy回退。事件响应不携带动作；插件无权生成业务动作。AI辅助和外部写熔断分别由后端独立控制。

## 目录

```text
src/
  actions/    # 高风险平台动作与权威订单契约
  backend/    # V2 后端客户端
  http/       # health/webhook 入口
  platform/   # 鱼麦多 SDK 注册与租户客户端
  runtime/    # 事件队列、会话串行和动作回执
  config.mjs  # 运行配置与日志脱敏
```

`datasets/`、`evaluation/`、`tools/`、`图片学习样本/` 都是开发材料，不进入生产包。`ui/` 仅发布已接线的鱼麦多运行状态面板：通过 `@yumaiduo/plugin-sdk-web` 握手、经同源网关调用，并由后端使用 `webhookSecret` 验证网关签名；它不承载报价公式、模型密钥或跨租户事件数据。

## 验证

```powershell
npm.cmd run check
```

`check` 会串行运行测试，并用 `npm pack --dry-run` 检查生产包白名单。当前目录不是 Git 仓库，修改前后的版本管理需要后续单独接入。
