"""取票图：店主的示意图 → 上传到平台 → 出票成功后附给买家。

★★ 为什么单独一个模块（而不是塞进 plugin_runtime）
   它管的是"一张图的生命周期"：本地存 → 上传 → 缓存 URL → 发出去。
   塞在命令生成里会让那段代码既要管订单又要管图片。

★ 三个状态：
   ① 没配图      ⇒ 什么都不做（默认；enabled 也默认 False）
   ② 配了图但没上传 ⇒ 上传一次，把 alicdn URL 缓存进同一个文件
   ③ 有 URL      ⇒ 出票成功后发一条带 image 的消息

★ 为什么缓存 URL：平台不让我们凭空造 URL，上传是唯一合法路径；
   每发一次就传一次是浪费，而且可能触发频率限制。
"""
import base64
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_FILE = Path(__file__).resolve().parent.parent / "data" / "ticket-image.json"

# ★ 平台校验：PNG / JPG / WEBP，≤5MB（照 reply-image.mjs 的规则）
_MAGIC = (
    ("image/png", b"\x89PNG\r\n\x1a\n"),
    ("image/jpeg", b"\xff\xd8\xff"),
    ("image/webp", b"RIFF"),
)
MAX_BYTES = 5 * 1024 * 1024

# ★★ 2026-10-14 依赖注入：怎么取插件密钥。
#   由最外层（plugin_runtime）在启动时塞进来，本模块不 import 任何
#   application 模块 —— 这样依赖方向只有【外层 → 内层】，不会成环。
#   默认返回空串（没注入时上传会失败 ⇒ 不发图，不会崩）。
_secret_provider = lambda: ""                                         # noqa: E731


def set_secret_provider(fn) -> None:
    """注册"怎么取插件密钥"。由 plugin_runtime 在启动时调用。"""
    global _secret_provider
    if callable(fn):
        _secret_provider = fn


