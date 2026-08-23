# 当前 main 上线审核清单

> 当前生产已部署源码候选 `336f8c2fb27a8d4b2eaae8ac09aaa0903e4a71a8`：V3契约为 `wanda-v3-v11-pricing-account-evidence`，插件Runtime仍为 `wanda-agent-runtime-v31-release-fence-cleanup`。本次只拆分报价客户端内部职责，不改变Agent工具或交易行为；生产继续保持Shadow、确定性执行Owner和Active 0%。

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
- [x] 当前源码插件测试 491 项、V3 后端测试 191 项通过；新增11项模块边界测试覆盖文字事实、识图融合、失败映射和回复展示
- [x] 每次新增Agent、识图或核价能力必须回归：首次图片报价、有效报价后问价、文字座位、确认、订单创建/付款、人工接管、平台系统消息；禁止有效报价后的普通追问重新识图或重复临时试价
- [x] 历史0.6.0审核包曾在干净目录执行 `npm ci --ignore-scripts && npm test` 通过
- [x] 历史0.6.0审核包官方 npm registry 生产依赖审计为0漏洞
- [x] 历史生产 release 的健康检查与回滚已验证
- [x] 万达官方直连具备创建状态、取消状态、实时座位释放、15/30秒后台复核、租约续期及最多3账号安全轮转契约
- [x] 直接报价使用独立HMAC密钥生成 `pricing_account_ref`，并在报价确认和平台改价前强制校验证据
- [x] 官方直连部署预检不会输出密钥、Token、手机号、账号标识或账号池路径
- [x] 生产已移除未启用的外部Dify advisory Provider；V31使用内部主Agent语义抽取与确定性交易引擎
- [x] 自然长句优先由版本化AI语义抽取器生成严格有界事实，正则只作失败降级；图片座位与价格证据不受文字模型覆盖
- [x] 明确城市成为V3联合匹配硬边界，跨城市影院缓存候选在读取实时座位前失败关闭
- [x] 生产语义Smoke已将“济南世贸万达影城今日12:35奥德赛这两个位置”提取为济南、影院、影片、绝对日期、时间、2张和图片指代
- [x] 生产只读场次Smoke已将截图“世茂杜比影院店”规范为“济南万达影城世茂广场店”，未调用临时试价
- [x] 买家3434436058会话审计确认失败仅表示试价座位未在0/2/5秒内恢复，不表示整场会员座售罄；随后只读查询确认8排有6个W+可选座位
- [x] 同场次试价现在跨账号串行；任一座位组仍在15/30秒后台释放复核时，该场后续请求在读取账号池和创建订单前失败关闭
- [x] 已删除无生产引用的旧插件本地设置Store和旧本地视觉Client，并新增当前架构文档

## 当前源码发布前必须完成

- [x] 已在受限生产环境中生成并配置至少32字节的 `WANDA_PRICING_ACCOUNT_REF_KEY`，未输出或复用账号Token
- [x] 已使用生产环境执行 `scripts/validate_direct_gateway_env.py --require-enabled`，结果为 `ready`，识别15个合格账号
- [x] 账号池权限为 `640 ticket-system:ticket-system`，预检确认文件类型和权限安全
- [x] 已在源码提交 `336f8c2` 使用干净依赖执行插件491项测试和V3 191项测试；生产release再次执行插件491项测试通过
- [x] 官方registry执行 `npm audit --omit=dev --json` 成功：生产依赖漏洞总数0；审计响应SHA-256为 `08886336e9ac4c3334d9e199091490029d90496fed849454723ebd7dbc3ceb6d`
- [x] 已为源码提交 `336f8c2fb27a8d4b2eaae8ac09aaa0903e4a71a8` 生成并验证确定性V3和插件候选包及独立SHA-256；本地候选目录为 `dist/release-candidates/336f8c2fb27a`
- [x] 已在服务器创建独立V3和插件release目录，未覆盖V10/V22历史release
- [x] 部署后Runtime契约校验为 `ready`；两个systemd服务均为 `active`、`NRestarts=0`，WorkingDirectory准确且最近warning日志为空
- [x] 已对牡丹江万达广场店17:15场次执行只读W+座位Smoke，8排返回6个实时可选座位；未创建临时订单
- [x] 已确认 `conversation_agent_mode=shadow`、`execution_owner=deterministic`、`conversation_agent_active_ready=false`、Active 0%、Canary关闭且kill switch开启
- [ ] V31重新采集至少100轮自动安全审计和100轮图片样本；V30及更旧版本样本不得补门槛
- [x] 已完成报价模块化release到前一V31 release回滚及恢复演练；两端健康、WorkingDirectory和队列恢复通过
- [x] 已修复候选包遗漏vendor SDK运行时的问题，并将SDK `dist/index.js`设为构建强制文件；首次失败切换已自动回滚，无买家交易状态迁移
- [x] 插件systemd沙箱已显式允许写入当前 `DATA_DIR=/var/lib/ticket-system/wanda-ai-plugin-data`，V28历史评测队列已恢复持久化

## 当前生产候选包证据

- V3 SHA-256：`2535dad2a0f6ece53fa48081ac1063c305f0a0f7d36f85de8b2b8057c9f22bc9`
- 插件 SHA-256：`b66ef64facc4b090828cb440c93465eadcc40d5dfa9bb8288ccd15dc5bc9a6d1`
- Manifest SHA-256：`f15995cb6c45a73666669615f6e6ff6d29282195eb262d192eb7c4c7ea29813a`
- 构建命令：`python deploy/build_release_bundles.py`
- 校验命令：`python deploy/verify_release_bundles.py dist/release-candidates/336f8c2fb27a`
- 生产V3：`/opt/wanda-v3-backend/releases/v11-pricing-account-evidence-336f8c2fb27a`
- 生产插件：`/opt/wanda-preview-plugin/releases/v31-quote-modules-336f8c2fb27a`

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
