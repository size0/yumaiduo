"""
server.py —— 纯 Python HTTP 服务（零依赖）

■ 为什么用标准库而不是 FastAPI
  这个项目是拿来学的，多一个依赖就多一层要理解的东西。
  `http.server` 够用，而且你能一眼看完整个服务。

■ 三套接口，对应三个页面

  /            店铺管理      →  /api/assistant   对话层怎么说话
  /replies     回复文案     →  /api/templates   发了什么话
  /features    功能设置     →  /api/settings    机器怎么运转

  ★ 为什么 AI助理 和 功能设置 分开：
    一边决定"机器怎么运转"（报价有效期、票速、退款条件），
    一边决定"对话层怎么说话"（人设、风格、记忆）。
    混在一起，运营调语气时容易误动业务参数。

运行：
    python server.py
"""

import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qa as qa_module                      # noqa: E402
from application import shop_scope
import settings as settings_module          # noqa: E402
import templates as templates_module        # noqa: E402
from domain import quote_rules as qr        # noqa: E402
from prompts import persona as persona_module   # noqa: E402
from application import store               # noqa: E402
from application import service as service_mod           # noqa: E402
from application.service import build_service, TicketService   # noqa: E402

ROOT = Path(__file__).parent
WEB = ROOT / "web"
DATA = ROOT / "data"

# ★ 所有落盘读写都在 application/store.py。
#   搬走的理由：service 也要读配置和规则，如果读写留在 server 里，
#   就形成 server → service → server 的循环依赖（审计抓到的）。
#   现在两边都依赖 store，方向是单向的。
PAGES = store.PAGES

HOST, PORT = os.getenv("TICKET_AGENT_HOST", "127.0.0.1"), int(
    os.getenv("TICKET_AGENT_PORT", "8801"))

# ==================================================================== 人类页面鉴权
# ★★★ 为什么必须有这个（2026-10-14，替换 V5 之前补的）
#
#   插件端点（/api/v1/*）有验签：integrations/yumaiduo/signature.py，
#   验的是插件与平台共享的那把 webhookSecret（server.py 的 _plugin_api）。
#   但【人用的页面】—— /api/settings、/api/orders、/api/quotes… ——
#   【一道闸都没有】：谁打开这个端口，谁就能改配置、看订单。
#
#   绑 127.0.0.1 时还好（只有本机能访问）。一旦因为部署把它对外绑
#   （TICKET_AGENT_HOST=0.0.0.0），这个洞就暴露了。
#
# ★ 三个开关（都不设时的行为最保守）：
#     TICKET_AGENT_AUTH=off   显式关掉（明确知道自己在做什么时才用）
#     TICKET_AGENT_AUTH=on    显式打开
#     不设                     ★ HOST 是本机 ⇒ 关（本机访问不受影响）
#                              ★ HOST 不是本机 ⇒ 【自动打开】
#        —— 为什么自动：对外绑定的人多半没意识到要加鉴权，
#           而"忘了加"的后果是整个后台裸奔。
# ★ 用户名/密码从哪来：环境变量，不落盘、不进代码库。
#     TICKET_AGENT_USER（默认 admin）
#     TICKET_AGENT_PASSWORD（自动打开时必须设，否则拒绝启动 ——
#                            宁可起不来，也不要一个没密码的公开后台）
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def _auth_config() -> tuple:
    """返回 (要不要鉴权, 用户名, 密码, 强制原因)。"""
    user = os.getenv("TICKET_AGENT_USER", "admin").strip() or "admin"
    pwd = os.getenv("TICKET_AGENT_PASSWORD", "").strip()
    mode = (os.getenv("TICKET_AGENT_AUTH", "") or "").strip().lower()
    if mode == "off":
        return False, user, pwd, "TICKET_AGENT_AUTH=off（显式关掉）"
    if mode == "on":
        return True, user, pwd, "TICKET_AGENT_AUTH=on（显式打开）"
    if HOST not in _LOCAL_HOSTS:
        return True, user, pwd, f"HOST={HOST} 不是本机 ⇒ 自动打开鉴权"
    return False, user, pwd, f"HOST={HOST} 是本机 ⇒ 默认不鉴权"


AUTH_REQUIRED, AUTH_USER, AUTH_PASSWORD, AUTH_WHY = _auth_config()


def _check_auth(headers) -> bool:
    """
    Basic Auth 校验。★ 恒定时间比较 —— 别用 == 比密码，
    那会通过响应时间泄露"前几位猜对了"。
    """
    import base64
    import hmac as _hmac
    raw = str(headers.get("Authorization") or "")
    if not raw.lower().startswith("basic "):
        return False
    try:
        got = base64.b64decode(raw[6:].strip()).decode("utf-8")
    except Exception:                                         # noqa: BLE001
        return False
    u, _, p = got.partition(":")
    return (_hmac.compare_digest(u, AUTH_USER)
            and _hmac.compare_digest(p, AUTH_PASSWORD))


# ★ 不需要鉴权的路径：插件端点（它们有自己的验签，而且插件不会带 Basic）。
_AUTH_EXEMPT = ("/api/v1/",)

MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml"}



# ==================================================================== 路径前缀
# ★ 我们的界面本来挂在根路径。V5 的挂在 /ui 下（所以 wd.xdw0.cn/v5/ui 能开）。
#   为了让 nginx 或插件把 /ui/* 转过来能用，这里统一剥掉前缀。
#   ★ 只剥前缀、不重写路由：保证只有一套路由实现。
# ★ 支持哪些前缀：/ui（插件 ui.mjs 转发的路径）+ /ta（nginx 直接反代用）。
#   ★ 为什么不只用 /ui：这台机器上 /ui/ 已经被别的系统占了
#     （nginx 的 location ^~ /ui/ 指向 4003）⇒ 我们必须另开一个前缀。
#   ★ 前缀列表集中在这里：加一个前缀只要往这个元组里加一项，
#     服务端自动剥掉它、HTML 里的 <base> 也自动跟着变。
PREFIXES = ("/ui", "/ta")


def _strip_prefix(path: str) -> str:
    """/ui/orders -> /orders；/ui -> /。不匹配则原样返回。"""
    for pre in PREFIXES:
        if path == pre:
            return "/"
        if path.startswith(pre + "/"):
            return path[len(pre):] or "/"
    return path


def _base_for(path: str) -> str:
    """这次请求的页面该用哪个 base（给 HTML 注入）。"""
    for pre in PREFIXES:
        if path == pre or path.startswith(pre + "/"):
            return pre + "/"
    return "/"

# ==================================================================== 配置读写
# ★ 持久化全部搬去了 application/store.py，这里只做转发，
#   让 server 内部的调用点不用改。
load_config = store.load_config
save_config = store.save_config
load_rules = store.load_rules
save_rules = store.save_rules
rules_payload = store.rules_payload
load_credentials = store.load_credentials
save_credentials = store.save_credentials


def assistant_payload(data: "settings_module.Settings") -> dict:
    """AI助理页要的全部数据。"""
    a = data.assistant
    from application.model_config import status as model_status
    return {
        "agent_enabled": a.agent_enabled,
        "business_scope": a.business_scope,
        "business_scope_options": a.business_scope_options,
        "takeover_seconds": a.takeover_seconds,
        "human_pause_seconds": a.human_pause_seconds,
        "memory_hours": a.memory_hours,
        "memory_messages": a.memory_messages,
        "model_name": a.model_name,
        "model_options": a.model_options,
        # ★★ 2026-10-14 店主定的：操作台上不许出现"光秃秃的英文"。
        #   模型名是英文标识符（发给模型的真实名字，改了就调不通），
        #   所以配一份中文名，界面显示成「极速版（deepseek-flash）」。
        "model_labels": getattr(a, "model_labels", {}) or {},
        **model_status(),
        "persona": a.persona,
        "precautions": a.precautions,
        "reply_style": a.reply_style,
        # ★★ 本店经验 —— 运营攒的经验写这里，模型答问题时优先照它答。
        #    它和「人设/注意事项/风格」是三件不同的事：
        #      那三段是「你是谁、怎么说」
        #      这一段是「这门生意里你知道什么」
        "experience": a.experience,
        # ★ style_presets 早先只是三个名字（"default"/"conservative"/"warm"），
        #   接口返回了但前端从来没用过 —— 因为预设的【文本】当时
        #   只硬编码在教学演示里，生产代码拿不到。
        #   现在把名字和文本一起给出去，"预设"才是真能用的东西。
        "style_presets": a.style_presets,
        "style_preset_texts": persona_module.STYLE_PRESETS,
        "stage_filter_enabled": a.stage_filter_enabled,
        "stage_labels": a.stage_labels,
        "stage_keys": ["intake", "quote", "order", "after_sale"],
        "allowed_stages": a.allowed_stages,
        "effective_prompt": a.build_prompt(),
        "problems": a.validate(),
        "pages": PAGES,
    }


