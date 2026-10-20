"""插件出站客户端：从插件读店铺列表。

★★ 为什么需要这个文件
   店铺列表只有【插件进程】能拿到 —— 它持有鱼麦多的 `client`（带 pluginToken），
   调 `core.shops.list()`。后端进程没有那个 client。
   插件的 server.mjs 已经开了 `/internal/shops/read` 给后端用，但后端一直没调它。

★ 签名算法（照插件 side 的 verify 写，两边必须逐字一致）
   插件 server.mjs：
       function verify(rawBody, req) {
         const timestamp = req.headers['x-yumaiduo-timestamp'];
         const signature = req.headers['x-yumaiduo-signature'];
         ...
         return verifyWebhookSignature({secret: cfg.secret, timestamp, signature, rawBody});
       }
   SDK webhook.js：
       expected = createHmac('sha256', secret).update(`${ts}.${rawBody}`).digest('hex')
       且 |now - ts| > 300_000 直接拒
   ⇒ 我们发出去要带：
       X-Yumaiduo-Timestamp: 毫秒时间戳
       X-Yumaiduo-Signature: hmac_sha256(secret, f"{ts}.{body}") 的 hex
     ★ 注意签的是 `ts.rawBody`（点号连接），不是只签 body。
     ★ 时间戳是【毫秒】—— SDK 用 Number() 后跟 Date.now() 比。
"""
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

# 后端到插件的地址。★ 同机部署就是 127.0.0.1:4015（插件的 runtime.port）。
ENV_PLUGIN_URL = "YUMAIDUO_PLUGIN_URL"
DEFAULT_PLUGIN_URL = "http://127.0.0.1:4015"

# 与 SDK 一致的时间戳容差（秒）
TOLERANCE_SEC = 300


class PluginShopError(RuntimeError):
    """读店铺失败。★ 带上一个稳定的 code，让上层能区分"没配"和"读不到"。"""

    def __init__(self, code: str, message: str = "", status: int = 0):
        super().__init__(message or code)
        self.code = code
        self.status = status


def plugin_url() -> str:
    return (os.getenv(ENV_PLUGIN_URL) or DEFAULT_PLUGIN_URL).strip().rstrip("/")


def signature(secret: str, timestamp_ms: int, raw_body: str) -> str:
    """★ 逐字对齐 SDK：HMAC-SHA256(secret, f"{ts}.{rawBody}")。"""
    msg = f"{timestamp_ms}.{raw_body}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


