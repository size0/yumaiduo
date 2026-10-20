# 架构

## 一句话

**编排层调用领域层，领域层调用接入层；接入层只认识 HTTP，领域层只认识规则，编排层只认识流程。**

依赖方向是单向的，`tests/_audit_architecture.py` 会检查它。

---

## 分层

```
┌─────────────────────────────────────────────────────────────┐
│  web/         4 个页面（AI助理 / 回复文案 / 功能设置 / 报价规则）│
└───────────────────────────┬─────────────────────────────────┘
                            │ HTTP
┌───────────────────────────▼─────────────────────────────────┐
│  server.py    纯标准库 HTTP 服务，路由 + 页面                     │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│  application/  编排：把各层串成一条链路                          │
│                                                             │
│    service.py     会话存取 + 一条买家消息的路由判定              │
│    ticket_flow.py 识别→询价→算价→下单→回调 的完整链路            │
│    store.py       所有落盘读写（配置/文案/规则/凭证/会话）         │
└──────────┬──────────────────────────────┬───────────────────┘
           │                              │
┌──────────▼──────────────┐   ┌───────────▼───────────────────┐
│  domain/   业务规则       │   │  integrations/  外部世界        │
│                          │   │                               │
│  conversation.py 状态机   │   │  liangpiao/                   │
│  quote_rules.py  算价     │   │    signature.py 签名          │
│                          │   │    client.py    HTTP + 重试    │
│  ★ 不依赖网络             │   │    api.py       26 个接口      │
│  ★ 不依赖 HTTP            │   │    callbacks.py 7 个回调事件   │
└──────────────────────────┘   └───────────────────────────────┘

顶层模块（被各层共用）
  settings.py    25 个配置节 / 63 个控件
  templates.py   31 条文案 + 23 个变量
  qa.py          18 条关键词回复
```

### 为什么这么分

| 层 | 变了要改什么 | 不会因为什么而改 |
|---|---|---|
| `domain/` | 业务规则（加价几块、几天能退） | 良票改接口、换 HTTP 库 |
| `integrations/` | 良票改协议、改签名算法 | 业务规则怎么定 |
| `application/` | 流程顺序（先询价还是先识别） | 良票字段名、文案措辞 |
| `web/` | 界面长什么样 | 后端怎么算 |

**判断标准是"改动频率"。** 频率不同的东西放一起，就一定会互相牵连 —— 这就是当初改一个配置页把 AI 助理整页弄坏的原因。

---

## 一次完整请求走的路

```
买家发选座截图
  │
  ├─ web/app.js                          （前端只负责显示）
  │
  ├─ PUT /api/message                    server.py
  │
  ├─ TicketService.on_message()          application/service.py
  │    判定这是"图片"还是"文字"
  │    有图片 → 走识别
  │
  ├─ TicketFlow.on_screenshot()          application/ticket_flow.py
  │    ├─ api.ch7_recognize()            integrations/liangpiao/api.py
  │    ├─ 按 matchLevel 分流（EXACT / CANDIDATE / NONE）
  │    ├─ api.ch5_preflight()            拿成本
  │    ├─ QuoteRuleEngine.quote()        domain/quote_rules.py 算售价
  │    ├─ 决定 priceMode / maxPrice      ★ 这两个值下单时必须原样复用
  │    └─ tpl.render_messages()          templates.py 出报价卡
  │
  └─ 返回 FlowResult(messages=[...])      service 落盘会话，返回 JSON

买家回"正确"
  └─ on_buyer_confirm() → conv.apply(BUYER_CONFIRM) → api.ch5_create()
       ↑ domain/conversation.py 判定状态合法     ↑ 用同一个 QuoteParams

良票回调 order.ticketed
  └─ POST /api/callback → 验签 → flow.on_callback() → conv.apply(TICKET_OK)
```

---

## 三条纪律

### 1. 依赖方向单向

`domain/` 不许 import `integrations/` 或 `application/`。
违反时审计会报出来。

### 2. 落盘只在 `application/store.py`

其他地方不许碰文件。这样换存储（文件 → 数据库）只改一个文件。

> 这条是被逼出来的：`service` 要读规则、读规则写在 `server` 里，
> 于是形成 `server → service → server` 的循环依赖。

### 3. ★ V5 只作参考，不得引用

V5 的代码（`E:\200行的agent\v5-prod-9bee216\`）**看得，但不能搬**：

- 不许 import 它的模块
- 不许把它的文件拷进本项目（哪怕改了名字）
- 不许在注释/文档里写"移植自 V5"、"V5 原文"

理由：**说不清来源的代码，改不动也不敢删。**
真的需要那段逻辑，就照良票文档自己写一遍 ——
写的过程本身就是理解。

`tests/_audit_v5_taint.py` 会持续检查这一条。

### 4. 外部事实单向流入

良票给什么就是什么。不让规则引擎或文案去"猜"。
识别失败、场次过期、出票失败 —— 都是**事实**，不是判断。

---

## 目录

```
ticket-agent/
├── server.py               HTTP 入口（纯标准库）
├── settings.py             配置定义（25 节 / 63 控件）
├── templates.py            文案（31 条 / 23 变量）
├── qa.py                   关键词回复（18 条）
├── run_checks.py           ★ 唯一测试入口
│
├── application/            编排
├── domain/                 业务规则（不依赖网络）
├── integrations/           良票接入
├── web/                    4 个页面
├── tests/                  自检（16 套）
└── demos/                  教学演示（用真模型跑）
```

> **关于 `legacy/`**：这里曾经放过几个从 V5 搬来的模块。
> 按「V5 只作参考、不得引用」的规则已经**全部删除** ——
> 需要那段逻辑就照良票文档自己写。
> `tests/_audit_v5_taint.py` 会持续检查有没有新的引用进来。

---

## 怎么验证架构没坏

```bash
python run_checks.py                     # 14 套全跑
python tests/_audit_architecture.py      # 分层 / 循环 / 孤儿 / 重复实现
```

审计会检查四件事：

1. **分层违规** —— 内层有没有依赖外层
2. **循环依赖** —— 有没有互相 import
3. **孤儿模块** —— 做完了却没人用的代码
4. **重复实现** —— 同一个概念两套代码

> 第 3 条抓到过一个真问题：`ticket_flow.py` 做完了、97 项测试全过，
> 但**没有任何生产代码引用它**。也就是说整条链路接不进界面。
> 「能跑」和「能上线」的差别就在这里。
