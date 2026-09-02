# V3 Active Probe 一次性真实脱敏 Capture Runbook

状态：**仅准备，不执行**。

本文是未来一次最小化 Capture 的操作清单。任何步骤都不能绕过以下开关：

```text
AGENT_HARNESS_READ_ONLY=true
NEW_AGENT_HARNESS_ENABLED=false
WANDA_ACTIVE_PROBE_ENABLED=false  # 当前及 Capture 前默认值
```

## 1. 允许启用的 V3 路径

仅允许使用冻结的 V3 Direct official path：

```text
WANDA_DIRECT_GATEWAY_ENABLED=true       # 仅一次人工批准的隔离窗口
WANDA_DIRECT_ACCOUNT_POOL_PATH=<隔离账号池>
WANDA_PRICING_ACCOUNT_REF_KEY=<仅内存/密钥管理器注入，不能 Capture>
WANDA_DIRECT_APP_CLIENT_KEY=<公开协议材料>
WANDA_ALLOW_FRIDAY_MEMBER_DAY=false
```

不得使用 Legacy local gateway，不得使用任何购票、付款、出票、退款路径。

Capture 使用隔离 V3 进程/工作目录，不能接入 V4 Composition Root。

## 2. 测试场次条件

- 单一 Wanda 影院、单一 show_id。
- 距离开场和自动取消窗口足够远，且由人工确认可回滚。
- 场次有明确可售的 W+ 座位。
- 官方实时座位能返回唯一 seat_id、area/zone 和官方原价。
- 没有买家正在请求该场次，也没有人工订单正在占用该座位。
- 账号池只放一个获批准的 W+ 测试账号。

## 3. 座位选择

第一单严格使用：

```text
单 show
单 seat type
单代表座
单账号
```

选择规则：

```text
实时可售座位
→ 固定一个真实代表 seat_id
→ 保存 area_code / zone_type / original_price_cents
```

不得使用截图价格、手绘圈数量或模型猜测 seat_id。

## 4. 开始前检查

1. V4 三个开关仍为关闭/只读。
2. V3 工作树与冻结 SHA-256 已核对。
3. V3 账号、租约、show lock、日志目录均为隔离环境。
4. Capture 输出目录为空且权限受限。
5. HTTP transport 只允许冻结的 Wanda 官方 host 和协议 endpoint。
6. 已准备人工终止和人工 cancel 负责人。
7. 已准备 `capture.py` 的离线脱敏脚本，未把原始日志上传或提交 Git。

## 5. 需要 Capture 的原始响应

只在受控内存/受限本地临时目录保存：

```text
create_order.response.json
lock_status.response.json
activity_offers.response.json
cancel.response.json
cancel_status.response.json
seat_release_0s.response.json
seat_release_2s.response.json
seat_release_5s.response.json
```

原始请求 headers、body、签名、Cookie、Token、手机号、设备信息不进入 Golden 文件；只记录脱敏后的必要响应字段和 endpoint 元数据。

## 6. create 成功后的 cleanup

只要响应中出现或可能出现订单引用：

```text
立即停止后续非必要动作
→ cancel_order
→ order_status
→ 必须确认 orderStatus=60 且 lockSeatTime=-1
→ 0/2/5 秒检查 expected seat 恢复可售
```

无论 activity、解析、超时或上层任务是否失败，都必须继续 cleanup。不得换账号或重新 create。

## 7. cancel 失败

- 不继续下一账号。
- 不重新 create。
- 保留内部 `temporary_order_reference`，只以脱敏不可逆引用写审计。
- 进入 `RELEASE_UNVERIFIED`/人工处理。
- 继续受控 status 与座位只读复核。
- Capture 标记为失败，不生成成功 `expected_probe_result`。

## 8. release 未确认

如果任一 `0/2/5` 秒未能同时确认订单取消和座位恢复：

```text
show → release pending / manual hold
account → 暂停复用
禁止新 Probe
```

按既定后台 reconciliation 继续两次 15 秒复核，但不等待过程中执行下一次 create。仍未确认则交人工处理。

## 9. Capture 后脱敏

将响应交给：

```text
app.probe.capture.redact_capture()
app.probe.capture.assert_capture_redacted()
```

脱敏规则：

```text
删除 Token / Cookie / Authorization / CSRF / AppSecret
删除手机号、设备 ID、签名
所有 orderId 替换为 fixture-order-1
```

原始文件在脱敏校验完成后立即销毁，不提交 Git，不上传聊天或日志系统。

## 10. 生成 expected_probe_result

只从已验证官方事实生成：

```text
status
error_code
area_code
zone_type
representative_seat_id
original_price_cents
member_price_cents
cancel_confirmed
release_verified
release_timing_class
```

禁止写入：

```text
selling_price
quote_price
total_quote
markup
discount
V4 Pricing rule
```

## 11. Fixture 验证

使用 `write_v3_capture()` 生成目录，然后验证：

```text
manifest.source == V3
manifest.sensitive_data_removed == true
fixture 文件齐全
ProbeResult schema 通过
assert_capture_redacted() 通过
文件中不存在手机号、Token、Cookie、Authorization、真实订单号
```

## 12. 立即关闭 Probe

Capture 成功或失败后立即：

```text
WANDA_DIRECT_GATEWAY_ENABLED=false
WANDA_ACTIVE_PROBE_ENABLED=false
```

确认进程退出、后台 reconciliation 已记录结果、账号和 show hold 已由人工确认处理。V4 从不启用 Active Probe。

## 13. 最小案例之后

使用：

```text
V3CaptureReplayProvider
→ canonical Provider Result
→ WandaActiveProbe
→ ProbeResult
→ ProbeShadowComparator
```

只在离线环境完成后续 Replay。未完成人工审核前，不进行第二个真实 Capture。