class PluginShopsClient:
    """读插件里的店铺列表。

    ★ transport 可替换 —— 测试里传假的，不碰网络（照 liangpiao 客户端的做法）。
    """

    def __init__(self, *, secret: str = "", base_url: str = "",
                 timeout: float = 10.0, transport=None):
        self.secret = secret
        self.base_url = (base_url or plugin_url()).rstrip("/")
        self.timeout = timeout
        self._transport = transport

    # ---------------------------------------------------------------- 发送
    def _post(self, path: str, payload: dict) -> tuple[int, str]:
        body = json.dumps(payload, ensure_ascii=False)
        ts = int(time.time() * 1000)
        headers = {
            "content-type": "application/json",
            "x-yumaiduo-timestamp": str(ts),
            "x-yumaiduo-signature": signature(self.secret, ts, body),
        }
        if self._transport is not None:
            return self._transport.send(self.base_url + path, body, headers,
                                        self.timeout)
        return self._urllib_send(self.base_url + path, body, headers)

    @staticmethod
    def _urllib_send(url: str, body: str, headers: dict) -> tuple[int, str]:
        data = body.encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.read().decode("utf-8", "replace")
            except Exception:                              # noqa: BLE001
                return exc.code, ""
        except Exception as exc:                            # noqa: BLE001
            raise PluginShopError("PLUGIN_UNREACHABLE", str(exc)) from exc

    # ---------------------------------------------------------------- 读店铺
    def upload_ticket_image(self, data_b64: str, tenant_id: str,
                            account_unb: str) -> dict:
        """
        把一张图上传到鱼麦多，返回 {imageUrl,width,height}。

        ★ 为什么必须上传、不能自己造 URL：
          插件的 validateReplyImage 要求 hostname 必须是 alicdn.com 且带
          尺寸 —— 这个 URL 只有平台上传接口会给。
        ★ 复用本类的 _post（同一把密钥、同一个签名算式）——
          不另写一套签名，那是"同一个东西两处实现"的老毛病。
        """
        status, body = self._post("/internal/ticket-image/upload", {
            "tenant_id": tenant_id, "accountUnb": account_unb,
            "data": data_b64,
        })
        if status != 200:
            raise PluginShopError("TICKET_IMAGE_UPLOAD_FAILED",
                                  f"上传失败 HTTP {status}: {body[:120]}", status)
        try:
            got = json.loads(body)
        except (ValueError, TypeError) as exc:
            raise PluginShopError("TICKET_IMAGE_BAD_JSON",
                                  f"上传返回的不是 JSON: {exc}") from exc
        if not isinstance(got, dict) or not got.get("imageUrl"):
            raise PluginShopError("TICKET_IMAGE_BAD_SHAPE", str(got)[:120])
        return got

    def query_blacklist(self, peer_unb: str, tenant_id: str) -> dict:
        """
        查买家黑名单。返回插件给的原样 {blacklisted, reason, peerNick, createdAt}。

        ★ 复用 _post（同一把密钥、同一个签名算式）——
          不另写一套签名，那是"同一个东西两处实现"的老毛病。
        ★ 403 单独给一个 code（BLACKLIST_SCOPE_REQUIRED）：
          那不是故障，是"权限还没授予" —— 处置方式完全不同
          （一个要去点同意，一个要查进程）。
        """
        if not self.secret:
            raise PluginShopError(
                "PLUGIN_SECRET_MISSING",
                "没有插件密钥（自注册交接文件没读到）—— 查不了黑名单")
        if not tenant_id:
            raise PluginShopError("TENANT_ID_MISSING", "没有租户 id")
        status, text = self._post("/internal/blacklist/query",
                                  {"tenant_id": tenant_id,
                                   "peer_unb": peer_unb})
        if status == 403:
            raise PluginShopError(
                "BLACKLIST_SCOPE_REQUIRED",
                "鱼麦多返回 403：这个租户还没同意「查询买家黑名单」权限"
                "（要去应用市场订阅页勾选同意）", status)
        if status != 200:
            raise PluginShopError("PLUGIN_BLACKLIST_FAILED",
                                  f"插件返回 HTTP {status}: {text[:120]}",
                                  status)
        try:
            got = json.loads(text)
        except (ValueError, TypeError) as exc:
            raise PluginShopError("PLUGIN_BLACKLIST_BAD_JSON",
                                  f"{exc}: {text[:120]}") from exc
        if not isinstance(got, dict):
            raise PluginShopError("PLUGIN_BLACKLIST_BAD_SHAPE",
                                  f"期望对象，得到 {type(got).__name__}")
        # ★ 插件自己判成失败时也走错误这条路（它会把 error 放在 body 里）
        if got.get("ok") is False:
            raise PluginShopError(str(got.get("error") or "BLACKLIST_FAILED"),
                                  str(got.get("error") or ""), status)
        return got

    def list_shops(self, tenant_id: str) -> list[dict]:
        """
        读店铺列表。

        返回插件给的原始形状（已经是我们这边要的字段名）：
            [{tenant_id, shop_id, display_name, platform_status,
              platform_enabled, im_enabled, area}, ...]

        ★ 失败一律抛 PluginShopError，带稳定 code。
          为什么不返回空列表：那会让界面显示"没有店铺"，
          而真实原因是"插件不通/没配密钥" —— 两种情况的处置完全不同。
        """
        if not self.secret:
            raise PluginShopError(
                "PLUGIN_SECRET_MISSING",
                "没有插件密钥（自注册交接文件没读到）—— 读不了店铺列表")
        if not tenant_id:
            raise PluginShopError("TENANT_ID_MISSING", "没有租户 id")

        status, text = self._post("/internal/shops/read",
                                  {"tenant_id": tenant_id})
        if status == 401:
            raise PluginShopError("PLUGIN_SIGNATURE_REJECTED",
                                  "插件拒绝了签名（密钥不一致）", status)
        if status == 403:
            raise PluginShopError("SHOP_SCOPE_REQUIRED",
                                  "缺 account.read 权限（租户没同意）", status)
        if status != 200:
            raise PluginShopError("PLUGIN_SHOP_READ_FAILED",
                                  f"HTTP {status}: {text[:200]}", status)
        try:
            rows = json.loads(text)
        except Exception as exc:                            # noqa: BLE001
            raise PluginShopError("PLUGIN_SHOP_BAD_JSON",
                                  f"{exc}: {text[:120]}") from exc
        if not isinstance(rows, list):
            raise PluginShopError("PLUGIN_SHOP_BAD_SHAPE",
                                  f"期望数组，得到 {type(rows).__name__}")
        return [r for r in rows if isinstance(r, dict) and r.get("shop_id")]