def feature_payload(data: "settings_module.Settings") -> dict:
    """功能设置页要的全部数据（布局由字段元数据生成，不手写 HTML）。"""
    return {
        "groups": data.layout(),
        "problems": data.validate(),
        "derived": {
            "buyer_commands": data.buyer_commands(),
            "speed_tiers": [{"tier": t, "min_minutes": m} for t, m in data.speed_tiers()],
            "quote_ttl_minutes": data.recognize_quote.quote_ttl_minutes,
            "confirm_keyword": data.fish_reprice.confirm_keyword,
        },
        "pages": PAGES,
    }


# ==================================================================== 模板 / 规则 / 凭证
# ★ 读写的实现在 application/store.py，这里只转发。
#   理由见文件顶部：读写留在 server 会造成 server → service → server 的循环依赖。
load_template_overrides = store.load_template_overrides
save_templates = store.save_templates


# ==================================================================== 服务实例
_service: TicketService | None = None


def get_service() -> TicketService:
    """
    惰性装配编排服务。

    ★ 为什么惰性：凭证可能还没填。填了一半也照样装配 ——
      缺 tel 会让下单失败、缺模型 Key 会让 AI 转人工，
      但界面能打开、关键词能回。这比整体起不来强。
    """
    global _service
    if _service is None:
        cred = load_credentials()
        _service = build_service(
            tel=cred.get("tel") or "",
            app_key=cred.get("app_key") or "",
            app_secret=cred.get("app_secret") or "",
            base_url=cred.get("base_url") or None,
            # ★★★ 「优先报价模式」在这里生效（与 application/service.py 同一套逻辑）。
            #   ★ 为什么复用 service 里那个辅助函数、而不是这里再写一遍：
            #     逻辑要在【一个地方】——两处各写一遍早晚不一致。
            #     它已经处理了"cfg 没这字段""值不是 list"两种边界。
            rules=qr.QuoteRuleEngine(
                qr.select_rules_by_priority(
                    load_rules(),
                    service_mod._quote_priority(load_config()))[0]))
    return _service


def invalidate_service() -> None:
    """凭证或规则变了 —— 丢掉旧实例，下次重新装配。"""
    global _service
    _service = None


def _validate_qa(rules: list, qa_module) -> list:
    """
    关键词回复的校验。

    ★ 这几类错误不拦下来，会等到【给买家发消息那一刻】才炸：
        · 关键字为空      → 永远匹配不上，或者匹配所有空消息
        · 关键字重复      → 后写的覆盖先写的，运营以为两条都在
        · 引用不存在的 ID → 买家收到 "[abc123]" 这种原文
        · 引用成环        → resolve 抛 ReferenceError，消息发不出去
    """
    problems = []
    seen: dict[str, int] = {}

    for i, r in enumerate(rules, 1):
        if not (r.keyword or "").strip():
            problems.append(f"第 {i} 条（{r.qa_id}）关键字为空")
        if not (r.qa_id or "").strip():
            problems.append(f"第 {i} 条缺少全局调用标识ID")
        key = (r.keyword or "").strip()
        if key:
            if key in seen:
                problems.append(f"关键字「{key}」重复（第 {seen[key]} 条和第 {i} 条）"
                                f"—— 后写的会覆盖先写的")
            else:
                seen[key] = i
        if not (r.content or "").strip():
            problems.append(f"第 {i} 条（{key}）内容为空")

    # Validate the proposed table rather than the previous runtime indexes.
    problems.extend(qa_module.reference_problems(rules))

    return problems


# ★ 这里删掉了 service_payload() —— 它是给「集成客服」占位页用的，
#   而那一页从来没实现过（导航里是个 disabled 按钮）。
#   既然占位页整个删了，这个只被它调用的数据面也一起删 ——
#   留着就是"名字存在、没人用"的死代码（_check_deadcode 会报）。
#   ★ 它原来读的会话概况（sessions/ready/rules_count）不是没用，
#     而是没有页面消费。要做的时候再按需要写，不要先摆着。


