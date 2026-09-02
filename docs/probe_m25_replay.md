# M2.5 V3 Capture / Comparator / V4 Provider Adapter

状态：M2.5 已完成，M3 Shadow Probe 未开始。

安全开关：

```text
AGENT_HARNESS_READ_ONLY=true
NEW_AGENT_HARNESS_ENABLED=false
WANDA_ACTIVE_PROBE_ENABLED=false
```

## Replay fixture

目录：

```text
backend/tests/fixtures/probe_replays/<fixture>/
```

文件：

```text
manifest.json
create_order.response.json
lock_status.response.json
activity_offers.response.json
cancel.response.json
cancel_status.response.json
seat_release_0s.response.json
seat_release_2s.response.json
seat_release_5s.response.json
expected_probe_result.json
```

Capture 工具：

```text
backend/app/probe/capture.py
```

真实 V3 capture 尚未执行；当前目录只保存格式说明，不伪造真实响应。

## Canonical Replay Protocol

```text
V3 raw response
→ V3CaptureReplayProvider
→ CreateOrderResult
→ OrderStatusResult
→ ActivityOffersResult
→ CancelResult
→ SeatAvailabilityResult
→ WandaActiveProbe
```

V4 Active Probe 只消费 canonical result，不依赖 V3 Python 对象或内部字段命名。

## Official Provider Adapter

```text
backend/app/probe/official_provider.py
```

`OfficialWandaProbeProvider` 支持注入离线 mock transport 进行 contract test；未注入 mock transport 时直接返回：

```text
real_provider_disabled
```

没有注册到生产 Composition Root，也没有真实 HTTP 路径。

## Comparator

```text
backend/app/probe/comparator.py
```

比较：

```text
status
error_code
representative seat facts
area_code
zone_type
original_price_cents
member_price_cents
cancel_confirmed
release_verified
release_timing_class
```

输出：

```text
MATCH
ACCEPTABLE_DIFFERENCE
MISMATCH
```

`MISMATCH` 包含字段级 diff。

不比较：

```text
probe_id
account_ref
revision
时间戳
temporary order reference
selling_price
quote_price
total_quote
markup
discount
```

## Unknown Create

新增：

```text
CREATE_UNKNOWN
create_unknown
```

明确 create 失败与请求超时/结果未知分离。未知结果不会换账号重试或再次 create。

当前万达适配器尚未提供可靠的 order lookup/reconciliation，因此 `CREATE_UNKNOWN` 的自动恢复仍是 M3 blocker；状态会持久化并保持 show pending gate。

## M2.5 Gate

已完成：

```text
Capture schema
Redaction tests
Canonical Replay Protocol
Official Provider contract
Shadow Comparator
multi-worker durable lock/lease tests
restart cleanup tests
unknown create semantics
account pool interface
WANDA/Liangpiao boundary checks
Kill Switch fail-closed checks
```

尚未满足进入 M3 的条件：真实脱敏 V3 Capture、真实 Provider adapter shadow 接入、CREATE_UNKNOWN lookup/reconciliation。因此 M3 Gate = NO-GO。
