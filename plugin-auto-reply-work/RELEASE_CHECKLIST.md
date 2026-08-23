# 当前 main 上线审核清单

> 当前生产V3来自 `63ee4fc01afbfe1c4d002236ae4eb5a87498e1a7`，插件功能release来自 `7293022b9f5d08e18a07e54ab89a72ebb347c395`：V3契约为 `wanda-v3-v16-vision-consistency-gates`，插件Runtime为 `wanda-agent-runtime-v33-shadow-evaluation-switch`。V16使用图像字节、视觉版本、模型配置和当日系统Prompt的内容哈希持久化纯图片事实，同图跨会话及并发只调用一次模型；不向视觉模型传买家文字或会话身份，不缓存报价、库存或订单。生产Smoke确认真实第8排红圈被保留、猫眼缩略图红框被排除，第二次同图从4.2秒降至约71毫秒且结果完全一致。自动报价和确定性待付款改价均保持开启；自由生成AI回复保持关闭，独立只读Shadow评测继续积累事件时点样本。

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
- [x] 当前源码插件测试 513 项、V3 后端测试 211 项通过；新增事件时点快照、未来状态隔离、人工卖家历史、上下文确认推理、纯文字W+查询、全排座位、候选序号、文字位置偏好、Shadow评测/AI发送开关分离及运营工作台模型测试
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
- [x] 已在源码提交 `63ee4fc` 执行插件513项测试和V3 211项测试；生产V3完成编译、健康、同图跨会话缓存、真假红圈识别Smoke和实际回滚恢复
- [x] 官方registry执行 `npm audit --omit=dev --json` 成功：生产依赖漏洞总数0；审计响应SHA-256为 `08886336e9ac4c3334d9e199091490029d90496fed849454723ebd7dbc3ceb6d`
- [x] 已为源码提交 `63ee4fc01afbfe1c4d002236ae4eb5a87498e1a7` 生成并验证确定性V3和插件候选包及独立SHA-256；本地候选目录为 `dist/release-candidates/63ee4fc01afb`
- [x] 已在服务器创建独立V3和插件release目录，未覆盖历史release
- [x] 新运营工作台以 `/ui/workbench` 隔离上线；Playwright使用本机Edge完成桌面、390px移动端、状态卡、队列筛选、样本门槛及横向溢出E2E，生产静态资源Smoke和实际回滚恢复通过
- [x] 运营工作台完成视觉重构：简化中文层级、统一蓝灰状态体系、压缩移动端长度、增加一致图标和响应式双栏；桌面/移动截图检查及实际回滚恢复通过
- [x] `/ui`是唯一工作台页面；`/ui/settings`和`/ui/workbench`均已移除且返回404，不存在新版/旧版或第二后台入口
- [x] 工作台CSS/JS固定解析到插件`/ui`挂载目录，已补`/__plugin__/ui`浏览器E2E，避免鱼麦多网关把相对资源请求错误发送到插件挂载目录之外
- [x] 人工模式不再把人工回复、人工报价、正常付款/履约或缺少自动报价当作异常；生产745条历史操作和198条订单只留下近24小时2条有明确张数冲突证据的风险
- [x] `quote_enabled=false`或`ai_reply_enabled=false`时不再发送“正在核对实时场次和优惠”；独立开关回归、生产release测试及实际回滚恢复通过
- [x] 部署后Runtime契约校验为 `ready`；两个systemd服务均为 `active`、`NRestarts=0`，WorkingDirectory准确且最近warning日志为空
- [x] 已对牡丹江万达广场店17:15场次执行只读W+座位Smoke，8排返回6个实时可选座位；未创建临时订单
- [x] 当前生产设置为 `automation_enabled=true`、`recognition_enabled=true`、`quote_enabled=true`、`auto_price_change=true`、`ai_reply_enabled=false`、`shadow_evaluation_enabled=true`；新报价与待付款改价均由确定性交易链路执行；`conversation_agent_mode=shadow`、`execution_owner=deterministic`、Active 0%、Canary关闭且kill switch开启
- [ ] V33重新采集至少100轮自动安全审计和100轮图片样本；V32及更旧版本样本不得补门槛
- [x] 已完成单座优惠修复release到前一V31 release回滚及恢复演练；两端健康、WorkingDirectory和队列恢复通过
- [x] V32生产只读Smoke把“泉州晋江万达今天15:50奥德赛还有W座位吗”规划为 `resolve_ticket_identity → show_available_wplus_seats`，官方唯一匹配晋江万达广场激光IMAX店并返回7排9座、7排10座；未创建临时试价订单
- [x] 已完成V32到前一V31 release的实际回滚及恢复演练；恢复后V12/V32契约匹配、两服务active且NRestarts=0
- [x] 已修复候选包遗漏vendor SDK运行时的问题，并将SDK `dist/index.js`设为构建强制文件；首次失败切换已自动回滚，无买家交易状态迁移
- [x] 插件systemd沙箱已显式允许写入当前 `DATA_DIR=/var/lib/ticket-system/wanda-ai-plugin-data`，V28历史评测队列已恢复持久化

## 当前生产候选包证据

- V3 SHA-256：`26ad7a6b00c2d53d0610727e36db806468c22ce4cf06871e2588db05bf9626d2`
- 插件 SHA-256：`c20938df9c9035e83663dec159d3dc22776d144d4de2c6c27d99c067d09210ef`
- Manifest SHA-256：`fcac72d994d59a8ab1ba2d2e9a5bd1e9cc870dee92c756fb5603c067575ebe11`
- 构建命令：`python deploy/build_release_bundles.py`
- 校验命令：`python deploy/verify_release_bundles.py dist/release-candidates/63ee4fc01afb`
- 生产V3：`/opt/wanda-v3-backend/releases/v16-vision-consistency-63ee4fc01afb`
- 生产插件：`/opt/wanda-preview-plugin/releases/v33-one-ui-page-7293022b9f5d`

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