# ==================================================================== HTTP
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    # ---------------------------------------------------------------- 工具
    def _send(self, status: int, body: bytes, content_type: str, *,
              cache_control: str = 'no-store', etag: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if status != 304:
            self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        if etag:
            self.send_header('ETag', etag)
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self) -> bool:
        """
        过不过鉴权闸门。不过就回 401（并带 WWW-Authenticate，
        浏览器会弹登录框，而不是给一个看不懂的错误）。
        """
        if not AUTH_REQUIRED:
            return True
        if str(self.path or "").startswith(_AUTH_EXEMPT):
            return True          # 插件端点：有自己的验签
        if _check_auth(self.headers):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="ticket-agent"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(b'{"ok":false,"error":"auth_required"}')
        return False

    def _json(self, data, status: int = 200) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _redirect(self, to: str, status: int = 302) -> None:
        """302 跳转。

        ★ 为什么需要它：把「报价记录」并进订单页后，旧的 /quotes 链接
          不该 404（旧书签、别人发过的链接都还会点）。
        ★ 用 302 而不是 301：301 会被浏览器【永久】缓存，
          以后想再改路由就改不动了。
        """
        self.send_response(status)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # ---------------------------------------------------------------- GET
    @shop_scope.http_scope
    def do_GET(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        # ★★ 2026-10-14 前缀支持：界面可以挂在 /ui 下（nginx 或插件转发过来）。
        #   为什么要 strip 而不是另写一套路由：
        #     另写一套 = 每个路由都要两份，早晚不一致。
        #     strip 掉前缀后走【原来那套】⇒ 只有一处实现。
        #   ★ 插件 ui.mjs 会把 /ui/api/* 【原样】转给后端，
        #     所以这里必须同时覆盖页面和 API。
        path = _strip_prefix(path)
        # ★★★ 鉴权闸门（2026-10-14，替换 V5 之前补的）。
        #   为什么接在【入口】而不是每条路由：漏一条就是一条裸奔。
        #   唯一豁免是 /api/v1/*（插件端点）—— 它们有自己的验签，
        #   而且插件不会带 Basic 头。
        if not self._auth_ok():
            return

        if path == "/api/assistant":
            return self._json(assistant_payload(load_config()))

        if path == "/api/settings":
            # ★★ 2026-10-14：把取票图的【当前状态】带出来。
            #   为什么：图存在 data/ticket-image.json（不进 settings.json），
            #   界面要显示"已传 / 未传 / 传了多大"，得知道那份文件的状态。
            #   ★ 不回 base64（那是几百 KB，每次开设置页都传一遍没意义），
            #     只回名字/类型/大小/有没有上传过。
            _payload = feature_payload(load_config())
            try:
                from application.ticket_image import load as _ti_load
                _ti = _ti_load()
                _payload["ticket_image"] = {
                    "has_image": bool(_ti.get("data")),
                    "name": _ti.get("name") or "",
                    "kind": _ti.get("kind") or "",
                    "bytes": int(_ti.get("bytes") or 0),
                    "uploaded": bool(_ti.get("url")),
                }
            except Exception:                                # noqa: BLE001
                _payload["ticket_image"] = {"has_image": False, "name": "",
                                            "kind": "", "bytes": 0,
                                            "uploaded": False}
            return self._json(_payload)

        if path == "/api/rules":
            # ★★ 顺手把院线名缓存补上（过期才拉，失败静默）。
            #   为什么在这里：这个接口就是规则页在读，而规则页的
            #   known_brands / brands 选项全靠这份缓存。
            #   早先 sync_brands_from_api 没有任何调用者 ⇒ 缓存永远是空的
            #   ⇒ 良票的 140 个院线名我们只认识 2 个（详见 store 里的注释）。
            #   ★ api 从现成的服务取，不再新建一个客户端 ——
            #     免得每次打开规则页都多一套连接/凭证对象。
            try:
                store.refresh_brands_if_stale(get_service().flow.api)
            except Exception:                                # noqa: BLE001
                pass
            params = parse_qs(urlparse(self.path).query)
            try:
                return self._json(rules_payload(params.get("tenant_id", [""])[0],
                                                params.get("shop_id", [""])[0]))
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)

        if path == "/api/orders":
            # ★ 订单管理页的数据：报价记录 + 订单 + 分桶计数。
            #   读模型在 application/ledger.py（server 不该自己拼 store）。
            from application import ledger
            params = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            return self._json(ledger.orders_payload(filters=params if params.get('kind') else None))

        if path == "/api/delivery-auth":
            # ★ 出票接口的【配置状态】。
            #   ★ 绝不返回密钥明文 —— 只回"配没配"和掩码。
            #     界面要能回答："出票接口能不能用？缺什么？"
            from application import delivery_auth
            return self._json(delivery_auth.config_status())

        if path.startswith("/api/preview/"):
            # ★★ 「回复文案」页的预览按钮调的就是这个（app.js:243）。
            #   前端在做、后端【一直没有】这个路由 —— 点了直接 404。
            #   这是"前端做了后端没做"的实例（审计脚本查出来的）。
            #
            #   ★ 为什么限制只能本机访问：
            #     预览会渲染模板正文，而模板里有运营话术、时效承诺、
            #     甚至可能被塞进订单信息。这个服务默认只开在 127.0.0.1，
            #     但万一有人把它暴露到公网，这个接口就成了"免费读全部话术"。
            #     ⇒ 只允许来自本机的请求（和 /api/delivery-token 一个道理）。
            client_host = (self.client_address or ("",))[0]
            if client_host not in ("127.0.0.1", "::1", "localhost"):
                return self._json(
                    {"ok": False, "error": "PREVIEW_LOCAL_ONLY",
                     "message": "预览接口只允许本机访问"}, status=403)
            key = unquote(path[len("/api/preview/"):])
            channel = parse_qs(urlparse(self.path).query).get("channel", ["global"])[0]
            if channel not in {"global", "wanda", "liangpiao"}:
                return self._json({"ok": False, "error": "INVALID_TEMPLATE_CHANNEL"}, 400)
            try:
                # ★★ 必须用 render_parts（不是 render）——
                #   一个模板可能渲染成【多条消息】（报价卡就是 3 段）。
                #   前端 app.js:243 读的是 joined 的 text；
                #   而 _smoke.py:284 要的是 parts（逐条看）。
                #   ⇒ 两个都给，谁也不迁就谁。
                #   踩过：只回 text 时，_smoke 的"报价卡能渲染出多条消息"
                #   永远拿到 0 条（它读 prev["parts"]）。
                parts = templates_module.render_messages(key, channel=channel) or []
                parts = [p for p in parts if isinstance(p, str)]
            except Exception as exc:                      # noqa: BLE001
                return self._json({"ok": False,
                                   "error": f"{type(exc).__name__}: {exc}"},
                                  status=400)
            return self._json({"ok": True, "key": key,
                               "channel": channel,
                               "text": "\n\n".join(parts),
                               "parts": parts})

        if path == "/api/plugin-auth":
            from application import delivery_auth as _da
            from application import plugin_api
            secret = plugin_api.plugin_secret()
            return self._json({
                "configured": bool(secret),
                "secret_hint": _da.mask_secret(secret),
                # ★ 插件默认指向 8000（V5 的端口），我们的是 8801 ——
                #   这个地址必须告诉用户，否则插件连不上。
                "backend_url": f"http://127.0.0.1:{PORT}",
                "queue": plugin_api.queue_status(),
                # ★ 密钥从哪来 —— 界面要能说清（不是"你自己随便设"）
                #   环境变量 / 插件交接文件 / 手动兜底
                "secret_source": (
                    "env" if (os.getenv("YUMAIDUO_WEBHOOK_SECRET") or "").strip()
                    else "handoff-file" if plugin_api.secret_file()
                    else "manual"),
                "secret_file": plugin_api.secret_file(),
            })

        if path == "/api/credentials":
            # ★ 良票凭证的【当前状态】（★ 密钥只给掩码，不回明文）。
            #
            #   ★ 为什么必须补这个 GET：
            #     原来只有 PUT，前端【没有任何地方能读】——
            #     于是界面上根本没有"填手机号"的入口，
            #     而 tel 是良票下单的必填项，缺了下单直接中止。
            #     反馈"手机号和密钥在哪设置、我没看到"就是这个原因。
            from application import delivery_auth as _da
            cred = load_credentials()
            return self._json({
                "app_key": cred.get("app_key") or "",
                # ★ 不回明文：密钥只给掩码
                "app_secret_hint": _da.mask_secret(cred.get("app_secret") or ""),
                "tel": cred.get("tel") or "",
                "base_url": cred.get("base_url") or "",
                "model_api_key_hint": _da.mask_secret(
                    cred.get("model_api_key") or ""),
                # ★ 下单能不能走 —— 这个才是界面该突出的
                "can_order": bool(cred.get("tel")),
                "missing": [k for k, v in
                            (("AppKey", cred.get("app_key")),
                             ("AppSecret", cred.get("app_secret")),
                             ("购票手机号", cred.get("tel")))
                            if not v],
            })

        if path == "/api/delivery-token":
            # ★ 给本地界面用的出票密钥。
            #
            #   ★ 为什么要有这个接口：
            #     出票走 /api/order/deliver（有鉴权）。界面点"出票"按钮
            #     也要过同一道鉴权 —— 不能给界面开一条绕过鉴权的后门，
            #     否则"鉴权"就只是装饰，而且两条路迟早行为不一致。
            #
            #   ★ 为什么在这里交出密钥是安全的：
            #     这个 HTTP 服务是【运营在本地/内网开的】管理后台，
            #     能访问它的人本来就是运营。密钥的价值是防【外部】
            #     的出票后台调用时伪造 —— 不是防运营自己。
            #
            #   ★★ 若要把这个后台暴露到公网，必须给后台本身加登录 ——
            #     那时这个接口就是泄露点（审计里有断言盯着它）。
            from application import delivery_auth
            cfg = delivery_auth._config()          # noqa: SLF001
            if not cfg["write_enabled"] or not cfg["write_key"]:
                return self._json({"ok": False, "token": "",
                                   "message": "出票接口未启用或未配置密钥"})
            return self._json({"ok": True, "token": cfg["write_key"]})

        if path == "/api/ping":
            # 连通性自检：用真凭证打良票的 ping，验签对不对
            cred = load_credentials()
            if not (cred.get("app_key") and cred.get("app_secret")):
                return self._json({"ok": False, "error": "还没配 AppKey / AppSecret"})
            try:
                svc = get_service()
                data = svc.flow.api.ch4_ping()
                return self._json({"ok": True, "data": data,
                                   "request_id": svc.flow.api.client.last_request_id()})
            except Exception as exc:                # noqa: BLE001
                return self._json({"ok": False,
                                   "error": f"{type(exc).__name__}: {exc}"})

        if path == "/api/shops":
            # ★★ 2026-10-14 新增：店主要求「当前账号读取当前店铺配置」。
            #
            #   数据来源：**只有插件进程能拿到店铺列表** ——
            #   它持有鱼麦多的 client（带 pluginToken），调 core.shops.list()。
            #   插件的 server.mjs 早就开了 /internal/shops/read 给后端用，
            #   但后端一直没调它。这里补上那条出站调用。
            #
            #   ★ 不返回 500：界面要能显示"为什么没有店铺"。
            #     几种情况处置完全不同，所以带上 error_code：
            #       PLUGIN_SECRET_MISSING      → 插件还没自注册交接密钥
            #       PLUGIN_UNREACHABLE         → 插件进程没起来 / 地址不对
            #       SHOP_SCOPE_REQUIRED        → 租户没同意 account.read
            #       PLUGIN_SIGNATURE_REJECTED  → 两边密钥不一致
            #   ★ 密钥在这里取（应用层），不在 integrations 里取 ——
            #     分层规矩：内层（integrations）不许 import 外层（application）。
            #     我第一版把 plugin_secret() 写进了 shop_client，
            #     被架构审计当场抓出来（分层违规 1 处）。
            from application.plugin_api import plugin_secret
            from integrations.yumaiduo.shop_client import read_shops
            payload = read_shops(secret=plugin_secret())
            # ★★★ 2026-10-14 店主加的：每个店铺一个「自动发送」开关。
            #   ★ 它【不是】平台的启用状态：
            #     平台状态是鱼麦多那边的（只读，官方只给了"读店铺列表"），
            #     这个开关是我们自己的 —— 关掉 ⇒ 不往这个店发自动消息。
            #   ★ 平台状态和我们的开关【分开两个字段返回】，界面也分两列。
            #     混在一起会让运营以为"我把平台上的店停用了"。
            try:
                prefs = store.load_shop_prefs()
                for s in (payload.get("shops") or []):
                    sid = str(s.get("shop_id") or "")
                    one = prefs.get(sid) if isinstance(prefs, dict) else None
                    s["send_enabled"] = (bool(one.get("send_enabled", True))
                                         if isinstance(one, dict) else True)
                payload["send_gate_configured"] = bool(prefs)
            except Exception as exc:                          # noqa: BLE001
                # ★ 读不到开关不能把整个店铺列表搞挂 ——
                #   店铺列表本身是有用的（核对平台状态），
                #   所以降级：都按"开"返回，并带上原因。
                payload["send_gate_error"] = f"{type(exc).__name__}: {exc}"
            return self._json(payload)

        if path == "/api/qa":
            # ★ 之前这里只返回 id/keyword/content/trigger/limits —— 少了
            #   message_type、ref_pattern，而且【没有 PUT】。
            #   结果是 18 条关键词回复界面上看不到、也改不了。
            #   现在数据补全，写接口在下面 do_PUT 里。
            return self._json(store.qa_payload())

        if path == "/api/templates":
            return self._json({
                "stages": [{"key": k, "name": n} for k, n in templates_module.STAGES],
                "channels": templates_module.CHANNELS,
                "catalog": templates_module.catalog(),
                "variables": {k: {"name": v[0], "description": v[1], "example": v[2]}
                              for k, v in templates_module.VARIABLES.items()},
            })

        # 静态文件
        # ★★ 2026-10-14 店主：「订单管理和报价记录要分为两个菜单栏」
        #   ⇒ /quotes 恢复成【独立页】，不再重定向到 /orders。
        #   （中段我一度把它并进订单页的第一个 tab，那是理解反了 ——
        #     看到 V5 截图里报价记录是个 tab 就以为要合并。
        #     ★ 教训：抄 V5 抄的是内容与列；页面组织结构（菜单几项、
        #       谁和谁一页）要单独确认，截图里的 tab 不等于我们的菜单结构。）
        routes = {"/": "/shops.html", "/assistant": "/index.html", "/replies": "/index.html",
                  "/features": "/config.html", "/config": "/config.html",
                  "/qa": "/qa.html",
                  "/rules": "/rules.html",
                  # ★ 店铺管理（本店在平台的授权与状态）
                  "/shops": "/shops.html",
                  # ★ 报价记录（独立页）
                  "/quotes": "/quotes.html",
                  # ★ 订单管理（订单 / 万达出票）
                  "/orders": "/orders.html",
                  # ★ 组件预览页（规范 §8 第②步的验收载体：
                  #   "预览页里把每个变体排一遍，不出现色值"）
                  "/components": "/components.html"}
        path = routes.get(path, path)
        target = (WEB / path.lstrip("/")).resolve()
        if WEB.resolve() not in target.parents or not target.is_file():
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        body = target.read_bytes()
        # ★★ 2026-10-14：HTML 注入两样东西，让界面能挂在任意前缀下（默认 /ui）。
        #   ① <base href="…">  管【相对】路径（config.css / config.js）
        #   ② window.__DSH_BASE__  管【绝对】路径（/api/orders 这类 fetch）
        #   ★ 两者缺一不可：
        #     只有 <base> ⇒ 页面能看，但所有按钮打不通（fetch 打到 /api 去了）
        #     只有 __DSH_BASE__ ⇒ css/js 404，页面是裸的
        #   为什么不在 HTML 里写死前缀：那样本机直接开 127.0.0.1:8801 就坏了。
        if target.suffix == ".html":
            base = _base_for(urlparse(self.path).path.rstrip("/") or "/")
            inject = (f'<base href="{base}">'
                      f'<script>window.__DSH_BASE__={base!r};</script>')
            text = body.decode("utf-8")
            from application.static_assets import version_links
            text = version_links(text, WEB)
            if "<head>" in text:
                text = text.replace("<head>", "<head>" + inject, 1)
            else:
                text = inject + text
            body = text.encode("utf-8")
            return self._send(200, body, MIME[".html"])
        if target.suffix in ('.css', '.js'):
            from application.static_assets import cache_headers
            policy, etag = cache_headers(body, urlparse(self.path).query)
            unchanged = etag in [part.strip() for part in self.headers.get('If-None-Match', '').split(',')]
            return self._send(304 if unchanged else 200, b'' if unchanged else body,
                              MIME[target.suffix], cache_control=policy, etag=etag)
        return self._send(200, body,
                          MIME.get(target.suffix, "application/octet-stream"))

    # ---------------------------------------------------------------- PUT
    @shop_scope.http_scope
    def do_PUT(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        # ★★ 2026-10-14 前缀支持：界面可以挂在 /ui 下（nginx 或插件转发过来）。
        #   为什么要 strip 而不是另写一套路由：
        #     另写一套 = 每个路由都要两份，早晚不一致。
        #     strip 掉前缀后走【原来那套】⇒ 只有一处实现。
        #   ★ 插件 ui.mjs 会把 /ui/api/* 【原样】转给后端，
        #     所以这里必须同时覆盖页面和 API。
        path = _strip_prefix(path)
        # ★★★ 鉴权闸门（见文件顶部 AUTH_REQUIRED 的说明）。
        if not self._auth_ok():
            return

        if path == "/api/model-list":
            from application.model_config import list_models
            try:
                return self._json(list_models(self._read_body()))
            except ValueError as exc:
                return self._json({"ok": False, "error": str(exc)}, 400)
        if path == "/api/model-key":
            from application import model_config, plugin_runtime
            try:
                result = model_config.save_connection(self._read_body())
            except ValueError as exc:
                return self._json({"ok": False, "error": str(exc)}, 400)
            model_config.refresh_service(_service)
            model_config.refresh_service(plugin_runtime._SERVICE.get("svc"))
            return self._json({"ok": True, **result})
        if path == "/api/model-test":
            from application.model_config import test_connection
            self._read_body()
            result = test_connection()
            return self._json(result, 200 if result["ok"] else 400)

        if path == "/api/assistant":
            data = load_config()
            from application.config_patch import validate_patch
            patch = self._read_body()
            problems = validate_patch(data.assistant, patch, section="assistant")
            if problems:
                return self._json({"ok": False, "problems": problems}, status=400)
            data.assistant.apply_dict(patch)
            problems = data.assistant.validate()
            if problems:
                return self._json({"ok": False, "problems": problems}, status=400)
            # ★ 有非法值就不落盘 —— 宁可报错，也不要把坏配置存进去
            if not problems:
                save_config(data)
                from application import model_config, plugin_runtime
                if shop_scope.path() is None:
                    model_config.refresh_service(_service)
                    model_config.refresh_service(plugin_runtime._SERVICE.get("svc"))
            return self._json({"ok": not problems, "problems": problems,
                               "effective_prompt": data.assistant.build_prompt()})

        if path == "/api/delivery-auth":
            # ★ 保存出票接口的鉴权配置（密钥只写不读）。
            #   三个字段都可选：只改其中一项时不会把别的抹掉。
            from application import delivery_auth
            body = self._read_body()
            return self._json(delivery_auth.save_config(
                tenant_id=body.get("tenant_id"),
                shop_ids=body.get("shop_ids"),
                # ★ 密钥：不传 = 保留原值；传空字符串 = 清掉。
                #   界面不回显明文，所以"没动"和"清掉"必须能区分。
                delivery_key=body.get("delivery_key"),
                write_enabled=body.get("write_enabled")))

        if path == "/api/shops":
            # ★★★ 2026-10-14 店主加的：切换某个店铺的「自动发送」。
            #   ★ 只改【我们自己的】开关，绝不碰平台状态 ——
            #     官方只给了"读店铺列表"一个能力，改平台状态是我们做不到
            #     也不该做的事。
            #   ★ 入参 {shop_id, send_enabled}；返回改完的全量开关表，
            #     前端不用自己猜（也顺手把"到底存成什么了"暴露出来）。
            _body = self._read_body()
            sid = str(_body.get("shop_id") or "").strip()
            if not sid:
                return self._json({"ok": False, "error": "缺少 shop_id"}, status=400)
            if "send_enabled" not in _body:
                return self._json({"ok": False, "error": "缺少 send_enabled"},
                                  status=400)
            prefs = store.load_shop_prefs()
            if not isinstance(prefs, dict):
                prefs = {}
            one = prefs.get(sid)
            one = dict(one) if isinstance(one, dict) else {}
            if type(_body.get("send_enabled")) is not bool:
                return self._json({"ok": False, "error": "send_enabled 必须为布尔值"}, status=400)
            one["send_enabled"] = _body["send_enabled"]
            prefs[sid] = one
            store.save_shop_prefs(prefs)
            return self._json({
                "ok": True,
                "shop_id": sid,
                "send_enabled": one["send_enabled"],
                "shops_prefs": prefs,
                # ★ 说清它管什么，免得运营以为"我把平台上的店停用了"
                "scope": "只影响我们是否往这个店自动发消息，"
                         "不改鱼麦多那边的店铺启用状态",
            })

        if path == "/api/settings":
            _body = self._read_body()
            from application.config_patch import validate_patch
            problems = validate_patch(load_config(), _body)
            if problems:
                return self._json({"ok": False, "problems": problems}, status=400)
            # ★★ 2026-10-14：取票图是【一张图】，不能进 settings.json。
            #   为什么：那是一段 base64，混进配置文件会让它难读、
            #   每次读盘都变慢（settings.load() 每次都读盘）。
            #   ⇒ 在这里把图截出来，交给 application/ticket_image.py 存；
            #     settings.json 里那个字段清空（图的"在不在"由
            #     ticket-image.json 决定，不由这个字段决定）。
            # ★★★ 2026-10-14 修一个真 bug：前端每次都发
            #     "ticket_image": {"enabled": "...", "template": ""}
            #   —— 那个 template 是【隐藏输入框的值】：
            #      · 上传了新图 ⇒ 里面有 base64
            #      · 没上传     ⇒ 空字符串
            #   而这里原来是 `if "template" in _sec` ⇒ 把【空字符串】
            #   也当成"新图片内容" ⇒ store_upload("") ⇒ "没有图片内容"
            #   ⇒ 整个保存被拦下（problems 非空 ⇒ 不落盘）。
            #
            #   后果：**只要没上传过取票图，功能设置页的「保存配置」就永远失败**
            #   —— 改任何一个开关都存不进去，而界面上只显示一句
            #   "未保存：配置有问题"。这正是店主说的"这里怎么无法开启"。
            #
            #   ★ 修法：只有【真给了非空图片内容】才去存图。
            #     空字符串 = "这次没传新图" = 什么都不做（保留原有图）。
            #     要删图得走单独的动作，不能拿"没传"当"删除"用 ——
            #     那会让"随手保存一下设置"变成"把店主的图删了"。
            _ti_payload = None
            try:
                _sec = _body.get("ticket_image")
                if isinstance(_sec, dict) and "template" in _sec:
                    _raw_ti = _sec.pop("template")
                    if isinstance(_raw_ti, str) and _raw_ti.strip():
                        _ti_payload = _raw_ti
            except Exception:                             # noqa: BLE001
                _ti_payload = None
            data = load_config()
            data.apply_dict(_body)
            problems = data.validate()
            _ti_err = ""
            if not problems:
                if _ti_payload is not None:
                    from application.ticket_image import store_upload
                    _r = store_upload("取票图", _ti_payload)
                    if not _r.get("ok"):
                        _ti_err = str(_r.get("error") or "存图失败")
                        problems = list(problems) + [f"取票图：{_ti_err}"]
                if not problems:
                    save_config(data)
            return self._json({"ok": not problems, "problems": problems,
                               "ticket_image_error": _ti_err,
                               "derived": {
                                   "buyer_commands": data.buyer_commands(),
                                   "speed_tiers": [{"tier": t, "min_minutes": m}
                                                   for t, m in data.speed_tiers()],
                                   "quote_ttl_minutes": data.recognize_quote.quote_ttl_minutes,
                                   "confirm_keyword": data.fish_reprice.confirm_keyword,
                               }})

        if path == "/api/templates":
            # ★★ 这里原来是【真 bug】：前端一直在发 channel，
            #   后端 `body.get("key"), body.get("text")` 把 channel 丢掉了，
            #   然后 row["text"] = text 直接盖全局。
            #
            #   后果：运营在"万达"tab 下改文案，看起来改了万达专属的，
            #   实际盖掉了【全局】文案 —— 切到"良票"或"全局"tab 看到的一样。
            #   数据是错的，而且看不出来。
            #
            #   现在：channel='global'（或没传）→ 写全局 text；
            #         其他 channel            → 写该渠道的覆盖 channel_text[channel]
            body = self._read_body()
            key = body.get("key")
            text = body.get("text", "")
            variant = str(body.get("variant") or "").strip()
            channel = str(body.get("channel") or "global").strip() or "global"
            if channel not in {"global", "wanda", "liangpiao"} or not isinstance(text, str):
                return self._json({"error": "INVALID_TEMPLATE_INPUT"}, 400)

            row = next((t for t in templates_module.TEMPLATES if t["key"] == key), None)
            if row is None:
                return self._json({"error": f"未知模板 {key}"}, 404)

            if shop_scope.path() is not None:
                shop_scope.save_template(key, text, channel, body.get("inherit") is True,
                                         variant=variant or None)
                return self._json({"ok": True, "channel": channel,
                    "effective": templates_module.text_for(key, channel),
                    "parts": templates_module.render_messages(key, channel=channel)})
            if variant:
                if variant not in (row.get("variants") or {}):
                    return self._json({"error": "UNKNOWN_TEMPLATE_VARIANT"}, 400)
                row.setdefault("variants", {})[variant] = text
            elif channel == "global":
                row["text"] = text
            else:
                overrides = row.setdefault("channel_text", {})
                if body.get("inherit") is True:
                    default = templates_module.DEFAULT_CHANNEL_TEXT.get(key, {})
                    if channel in default:
                        overrides[channel] = default[channel]
                    else:
                        overrides.pop(channel, None)
                else:
                    overrides[channel] = text
            save_templates()

            return self._json({
                "ok": True,
                "channel": channel,
                # 实际会用的那份（便于前端立刻回显）
                "effective": templates_module.text_for(key, channel),
                "text": row["text"],
                "channel_text": row.get("channel_text") or {},
                "variants": row.get("variants") or {},
                "parts": templates_module.render_messages(key, channel=channel),
            })

        if path == "/api/credentials":
            body = self._read_body()
            cred = load_credentials()
            for key in ("app_key", "app_secret", "tel", "base_url"):
                if key in body:
                    cred[key] = str(body[key] or "").strip()
            save_credentials(cred)
            invalidate_service()          # 凭证变了，重新装配
            return self._json({"ok": True,
                               "configured": {
                                   "app_key": cred["app_key"][:6] + "…" if cred["app_key"] else "",
                                   "tel": cred["tel"]}})

        if path == "/api/message":
            # ★ 买家消息的唯一入口 —— 把编排层接到 HTTP 上
            body = self._read_body()
            chat_id = str(body.get("chat_id") or "").strip()
            if not chat_id:
                return self._json({"ok": False, "error": "chat_id 必填"}, 400)
            cred = load_credentials()
            # ★★ 门槛放在【正确的地方】：
            #   tel 是【下单】才需要的（良票文档：tel 必填）。
            #   而"回一句话"不需要 —— 早先一进门就拦 tel，
            #   结果没配 tel 时连「在吗」都回不了，AI 也调不起来。
            #   现在只有走到要下单那一步才需要，缺了会在下单分支报错。
            if body.get("image_url") and not cred.get("tel"):
                return self._json(
                    {"ok": False,
                     "error": "还没配购票预留手机号 tel —— 下单必填，"
                              "填了才能识别截图和报价"}, 400)
            try:
                svc = get_service()
                # ★ 渠道从【店铺配置】来，不从请求体来。
                #   理由：买家进来时还不知道影院，无法靠影院身份路由
                #   （V5 那套 ProviderRouter 是【询价时】才定供商的）。
                #   而"这个店属于哪个渠道"是店铺的固有属性，配置一次就够。
                #   请求体里给了 channel 就优先用它（测试/多店场景要）。
                body_channel = str(body.get("channel") or "").strip()
                result = svc.on_message(
                    chat_id, str(body.get("text") or ""),
                    image_url=str(body.get("image_url") or ""),
                    city_name=body.get("city_name") or None,
                    channel=body_channel or (cred.get("channel") or None))
            except ValueError as exc:
                # 参数类错误（比如缺 tel 去下单）→ 400，说清楚
                return self._json({"ok": False, "error": str(exc)}, 400)
            except Exception as exc:                # noqa: BLE001
                return self._json({"ok": False,
                                   "error": f"{type(exc).__name__}: {exc}"}, 500)
            return self._json({
                "ok": result.ok,
                "state": result.state.value,
                "messages": result.messages,
                "template_keys": result.template_keys,
                "need_human": result.need_human,
                "error": result.error,
                "notes": result.notes,
                "quote": {
                    "rule": result.quote.rule_name,
                    "price_mode": result.quote.price_mode,
                    "total_fen": result.quote.total_price_fen,
                    "max_price_fen": result.quote.max_price_fen,
                } if result.quote else None,
            })

        if path == "/api/refund":
            body = self._read_body()
            chat_id = str(body.get("chat_id") or "").strip()
            if not chat_id:
                return self._json({"ok": False, "error": "chat_id 必填"}, 400)
            try:
                result = get_service().on_refund(chat_id)
            except Exception as exc:                # noqa: BLE001
                return self._json({"ok": False,
                                   "error": f"{type(exc).__name__}: {exc}"}, 500)
            return self._json({"ok": result.ok, "state": result.state.value,
                               "messages": result.messages,
                               "template_keys": result.template_keys,
                               "notes": result.notes, "error": result.error})

        if path == "/api/rules":
            body = self._read_body()
            tenant_id, shop_id = str(body.get("tenant_id") or ""), str(body.get("shop_id") or "")
            try:
                store.rules_path(tenant_id, shop_id)
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
            incoming = body.get("rules")
            if not isinstance(incoming, list):
                return self._json({"error": "rules 必须是数组"}, 400)
            known = qr.QuoteRule.__dataclass_fields__
            rules = []
            for i, item in enumerate(incoming):
                if not isinstance(item, dict):
                    return self._json(
                        {"error": f"第 {i + 1} 条不是对象"}, 400)
                try:
                    # ★★ 分档表要单独还原成 PricingTier。
                    #   直接 QuoteRule(**item) 会把 tiers 留成一堆 dict，
                    #   报价时 `getattr(row, "lower_bps")` 拿不到值 ——
                    #   落档会静默落到默认档（0-10000），价格看起来"正常"
                    #   但是错的。这是加分档时最容易漏的一步。
                    tiers = [
                        qr.PricingTier(
                            **{k: v for k, v in t.items()
                               if k in qr.PricingTier.__dataclass_fields__})
                        for t in (item.get("tiers") or [])
                        if isinstance(t, dict)
                    ]
                    rules.append(qr.QuoteRule(
                        **{k: v for k, v in item.items()
                           if k in known and k != "tiers"},
                        tiers=tiers))
                except (TypeError, AttributeError) as exc:
                    return self._json(
                        {"error": f"第 {i + 1} 条规则格式不对：{exc}"}, 400)

            problems = qr.validate_rules(rules)
            # ★ 有矛盾就不落盘 —— 与配置页同一口径。
            #   规则自相矛盾会导致下单被 300005 拒，不能存进去。
            if not problems:
                save_rules(rules, tenant_id, shop_id)
            return self._json({"ok": not problems, "problems": problems})

        if path == "/api/qa":
            # ★ 保存关键词回复（智能问答）。
            #   整表提交：界面把全部条目（改过的）一起发上来，整体替换。
            #   为什么不做单条 PATCH：运营可能同时改多条、调顺序、删条目，
            #   整表提交语义最清楚，也不会出现"改了一半"的中间态。
            body = self._read_body()
            rows = body.get("rules")
            if not isinstance(rows, list):
                return self._json({"ok": False, "error": "rules 必须是数组"}, 400)

            import qa as qa_module
            known = set(qa_module.QARule.__dataclass_fields__)
            rules = []
            for i, item in enumerate(rows):
                if not isinstance(item, dict):
                    return self._json({"ok": False,
                                       "error": f"第 {i+1} 条不是对象"}, 400)
                try:
                    rules.append(qa_module.QARule(
                        **{k: v for k, v in item.items() if k in known}))
                except TypeError as exc:
                    return self._json({"ok": False,
                                       "error": f"第 {i+1} 条字段有问题：{exc}"}, 400)

            # ★ 保存前校验：这些错误不拦，会等到给买家发消息时才炸
            problems = _validate_qa(rules, qa_module)
            if problems:
                return self._json({"ok": False,
                                   "error": "；".join(problems[:3]),
                                   "problems": problems}, 400)

            store.save_qa_rules(rules)
            qa_module.QA_RULES[:] = rules
            qa_module.reindex()          # ★ 不重建索引 = 改了不生效
            return self._json({"ok": True, "count": len(rules)})

        return self._json({"error": "unknown endpoint"}, 404)

    # ---------------------------------------------------------------- POST
    def _handle_plugin(self, path: str) -> None:
        """
        鱼麦多的 /api/v1/* 端点（★ 全部先验签）。

        ★ 响应形状照插件源码的读法：
            claim    → {"commands": [...]}
            result   → 200 即可
            events   → 任意（插件不读内容）
        """
        from application import plugin_api
        from integrations.yumaiduo import signature as sig

        raw_bytes = self._read_raw()
        raw = raw_bytes.decode("utf-8") if isinstance(raw_bytes, bytes) else raw_bytes
        ok, why = sig.verify(
            plugin_api.plugin_secret(),
            self.headers.get("x-yumaiduo-timestamp"),
            self.headers.get("x-yumaiduo-signature"), raw)
        if not ok:
            # ★ 不说具体哪里错（签名错/时间戳过期）——
            #   那会给攻击者信息。只记在我们自己的日志里。
            print(f"[插件] 验签失败 {path}: {why}")
            return self._json({"ok": False, "error": "invalid_plugin_signature"},
                              status=401)

        try:
            body = json.loads(raw) if raw.strip() else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._json({"ok": False, "error": "invalid_json"}, status=400)

        # ★★★ 插件端点请求日志（2026-10-14 加，切换 V5 时排障用）。
        #   为什么需要：原来这条路上【一句日志都没有】——
        #   切换时完全不知道插件有没有打进来，排查全靠猜。
        #   ★ 只记路径和结果，不记 body：body 里有买家的消息内容，
        #     写进日志等于把买家隐私落盘。
        try:
            import time as _t
            _t0 = _t.monotonic()
            _who = str(self.headers.get("x-yumaiduo-timestamp") or "")
            print(f"[插件] → {path} (ts={_who[:6] or '-'})", flush=True)
        except Exception:                                     # noqa: BLE001
            _t0 = None

        if path == "/api/v1/events":
            # ★ 走到这里说明签名对过了，verified=True
            return self._json(plugin_api.handle_event(body, verified=True))

        if path == "/api/v1/events/run-due":
            return self._json(plugin_api.handle_run_due())

        if path == "/api/v1/commands/claim":
            limit = body.get("limit", 10) if isinstance(body, dict) else 10
            result = plugin_api.handle_claim(int(limit or 10))
            # ★ 形状：插件读 data.commands（V5 是 {"commands": [...]}）
            return self._json({"commands": result.get("commands") or []})

        if path == "/api/v1/fulfillment/auto/run":
            return self._json(plugin_api.handle_fulfillment_auto())

        if path == "/api/v1/recovery/inspect":
            return self._json(plugin_api.handle_recovery_inspect())

        # /api/v1/commands/{command_id}/result
        m = re.match(r"^/api/v1/commands/([^/]+)/result$", path)
        if m:
            return self._json(plugin_api.handle_result(
                m.group(1), body if isinstance(body, dict) else {}))

        # ★★ 六个授权闸门（命令排队后、真正动手前再判一次）。
        #
        #   插件源码里的调用点：
        #     authorize-message-send      message-history.mjs:29
        #     authorize-execution         reprice.mjs:138
        #     authorize-ticket-delivery   ticket-delivery.mjs:17 / wanda-shipment.mjs:11
        #     authorize-polish            product-polish.mjs:11
        #     authorize-close             failed-order-close.mjs:22
        #     authorize-engagement        order-engagement.mjs:30
        #
        #   ★ 不做这些的后果：插件领到命令后【第一步就 GATE_REJECTED】，
        #     永远发不出去 —— 看着"命令队列做完了"，实际一步都没执行。
        #     （这就是我一开始漏掉的那一环。）
        from application import plugin_authorize as _auth
        m = re.match(
            r"^/api/v1/commands/([^/]+)/authorize-"
            r"(message-send|execution|ticket-delivery|polish|close|engagement)$",
            path)
        if m:
            cid, kind = m.group(1), m.group(2)
            payload = body if isinstance(body, dict) else {}
            if kind == "message-send":
                return self._json(_auth.authorize_message_send(cid))
            if kind == "execution":
                return self._json(_auth.authorize_execution(cid, payload))
            if kind == "ticket-delivery":
                return self._json(_auth.authorize_ticket_delivery(
                    cid, detail=payload.get("detail"),
                    session=payload.get("session")))
            if kind == "polish":
                return self._json(_auth.authorize_polish(cid))
            if kind == "close":
                # ★★★ 2026-10-14 实现（原来一律回 NOT_IMPLEMENTED:close）。
                #   插件 failed-order-close.mjs:22 就在等这个接口，
                #   而"金额不符/没报价就付款"两条路都要靠关单止损。
                #   插件会把线上订单详情放在 body 里 ——
                #   我们用【后端此刻的状态】再判一次能不能关。
                return self._json(_auth.authorize_close(
                    cid, payload if payload else None))
            # engagement：★ 我们还没用这类命令，但插件会来问。
            #   直接回"不许" —— 不能让一个没实现的动作被执行。
            return self._json({"allowed": False,
                               "reason": f"NOT_IMPLEMENTED:{kind}"})

        if path == "/api/v1/products/polish/run":
            # ★ 插件每 60 秒来催一次（server.mjs:49）。
            #   我们还没做商品擦亮 —— 回 queued=0，不假装做了。
            return self._json({"ok": True, "queued": 0})

        return self._json({"ok": False, "error": "unknown_plugin_endpoint"},
                          status=404)

    @shop_scope.http_scope
    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        # ★★ 2026-10-14 前缀支持：界面可以挂在 /ui 下（nginx 或插件转发过来）。
        #   为什么要 strip 而不是另写一套路由：
        #     另写一套 = 每个路由都要两份，早晚不一致。
        #     strip 掉前缀后走【原来那套】⇒ 只有一处实现。
        #   ★ 插件 ui.mjs 会把 /ui/api/* 【原样】转给后端，
        #     所以这里必须同时覆盖页面和 API。
        path = _strip_prefix(path)
        # ★★★ 鉴权闸门（见文件顶部 AUTH_REQUIRED 的说明）。
        if not self._auth_ok():
            return

        if path == "/api/config-push":
            body = self._read_body()
            from integrations.yumaiduo.shop_client import read_shops
            from application.plugin_api import plugin_secret
            shops = read_shops(secret=plugin_secret()).get("shops", [])
            tenant, target = str(body.get("tenant_id") or ""), str(body.get("shop_id") or "")
            if not any(str(x.get("tenant_id")) == tenant and str(x.get("shop_id")) == target for x in shops):
                return self._json({"error": "目标店铺不在当前授权店铺列表"}, 400)
            if shop_scope.CURRENT.get() == (tenant, target):
                return self._json({"error": "请选择其他目标店铺"}, 400)
            try:
                return self._json(shop_scope.push(body.get("kind"), tenant, target))
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)

        # ★ 保存插件密钥（状态查询在 do_GET 的同名分支里）。
        #   ★ 只写不读：不传 = 保留原值。
        if path == "/api/plugin-auth":
            from application import delivery_auth as _da
            from application import plugin_api
            body = self._read_body()
            key = body.get("secret")
            if key is not None and str(key).strip():
                plugin_api.save_plugin_secret(str(key).strip())
            secret = plugin_api.plugin_secret()
            return self._json({"ok": True, "configured": bool(secret),
                               "secret_hint": _da.mask_secret(secret)})

        if path == "/api/delivery-auth":
            # ★ 保存出票接口的鉴权配置（密钥只写不读）。
            #   三个字段都可选：只改其中一项时不会把别的抹掉。
            from application import delivery_auth
            body = self._read_body()
            return self._json(delivery_auth.save_config(
                tenant_id=body.get("tenant_id"),
                shop_ids=body.get("shop_ids"),
                # ★ 密钥：不传 = 保留原值；传空字符串 = 清掉。
                #   界面不回显明文，所以"没动"和"清掉"必须能区分。
                delivery_key=body.get("delivery_key"),
                write_enabled=body.get("write_enabled")))

        # ★★ 鱼麦多插件端点。全部要过 HMAC 签名校验。
        #
        #   为什么必须验签：这些端点能
        #     · 往里塞事件（伪造买家消息 → 让系统照着下单）
        #     · 领走命令（真插件就领不到了）
        #     · 报执行结果（把没发的消息标成已发）
        #   ⇒ 没有验签等于把这三个能力开放给任何知道地址的人。
        #
        #   ★ 签名要对【原始字节】算，所以这里用 _read_raw() 而不是
        #     _read_body() —— 后者解析过 JSON，再序列化键序/空格会变，
        #     合法请求会被判成伪造（踩过这个坑在 signature.py 里写了）。
        if path == "/api/v1/ticket-system":
            from application import ticket_system_handoff as handoff
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                handoff.authenticate(self.headers, raw)
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError("INVALID_BODY")
                return self._json(handoff.handle(body))
            except ValueError as exc:
                return self._json({"ok": False, "error": str(exc)}, status=409)
            except Exception:
                return self._json({"ok": False, "error": "HANDOFF_UNAVAILABLE"}, status=503)

        if path.startswith("/api/v1/"):
            return self._handle_plugin(path)

        if path == '/api/order/manual-settlement':
            if (self.headers.get('Sec-Fetch-Site') == 'cross-site'
                    or not self.headers.get('Content-Type', '').lower().startswith('application/json')):
                return self._json({'error': '仅支持后台页面的JSON请求'}, 403)
            body = self._read_body()
            if not isinstance(body, dict):
                return self._json({'error': 'INVALID_BODY'}, 400)
            from application import ledger, payment_sync
            from application.purchase_actions import platform_order
            from integrations.yumaiduo.shop_client import read_shops
            from application.plugin_api import plugin_secret
            tenant, shop = str(body.get('tenant_id') or ''), str(body.get('shop_id') or '')
            try:
                shops = read_shops(secret=plugin_secret()).get('shops', [])
                if not any(str(x.get('tenant_id')) == tenant and str(x.get('shop_id')) == shop for x in shops):
                    return self._json({'error': '店铺未授权'}, 403)
                result = payment_sync.confirm_manual_price(ledger.stores(),
                    order_id=str(body.get('order_id') or ''), tenant_id=tenant, shop_id=shop,
                    amount_fen=body.get('amount_fen'), expected_quote_id=body.get('expected_quote_id'),
                    expected_amount_fen=body.get('expected_amount_fen'),
                    expected_updated_at=body.get('expected_updated_at'), actor=AUTH_USER,
                    approval_id=str(body.get('approval_id') or ''), platform_read=platform_order)
                return self._json(result)
            except ValueError as exc:
                return self._json({'error': str(exc)}, 409)
            except Exception:
                return self._json({'error': '人工成交价未登记，请刷新核对后重试'}, 503)

        if path == "/api/order/deliver":
            # ★ 出票后台的入口（界面上"标记出票"按钮也走这里）。
            #
            #   为什么走 HTTP 而不是让界面直接调函数：
            #   真实场景里【店家的出票后台】在另一个进程/另一台机器，
            #   它只能通过 HTTP 把出票结果交进来。
            #   界面按钮走同一条路，就不会出现"界面能用、后台不能用"。
            #
            #   ★★ 鉴权（照线上 V5 的 integrations/wanda_ticketing.py）：
            #     Bearer 密钥 + 租户白名单 + 店铺白名单 + 写开关。
            #     这个接口能决定给买家的票 —— 暴露在公网而没有鉴权，
            #     就等于谁都能把别人的单标成已出票、或塞伪造的取票码。
            #     实现在 application/delivery_auth.py。
            body = self._read_body()
            from application import delivery_auth, ledger

            order_id = (body.get("order_id") or "").strip()
            # ★ 店铺白名单必须按【订单自己的 shop_id】判 ——
            #   不能让调用方自己声明是哪个店（那等于没有白名单）。
            shop_id = ""
            stores = ledger.stores()
            if stores.orders is not None and order_id:
                found = stores.orders.get(order_id)
                shop_id = (found.shop_id if found else "") or ""

            auth = delivery_auth.check(
                self.headers.get("Authorization"), shop_id=shop_id, write=True)
            if not auth.ok:
                return self._json(
                    {"ok": False, "error": auth.error,
                     "message": auth.message}, status=auth.status)

            from application import wanda_qr
            try:
                result = wanda_qr.receive(order_id, body.get("image_data"),
                    tenant=auth.principal.tenant_id, shops=auth.principal.shop_ids,
                    operator=str(body.get("operator") or "").strip(),
                    upstream_id=str(body.get("provider_order_id") or "").strip())
            except ValueError as exc:
                return self._json({"ok": False, "error": str(exc)}, status=400)
            return self._json(result)

        if path == "/api/callback":
            # ★ 良票回调。文档：平台【只看响应体，不看 HTTP 状态码】，
            #   必须在 10 秒内返回 {"code":0}。
            #   ★★ 验签失败绝不能回 {"code":0} —— 那等于接受了伪造回调。
            #
            #   ★ 注意顺序：【先验签，再碰服务】。
            #     早先写成先 get_service()，结果 tel 没配时服务装配就抛异常，
            #     应答变成 500 —— 看起来像"验签失败"，其实根本没验签。
            #     验签只依赖 app_key/app_secret，不该被别的配置拖累。
            raw = self._read_raw()
            headers = {k: v for k, v in self.headers.items()}
            try:
                ack, result, notes = self._handle_callback(raw, headers)
            except Exception as exc:                # noqa: BLE001
                # 处理出错时回非 0，让平台按退避策略重推 ——
                # 重推能救回来；回了 0 就永远丢了。
                ack = json.dumps({"code": 500, "message": str(exc)},
                                 ensure_ascii=False)
                result, notes = None, []
            for line in notes:
                print(f"[回调] {line}")
            return self._send(200, ack.encode("utf-8"),
                              "application/json; charset=utf-8")

        return self._json({"error": "method not allowed"}, 405)

    def _handle_callback(self, raw: str, headers: dict):
        """
        回调处理：分成"验签"和"执行业务"两步。

        ★ 验签用凭证文件里的值，不依赖 tel、不依赖服务能否装配。
          这样"凭证填了一半"也能正确拒绝伪造回调。
        """
        from integrations.liangpiao import callbacks as cb

        cred = load_credentials()
        app_key = cred.get("app_key") or ""
        app_secret = cred.get("app_secret") or ""
        if not (app_key and app_secret):
            return (json.dumps({"code": 503, "message": "服务端还没配良票凭证"},
                               ensure_ascii=False), None, [])

        # Verify once, acknowledge only after durable receipt. Recognition keeps
        # its existing path; order status processing no longer needs an AI service.
        event = cb.parse(raw)
        if event.event == cb.EVENT_RECOGNITION_FINISHED:
            cb.verify(app_key, app_secret, headers, raw, now=int(__import__('time').time()))
            ack, result = get_service().on_callback(raw, headers)
            return ack, result, (result.notes if result else [])
        from application import liangpiao_sync, plugin_api
        ack = liangpiao_sync.receive(raw, headers, app_key, app_secret, plugin_api.queue())
        return ack, None, []

    def _read_raw(self) -> str:
        """原样读出请求体。

        ★ 必须【原样】—— 回调验签是对原始字节算的，
          重新 json.dumps 一次会导致验签失败。
        """
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return ""
        return self.rfile.read(length).decode("utf-8", "replace")

    do_DELETE = do_PATCH = lambda self: self._json(
        {"error": "method not allowed"}, 405)


