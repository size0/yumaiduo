"""
application/blacklist.py —— 买家黑名单（鱼麦多的平台内部标记）。

════════════════════════════════════════════════════════════════
★ 这一层干什么

  「设置 → 黑名单 → 黑名单用户回复话术」那一项需要知道
  "这个买家在不在黑名单里"。名单本身【只有鱼麦多有】——
  后端没有平台客户端，只有插件进程有。
  ⇒ 链路：后端 → 插件 /internal/blacklist/query → 鱼麦多 core.im.blacklist.query

★ 三件必须守的事

  【1】★★★ 查不到就当"不在名单"，绝不拦人（fail-open）
        黑名单是【可选的加固】。没授权、插件不通、没配买家标识 ——
        这些情况下系统必须照常自动回复。
        反过来（fail-closed）会让"权限没配好"变成"整个店不能回复"，
        拿一个小功能的风险换掉整条业务线。

  【2】★ 结果要缓存，不然每条消息都打一次插件 + 平台
        按 (shop_id, peer_unb) 缓存，TTL 默认 10 分钟。
        ★ 缓存的是"查到了的结果"。查失败【不缓存】——
          不然一次网络抖动会让这个买家被当成"不在名单"十分钟。
        ★ TTL 到期后重新查：店主把买家拉黑了，最迟 10 分钟内生效。

  【3】★ "在不在名单"和"查没查成"必须分开表达
        把它们合成一个布尔值，上层就没法区分
        "他不在名单" 和 "我不知道" —— 而这两件事的处置完全不同。
        所以返回的是 dict，带 ok / blacklisted / error_code。

★ 权限现状（实测，不是推测）
  · 接口要 scope `im.buyer_blacklist.read`
  · 我们把它加进 manifest 后【注册依然成功】，但查询返回
      403 E_FORBIDDEN「插件缺少所需 scope: im.buyer_blacklist.read
                        （租户未授予或凭证无此 scope）」
  · ⇒ 还需要租户在应用市场的订阅页【同意】这个权限。
    在那之前每次查都会得到 ok=False + error_code=BLACKLIST_SCOPE_REQUIRED，
    界面/日志会明确说出来，而不是静默当成"不在名单"。
════════════════════════════════════════════════════════════════
"""
import os
import time
from typing import Any

# 查到了的结果缓存多久（秒）。
# ★ 为什么是 10 分钟：黑名单是"店主手动标注"，不是秒级事实；
#   而每条买家消息都查一次会在这个热路径上多两次网络往返。
DEFAULT_TTL_SECONDS = 600

# 缓存上限。满了就整体清空（简单、不会无限长）。
# ★ 为什么不做 LRU：这是"一个店几百个买家"的量级，
#   清空的代价只是多查几次，不值得为它引入淘汰逻辑。
MAX_CACHE_ENTRIES = 4096

# {(shop_id, peer_unb): (expires_at, result_dict)}
_CACHE: dict[tuple, tuple] = {}


def _plugin_secret() -> str:
    """
    取插件密钥（默认实现）。

    ★★★ 为什么不在这一层直接 import application.plugin_api：
      我第一版就是这么写的 —— 架构审计当场抓出一个【循环依赖】：
          blacklist → plugin_api → plugin_runtime → service → blacklist
      为什么成环：plugin_api 需要 plugin_runtime（队列），
      plugin_runtime 需要 service（编排），而 service 需要 blacklist（闸门）。
      ⇒ 修法：把"密钥从哪来"这件事挪到最内层 integrations（那里本来就是
        管外部事实的），这一层只取用。见 shop_client.resolve_plugin_secret。
    """
    try:
        from integrations.yumaiduo.shop_client import resolve_plugin_secret
        return resolve_plugin_secret() or ""
    except Exception:                                         # noqa: BLE001
        return ""


def _tenant_id() -> str:
    return (os.getenv("YUMAIDUO_TENANT_ID") or "").strip()


def cache_key(shop_id: str, peer_unb: str) -> tuple:
    return (str(shop_id or ""), str(peer_unb or ""))


