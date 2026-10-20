"""
application/delivery_auth.py —— 出票接口的鉴权

════════════════════════════════════════════════════════════════
★ 为什么必须要有这个

  `/api/order/deliver` 是【能决定给买家的票】的接口。
  插件要上服务器 —— 一旦暴露在公网，没有鉴权就等于：
  任何人 POST 一个 order_id 就能把别人的单标成已出票，
  或者把伪造的取票码发给买家。

★ 机制完全照线上 V5 的 integrations/wanda_ticketing.py（不自己发明）

    ① Bearer 密钥  + hmac.compare_digest 比较
         ★ 为什么用 compare_digest 而不是 == ：
           字符串 == 会在第一个不同字符处返回，
           攻击者能靠响应时间差逐字节猜出密钥（时序攻击）。
           compare_digest 是恒定时间比较。

    ② 租户白名单  WANDA_TICKETING_INTEGRATION_TENANT_ID
    ③ 店铺白名单  WANDA_TICKETING_INTEGRATION_SHOP_IDS
         ★ 为什么密钥之外还要白名单：
           密钥泄露时，白名单把影响限制在指定的店 ——
           否则一把密钥能操作所有店。

    ④ 写操作单独一把钥匙 + 单独开关
         WANDA_TICKETING_DELIVERY_KEY / WANDA_TICKET_DELIVERY_ENABLED
         ★ 为什么读写分钥匙：出票后台要读订单列表（只读），
           但只有真正出票的那一步才需要写权限。
           分开了，读接口的钥匙泄露不会导致能出票。

★ 错误码也照线上（这样出票后台按同一套码处理）
     503 WANDA_TICKETING_INTEGRATION_NOT_CONFIGURED  没配
     401 INVALID_INTEGRATION_CREDENTIAL              密钥不对
     403 SHOP_OUTSIDE_INTEGRATION_SCOPE              店不在白名单
     503 WANDA_DELIVERY_DISABLED                     写开关关了

★ 密钥从哪来：环境变量优先，其次 data/liangpiao.json（凭证文件）。
    ★ 为什么要能存文件：插件在服务器上跑，本地要能先配好试通；
      环境变量交给部署时覆盖（线上就是纯环境变量）。
    ★ 密钥【只写不读】—— 界面上不回显明文，只显示"已配置"和掩码。
════════════════════════════════════════════════════════════════
"""

import hmac
import os
from dataclasses import dataclass, field

# ==================================================================== 常量
ENV_TENANT = "WANDA_TICKETING_INTEGRATION_TENANT_ID"
ENV_SHOPS = "WANDA_TICKETING_INTEGRATION_SHOP_IDS"
ENV_READ_KEY = "WANDA_TICKETING_INTEGRATION_KEY"
ENV_WRITE_KEY = "WANDA_TICKETING_DELIVERY_KEY"
ENV_WRITE_ENABLED = "WANDA_TICKET_DELIVERY_ENABLED"

# ★ 凭证文件里的字段名（与线上环境变量一一对应）
CRED_TENANT = "wanda_ticketing_tenant_id"
CRED_SHOPS = "wanda_ticketing_shop_ids"
CRED_WRITE_KEY = "wanda_ticketing_delivery_key"
CRED_WRITE_ENABLED = "wanda_ticketing_delivery_enabled"


@dataclass(frozen=True)
class DeliveryPrincipal:
    """通过鉴权的调用方。"""
    tenant_id: str
    shop_ids: frozenset = field(default_factory=frozenset)


@dataclass(frozen=True)
class AuthResult:
    """
    鉴权结果。

    ★ 用结果对象而不是抛异常：HTTP 层要按 status/error 组响应体，
      抛异常会让每个调用点都写 try/except（还容易漏）。
    """
    ok: bool
    status: int = 200
    error: str = ""
    message: str = ""
    principal: DeliveryPrincipal | None = None


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _cred(name: str) -> str:
    """
    读凭证文件里的字段。

    ★ 读不到就返回空 —— 不抛异常。
      理由：没配鉴权时应该是"接口不可用（503）"，
      而不是让整个服务起不来。
    """
    try:
        from . import store as store_module
        data = store_module.load_credentials() or {}
        value = data.get(name)
        return str(value).strip() if value is not None else ""
    except Exception:                                    # noqa: BLE001
        return ""


def _config() -> dict:
    """
    取出全部鉴权配置。★ 环境变量优先于凭证文件。

    ★ 为什么要 env 优先：部署时环境变量是权威（线上就是这么配的），
      文件只是本地开发方便。若文件优先，服务器上留了个旧文件
      就会盖掉运维设的环境变量 —— 那种问题极难排查。
    """
    shops_raw = _env(ENV_SHOPS) or _cred(CRED_SHOPS)
    shop_ids = frozenset(s.strip() for s in shops_raw.split(",") if s.strip())
    enabled_raw = _env(ENV_WRITE_ENABLED) or _cred(CRED_WRITE_ENABLED)
    # ★ 默认关：没显式打开就不允许写出票。
    #   线上默认 'true'，但那是它已经部署好了。
    #   我们默认关更安全 —— 要出票必须显式打开。
    enabled = str(enabled_raw).strip().lower() in ("1", "true", "yes", "on") \
        if enabled_raw else False
    return {
        "tenant_id": _env(ENV_TENANT) or _cred(CRED_TENANT),
        "shop_ids": shop_ids,
        "read_key": _env(ENV_READ_KEY),
        "write_key": _env(ENV_WRITE_KEY) or _cred(CRED_WRITE_KEY),
        "write_enabled": enabled,
    }


