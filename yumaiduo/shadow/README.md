# shadow/ —— V5 影子测试台（重建）

## ⚠️ 先说清楚：这三个脚本是**重建的**

`.shadow-run.py` / `.shadow-plugin-mock2.py` / `.shadow-walk.py` **原文件已经不在工作区**
（四个可能的文件名全工作区搜过，都没有）。

**重建的全部依据都来自可查的代码和残留证据**，逐条列在下面。
凡是我推理出来的，都标了「我推的」。

---

## 三个脚本各干什么

| 脚本 | 位置 | 端口 | 作用 |
|---|---|---|---|
| `shadow-run.py` | **工作区根目录** | 8199 | 起 V5 后端的隔离实例（shadow 模式，**不发消息**） |
| `shadow-plugin-mock2.py` | **工作区根目录** | 4100 | 替真插件的四个 `/internal/*` 端点 |
| `shadow-walk.py` | **工作区根目录** | — | 读轮次定义，签名后灌给影子实例，读回命令和轨迹 |
| `start-shadow.py` | 本目录 | — | 一键起前两个（**我加的**，原来没有） |

> ★ 三个脚本的**本体在 `E:\200行的agent\` 根目录**，不在这个目录里 ——
> 那是它们原来的位置（`.identity-gate-交接.md:204-206` 引用的就是那个路径）。
> 这个目录只放说明和启动器。

**为什么需要 mock 插件**（`.identity-gate-交接.md:234` 原话）：

> `history_reader.py:11-28` 会往 `V5_PLUGIN_INTERNAL_URL`（默认 `127.0.0.1:4100`）
> 签名拉聊天历史。**没有 mock 插件，每条都停在 `HISTORY_UNAVAILABLE`。**

---

## 怎么跑

```powershell
cd ticket-agent\yumaiduo\shadow
D:\python\python.exe start-shadow.py          # 起 mock + 影子实例

# 另开一个终端
cd E:\200行的agent
D:\python\python.exe shadow-walk.py                          # 默认 .shadow-turns-1.json
D:\python\python.exe shadow-walk.py .shadow-turns-2.json
```

单独起也行：

```powershell
cd E:\200行的agent
D:\python\python.exe -u shadow-plugin-mock2.py 4100
D:\python\python.exe shadow-run.py --port 8199 --mode shadow
```

停止：

```powershell
Get-Process python -EA SilentlyContinue | Stop-Process -Force
```

> ★ 原来的 `.cmd` 启动器**已经删掉**，换成 `start-shadow.py`。
> 原因见下面「踩过的坑 ⑥」—— 批处理在中文路径上连续踩了四个坑。

---

## 实测结果（2026-10-11）

```
mock   :4100  {"ok":true,"mode":"shadow-mock","stats":{...}}
影子   :8199  {"ok":true,"contract":"v5-native","mode":"shadow"}

灌轮次：.shadow-turns-1.json（2 轮）
  第 1 轮 [img]  → inject status=WAITING_TAKEOVER，history +1
  第 2 轮 [city] → inject status=WAITING_TAKEOVER，history +1
                 → [SEND_MESSAGE] status=PENDING_SHADOW  正在识别你的选座截图，稍等几秒~

=== all commands produced by THIS run ===
  共 3 条，全部 status=PENDING_SHADOW（★ 一条都没发出去）
