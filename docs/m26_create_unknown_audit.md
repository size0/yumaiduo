# M2.6 CREATE_UNKNOWN 只读审计结论

审计范围：

```text
V3/v3-backend-gateway-work/app/wanda_official_api.py
V3/v3-backend-gateway-work/app/wanda_direct_gateway.py
V3/ticket-system-v3-gateway-work/backend/app/wanda_client.py
V3/.tmp/ticket-prod-sync/app/wanda_client.py
V3/.tmp-order-production*.py
V4/backend/app/wanda_direct_quote.py
V4/backend/app/probe/official_provider.py
V4/backend/app/liangpiao_client.py
V3/V4 现有测试、日志和静态抓包引用
```

本审计没有发送请求。

## 1. 已存在的 Wanda endpoint

| Endpoint | 方法/参数 | 只读 | 能否发现未知 create |
|---|---|---:|---:|
| `/order/order_status.api` | POST `json=true`, `orderId` | 是 | 否；必须先有 orderId |
| `/order/order_detail.api` | GET `orderId` | 是 | 否；必须先有 orderId |
| `/order/query_by_userid.api` | POST `json=true`, `orderId`，可选 `busiType/timeLeagth` | 是 | 否；参数名虽含 userid，实际仍需要 orderId |
| `/order/query_order_list.api` | POST `pageIndex`, `timeLeagth`, `busiType`，账号会话 | 是 | 仅可作为候选列表，未证明可可靠定位临时锁座 |
| `/event/query_order_list.api` | POST `pageIndex`, `pageSize`, `source` | 是 | 活动订单列表，不是电影临时订单 lookup |
| `/order/real_time_seat.api` | GET `dId=showtime_id` | 是 | 只能观察座位可售状态，不能返回订单身份 |
| `/mkt/activity/secret/list.api` | GET `partition`, `orderId`, `did` | 读取活动 | 否；必须先有 orderId |
| `/api/film/order/orderList` | ticket-api | 未确认；V3 Client 注释记录 404 | 否 |
| `/api/film/order/officialQuotation` | POST `showtimeId`, `seatIds`, `appId` | 报价查询 | 否；不是订单查询 |

### 订单列表特别说明

在历史/另一套 V3 ticket-system Client 中存在：

```text
POST /order/query_order_list.api
```

参数：

```text
pageIndex
busiType：默认 3
timeLeagth：默认 0
```

返回结构被 V3 代码按以下字段读取：

```text
data.listOrderInf[].orderId
```

该接口是账号级历史订单列表，不接收 `showId`、`seatId`、`clientOrderNo` 或时间窗口作为精确 lookup 参数。V3 同步逻辑通过分页后自行读取 `orderId`、影院、影片、场次和座位字段。

当前没有真实 Capture 证明：

```text
临时锁座订单一定及时出现在列表
列表结果具有 read-after-create 一致性
列表不存在某条记录可以证明订单未创建
同 show/seat/time 能唯一对应本次超时请求
```

因此它不是当前可直接启用的可靠 CREATE_UNKNOWN lookup。

## 2. 各种 CREATE_UNKNOWN 结论

### 已知 orderId

如果 create 响应已经返回 `orderId`，但业务码/锁状态未知：

```text
可以用 /order/order_status.api 查询
可以在确认后调用 /order/cancel.api
可以用 /order/real_time_seat.api 做释放验证
```

这是已有的 cleanup 路径，不是 CREATE_UNKNOWN 的“发现 orderId”能力。

### 未知 orderId

对于 create timeout、连接断开或响应丢失：

```text
无法从 order_status 获得 orderId
无法从 order_detail 获得 orderId
无法从 activity endpoint 获得 orderId
real_time_seat 不包含订单身份
order list 只能提供未证明可靠的候选
```

不能确认：

```text
订单不存在
订单仍锁座
可以安全取得本次订单 orderId
```

结论：

```text
CREATE_UNKNOWN_UNRECOVERABLE_AUTOMATICALLY
```

## 3. V4 当前状态

`WandaDirectQuoteService` 当前旧路径包含：

```text
/order/create_order.api
/order/order_status.api
/mkt/activity/secret/list.api
/order/cancel.api
/order/real_time_seat.api
```

但没有未知订单 lookup。

`OfficialWandaProbeProvider` 目前只接受离线 mock transport，并没有真实 lookup 方法。

`CreateUnknownReconciler` 已建立明确接口：

```text
lookup_probe_order(show_id, seat_ids, account_ref, since)
```

未提供该接口的 Provider 返回：

```text
UNSUPPORTED
```

并保留 `CREATE_UNKNOWN`，不会重新 create。

## 4. 座位状态只能作为辅助信号

如果 timeout 后：

```text
原 Probe seat 从 AVAILABLE 变成不可售
```

只能说明：

```text
可能有临时订单或其他买家/系统占用
```

不能说明：

```text
本次 create 已成功
本次 create 订单号是什么
订单不存在
可以安全换账号重试
```

因此座位状态可以作为 reconciliation 的辅助 signal，但不能替代订单 lookup。

## 5. Fail-safe

当前没有可靠 lookup 时：

```text
ProbeOrder.status = CREATE_UNKNOWN
show lock_state = PROBE_UNKNOWN_HOLD
account lease 延长至 unknown_create_hold_seconds
禁止同 show 新 Probe
禁止换账号重试
只允许有限只读观察
```

当前默认安全窗口：

```text
unknown_create_hold_seconds = 900 秒
```

但安全窗口到期不自动证明订单不存在；show 的 unknown hold 仍应由人工/经审核的后台 reconciliation 清除。最坏阻塞时长为：

```text
单次自动安全窗口至少 15 分钟；若没有可靠 lookup 或人工确认，同 show 可以无限期保持 hold。
```

风险权衡：

```text
代价：可能暂时阻塞一个 show 和一个账号
收益：避免重复创建、重复锁座、遗漏未知临时订单和双重占座
```
