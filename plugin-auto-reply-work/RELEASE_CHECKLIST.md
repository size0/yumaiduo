# 当前 main 上线审核清单

> 当前 main 尚未部署。生产插件仍是 V22 行为基线；当前 Agent Runtime 为 `wanda-agent-runtime-v28-pricing-evidence-gate`，旧Runtime样本不得计入放行门槛。下方旧0.6.0审核包不能代表当前源码。

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
- [x] 插件测试 494 项、V3 后端测试 184 项通过；V3 测试无弃用警告
- [x] 每次新增Agent、识图或核价能力必须回归：首次图片报价、有效报价后问价、文字座位、确认、订单创建/付款、人工接管、平台系统消息；禁止有效报价后的普通追问重新识图或重复临时试价
- [x] 历史0.6.0审核包曾在干净目录执行 `npm ci --ignore-scripts && npm test` 通过
- [x] 历史0.6.0审核包官方 npm registry 生产依赖审计为0漏洞
- [x] 历史生产 release 的健康检查与回滚已验证
- [x] 万达官方直连具备创建状态、取消状态、实时座位释放、15/30秒后台复核、租约续期及最多3账号安全轮转契约
- [x] 直接报价使用独立HMAC密钥生成 `pricing_account_ref`，并在报价确认和平台改价前强制校验证据
- [x] 官方直连部署预检不会输出密钥、Token、手机号、账号标识或账号池路径
- [x] Dify仅为默认关闭的Shadow advisory Provider，不具备工具、发送、报价、订单或履约权限

## 当前源码发布前必须完成

- [ ] 在受限环境中生成并配置至少32字节的 `WANDA_PRICING_ACCOUNT_REF_KEY`，不得复用账号Token
- [ ] 执行 `cd v3-backend-gateway-work && python scripts/validate_direct_gateway_env.py --require-enabled`
- [ ] 确认账号池权限为服务账号可读、不可组写、其他用户无权限，并确认预检至少发现1个可用W+账号
- [x] 已在源码提交 `7b809e9` 使用官方registry干净安装的依赖执行插件494项测试和V3 184项测试；代码未发生变化
- [x] 官方registry执行 `npm audit --omit=dev --json` 成功：生产依赖漏洞总数0；审计响应SHA-256为 `08886336e9ac4c3334d9e199091490029d90496fed849454723ebd7dbc3ceb6d`
- [x] 已为源码提交 `7b809e91d850b76a0c40207ec1d5b2ba81701a1d` 生成并验证确定性V3和插件候选包及独立SHA-256；本地候选目录为 `dist/release-candidates/7b809e91d850`，尚未上传或部署
- [ ] 在服务器创建独立、不可变、可回滚的V3和插件release目录；不得覆盖当前生产release
- [ ] 部署后执行 `python deploy/verify_runtime_contracts.py --v3-health-url http://127.0.0.1:8011/health --plugin-health-url http://127.0.0.1:<插件端口>/healthz`，并验证systemd `active`、`NRestarts=0`、WorkingDirectory和错误日志
- [ ] 使用只读官方座位请求进行smoke；临时试价只能使用预先批准的受控场次，并必须确认取消和座位恢复
- [ ] 确认 `conversation_agent_mode=shadow`、`execution_owner=deterministic`、Active 0%、Canary关闭且kill switch开启
- [ ] 为当前V28 Runtime重新采集至少100轮自动安全审计和100轮图片样本；旧版本样本不得补门槛
- [ ] 完成当前release回滚演练并记录恢复后的WorkingDirectory、健康状态和队列恢复结果

## 当前候选包证据

- V3 SHA-256：`c3a549da5ea9e44348f9125896c26e3b063f1a392bccf33f93c5137aae1e9e0c`
- 插件 SHA-256：`03ba36f783cf54c533c9d9bdcfb79d01a503df68892c0b3b40cffb62aa9c86ae`
- Manifest SHA-256：`b97928f8124d24936b8c46dfb061ef73a0b426b12bab82dfe38c8d022e215bff`
- 构建命令：`python deploy/build_release_bundles.py`
- 校验命令：`python deploy/verify_release_bundles.py dist/release-candidates/7b809e91d850`

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
