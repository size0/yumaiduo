# 万达电影票规则主导交易与AI辅助识别

## 本地目录结构与仓库归属

鱼麦多插件统一放在本目录，出票系统单独维护，不能混用仓库：

```text
E:\鱼麦多\v4\
├─ backend\                              V4 业务后端（Python/FastAPI）
├─ frontend\v4\                         V4 管理页面
├─ plugin-runtime\wanda-seat-autoquote\  鱼麦多插件运行时与插件页面
├─ data\                                 本地数据，不提交
├─ logs\                                 本地日志，不提交
├─ deploy\                               部署脚本
└─ start.ps1                             本地启动脚本
```

仓库规则：

- 鱼麦多插件：`https://github.com/size0/yumaiduo.git`
- 出票系统：`https://github.com/size0/wd.git`
- 插件代码禁止推送到 `wd.git`；`wd.git` 只用于出票系统。
- `E:\票务系统\plugins\wanda-seat-autoquote` 是迁移期间的旧副本，后续不在两处同时修改。

## 每次提交与 GitHub 同步流程

每次修改完成后必须按顺序执行：

1. 检查工作区状态和本地提交：`git status`、`git log --oneline`。
2. `git fetch` 对应远端，检查其他 AI 的新提交和分支；不盲目覆盖或合并。
3. 运行相关测试，并执行 `git diff --check`。
4. 只提交确认过的文件，写清楚提交内容。
5. 推送到 `yumaiduo.git` 的对应分支；远端有新提交或无法安全快进时停止，先人工确认。
6. 记录提交哈希；需要发布时，再从已同步提交打包部署。

不得把密钥、账号池、Cookie、真实订单数据、数据库、日志或生产配置提交到 GitHub。

唯一交易规范见 `E:\鱼麦多\V3\docs\RULES_FIRST_TRANSACTION_PLAN.md`。规则状态机独占交易决策与写授权；AI仅提供识图、字段、FAQ和低风险表达候选，关闭后可通过结构化文字继续固定交易流程。

本服务支持：

- 浅青色管理台与顶部横向导航：客服工作台、店铺开关、回复话术、运营报价、模型接口、知识库、安全门禁、运行日志
- 左右客服对话界面：买家消息在右、AI 回复在左
- 对话区拖拽/点击/粘贴图片，识别完成后自动回复
- 规则主导客服：同一会话持久归约事件、权威订单状态、结构化识图候选和官方报价
- 保留独立的 `multipart/form-data` 识图 API
- 本地图片命令行识别
- 影院、影片、日期场次、影厅、语言制式、可见已选座、画面金额与价格区域提取
- 混合图片处理：千问快速直出；格式异常、低置信度或关键字段为空时才调用 AI 判断模型兜底
- 会话级多图合并：同一 conversation_id 最近 3 张结果在 30 分钟内可安全补全
- W+会员锁座报价：固定登录账号临时锁座读取会员专享优惠，取消并确认座位恢复后才返回金额
- 图片签名/大小校验、稳定错误码
- 默认每个客户端每分钟 20 次识图限制及基础浏览器安全响应头

> 截图价格仅作为画面原文提取，不代表实时库存、优惠或最终销售报价。

座位展示规则：底部官方卡片明确出现“X排Y座”时显示具体座位；底部只有“请选择座位/推荐座位”而没有具体座位时，不从手绘圈或座位图猜座位号，统一显示“W+座位”。

## 安装与启动

```powershell
cd "E:\鱼麦多\v4"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r .\backend\requirements.txt
.\start.ps1
```

脚本会自动打开前端 <http://127.0.0.1:8000>；接口文档位于 <http://127.0.0.1:8000/docs>。首次启动后从顶部导航进入“模型接口”，填写 API Key 并保存。以后直接运行 `.\start.ps1`，不需要再次输入密钥；聊天、报价、知识、安全门禁和诊断日志也从顶部独立页签进入。

设置页持久化：

- API Key：通过当前 Windows 用户的 DPAPI 加密保存
- 识图接口与 AI 判断/回复接口：各自独立保存 Base URL、DPAPI 加密 Key 和模型
- Qwen 思考模式：默认关闭，可随时切换
- GPT 推理强度：默认最轻，持久化 `none/minimal/low/medium/high`
- 识图与兜底提示词：千问快速路径和 AI 兜底路径共用严格业务契约
- 文字客服提示词：独立持久化，纯文字消息会调用 AI 回复模型
- 运营与报价：独立页签管理整数分报价规则、规则试算、版本和强制安全门禁
- 接口地址：可编辑
- 万达官方直连：复用票务系统已登录的固定账号，无额外报价 Key 或租户 ID
- 模型列表：使用当前输入或已保存的 Key 调用兼容 `/v1/models` 接口动态获取