def main() -> None:
    # ★ 启动时把界面保存过的改动读回来：
    #   文案、配置、关键词回复 —— 都不是改代码，是覆盖默认值。
    load_template_overrides()
    store.load_qa_overrides()
    # ★★★ 要鉴权却没有密码 ⇒ 【拒绝启动】。
    #   为什么不"自动生成一个密码"或"降级成不鉴权"：
    #     · 自动生成 ⇒ 密码只出现在日志里，运维多半看不到，
    #       结果是"以为有鉴权、其实没人知道密码"
    #     · 降级不鉴权 ⇒ 留一个公开后台，比"起不来"严重得多
    #   ⇒ 宁可起不来，也不留一个没密码的公开后台。
    #   （我第一版把这段写丢了 —— 脚本在内联引号处崩了，没写进文件，
    #     而我又没核对，结果"C 情形"实际上没生效。核对过才对。）
    if AUTH_REQUIRED and not AUTH_PASSWORD:
        print("★ 拒绝启动：已要求鉴权（" + AUTH_WHY + "）但没设 TICKET_AGENT_PASSWORD。")
        print("  ⇒ 设密码：export TICKET_AGENT_PASSWORD=<你的密码>")
        print("  ⇒ 或显式关掉：export TICKET_AGENT_AUTH=off（确认只绑本机时才用）")
        raise SystemExit(2)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"服务已启动：http://{HOST}:{PORT}")
    print(f"  鉴权：{'★ 已开启（Basic Auth，用户 ' + AUTH_USER + '）' if AUTH_REQUIRED else '关闭'}"
          f" —— {AUTH_WHY}")
    print(f"  http://{HOST}:{PORT}/          AI助理")
    print(f"  http://{HOST}:{PORT}/replies   回复文案")
    print(f"  http://{HOST}:{PORT}/qa        关键词回复")
    print(f"  http://{HOST}:{PORT}/features  功能设置")
    print(f"  http://{HOST}:{PORT}/rules     报价规则")
    print(f"  http://{HOST}:{PORT}/quotes    报价记录")
    print(f"  http://{HOST}:{PORT}/orders    订单管理")
    print("  按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        server.server_close()


if __name__ == "__main__":
    main()
