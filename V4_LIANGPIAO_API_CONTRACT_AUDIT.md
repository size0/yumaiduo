# V4 良票 API 契约对齐审计

对照基准：`D:\13250\Users\良票开放平台-API接入文档 (2).md`（2026-08-19 更新记录）。本文件记录代码事实，不把本地 fixture 当作真实平台响应。

## 已对齐的接口面

| 文档接口 | 当前实现 | 结论 |
|---|---|---|
| `/ping` | `LiangpiaoClient.ping()`，POST `{}` | 已补齐，用于部署前验签和服务器对时 |
| `/city/list`、`/city/locate`、`/region/list` | 窄参数方法，校验关键词、区划码和经纬度 | 已对齐 |
| `/brand/list`、`/cinema/list/detail` | allowlist 方法 | 路径已对齐；目前仍是通用 mapping 入参 |
| `/movie/list/detail` | 窄参数方法，校验 `HOT/COMING`、城市码、分页 | 已对齐 |
| `/show/dates` | `cinemaId` 必填，`movieId` 可选 | 已对齐 |
| `/show/list/detail`、`/seat/list` | 报价服务只发送文档字段；`seat/list` 只传 `showId` | 主链路已对齐；底层方法仍是通用 mapping |
| `/recognize/seat-shot` | `imageUrl` + 可选 `cityName`；同步调用单独使用 5 秒超时 | 已对齐 |
| `/recognize/seat-shot/async`、`/task/detail` | 支持 `outTradeNo` 幂等与轮询 | 已对齐 |
| `/recognize/confirm` | 支持 `cinemaId/movieId/showId` 任意一个或组合，并续传 `cityName` | 已对齐 |
| `/order/preflight` | 只发送 `showId/seats/ticketMode/priceMode`；逐座保留 `areaId` | 当前默认不传可选 `areaQuoteStrategy`，因此预检与下单都使用平台默认策略 |
| `/order/create` | 1–6 座、三种 `ticketMode`、两种 `priceMode`；`maxPrice` 使用供应商上限；`outOrderNo` 幂等 | 已对齐；内部 trace 只进入 `X-Trace-Id`，不再污染业务 body |
| `/order/detail` | 只允许 `orderNo` | 已收紧，禁止误传 `outOrderNo/providerOrderNo/showId` |
| `/order/list` | `status/startTime/endTime/page/pageSize` | 当前业务入口使用文档字段；底层方法仍是通用 mapping |
| `/order/cancel` | 只允许 `orderNo` 和可选 `reason` | 参数已收紧；两段式 `cancelled=false/true` 仍需由退款关闭状态机消费 |
| `/order/urge` | 只发 `orderNo` | 已对齐；不得轮询式调用 |
| `/order/refund/detail` | `orderNo`，原因最多 255 字 | 已对齐；`FAILED` 订单禁止再次调用 refund 属于上层门禁 |
| `/account/balance`、`/account/transaction/list` | allowlist 方法，流水分页上限 100 | 已补齐 |

## 本轮修正的确定性偏差

1. 下单首响应丢失时，旧代码用 `outOrderNo + showId` 调 `/order/detail`，但文档只接受平台 `orderNo`。现改为仅在网络/超时导致结果未知时，使用相同 `outOrderNo` 和完全相同 body 幂等重放 `/order/create`。
2. `300004` 等明确业务拒绝不再重试，直接返回 `LIANGPIAO_ORDER_CREATE_REJECTED`；只有 `TimeoutError`、`ConnectionError` 或 `liangpiao_network_error` 可触发一次幂等重放。
3. `traceId` 从 `/order/create` body 移至签名请求的 `X-Trace-Id` 头。
4. 选座询价从最多 20 座收紧到良票文档规定的 1–6 座；`ticketMode` 收紧为 `STANDARD/FAST/FLASH`，`priceMode` 收紧为 `FIXED/LIMIT`。
5. 回调补查票码时，只在已知平台 `orderNo` 时调用 `/order/detail`；只有 `outOrderNo` 时不构造未文档化请求。
6. 补齐 `/ping` 和 `/account/transaction/list`，并为同步识图设置文档要求的 5 秒请求超时。

## 仍需继续收口的项目

| 优先级 | 项目 | 当前影响 | 下一步 |
|---|---|---|---|
| 已完成 | LIMIT 失败后的闲鱼退款/关闭命令链 | 验签当前代际 LIMIT `order.failed` 生成耐久闲鱼关单 command；Runtime 校验租户/会话/订单/付款后执行；closed 后才开放 FIXED | 本地 Python/Node 全链测试已通过；剩余为真实闲鱼灰度权限和回读证据 |
| P1 | `areaQuoteStrategy` 显式建模 | 当前预检和下单都不传，统一使用平台默认 `AVERAGE`；无法选择 `HIGHEST/LOWEST` | 若后台要开放策略，加入报价快照并强制 preflight/create 取同一值 |
| P1 | 通用 mapping 方法继续窄化 | `brand/cinema/show/seat/preflight/order-list` 的底层 client 仍可被内部调用方传入额外字段 | 逐接口改为显式签名并增加 payload 等值测试 |
| P1 | `order/cancel` 两段式语义 | `cancelled=false` 不能视为关闭，必须继续等回调 | 在状态机测试中覆盖 false 保持 TICKETING、true 才关闭 |
| P1 | `pickupUrl`、票根 `version`、`estimatedSettleAmount` 的持久化投影 | 原始响应已保留，部分字段已有业务投影，但需要逐字段验收重启恢复 | 增加 order.detail/callback → store → 回复的重启测试 |
| P2 | 可选 `notifyUrl` | 当前依赖良票应用默认回调地址，业务可运行 | 仅在需要每单覆盖回调时加入，必须做 HTTPS allowlist |

## 可重复验证

```powershell
Set-Location E:\鱼麦多\v4\backend
$env:PYTHONPATH='.'
python -m pytest -q `
  tests/test_liangpiao_client.py `
  tests/test_liangpiao_recognition.py `
  tests/test_selected_seat_quote.py `
  tests/test_liangpiao_order.py `
  tests/test_liangpiao_callbacks.py `
  tests/test_agent_simulation_matrix.py
python -m ruff check `
  app/liangpiao_client.py `
  app/liangpiao_order_service.py `
  app/selected_seat_quote_service.py `
  tests/test_liangpiao_client.py `
  tests/test_liangpiao_order.py `
  tests/test_selected_seat_quote.py `
  tests/test_agent_simulation_matrix.py
```

真实凭证验收还必须在服务器保持两个写熔断关闭：`EXTERNAL_WRITES_ENABLED=false`、`LIANGPIAO_ORDER_CREATE_ENABLED=false`。真实两图识别与预检证据按 `V4_AGENT_EXECUTABLE_SIMULATION_MATRIX.md` 的 C 层执行。