def config_status() -> dict:
    """
    给界面看的配置状态。★ 绝不返回密钥明文。

    界面要能回答："出票接口能不能用？缺什么？"
    """
    cfg = _config()
    missing = []
    if not cfg["tenant_id"]:
        missing.append("租户 ID")
    if not cfg["shop_ids"]:
        missing.append("店铺白名单")
    if not cfg["write_key"]:
        missing.append("出票密钥")
    return {
        "configured": not missing,
        "missing": missing,
        "write_enabled": cfg["write_enabled"],
        # ★ 只报"配没配"和掩码，不回显明文
        "tenant_id": cfg["tenant_id"],
        "shop_ids": sorted(cfg["shop_ids"]),
        "delivery_key_hint": mask_secret(cfg["write_key"]),
        "env_vars": [ENV_TENANT, ENV_SHOPS, ENV_WRITE_KEY, ENV_WRITE_ENABLED],
    }


def mask_secret(value: str) -> str:
    """
    掩码显示。★ 只露首尾各 2 个字符。

    ★ 为什么连部分都不该露：密钥通常有结构（前缀表用途），
      露多了等于泄露信息。掩码只用来让运营确认"配了哪一把"。
    """
    if not value:
        return ""
    if len(value) <= 6:
        return "•" * len(value)
    return f"{value[:2]}{'•' * 8}{value[-2:]}"


def check(authorization: str | None, *, shop_id: str = "",
          write: bool = True) -> AuthResult:
    """
    校验一次调用。★ 这是唯一入口，HTTP 层只调它。

    ★★ 检查顺序（顺序有讲究）：
        ① 有没有配（没配 → 503 NOT_CONFIGURED）
           ★ 必须先查这个：什么都没配的时候，报"开关关着"是误导 ——
             运维会去打开开关，然后发现还是不通（因为没密钥）。
             报"没配置 + 缺哪几项"才是能直接照着修的。
        ② 写开关（配好了但开关关 → 503 DISABLED）
           ★ 再查开关：开关关着时不该因为"密钥对"给出任何暗示。
        ③ Bearer 密钥（恒定时间比较）
        ④ 店铺白名单
    """
    cfg = _config()
    if not cfg["write_key"] or not cfg["tenant_id"] or not cfg["shop_ids"]:
        return AuthResult(False, 503, "WANDA_TICKETING_INTEGRATION_NOT_CONFIGURED",
                          "出票接口没有配置（缺密钥/租户/店铺白名单）")
    if write and not cfg["write_enabled"]:
        return AuthResult(False, 503, "WANDA_DELIVERY_DISABLED",
                          "出票接口未启用（写开关是关的）")

    scheme, _, credential = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
            credential, cfg["write_key"]):
        # ★ 不在错误信息里说"密钥错在第几位"之类 —— 只给一个码
        return AuthResult(False, 401, "INVALID_INTEGRATION_CREDENTIAL",
                          "鉴权失败")

    principal = DeliveryPrincipal(tenant_id=cfg["tenant_id"],
                                  shop_ids=cfg["shop_ids"])
    if shop_id and shop_id not in principal.shop_ids:
        return AuthResult(False, 403, "SHOP_OUTSIDE_INTEGRATION_SCOPE",
                          f"店铺 {shop_id} 不在白名单内")
    return AuthResult(True, principal=principal)


# ==================================================================== 写配置
def save_config(*, tenant_id: str | None = None, shop_ids=None,
                delivery_key: str | None = None,
                write_enabled: bool | None = None) -> dict:
    """
    把鉴权配置写进凭证文件。

    ★ 三个都传 None 就什么都不改（区别于"传空字符串清掉"）。
      ⇒ 界面只改其中一项时，不会把别的抹掉。

    ★ 密钥【只写】：传 None 保留原值；传 "" 才是清掉。
    """
    from . import store as store_module
    data = dict(store_module.load_credentials() or {})
    if tenant_id is not None:
        data[CRED_TENANT] = tenant_id.strip()
    if shop_ids is not None:
        if isinstance(shop_ids, (list, tuple, set)):
            shop_ids = ",".join(str(s).strip() for s in shop_ids if str(s).strip())
        data[CRED_SHOPS] = str(shop_ids).strip()
    if delivery_key is not None:
        data[CRED_WRITE_KEY] = delivery_key.strip()
    if write_enabled is not None:
        data[CRED_WRITE_ENABLED] = "true" if write_enabled else "false"
    store_module.save_credentials(data)
    return config_status()