两套接口都可在设置页单独填入兼容服务地址和 Key，并分别获取 `/v1/models`。Airelvo 预设为 `https://airelvo.cc/v1`。视觉模型必须支持图片输入；AI 判断/回复模型只需支持文本和 JSON 输出。两套 Key 分别通过 DPAPI 加密，互不覆盖。

图片首先由视觉模型直接生成最终结构。Schema 合法、置信度至少 0.55 且存在影院/影片/场次/影厅之一时立即返回；否则才把快速结果、买家附言和近期同会话结果发送给 AI 判断模型修正。正常图片只调用一次模型，异常图片才串行调用两次。

同一 `conversation_id` 的最近 3 个识别结果会在进程内保留 30 分钟，适合用“排片图 + 选座图”补全同一订单。只有影片、场次、明确月日等信息不冲突且至少存在一个匹配信号时才补全缺失字段；不同影片或不同明确日期不会合并。AI 客服同时保留最近 10 条文字消息和最近 3 份结构化图片/报价上下文，因此可以回答“多少钱”“是哪家影院”等承接问题。只保存结构化结果和有限文字，不保存原图、Base64、万达凭据或模型 Key，重启后自动清空。相对日期会与当前日期和星期校验，旧截图、已过期场次不会进入官方锁座报价。

图片识别后，后端从票务系统账号池读取固定在线 W+ 账号。系统先调用万达官方场次与实时座位接口，从目标 W+ 区域的 `wPlusActivity.price` 读取会员活动价；存在有效会员活动价时直接作为计价基准，全程只读。仅当实时座位未显示会员活动价时，系统才在同一 W+ 区域选择实时可售座位创建临时探针订单，通过会员活动接口读取唯一可用的 `W+会员专享优惠`，随后立即取消订单；只有订单状态确认取消且座位重新恢复可售时才返回报价，否则失败关闭。最终金额按整数分应用版本化确定性加价与取整规则。千问、GPT 和截图金额均不能成为权威报价来源，正式支付金额仍以最终订单为准。

思考配置按模型转换：

- Qwen：使用 `enable_thinking=false/true` 开关
- GPT-4.1、GPT-4o 等非显式推理模型：不发送推理参数
- GPT-5.1/5.5 等：使用推理强度选择，默认 `reasoning_effort=none`
- 旧 GPT-5：选择 `none` 时自动降级为其支持的最低 `minimal`
- 可选强度：`none`、`minimal`、`low`、`medium`、`high`

设置保存后，下一张图片立即使用新配置，不需要重启。配置文件位于 `data/vision-settings.json`，其中不含明文密钥。

不希望自动打开浏览器时使用 `.\start.ps1 -NoBrowser`。环境变量仍可作为尚未保存页面设置时的临时回退：

也可以自行设置环境变量：

```powershell
$env:DASHSCOPE_API_KEY = "你的新 Key"
$env:DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
$env:DASHSCOPE_MODEL = "qwen3.5-flash-2026-02-23"      # 视觉模型
$env:DASHSCOPE_CHAT_API_KEY = "AI 回复接口 Key"
$env:DASHSCOPE_CHAT_BASE_URL = "https://airelvo.cc/v1"
$env:DASHSCOPE_CHAT_MODEL = "gpt-5.5"                   # AI 判断/回复模型
$env:WANDA_ACCOUNT_POOL_PATH = "E:/票务系统/backend/data/accounts.json"
$env:WANDA_CINEMA_CACHE_PATH = "E:/票务系统/backend/data/cinema_cache.sqlite"
$env:WANDA_FIXED_ACCOUNT_PHONE = "固定登录账号手机号"  # 留空则进程启动后固定使用首个在线账号
$env:WANDA_REQUEST_TIMEOUT_SECONDS = "12"
$env:LIANGPIAO_BASE_URL = "https://portal-web-v3.liangpiao.net.cn"
$env:LIANGPIAO_APP_KEY = "良票 AppKey"
$env:LIANGPIAO_APP_SECRET = "良票 AppSecret"
$env:LIANGPIAO_REQUEST_TIMEOUT_SECONDS = "20"
$env:WANDA_PRICING_RULES_PATH = "data/pricing-rules.json"
$env:RECOGNITION_RATE_LIMIT_PER_MINUTE = "20"
$env:PYTHONPATH = "E:\鱼麦多\v4\backend"
python -m uvicorn app.main:app --app-dir "E:\鱼麦多\v4\backend" --host 127.0.0.1 --port 8000
```