```

**完整 event trace（25 步）**：

```
YUMAIDUO_EVENT_RECEIVED → EVENT_VALIDATED → EVENT_NORMALIZED → MESSAGE_CLASSIFIED
→ ORDER_STATE:NONE → PLATFORM_HISTORY_SYNCED → SESSION_RESOLVED → SESSION_LOADED
→ CONTEXT_BUILT → CONVERSATION_BOUNDARY_ACTIVE → MODEL_INTERPRETATION_STARTED
→ CONVERSATION_INTENT:CLARIFICATION → QUOTE_INTENT:CLARIFICATION
→ QUOTE_INVALIDATED_ON_REQUEST_CHANGE → TOOL_STARTED → TOOL_COMPLETED
→ TOOL_RESULT_TO_AGENT_CONTEXT → REPLY_GUARD_PASSED → SESSION_UPDATED
→ AGENT_FINISHED → COMMAND_CREATED → COMMAND_QUEUED → COMMAND_WOULD_SEND
→ EVENT_COMPLETED → AGENT_INTERPRET_DIAGNOSTIC:{...}
```

---

## 出处（逐条，可核对）

### 端口与数据目录
| 事实 | 出处 |
|---|---|
| 影子实例 :8199 | `.identity-gate-交接.md:201` |
| 数据目录 `.shadow-data` | `.identity-gate-交接.md:209` |
| 启动命令 `D:\python\python.exe .shadow-run.py` | `.identity-gate-交接.md:204` |
| mock 插件 :4100 | `.identity-gate-交接.md:201`、`:205` |
| 图服务 :8299（已不用） | `.identity-gate-交接.md:201` |

### 后端配置
| 事实 | 出处 |
|---|---|
| 数据目录变量 `V5_DATA_PATH` | `v5-prod-9bee216/backend/app/main.py:47` |
| 模式变量 `YUMAIDUO_COMMAND_MODE` | `backend/app/integrations/yumaiduo/executor.py:6` |
| 只接受 `shadow`/`live` | `executor.py:8` |
| shadow 下命令落 `WOULD_SEND` | `executor.py:12` ShadowCommandExecutor |
| 插件接口变量 `V5_PLUGIN_INTERNAL_URL` | `history_reader.py:20` |
| 入站路由 `/api/integrations/yumaiduo/events` | `main.py:922` |
| run-due 路由 `/api/v1/events/run-due` | `main.py:1000` |
| 命令列表 `/api/commands`（**不需要签名**） | `main.py:934` |
| 健康检查 `/api/v1/health` | `main.py:941` |
| 起服务的框架 `uvicorn[standard]` | `backend/requirements.txt` |

### 信封与签名（★ 毫秒，不是秒）
| 事实 | 出处 |
|---|---|
| 信封字段 `{id,tenantId,event,ts,payload{chatId,accountUnb,peerUnb,messageType,content,imageUrls,remoteMessageId}}` | `backend/tests/test_phase7_yumaiduo.py:44-47` |
| 签名 `hmac_sha256(secret, f"{ts}." + raw)` hex | `test_phase7_yumaiduo.py:49` |
| 5 分钟容差 | `test_phase7_yumaiduo.py:50` |
| 请求头 `x-yumaiduo-timestamp` / `x-yumaiduo-signature` | `main.py:925` |
| 字段映射 chatId→chat_id 等 | `backend/app/integrations/yumaiduo/normalizer.py` |
| 密钥 `shadow-test-secret` | `.identity-gate-交接.md:214` |

### mock 的四个端点
| 端点 | 请求体 | 出处 |
|---|---|---|
| `/internal/messages/read` | `{tenant_id,shop_id,buyer_id,chat_id}` | `history_reader.py:20` |
| `/internal/shops/read` | `{}` | `shop_reader.py:18` |
| `/internal/orders/read` | `{tenant_id,order_id}` | `order_reader.py:19` |
| `/internal/ticket-image/upload` | `{tenant_id,accountUnb,data}` | `ticket_image.py:18` |
| 返回值形状（身份字段、direction、sentAtMs 类型） | — | `history_reader.py:37-60` `normalize_history()` |
| 空数组/超 100 条 → `IM_HISTORY_INVALID_OR_EMPTY` | — | `history_reader.py:38` |

### 输出格式
| 事实 | 出处 |
|---|---|
| `=== all commands, in order ===` 等标题 | `.shadow-walk.txt`、`.shadow-report.txt` |
| 命令行格式 `[TYPE] TYPE origin=... status=...` | `.shadow-walk.txt` |
| 轮次定义格式 `{"tag","mtype","body"}` | `.shadow-turns-1.json`、`.shadow-turns-2.json` |

---

## ★ 我推理出来的部分（不是文档说的）

1. **`YUMAIDUO_WEBHOOK_SECRET` 在影子台用 `shadow-test-secret`**
   —— 交接文档:214 给了这个值，但没说影子实例必须设它。
   我设了，因为**不设就完全不验签**，那"验签通过"这件事根本没被测到。

2. **`shadow-walk.py` 的逐轮输出结构**
   —— 原来的输出文件只有两条轮次的结果，看不出它内部怎么组织的。
   我按"每轮注入 → 等异步 → 读命令 → 读 trace"来写。

3. **`start-shadow.cmd`** —— 原来没有，我加的便利脚本。

4. **聊天历史由 walker 记录、mock 回放**（`.shadow-history.json`）
   —— 这一条是**踩出来的**，不是文档说的。见下。

---

## 踩过的坑（会再踩，先记下）

### ① Windows 上 Python stdout 默认 GBK

`shadow-run.py` 第一版在打印 `✓ 起来了` 时崩了：

```
UnicodeEncodeError: 'gbk' codec can't encode character '\u2713'
```

**而且崩的是父进程** —— 影子实例（子进程）其实已经起好了，
父进程一崩子进程成孤儿，看起来像"启动失败"。

⇒ 三个脚本开头都加了 `sys.stdout.reconfigure(encoding="utf-8")`。
这和交接文档:236 记的是同一类坑（"中文一律用 Python `encoding='utf-8'`"）。

### ② 命令列表是**整个影子库**，不是本次运行

第一版按 `since=len(commands)` 切片算增量，把 `.shadow-data` 里
`2026-10-09` 的旧命令当成自己的 —— **输出长得完全正确，但一条新的都没有**。

⇒ 判据换成「`chat_id` 或 `event_id` 属于本次运行」。

### ③ `run-due` 是**异步**的

`runtime.py:295` 用线程池，返回的是"开了几个 worker"的计数，不是处理结果。
第一版固定 `sleep(2)` 就读，第 1 轮永远是空的。

⇒ 改成轮询，直到 trace 出现 `EVENT_COMPLETED` 或终态错误。

### ④ mock 的日志必须 flush

重定向到文件时 Python 默认块缓冲，不 flush 日志会卡在缓冲区 ——
看起来像"mock 没被调用"。而 `/healthz` 的 `stats.messages` 明明在涨。

⇒ 用 `python -u`，日志走带 `flush=True` 的 `_log()`。

### ⑤ 空历史 = 非法，不是"没历史"

`.shadow-data/conversations.sqlite3` 里两个会话的 `messages` 都是空列表，
所以 mock 只能返回 `[]` —— 而 `normalize_history` 判 `[]` 为**非法**（`history_reader.py:38`），
于是每条都停在 `HISTORY_UNAVAILABLE`。

⇒ walker 注入时把消息记进 `.shadow-history.json`，mock 如实回放。
**这不是编数据** —— 记的就是刚注入的那条；
真实插件返回的历史里也必然包含买家刚发的那条（平台存着）。

### ⑥ ★ 批处理启动器连续踩了四个坑（已放弃，改用 Python）

我写了 `start-shadow.cmd`，起不来。四个坑，每个都是
「Windows 批处理 + 中文路径 + 非 ASCII」这一类：

| # | 现象 | 原因 |
|---|---|---|
| 1 | 中文全变 `?` | 用 ASCII 保存 |
| 2 | `@echo off` 前面多个字符 | UTF-8 带 BOM，cmd 把 BOM 当正文 |
| 3 | `The system cannot find the file shadow-mock` | UTF-8 保存后引号变全角，`start` 参数解析坏掉 |
| 4 | `'entlyContinue' is not recognized...` | **`rem` 行里写了 `\|`** —— cmd 把 `\|` 当管道，后半行被当命令执行 |

而且 `chcp 65001` 是在**解析之后**才生效的，
救不了同一文件里已经被解析坏的中文路径。

⇒ 换成 `start-shadow.py`：用 `subprocess.Popen`，不经过 cmd 的文本解析。
路径、编码、引号全都不再是问题。

**教训**：在 Windows 上用批处理拼中文路径，成本远高于直接用 Python 起子进程。
下次直接写 Python。

---

## 与生产的关系

| | 生产 | 影子台 |
|---|---|---|
| 位置 | `124.220.29.179:/opt/ticket-system-v5` | 本机 |
| 端口 | 8101（后端）· 4100（插件，**live**） | 8199 · 4100（mock） |
| 模式 | `live` —— **真发消息** | `shadow` —— 只落 `WOULD_SEND` |
| 数据 | `/var/lib/ticket-system-v5/` | `.shadow-data/` |

**两者数据目录不同、进程不同、互不影响。**

⚠️ **别抢端口**（交接文档:199 原话：「由 `082c498f` 搭的，可能还在用，别抢端口」）。
`shadow-run.py` 启动前会探一下端口，被占了就拒绝启动。