def load() -> dict:
    """读本地存的取票图配置。坏了就当没配（不让它拖垮主流程）。"""
    from .shop_scope import read
    scoped = read()
    if "ticket_image_asset" in scoped:
        return scoped["ticket_image_asset"]
    if not _FILE.exists():
        return {}
    try:
        d = json.loads(_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return d if isinstance(d, dict) else {}


def save(data: dict) -> None:
    from .shop_scope import path, update
    if path() is not None:
        update({"ticket_image_asset": data})
        return
    _FILE.parent.mkdir(parents=True, exist_ok=True)
    _FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def detect_kind(raw: bytes) -> str:
    """按魔数判图片类型。返回 content-type，认不出返回空串。

    ★ 为什么不看文件扩展名：扩展名可以随便改，魔数不会。
    """
    for ctype, magic in _MAGIC:
        if raw.startswith(magic):
            if ctype == "image/webp" and raw[8:12] != b"WEBP":
                continue
            return ctype
    return ""


def store_upload(name: str, data_b64: str) -> dict:
    """存一张店主的图（前端传 base64）。返回 {ok, error, kind, bytes}。

    ★ 校验在【后端】做一遍，不只在界面上做 ——
      界面的校验只是提示，后端才是防线。
    """
    s = str(data_b64 or "").strip()
    if not s:
        return {"ok": False, "error": "没有图片内容"}
    # ★★ 2026-10-14 修：剥掉 data URL 前缀。
    #   浏览器的 FileReader.readAsDataURL() 给的就是
    #       data:image/png;base64,iVBORw0KGgo...
    #   带着前缀去 base64 解码必然失败 —— 而这是【真实浏览器一定会走的路】。
    #   ⇒ 前缀在这里剥掉，而不是要求前端记得剥（前端总有一天会忘）。
    if s.startswith("data:"):
        _comma = s.find(",")
        if _comma < 0:
            return {"ok": False, "error": "data URL 格式不对（没有逗号）"}
        s = s[_comma + 1:].strip()
    if len(s) > 8 * 1024 * 1024:
        return {"ok": False, "error": "图片太大"}
    try:
        raw = base64.b64decode(s, validate=True)
    except Exception:                                     # noqa: BLE001
        return {"ok": False, "error": "图片内容不是合法的 base64"}
    if not raw:
        return {"ok": False, "error": "图片内容为空"}
    if len(raw) > MAX_BYTES:
        return {"ok": False, "error": f"图片不能超过 {MAX_BYTES // 1024 // 1024}MB"}
    kind = detect_kind(raw)
    if not kind:
        return {"ok": False, "error": "只支持 PNG / JPG / WEBP"}
    d = load()
    d["name"] = str(name or "取票图")[:60]
    d["kind"] = kind
    d["bytes"] = len(raw)
    d["data"] = s
    d.pop("url", None)          # ★ 换了图就把旧 URL 清掉（否则会发旧图！）
    d.pop("width", None)
    d.pop("height", None)
    save(d)
    return {"ok": True, "error": "", "kind": kind, "bytes": len(raw)}


def image_for_send(cfg=None) -> dict | None:
    """要发给买家的 image 对象；没配/没开就返回 None。

    ★ 返回 None 就是"什么都不做" —— 调用方不用判断开关。
    """
    if cfg is None:
        try:
            import settings as _st
            cfg = _st.load()
        except Exception:                                 # noqa: BLE001
            return None
    ti = getattr(cfg, "ticket_image", None)
    if ti is None or not bool(getattr(ti, "enabled", False)):
        return None
    d = load()
    url = str(d.get("url") or "").strip()
    w, h = d.get("width"), d.get("height")
    if not url or not isinstance(w, int) or not isinstance(h, int):
        return None         # ★ 还没上传成功 ⇒ 不发（宁可没有图，也不发坏图）
    return {"imageUrl": url, "width": w, "height": h}


def ensure_uploaded(chat_identity: dict | None = None) -> dict | None:
    """
    确保有可用的 alicdn URL；没有就上传一次并缓存。

    ★ 只在出票要发图时调 —— 不在配置保存时调：
      配置保存时插件可能没跑，上传会失败，店主还以为存上了。
    ★ 上传失败 ⇒ 返回 None（不发图），并【不抛异常】——
      票码才是买家真正要的，图是附赠的。
    """
    got = image_for_send()
    if got is not None:
        return got                    # 已有 URL，直接用（不重复上传）
    d = load()
    data = str(d.get("data") or "").strip()
    if not data:
        return None                   # 根本没配图
    ident = chat_identity or {}
    shop_id = str(ident.get("shop_id") or "")
    if not shop_id:
        return None                   # 上传要 accountUnb（店铺号）
    try:
        # ★★ 密钥从【注入的提供者】拿 —— 本模块不 import plugin_api。
        #   为什么：那会成环
        #     plugin_api → plugin_runtime → service → ticket_flow
        #       → ticket_image → plugin_api
        #   我加模块级 import 消除"孤儿模块"时正好把这个环补上了 ——
        #   修一个警告、引入一个更严重的问题。
        #   ⇒ 改用【依赖注入】：最外层（plugin_runtime）启动时把
        #     "怎么取密钥"塞进来，本模块只管调。
        #     这样依赖方向是【外层 → 内层】，不会成环。
        from integrations.yumaiduo.shop_client import upload_ticket_image
        res = upload_ticket_image(data, secret=_secret_provider(),
                                  account_unb=shop_id)
    except Exception:                                     # noqa: BLE001
        return None
    if not res.get("ok"):
        return None
    img = res.get("image") or {}
    try:
        mark_uploaded(img.get("imageUrl") or "", img.get("width") or 0,
                      img.get("height") or 0)
    except Exception:                                     # noqa: BLE001
        return None
    return image_for_send()


def mark_uploaded(url: str, width: int, height: int) -> None:
    """记下上传结果（由上传那条路调用）。"""
    d = load()
    d["url"] = str(url or "")
    d["width"] = int(width or 0)
    d["height"] = int(height or 0)
    save(d)