## 命令行识别

命令行会读取环境变量；网页识图优先读取持久化设置：

```powershell
python -m app.cli "C:\path\to\ticket.jpg"
```

## 动态获取模型 API

```http
POST /api/settings/vision/models
Content-Type: application/json

{
  "base_url": "https://airelvo.cc/v1",
  "api_key": null
}
```

`api_key=null` 时使用已通过设置页加密保存的 Key；请求中的 Key 不会写入日志。

## 运营报价设置 API

```http
GET /api/settings/operations
PUT /api/settings/operations
```

报价参数全部使用整数分并接受严格范围校验。规则默认关闭；保存后下一次核价立即生效，无需重启。持久化文件默认是 `data/pricing-rules.json`。

## 聊天 API

图片消息（当前会自动识图并返回左侧 AI 回复）：

```bash
curl -X POST http://127.0.0.1:8000/api/chat/image-messages \
  -F "conversation_id=demo-chat" \
  -F "message_text=帮我看看这个场次" \
  -F "image=@ticket.jpg"
```

文本消息（已配置 Key 时调用当前模型生成客服回复；无 Key 时返回安全引导）：

```bash
curl -X POST http://127.0.0.1:8000/api/chat/text-messages \
  -H "Content-Type: application/json" \
  -d '{"conversation_id":"demo-chat","text":"你好，怎么买票？"}'
```

原始识图接口继续兼容：

```bash
curl -X POST http://127.0.0.1:8000/api/movie-images/recognize \
  -F "image=@ticket.jpg"
```

成功响应：

```json
{
  "ok": true,
  "data": {
    "cinema_name": "万达影城…",
    "movie_name": "奥德赛",
    "showtime_start": "22:40",
    "selected_seats": [],
    "displayed_total": 413.4,
    "confidence": 0.95
  }
}
```

完整接口文档：<http://127.0.0.1:8000/docs>。

AI 客服已接入 `/api/chat/text-messages`，并按 `conversation_id` 使用有界、30分钟过期的内存上下文；前端聊天协议无需改变。

## 鱼麦多自动改价

插件桥接采用唯一规则主链：事件接口只持久接收，规则worker归约后写入SQLite WAL命令Outbox，插件通过60秒租约领取并执行命令。不存在生产Shadow或legacy回退。AI辅助与外部写操作分别由 `WANDA_AI_ASSIST_ENABLED`、`WANDA_EXTERNAL_WRITES_ENABLED` 控制。

人工任务领取只用于异常核验：客服提交`operator_id`后获得默认15分钟排他租约，防止多人同时处理。正常出票不需要领取或上传图片；支付金额复核通过后，卖家直接在聊天中发送取票信息，再在现有订单管理点击发货，系统以官方已发货事件完成履约状态。

“店铺开关”按租户保存每家店的自动回复与自动改价开关；“回复话术”可以编辑识图、精确报价、区域报价、报价失败、上传引导和改价成功通知。模板变量统一使用中文名称，例如 `{影片}`、`{影院}`、`{座位}`、`{报价内容}`、`{失败原因}`、`{订单金额}`。自动识图回复默认不展示语言/制式和截图金额；截断影院名只有在官方影院缓存中唯一匹配时才补全，无法唯一确认时继续失败关闭。

Node执行器在改价前再次读取订单和会话，拒绝身份不一致、已付款、已关闭、退款中或人工/买家新消息介入的订单；调用鱼麦多改价后必须重新读取订单并确认金额一致，才会发送“改价完成”通知。超时结果不会盲目重试，而是依靠持久化幂等收据重新读取对账。区域探针、截图金额、AI生成金额和不完整报价均不能触发改价。

报价按座位类型逐座计算：周五会员日开关开启时，优先使用万达实时返回的W+周五活动价（活动价缺失则回退官方普通会员价）；关闭时不使用该活动价。普通座随后叠加普通座调整（默认+¥1.00）；物理W+座随后应用会员价阈值规则，会员价不高于¥60时=`max(实时原价-¥2.90,会员价)`，高于¥60时直接使用会员价。单价按¥0.10轮整且不得低于会员成本或高于实时原价；混合座区分别计算后求和，channelFee只记录不计价。