def clear_cache() -> None:
    """清空缓存。测试和"店主刚改了名单"时用。"""
    _CACHE.clear()


def check(shop_id: str, peer_unb: str, *, ttl: int | None = None,
          transport=None, now: float | None = None,
          secret: str | None = None, tenant_id: str | None = None,
          ) -> dict[str, Any]:
    """
    查这个买家在不在黑名单里。

    返回：
        {"ok": True,  "blacklisted": bool, "reason": str, "peer_nick": str,
         "cached": bool, "error": "", "error_code": ""}
        {"ok": False, "blacklisted": False, ..., "error": str,
         "error_code": str}          ← ★ "不知道"，不是"不在名单"

    ★ ok=False 时 blacklisted 一律是 False ——
      但调用方【必须看 ok】，不能只看 blacklisted。
      这里给 False 只是为了让"取错字段也不会误拦人"。

    secret / tenant_id 一般不用传（自动取）。留给测试注入 ——
    不注入的话本机没插件密钥，永远走不到传输层，测不到解析和缓存。
    """
    peer = str(peer_unb or "").strip()
    if not peer:
        return {"ok": False, "blacklisted": False, "reason": "",
                "peer_nick": "", "cached": False,
                "error": "这条消息里没有买家标识（peerUnb）",
                "error_code": "BLACKLIST_NO_PEER"}

    key = cache_key(shop_id, peer)
    clock = time.time() if now is None else float(now)
    hit = _CACHE.get(key)
    if hit is not None:
        expires_at, result = hit
        if expires_at > clock:
            out = dict(result)
            out["cached"] = True
            return out
        # 过期了，顺手删掉（不删也不影响正确性，只是占内存）
        _CACHE.pop(key, None)

    from integrations.yumaiduo.shop_client import read_blacklist
    raw = read_blacklist(peer,
                         secret=(_plugin_secret() if secret is None
                                 else secret),
                         tenant_id=(_tenant_id() if tenant_id is None
                                    else tenant_id),
                         transport=transport)

    result = {
        "ok": bool(raw.get("ok")),
        "blacklisted": bool(raw.get("blacklisted")),
        "reason": str(raw.get("reason") or ""),
        "peer_nick": str(raw.get("peer_nick") or ""),
        "cached": False,
        "error": str(raw.get("error") or ""),
        "error_code": str(raw.get("error_code") or ""),
    }

    # ★★ 只缓存成功的查询。
    #   缓存失败的话，一次网络抖动会让这个买家被当成"不在名单"整整一个 TTL
    #   —— 那正好把黑名单该拦的人放进来。
    if result["ok"]:
        if len(_CACHE) >= MAX_CACHE_ENTRIES:
            _CACHE.clear()
        _CACHE[key] = (clock + (DEFAULT_TTL_SECONDS if ttl is None else ttl),
                       result)
    return result


def is_blacklisted(shop_id: str, peer_unb: str, **kw) -> bool:
    """
    便利函数：只在【确定查到了】且【确实在名单里】时返回 True。
    查不到 ⇒ False（fail-open，见模块开头【1】）。

    ★ 只查一次（我第一版写了两次 check(...)，白白多打一次插件+平台）。
    """
    got = check(shop_id, peer_unb, **kw)
    return bool(got.get("ok") and got.get("blacklisted"))


def status_text(result: dict) -> str:
    """给日志/界面用的一句话说明（说清是"不在"还是"没查成"）。"""
    if result.get("ok"):
        if result.get("blacklisted"):
            nick = result.get("peer_nick") or ""
            why = result.get("reason") or ""
            return (f"在黑名单里（{nick}｜{why}）" if (nick or why)
                    else "在黑名单里")
        return "不在黑名单（已查证）"
    code = result.get("error_code") or ""
    if code == "BLACKLIST_SCOPE_REQUIRED":
        return ("黑名单【没查成】：租户还没同意「查询买家黑名单」权限 "
                "（去应用市场订阅页勾选同意后生效）")
    return f"黑名单【没查成】：{result.get('error') or code or '未知原因'}"
