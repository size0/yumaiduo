# 当前 main 上线审核清单

> 当前生产已部署源码候选 `a87a11b77c4644d18c590a5d33a2da5b094fd477`：V3契约为 `wanda-v3-v11-pricing-account-evidence`，插件Runtime为 `wanda-agent-runtime-v29-primary-only`。外部Dify advisory Provider已移除；生产继续保持Shadow、确定性执行Owner和Active 0%。

## 已完成

- [x] manifest：`id=wanda-seat-autoquote`、`vendor=xdl`、版本 `0.6.0`
- [x] 权限最小化：订单只读/改价、店铺只读、IM 会话/消息只读与发送
- [x] 只配置单一运行模式；未混用 usage_based 与 freemium 计费链路
- [x] webhook 原始 body HMAC 验签与 ±300 秒防重放
- [x] 长任务持久入队后 ACK；跨事件消息幂等；worker 可恢复
- [x] `/healthz` 返回 `registered:true`
- [x] 会话智能体采用严格 action 白名单、最多8步、双层参数校验、逐工具读写/Observation契约、独立持久化worker和失败关闭
- [x] 生产先以 `shadow` 模式运行，智能体计划不改变买家现有回复
- [x] 高危发货、假发货、催收货、评价、退款和自动出票能力未申请且代码硬禁用
- [x] 隐藏开发页面、图片测试、待报价页和手动改价验证接口已删除
- [x] UI 使用平台 iframe SDK、网关鉴权、主题变量和自适应宽度
- [x] Agent轨迹可按租户脱敏回放；人工任务支持负责人、优先级、标签、SLA和内部备注
- [x] Agent回复知识按确定性会话场景有界检索，价格、库存和订单事实仍只来自权威工具
- [x] 当前源码插件测试 484 项、V3 后端测试 185 项通过；V3 测试无弃用警告
- [x] 每次新增Agent、识图或核价能力必须回归：首次图片报价、有效报价后问价、文字座位、确认、订单创建/付款、人工接管、平台系统消息；禁止有效报价后的普通追问重新识图或重复临时试价
- [x] 历史0.6.0审核包曾在干净目录执行 `npm ci --ignore-scripts && npm test` 通过
- [x] 历史0.6.0审核包官方 npm registry 生产依赖审计为0漏洞
- [x] 历史生产 release 的健康检查与回滚已验证
- [x] 万达官方直连具备创建状态、取消状态、实时座位释放、15/30秒后台复核、租约续期及最多3账号安全轮转契约
- [x] 直接报价使用独立HMAC密钥生成 `pricing_account_ref`，并在报价确认和平台改价前强制校验证据
- [x] 官方直连部署预检不会输出密钥、Token、手机号、账号标识或账号池路径
- [x] 生产已移除未启用的外部Dify advisory Provider；V29仅保留主Agent与确定性交易引擎
- [x] 场次零命中但仅缺影片名时精确追问“影片：完整影片名”，买家补充后复用识图草稿且不重复识图

## 当前源码发布前必须完成

- [x] 已在受限生产环境中生成并配置至少32字节的 `WANDA_PRICING_ACCOUNT_REF_KEY`，未输出或复用账号Token
- [x] 已使用生产环境执行 `scripts/validate_direct_gateway_env.py --require-enabled`，结果为 `ready`，识别15个合格账号
- [x] 账号池权限为 `640 ticket-system:ticket-system`，预检确认文件类型和权限安全
- [x] 已在源码提交 `a87a11b` 使用干净依赖执行插件484项测试和V3 185项测试；生产release再次执行插件484项测试通过
- [x] 官方registry执行 `npm audit --omit=dev --json` 成功：生产依赖漏洞总数0；审计响应SHA-256为 `08886336e9ac4c3334d9e199091490029d90496fed849454723ebd7dbc3ceb6d`
- [x] 已为源码提交 `a87a11b77c4644d18c590a5d33a2da5b094fd477` 生成并验证确定性V3和插件候选包及独立SHA-256；本地候选目录为 `dist/release-candidates/a87a11b77c46`
- [x] 已在服务器创建独立V3和插件release目录，未覆盖V10/V22历史release
- [x] 部署后Runtime契约校验为 `ready`；两个systemd服务均为 `active`、`NRestarts=0`，WorkingDirectory准确且最近warning日志为空
- [ ] 使用只读官方座位请求进行smoke；临时试价只能使用预先批准的受控场次，并必须确认取消和座位恢复
- [x] 已确认 `conversation_agent_mode=shadow`、`execution_owner=deterministic`、`conversation_agent_active_ready=false`、Active 0%、Canary关闭且kill switch开启
- [ ] V29重新采集至少100轮自动安全审计和100轮图片样本；当前已有1轮文字、1轮图片evaluation和1轮实时Shadow，V28及更旧版本样本不得补门槛
- [x] 已完成V29到V28回滚及V29恢复演练；两端健康、WorkingDirectory和队列恢复通过
- [x] 已修复候选包遗漏vendor SDK运行时的问题，并将SDK `dist/index.js`设为构建强制文件；首次失败切换已自动回滚，无买家交易状态迁移
- [x] 插件systemd沙箱已显式允许写入当前 `DATA_DIR=/var/lib/ticket-system/wanda-ai-plugin-data`，V28历史评测队列已恢复持久化

## 当前生产候选包证据

- V3 SHA-256：`1dacdc375323f1cd6725fe5bfbd980344758e51dbf9119843738d0fa2ec180d6`
- 插件 SHA-256：`57e563932b8d22c233227583d6c861599fabe12edfa3d2742b1a003d98a19c90`
- Manifest SHA-256：`07b22ef7177df033ff1b9632f3b679f4465738c5fafeb25a3ade64395ec97334`
- 构建命令：`python deploy/build_release_bundles.py`
- 校验命令：`python deploy/verify_release_bundles.py dist/release-candidates/a87a11b77c46`
- 生产V3：`/opt/wanda-v3-backend/releases/v11-pricing-account-evidence-a87a11b77c46`
- 生产插件：`/opt/wanda-preview-plugin/releases/v29-primary-only-a87a11b77c46`

## 提交前由门户确认

- [ ] “我的插件 → 详情”粘贴/维护 README 中的使用文档（重点步骤置于前 4000 字符）
- [ ] “设置”填写真实售后联系方式
- [ ] 确认可见性（公开或私有白名单）
- [ ] 绑定测试租户，在测试窗口内完成截图识别、报价、确认、待付款改价和异常回退
- [ ] 若配置付费定价，完成 KYC、保证金与定价审核；只选择一种 `billing_model`
- [ ] 用 0.6.0 包启动一次自注册，确认版本状态进入平台测试验证期
- [ ] 测试验证通过后申请正式上架

## 历史审核包（不可直接部署当前 main）

- `dist/wanda-seat-autoquote-0.6.0-review.zip`
- SHA-256 见同名 `.sha256` 文件
