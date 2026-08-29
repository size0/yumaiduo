# 图片与聊天经验数据治理

## 目的

本流程把一次性会话理解转成可追溯、可撤回、可版本化的插件经验。插件经验是经过治理的样本与标签，不代表基础模型被训练或参数被更新。

## 数据阶段

1. **会话上下文**：用户在聊天中附图，系统可在当前会话内识别。默认不持久化为经验。
2. **Inbox**：只读发现本地图片，记录路径、SHA-256、格式和尺寸。统一为 `review_status=pending`，不含 gold 标签。
3. **治理副本**：获得许可后复制到 `datasets/samples/`，不移动、不改名、不覆盖来源原图。
4. **脱敏与许可**：完成敏感信息处理，记录许可依据、保留期和删除时间。
5. **人工标注**：按 schema 记录类别、座位类型、座位名、张数、证据和人工纠正。
6. **Eligible**：validator 通过后才允许进入评测集或插件经验集。
7. **评测与回流**：保存预测、指标和失败样本；失败样本仍需人工复核、去重和治理，不能自动变成 gold。

## 最小化与脱敏

- 只保留完成座位识别所需的画面区域；账号、手机号、订单号、二维码、头像和通知内容应裁剪、遮盖或确认不存在。
- SHA-256 用于去重和追踪，不替代许可，也不表示图片已匿名化。
- 未脱敏图片不得发送到配置的外部 vision endpoint。
- 日志和失败报告只写 sample ID、类别和错误，不嵌入原图或完整聊天文本。

## 来源许可与保留

- `allowed` 必须有可解释的 `basis`；`unknown`、`restricted`、`denied` 均不能成为 eligible。
- `pending_review` 不是最终保留策略。可选 `delete_after` 或 `retain_until_replaced`，并填写复核或过期时间。
- 用户撤回许可时，将记录设为 `excluded`，删除治理副本，并在下一数据集版本中记录撤回，不改写既有评测报告。

## 标注与纠正

- pending 记录禁止预填 label，避免未标注样本混入 gold。
- reviewed 只说明标签被人工确认；仍需检查图片资产、脱敏、许可和保留策略。
- 人工纠正必须记录旧标签与原因，不直接覆盖历史事实。
- 两张聊天临时图目前只有用户明确标签，图片未入 `datasets/samples/`，所以状态为 `blocked_not_ingested`，不能用于 vision 评测。

## 聊天与价格边界

聊天案例必须包含状态、买家意图、事实、期望动作和回复约束。价格只能来自 `OFFICIAL_BACKEND`、`PLUGIN_QUOTE_ENGINE` 或 `HUMAN_CONFIRMED` 的核验事实，并带证据引用。

模型不得估算、补全或生成价格。没有已核验价格时，期望动作只能请求官方报价、等待或转人工，不能发送具体报价。

## 评测口径

- 分类指标仅使用 schema 合法且 `experience_status=eligible` 的人工 gold。
- 每类报告 true positive、false positive、false negative、precision、recall 和 support。
- 自动通道 precision 单独报告，并同时报告 coverage，防止通过全部转人工来虚增精度。
- schema 合法率包含非法 JSON、字段错误与重复预测 ID。
- 报告保留失败 sample ID 与原因；不把未标注样本的缺失预测算作模型错误，也不把它们当 gold。
- endpoint、模型或 prompt 未实际运行时，必须明确写“未运行”，不能从 schema/单元测试推导模型效果。

## 版本与审计

每次评测保存 dataset version、model version、prompt version、运行时间、预测 JSONL 和报告。数据集、开发调参集与最终验收集应分开版本；复用样本时标记污染风险。