## 日志与耗时

控制台和 `logs/app.log` 会记录脱敏的请求阶段、模型调用状态与毫秒耗时，不记录图片、密钥、Authorization 请求头或模型原文。

另开一个 PowerShell 窗口实时查看：

```powershell
cd "E:\鱼麦多\v4"
Get-Content .\logs\app.log -Wait
```

常用事件：

- `request_started`：后端收到请求
- `vision_started`：开始处理图片
- `vision_provider_started`：开始调用百炼模型
- `vision_provider_completed`：百炼返回及其耗时
- `vision_completed`：结构化识别完成及总耗时
- `vision_failed`：脱敏失败代码

浏览器响应还包含 `X-Request-ID` 和 `Server-Timing`，前端识别气泡会实时显示等待秒数。

聊天页面底部提供“调试日志与完整返回参数”面板，格式化展示：

- 请求 ID、接口、状态和总耗时
- 百炼状态码、响应头与完整响应 JSON
- 模型 `choices[].message.content` 原文
- JSON 解析结果
- Pydantic Schema 校验字段、错误类型、错误输入
- Token usage 与百炼响应中的其他字段

完整模型返回只保存在有界进程内存中，不写入 `logs/app.log`，重启服务自动清空。也可通过 `GET /api/diagnostics/recent` 查看或用 `DELETE /api/diagnostics/recent` 清空。

## 测试

```powershell
cd .\backend
python -m pytest -q
```

测试使用模拟模型响应，不消耗百炼额度，也不会读取真实密钥。

## 安全说明

- 网页保存的真实密钥使用 Windows DPAPI 加密，只能由保存它的同一 Windows 用户解密；也可临时通过 `DASHSCOPE_API_KEY` 注入。
- `data/`、`.env` 均已被 Git 忽略，不提交配置与密钥。
- 不记录图片、Base64 内容、Authorization 请求头或上游错误正文。
- 支持 JPG、PNG、WebP，默认最大 10 MB。
- 启动脚本只监听 `127.0.0.1`；如需暴露到公网，应在反向代理层增加 HTTPS、身份认证和持久化限流。
- 你曾在聊天中发送过一个明文 Key，建议在阿里云控制台立即轮换后再使用本项目。

 当前生产插件不是直接覆盖上传，而是“发布目录 + current 软链接”方式部署。

 当前插件目录

 服务器：

 ```text
   ubuntu@124.220.29.179
 ```

 当前生效插件：

 ```text
   /opt/wanda-seat-autoquote/current
 ```

 实际指向：

 ```text
   /opt/wanda-seat-autoquote/releases/plugin-2.2.49-date-seat-fix-20260828-1045
 ```

 历史版本也保留在：

 ```text
   /opt/wanda-seat-autoquote/releases/
 ```

 当前插件版本：

 ```text
   2.2.49
 ```

 插件服务

 服务名：

 ```text
   wanda-seat-autoquote
 ```

 启动入口：

 ```text
   /opt/wanda-seat-autoquote/current/index.mjs
 ```

 查看配置：

 ```bash
   sudo systemctl cat wanda-seat-autoquote
 ```

 插件监听端口：

 ```text
   127.0.0.1:4003
 ```

 插件数据和环境配置

 运行数据：

 ```text
   /var/lib/ticket-system/wanda-ai-plugin-data
 ```

 环境配置：

 ```text
   /etc/ticket-system/wanda-seat-autoquote.env
 ```

 环境文件不放在代码发布目录中，里面的密钥不能提交到仓库或输出。

 上传部署流程

 本地插件源码目录：

 ```text
   E:\票务系统\plugins\wanda-seat-autoquote
 ```

 通常流程是：

 ```text
   本地打包插件
   → scp 上传到服务器 /tmp
   → 解压到 /opt/wanda-seat-autoquote/releases/<版本目录>
   → npm ci --omit=dev
   → 切换 current 软链接
   → 重启 wanda-seat-autoquote
   → 检查端口和服务状态
 ```

 不会直接修改：

 ```text
   /opt/wanda-seat-autoquote/current
 ```

 这样可以保留旧版本，出现问题时切回旧的 current 软链接。

 另外，基础发布包里还存在：

 ```text
   /opt/ticket-system/current/plugins/wanda-seat-autoquote
 ```

 但当前 systemd 配置通过 override 实际使用的是：

 ```text
   /opt/wanda-seat-autoquote/current
 ```

 因此以 /opt/wanda-seat-autoquote/current 为准。