def upload_ticket_image(data_b64: str, *, secret: str = "", tenant_id: str = "",
                        account_unb: str = "", transport=None) -> dict:
    """
    门面：上传取票图。返回 {ok, image, error, error_code}。

    ★ 和 read_shops 一样：永不抛异常到调用方（尤其是出票那条路）——
      取票图是附赠的，传不上去不该让出票结果失败。
    """
    if not tenant_id:
        import os
        tenant_id = (os.getenv("YUMAIDUO_TENANT_ID") or "").strip()
    client = PluginShopsClient(secret=secret, transport=transport)
    try:
        img = client.upload_ticket_image(data_b64, tenant_id, account_unb)
    except PluginShopError as exc:
        return {"ok": False, "image": None, "error": str(exc),
                "error_code": exc.code}
    return {"ok": True, "image": img, "error": "", "error_code": ""}


# 插件密钥相关的环境变量名与默认路径
ENV_PLUGIN_SECRET = "YUMAIDUO_WEBHOOK_SECRET"
ENV_SECRET_FILE = "V5_BACKEND_SECRET_FILE"

# ★ 手动兜底值由【调用方】提供（见 resolve_plugin_secret 的说明）。
#   为什么用模块变量而不是参数：它要在很深的调用链里被用到，
#   一路往下传参数会污染每一层的签名。上层启动时设一次即可。
_CREDENTIAL_FALLBACK = ""


def set_credential_fallback(value: str) -> None:
    """上层启动时把凭证文件里的手动兜底密钥设进来（可选）。"""
    global _CREDENTIAL_FALLBACK
    _CREDENTIAL_FALLBACK = str(value or "").strip()


def resolve_plugin_secret() -> str:
    """
    插件密钥从哪来（取值优先级，照 application/plugin_api 的原实现搬下来）。

        ① 环境变量 YUMAIDUO_WEBHOOK_SECRET（运维显式指定，最权威）
        ② 插件自注册后交接的文件（V5_BACKEND_SECRET_FILE）★ 正常路径
        ③ 手动兜底值（由上层 set_credential_fallback 设进来）

    ★★ 为什么这一层管这件事（2026-10-14 挪下来的）：
      原来它在 application/plugin_api.py。但 application/blacklist.py
      也要取密钥（查买家黑名单），一 import plugin_api 就成环：
          blacklist → plugin_api → plugin_runtime → service → blacklist
      「密钥从哪来」是【外部事实】（环境变量、文件路径），
      本来就该由最内层 integrations 负责读，上层取用。
      ⇒ 挪到这里，环自然断了；plugin_api.plugin_secret() 改为转调本函数，
        优先级规则只有一份（避免"两处实现早晚不一致"）。

    ★ 为什么 ③ 是"上层设进来"的而不是在这里 import application.store：
      分层契约是"内层不许依赖外层"。为了不多一处违规，
      凭证文件的读取留在上层（application），设进来即可。
    """
    env = (os.getenv(ENV_PLUGIN_SECRET) or "").strip()
    if env:
        return env
    handed = _secret_from_handoff_file()
    if handed:
        return handed
    return _CREDENTIAL_FALLBACK


