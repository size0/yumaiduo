# V3 Golden Capture replay fixtures

每个目录代表一个脱敏、版本化的 V3 Active Probe capture，文件固定为：

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

`manifest.json` 至少包含：

```json
{
  "fixture_version": "v3-probe-001",
  "provider": "WANDA",
  "cinema_id": "6669",
  "show_id": "fixture-show-1",
  "seat_type": "W+",
  "capture_schema_version": "1",
  "captured_at": "2026-09-02T00:00:00Z",
  "source": "V3",
  "sensitive_data_removed": true
}
```

`orderId` 必须统一替换为 `fixture-order-1`。禁止保存手机号、Token、Cookie、Authorization、设备 ID、签名、AppSecret 和真实完整临时订单号。

本目录目前只有格式说明，没有伪造的真实 V3 Capture。真实 Capture 获得授权并脱敏后，使用 `app.probe.capture.write_v3_capture()` 生成。
