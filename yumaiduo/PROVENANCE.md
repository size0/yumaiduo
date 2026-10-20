# yumaiduo/ —— 鱼麦多插件（拷贝自 V5）

## ⚠️ 先读这段

这个目录里的代码**不是我自己写的**，是从 V5 拷过来的。

**这是经你明确授权的一次例外**：
> 「这里你可以抄 V5 的」+ 范围「只限鱼麦多插件这一块」+ 方式「拷进来 + 注明出处」

**其余部分仍然禁止从 V5 拷贝**（良票接入、报价规则、状态机、文案、前端 —— 全部自己写）。
`tests/_audit_v5_taint.py` 会持续检查，例外清单写在那个文件里。

---

## 出处（精确到文件）

| 项 | 值 |
|---|---|
| 来源目录 | `E:\200行的agent\v5-prod-9bee216\plugin-runtime\` |
| 拷贝时间 | 见下方文件清单（`_copy_plugin.py` 一次性执行） |
| 文件数 | 39 个（已排除 `node_modules/`） |
| 总大小 | 169,057 B |
| 快照标识 | `v5-prod-9bee216` |
| `.git` 指向 | `C:/Users/13250/v5-prod-snapshot/20261003-112920/.git/worktrees/v5-prod-9bee216` |
| `source_aggregate` | `9eb03aa2620eca64da9aff1ca1dabd5f581a85009d4d2b0ab70010bc34c886fc`（见 `BUILD_INFO.json`） |
| 插件版本 | `0.2.0-rc.1`（`yumaiduo.plugin.json` / `BUILD_INFO.json`） |

**校验方式**：`BUILD_INFO.json` 里有每个文件的 sha256（含 `node_modules` 的）。
比对它就能确认拷过来的文件与 V5 一致 —— 这也是为什么它一起拷过来了。

---

## ★★ 拷贝之后的【有意改动】（与 BUILD_INFO 哈希不再一致，这里是清单）

拷过来之后我改了几个文件。**记在这里，免得以后有人比对哈希时以为是被污染的。**

| 文件 | 改了什么 | 为什么 |
|---|---|---|
| `src/server.mjs` | `/healthz` 增加 `registered` / `registration` 字段 | 官方 §14 要求 `{ ok:true, registered:true }`；原来只回 `{ok,mode}`，**注册失败看不出来**。★ 官方还特意警告："未签 DPA 的插件重启后会失能（验不了签、调不了接口），**且进程不会退出，健康检查大概率发现不了**" —— 这个字段就是用来发现它的 |
| `src/server.mjs` | 注册结果写入 `cfg.registration` | 同上 —— 原来只 `console.error` 一行，容易被漏掉 |
| `src/server.mjs` | `server.listen(cfg.port, process.env.HOST \|\| undefined)` | 官方 §14 要求生产 `HOST=0.0.0.0`；原来没绑 host |

**没改的**（仍然是 V5 原样）：其余所有 `src/*.mjs`、`vendor/`、`test/`、
以及 **`yumaiduo.plugin.json` 全部字段（`id` / `vendor` / `version` 都没动）**。
**未改动的文件仍可用 `BUILD_INFO.json` 的 sha256 复核。**

### ★ `id` 与 `vendor` 保持与线上一致 —— 这是有意的

```
线上真实插件：  id=wanda-seat-autoquote   vendor=xdl   2.2.51
我们这份：      id=wanda-seat-autoquote   vendor=xdl   0.2.0-rc.1
```

**同一个 id 是对的**：我们的目标是**替换**线上那个插件，不是再注册一个新插件。
官方 §5.1：「重复注册同 `id` 为**幂等更新**（覆盖凭证、按最新 manifest 同步配额定义）」
—— 这就是"换一份实现上去"的正确方式。

★ 代价（必须知道）：**同一个 id 同时只有一个进程能收事件**
（官方 §9.3：webhook 只投给已注册的那个进程）。所以替换时必须
**先停旧进程再起新进程**，不能两个同时跑 —— 否则
两者的 `webhookSecret` 会互相覆盖，事件也会被其中一个吃掉。


---

## 文件清单

### 入口与配置
| 文件 | 说明 |
|---|---|
| `yumaiduo.plugin.json` | 插件 manifest。**manfiest 里的 vendor 是 `xdl`，id 是 `wanda-seat-autoquote`** |
| `package.json` | `type: module`，`engines.node >=22`，依赖 `json-bigint@1.0.0` |
| `BUILD_INFO.json` | 构建信息 + 全文件 sha256 |
| `package-lock.json` | 锁文件 |

### src/ —— 插件本体
| 文件 | 说明 |
|---|---|
| `server.mjs` | HTTP 入口。healthz、webhook 验签、两个 `/internal/*` 端点 |
| `register.mjs` | 自注册（读 `../yumaiduo.plugin.json`） |
| `backend-request.mjs` | 回调 V5 后端的 HTTP 封装 |
| `execute-command.mjs` | 命令分发（把后端下发的 command 分给各处理器） |
| `execution-journal.mjs` | 执行日志 + 恢复（`recovery_required`） |
| `execution-result.mjs` | 外部写入的结果分类（`refused` / `unknown`） |
| `reprice.mjs` | 改价（188 行，最大的一个） |
| `ticket-delivery.mjs` | 发出票 |
| `wanda-shipment.mjs` | 万达发货 |
| `failed-order-close.mjs` | 失败关单 |
| `product-polish.mjs` | 商品润色 |
| `order-engagement.mjs` | 订单互动 |
| `order-event.mjs` | 订单事件 |
| `message-history.mjs` | 读官方消息历史 |
| `reply-image.mjs` | 回复配图上传 |
| `shops.mjs` | 读店铺 |
| `ui.mjs` | 内嵌 UI（`serveUi`） |

### test/
| 文件 | 说明 |
|---|---|
| `reprice.test.mjs` | 221 行，**用 `node --test` 跑，不需要平台** |

### vendor/plugin-sdk-server/
鱼麦多官方 SDK 的运行时副本（`@yumaiduo/plugin-sdk-server` v0.19.0）。
`dist/*.js` 是 CommonJS，`package.json` 声明 `main: ./dist/index.js`，依赖 `json-bigint`。

---

## 怎么跑

```bash
cd yumaiduo/plugin-runtime

# 1) 装唯一的外部依赖（SDK 用它做大整数安全解析）
npm install

# 2) ★ 插件自带的测试 —— 不依赖平台、不需要令牌
npm test        # = node --test test/*.test.mjs

# 3) 以 shadow 模式启动（★ 不联系平台）
node src/server.mjs
curl http://127.0.0.1:4015/healthz
```

### 两种模式（源码里的原文）

```
server.mjs:14  mode = process.env.YUMAIDUO_COMMAND_MODE || 'shadow'
server.mjs:15  必须是 shadow 或 live
server.mjs:23  // Registration is opt-in; local Shadow runs never contact the platform.
server.mjs:24  只有 YUMAIDUO_AUTO_REGISTER==='true' 才自注册
```

| 模式 | 行为 |
|---|---|
| `shadow`（默认） | **不联系平台**，不自注册 |
| `live` | 联系平台；启动时强制要求 `PLUGIN_EXECUTION_JOURNAL_DIR` 是绝对路径 |

**★ 这正是「本地怎么测」的答案**：shadow 模式下插件完全不碰平台。

---

## 环境变量（从源码里读出来的，不是文档抄的）

| 变量 | 默认值 | 出处 |
|---|---|---|
| `PORT` | `4015` | `server.mjs:14` |
| `YUMAIDUO_COMMAND_MODE` | `shadow` | `server.mjs:14` |
| `V5_BACKEND_URL` | `http://127.0.0.1:8000` | `server.mjs:14` |
| `YUMAIDUO_WEBHOOK_SECRET` | 空 | `server.mjs:14` |
| `V5_BACKEND_TIMEOUT_MS` | `15000` | `server.mjs:18` |
| `PLUGIN_EXECUTION_JOURNAL_DIR` | `.v5-execution-journal` | `server.mjs:21` |
| `CORE_URL` / `PLUGIN_DEVELOPER_TOKEN` / `BASE_URL` | 无 | `server.mjs:24`（自注册才需要） |
| `YUMAIDUO_PLUGIN_TOKEN` / `YUMAIDUO_TENANT_ID` | 无 | `server.mjs:22`（要调平台 API 才有用） |
| `YUMAIDUO_AUTO_REGISTER` | 未设 | `server.mjs:24` |

---

## ⚠️ 没拷的部分，以及为什么

**`backend/app/integrations/yumaiduo/`（13 个文件，183 KB）没有拷。**

原因：它**不是自包含的**，是 V5 后端的一部分。它 import 了 20+ 个 V5 内部模块：

```
runtime.py (1419 行, 96 KB) →
    app.agent.reply_guard / app.agent.context
    app.application.assistant_settings / business_template_matrix /
        continuous_input / order_replies / purchase_request /
        reply_delivery / reply_policy
    app.conversation.models / .diagnostics
    app.domain.quote.models / app.domain.recognition.outcome
    app.domain.transaction.liangpiao_order / .reprice
    app.provider_names / app.trace
    外部: httpx, pydantic
```

**拷过来会牵出一大串**，而且拷过来也只是个"接不上的仪表盘"。

而插件那边是**自包含**的：只依赖 node 内置模块 + `json-bigint`。
它通过 HTTP 跟后端说话（`backend-request.mjs` → `V5_BACKEND_URL`），
所以**我们可以自己写后端，或者把后端换成 ticket-agent**。

**要不要拷那 13 个文件，等你定。**（当时你选的"只限鱼麦多插件这一块"字面上包含了它，
但它的性质完全不同，所以我先停在插件这一半。）