def _secret_from_handoff_file() -> str:
    """
    读插件自注册后写的交接文件（正常路径）。

    ★ 为什么这是"正常路径"：平台在【每次插件启动】时重新颁发密钥，
      插件把它写到 V5_BACKEND_SECRET_FILE。读这个文件永远拿到最新的。
    ★ 读不到就返回空，不抛 —— 调用方按"没有密钥"处理即可。
    """
    path = (os.getenv(ENV_SECRET_FILE) or "").strip()
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return str((data or {}).get("webhookSecret") or "").strip()
    except Exception:                                         # noqa: BLE001
        return ""


def read_blacklist(peer_unb: str, *, secret: str = "", tenant_id: str = "",
                   transport=None) -> dict[str, Any]:
    """
    门面：查这个买家在不在鱼麦多黑名单里。
    返回 {ok, blacklisted, reason, peer_nick, error, error_code}。

    ★★★ ok=False 的两种含义必须分清（这是本函数最要紧的一点）：
        ok=True,  blacklisted=False  → 查到了，这买家【不在】黑名单
        ok=False                      → 【没查成】（没授权 / 插件不通 / 没配 peer）
      ⇒ 调用方绝不能把 ok=False 当成"不在黑名单"就静默放过，
        也不能当成"在黑名单"就把买家挡掉。它是"不知道"。
      ★ 我们选择"不知道 ⇒ 照常回复"（fail-open）：
        黑名单是个【可选的加固】，没它系统也该正常工作。
        反过来 fail-closed 会让授权没配好的店【整个不能自动回复】——
        那是拿一个小功能的风险换掉整条业务线。

    ★ 权限说明（实测结论，不是推测）：
      接口要求 scope `im.buyer_blacklist.read`。
      我们试过：把它加进 manifest 后【注册成功】，但查询返回
        403 E_FORBIDDEN「租户未授予或凭证无此 scope」
      ⇒ 还需要租户在应用市场订阅页【同意】这个权限。
        在那之前，本函数会一直返回 ok=False（原因码 BLACKLIST_SCOPE_REQUIRED）。
    """
    if not tenant_id:
        tenant_id = (os.getenv("YUMAIDUO_TENANT_ID") or "").strip()
    peer = str(peer_unb or "").strip()
    if not peer:
        return {"ok": False, "blacklisted": False, "reason": "",
                "peer_nick": "", "error": "没有买家标识（peerUnb），查不了黑名单",
                "error_code": "BLACKLIST_NO_PEER"}
    client = PluginShopsClient(secret=secret, transport=transport)
    try:
        got = client.query_blacklist(peer, tenant_id)
    except PluginShopError as exc:
        return {"ok": False, "blacklisted": False, "reason": "",
                "peer_nick": "", "error": str(exc), "error_code": exc.code}
    return {"ok": True, "blacklisted": bool(got.get("blacklisted")),
            "reason": str(got.get("reason") or ""),
            "peer_nick": str(got.get("peerNick") or got.get("peer_nick") or ""),
            "error": "", "error_code": ""}


def read_shops(tenant_id: str = "", *, secret: str = "",
               transport=None) -> dict[str, Any]:
    """
    给 /api/shops 用的门面。返回 {ok, shops, error, error_code}。

    ★★ 这里【故意不 import application.plugin_api 去取密钥】。
       为什么：分层规矩是"内层不许依赖外层"，
       integrations 是最内层，application 是外层。
       我第一版在这里调了 application.plugin_api.plugin_secret() ——
       被架构审计当场抓出来（分层违规 1 处）。
       ⇒ 密钥由【调用方】（server.py，应用层）传进来。
       tenant_id 也一样：能传就传，传不了才回落到环境变量
       （环境变量不是"依赖外层"，是进程配置）。

    ★ 永远不抛异常到 HTTP 层 —— 界面要能显示"为什么没有店铺"，
      而不是一个 500 让人猜。
    """
    if not tenant_id:
        tenant_id = (os.getenv("YUMAIDUO_TENANT_ID") or "").strip()
    client = PluginShopsClient(secret=secret, transport=transport)
    try:
        shops = client.list_shops(tenant_id)
    except PluginShopError as exc:
        return {"ok": False, "shops": [], "error": str(exc),
                "error_code": exc.code}
    return {"ok": True, "shops": shops, "error": "", "error_code": ""}
