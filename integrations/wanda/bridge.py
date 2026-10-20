"""
integrations/wanda/bridge.py —— 出票系统的插件桥客户端

════════════════════════════════════════════════════════════════
★ 为什么要有这一层（而不是我们直接调万达）

  万达的凭证（token / client_key / mobile / device_id / shumei_box_id）
  在【出票系统后端】手里，不在我们这里。

  我们只有一个 X-Plugin-Bridge-Key，通过它去问后端：
    ① GET /api/auth/internal/wplus-accounts    号池（只给手机号）
    ② GET /api/showtime/seats?showtimeId=&phone=   座位图
  真正的万达调用由后端做。

  ★ 依据（实测，2026-10-13）：
    · /api/auth/internal/wplus-accounts  → HTTP 200，15 个账号
    · /api/showtime/seats?showtimeId=103172465
      → HTTP 200 code=0 bizCode=0，realtimeSeats.area 有 4 个区
      且 area[].wPlusActivity = {"activityCode":..., "price":3026, ...}
    · 白名单外的接口返回 401「需要管理后台登录」
      （例如 /api/showtime/by-cinema）

★ 怎么配（★ 全部走【环境变量】，不落盘、不进仓库）

  TICKET_BRIDGE_BASE                  默认 http://127.0.0.1:8000
                                      ★ 插件和出票系统不同机时必须改
  TICKET_BRIDGE_KEY                   通用桥密钥（PLUGIN_BRIDGE_KEY）
                                      —— 账号池必须用它
  TICKET_BRIDGE_KEY_LIANGPIAO         集成专用密钥（LIANGPIAO_WANDA_BRIDGE_KEY）
                                      —— 5.1/5.2 必须用它
  TICKET_APP_KEY / TICKET_APP_SECRET  应用签名（后台「设置 → 应用凭据」生成）
                                      —— 4 个只读接口用它，更安全

  ★ 三样都配是最佳状态：签名用在能用的地方，桥密钥兜住那三个例外。
    只配桥密钥也能跑（就是少一层安全性）。

  ★ 推荐把这些写进插件进程的环境（systemd EnvironmentFile / 启动脚本），
    不要写进 data/ 或任何提交进仓库的文件。
    出票系统那侧对应的环境变量名是 PLUGIN_BRIDGE_KEY /
    LIANGPIAO_WANDA_BRIDGE_KEY，在 /etc/ticket-system/backend.env 里。

════════════════════════════════════════════════════════════════
★★★ 三条纪律（都是踩过坑才立下的）

  ① **HTTP 200 不等于成功。**
     后端会把万达的业务失败原样透出来：
         HTTP 200  +  {"code":0,"data":{"bizCode":1000015,
                        "bizMsg":"服务器繁忙,请稍后再试","realtimeSeats":null}}
     只看 HTTP 会把这个当成"这场没有座位" —— 比报错更糟。
     ⇒ 必须同时看 `code` 和 `data.bizCode`。

  ② **手机号必须显式传，而且不许信"静默回退"。**
     后端的选号逻辑（routes/showtime.py `_resolve_client_token`）：
         传了 phone 且该账号有 token → 用它
         ⚠️ 否则 → 回退到第一个在线账号
         ⚠️ 都没有 token → 返回 ("default", "")
     ⇒ 我们不传 phone，就会静默用别人的号。
        更糟的是拿空 token 去调万达，拿到 1000015，
        而那个错误消息（"服务器繁忙"）会让人以为是限流，其实是没凭证。

  ③ **号池只给手机号，不给 token。**
     ⇒ 我们无法"自己选 token"，只能"指定手机号让后端去用那个号的 token"。
        所以租约（account_leases）是【后端侧】的并发防撞机制，
        我们这边要做的就是：显式传 phone + 用完释放。

════════════════════════════════════════════════════════════════
★ 与调用方（客服链路）的契约

  · 本模块只做【读】。写操作（锁座/下单/支付）一律不在这里。
  · 报错一律抛 WandaBridgeError，带 code —— 调用方按 code 分流文案。
  · **不吞异常**：拿不到会员价就抛，不许退化成"按最低价报"。

    ★★★ 为什么强调这条：**买家没选座位 = 他要【会员座位】**。
        这时报一个普通区的最低价是【报错价】——
        等于承诺了一个我们给不了的价。

    ★★★ 那"会员座位"到底怎么认（我第一版认错了，用户纠正的）：
        判据是 **seat[].payMemberSeatStatus == 1**（座位级结构化字段），
        **不是**"这个区域有 wPlusActivity.price"，**也不是**区域名叫「W+专享」。

        实测（showtimeId=103172465，泸州万达步步高广场店）：
            areaId=36 「W+专享」 seats=10  payMemberSeatStatus 全 = 1  ← 会员座位
            areaId=33 「特惠区」 seats=25  payMemberSeatStatus 全 = 0
            areaId=32 「优选区」 seats=35  payMemberSeatStatus 全 = 0
            areaId=1  「普通区」 seats=48  payMemberSeatStatus 全 = 0
        ★ 后三个区【也有】 wPlusActivity.price（那是会员折扣价），
          所以"有 wPlusActivity"判不出会员座位。
          ⇒ 用 SeatMapResult.member_seat_areas()，别用 member_areas()。

        ★ 为什么不靠区域名：区域名是万达自己起的，可能改成
          「W+会员区」或加后缀；payMemberSeatStatus 改名也还在。
"""
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

# ============================================================ 常量

ENV_BASE = "TICKET_BRIDGE_BASE"
ENV_KEY = "TICKET_BRIDGE_KEY"
ENV_KEY_LP = "TICKET_BRIDGE_KEY_LIANGPIAO"
ENV_APP_KEY = "TICKET_APP_KEY"
ENV_APP_SECRET = "TICKET_APP_SECRET"
DEFAULT_BASE = "http://127.0.0.1:8000"
HEADER_KEY = "X-Plugin-Bridge-Key"
HEADER_APP_KEY = "X-App-Key"
HEADER_APP_TS = "X-Timestamp"
HEADER_APP_NONCE = "X-Nonce"
HEADER_APP_SIGN = "X-Sign"

PATH_ACCOUNTS = "/api/auth/internal/wplus-accounts"
PATH_SEATS = "/api/showtime/seats"
PATH_CINEMA_MAP = "/api/cinema/wanda-lookup"
PATH_SHOWTIMES = "/api/showtime/by-cinema"
PATH_WPLUS_PRICE = "/api/order/internal/wplus-price"
PATH_WPLUS_RECOVERY = "/api/order/internal/wplus-recovery"

# ============================================================ 鉴权实况
#
# ★★★ 哪种凭证能调哪些接口 —— 全部【线上实测】（2026-10-13），不是照文档抄的。
#     实测发现文档 §2.2 那句"同一批接口两种方式任选其一"对下面标 ✗ 的接口
#     【不成立】。
#
#   接口                                   应用签名  通用桥密钥  集成专用密钥
#   /api/showtime/by-cinema                  200      200        200
#   /api/showtime/seats                      200      200        200
#   /api/cinema/wanda-lookup                 200      200        401
#   /api/order/match                         200      200        401
#   /api/auth/internal/wplus-accounts        403 ✗    200        401
#   /api/order/internal/wplus-price          401 ✗    401        200
#   /api/order/internal/wplus-recovery       401 ✗    401        200
#
#   ★ 为什么 ✗：
#     · wplus-accounts —— 路由自己挂了依赖 `_require_internal_bridge_key`
#       （auth.py:818-828），只认 X-Plugin-Bridge-Key，应用签名到不了它。
#     · wplus-price / wplus-recovery —— 中间件对这两条路径【遇到匹配就 return】，
#       根本不走后面的应用签名分支；路由内 `_require_plugin_bridge_key`
#       （order.py:350-354）只认 LIANGPIAO_WANDA_BRIDGE_KEY。
#
#   ⇒ 结论：**应用签名替代不了桥密钥。** 要用账号池和 5.1/5.2，
#     就必须还持有通用密钥 + 集成专用密钥。应用签名是"更安全的额外通道"，
#     不是替代品。
#
#   ⇒ 所以本模块的策略：**签名优先，某接口不支持就自动回退到桥密钥。**
#     既用上签名（少暴露共享口令），又不会被上面的例外卡住。
# 2026-10-15 后台已统一支持应用签名（含账号池），且线上关闭旧桥密钥。
# 上方鉴权表是旧版本记录，不能再据此绕开应用签名。
NO_APP_SIGNATURE_PATHS = frozenset()

# 号池里只返回 status=online 且 is_wplus=True 的号（后端侧过滤）


@dataclass(frozen=True)
class CinemaRoute:
    """
    影院渠道判定结果。

    ★★★ 只用 ID 判断，绝不用名字。
        出票系统那边特意提醒过（见他们的答复文档第三节）：
          wandaCinemaName 可能【不含"万达"】——
          实测用例 2 的万达规范名是
              「儒意影城(CBD万达广场店)原万达影城」
          影院改名过，但 ID 仍是万达体系的 595。
          ⇒ 拿名字做校验或二次匹配会误判。

    ★ 三种情形必须分开（对应出票系统的 mapped / reason）：
        mapped=True                            → 走万达（WANDA）
        mapped=False, reason=NO_MAPPING        → 这单走良票（LIANGPIAO），【正常】
        mapped=False, reason=IDENTITY_STORE_UNAVAILABLE
                                               → 映射库不可用，【异常】
        前两种都返回 HTTP 200（他们的设计），所以不能靠状态码分。
    """
    mapped: bool
    wanda_city_id: str = ""
    wanda_cinema_id: str = ""
    wanda_cinema_name: str = ""
    source_cinema_name: str = ""
    reason: str = ""

    @property
    def store_unavailable(self) -> bool:
        """映射库不可用（不是"这家没映射"）。"""
        return self.reason == "IDENTITY_STORE_UNAVAILABLE"

    @property
    def use_wanda(self) -> bool:
        """这一单该不该走万达渠道。"""
        return bool(self.mapped and self.wanda_cinema_id)

    def describe(self) -> str:
        if self.use_wanda:
            return (f"万达 {self.wanda_city_id}/{self.wanda_cinema_id} "
                    f"({self.wanda_cinema_name or '名字未提供'})")
        if self.store_unavailable:
            return "★ 映射库不可用 —— 无法确定渠道"
        return "无万达映射 → 走良票"


class WandaBridgeError(RuntimeError):
    """
    桥调用失败。

    code 取值：
      BRIDGE_NOT_CONFIGURED    没配 base/key
      BRIDGE_UNREACHABLE       网络/连接层失败
      BRIDGE_HTTP_<n>          HTTP 非 200
      BRIDGE_FORBIDDEN         401/403（密钥或白名单问题）
      WANDA_BIZ_<bizCode>      万达业务失败（HTTP 200 但 bizCode != 0）
      BRIDGE_PAYLOAD_INVALID   响应结构不是预期的
      ACCOUNT_POOL_EMPTY       号池没有可用账号
      ACCOUNT_NOT_IN_POOL      指定的手机号不在号池里
      NO_MEMBER_PRICE          座位图里没有会员价（★ 不是"没座位"）
      SEAT_NOT_FOUND           指定座位在座位图里找不到
      SEAT_UNAVAILABLE         指定座位不可售
    """
    def __init__(self, code: str, message: str, *, detail: Any = None):
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {message}")


@dataclass(frozen=True)
class BridgeConfig:
    base: str
    key: str
    liangpiao_key: str = ""
    app_key: str = ""
    app_secret: str = ""

    @classmethod
    def from_env(cls) -> "BridgeConfig":
        base = (os.getenv(ENV_BASE) or DEFAULT_BASE).strip()
        return cls(
            base=base.rstrip("/"),
            key=(os.getenv(ENV_KEY) or "").strip(),
            liangpiao_key=(os.getenv(ENV_KEY_LP) or "").strip(),
            app_key=(os.getenv(ENV_APP_KEY) or "").strip(),
            app_secret=(os.getenv(ENV_APP_SECRET) or "").strip(),
        )

    @property
    def has_app_signature(self) -> bool:
        return bool(self.app_key and self.app_secret)

    def require_key(self) -> None:
        if not self.key and not self.has_app_signature:
            raise WandaBridgeError(
                "BRIDGE_NOT_CONFIGURED",
                f"没配任何凭证：要么 {ENV_KEY}（桥密钥），"
                f"要么 {ENV_APP_KEY} + {ENV_APP_SECRET}（应用签名）")

    def key_for_path(self, path: str) -> str:
        """
        这个路径该用哪把桥密钥。

        ★ wplus-* 只认集成专用密钥（实测：通用密钥 401）。
          没配集成密钥就回退通用密钥 —— 让它自己去 401，
          而不是在这里猜（错误信息更准确）。
        """
        if path in (PATH_WPLUS_PRICE, PATH_WPLUS_RECOVERY):
            return self.liangpiao_key or self.key
        return self.key


def build_app_signature_headers(app_key: str, app_secret: str,
                                body_text: str) -> dict:
    """
    按开放平台文档 §2.2 生成应用签名头。

        canonical_body = 请求体原始字节；GET 或空体时用 "{}"
        待签名串        = appKey + timestamp + nonce + canonical_body  # 无分隔符
        X-Sign         = hex(HMAC-SHA256(待签名串, appSecret))

    ★ 三条实测确认的细节：
      ① GET 也必须签，body 用固定的 "{}"（不是空串）。
      ② POST 必须"先序列化 → 用该字符串签名 → 发送同一个字符串"，
         否则服务端算出来的待签名串和你签的不一样。
      ③ 时间戳偏差 ±300 秒、nonce 300 秒内只能用一次（复用 409）。
         ★ "签名不通过不会消费 nonce" —— 所以错签名可以放心重试。
    """
    ts = str(int(time.time()))
    nonce = secrets.token_hex(16)
    signing = app_key + ts + nonce + body_text
    sign = hmac.new(app_secret.encode("utf-8"),
                    signing.encode("utf-8"), hashlib.sha256).hexdigest()
    return {HEADER_APP_KEY: app_key, HEADER_APP_TS: ts,
            HEADER_APP_NONCE: nonce, HEADER_APP_SIGN: sign}


@dataclass
class MemberArea:
    """
    座位图里的一个区域。

    ★★ 三个价格字段必须分清（混淆任何一个都会报错价）：

        sales_price_fen    原价（area[].areaPrice.salesPrice）
        member_price_fen   W+会员价（area[].wPlusActivity.price）
        member_seats       ★ 真正的【会员座位】（seat[].payMemberSeatStatus == 1）

    ★★★ 「会员座位」= W+专享座位，判据是【座位级的 payMemberSeatStatus】
        —— 不是"这个区域有 wPlusActivity"，也不是靠区域名叫「W+专享」。

        实测（showtimeId=103172465，泸州万达步步高广场店）：
            areaId=36 「W+专享」 seats=10  payMemberSeatStatus 全 = 1   ← 会员座位在这
            areaId=33 「特惠区」 seats=25  payMemberSeatStatus 全 = 0
            areaId=32 「优选区」 seats=35  payMemberSeatStatus 全 = 0
            areaId=1  「普通区」 seats=48  payMemberSeatStatus 全 = 0
        ★ 后三个区【也有】 wPlusActivity.price（会员折扣价）——
          所以"有 wPlusActivity"【不能】用来判断会员座位。
          我第一版就是差一点这么写。

    ★ 为什么要按座位级字段判、而不是按区域名：
      区域名是万达自己起的，可能变（「W+专享」/「W+会员区」/加后缀…）。
      payMemberSeatStatus 是结构化字段，改名也还在。
    """
    area_id: str
    area_name: str
    sales_price_fen: int | None = None
    member_price_fen: int | None = None
    activity_code: str = ""
    activity_name: str = ""
    user_limit_num: int | None = None
    seats: list[dict] = field(default_factory=list)
    # ★ 该区里 payMemberSeatStatus==1 的座位（真正的会员座位）
    member_seats: list[dict] = field(default_factory=list)
    member_activity_present: bool = False

    @property
    def has_member_price(self) -> bool:
        """这个区有没有会员价（≠ 有没有会员座位）。"""
        return bool(self.member_price_fen and self.member_price_fen > 0)

    @property
    def is_member_area(self) -> bool:
        """
        ★★ 这是不是【会员座位区】—— 判据是座位级 payMemberSeatStatus。

        ★ 不看区域名，也不看 wPlusActivity 有没有。
        ★ 这个语义和出票系统 wplus-price 返回的 `is_wplus_area` 字段【等价】：
            它那边： re.search(r"W\\s*[+＋]|WPLUS|会员专享", area_name)
                     or any(_is_wplus_seat(seat))
            我们这边：bool(member_seats)
          两者都覆盖"名字带 W+"和"座位是会员座"两种情况。
          （出票系统 2026-10-13 的 wplus-price 也开始返回 is_wplus_area，
            我们若改成走它，可以直接用那个字段取代本属性。）
        """
        return bool(self.member_seats)

    def available_member_seats(self) -> list[dict]:
        """可售的会员座位（status==1）。"""
        return [s for s in self.member_seats if _seat_available(s)]


@dataclass
class SeatMapResult:
    showtime_id: str
    phone: str
    areas: list[MemberArea] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def area_of_seat(self, seat_name: str) -> MemberArea | None:
        """
        按座位名（如 "6排9座"）找它属于哪个区域。

        ★ 为什么按名字找：万达座位图是 area[].seat[] 的嵌套结构，
          座位自己的 seatId 是【数字】（如 22793128），而买家/截图给的是
          「6排9座」这种名字。两边都要能对上。
        """
        want = _norm_seat(seat_name)
        if not want:
            return None
        for a in self.areas:
            for s in a.seats:
                if _norm_seat(s.get("name") or "") == want:
                    return a
        return None

    def member_areas(self) -> list[MemberArea]:
        """有会员【价】的区域（★ 不是"有会员座位"）。"""
        return [a for a in self.areas if a.has_member_price]

    def member_seat_areas(self) -> list[MemberArea]:
        """
        ★★ 有会员【座位】的区域 —— 判据是座位级 payMemberSeatStatus==1。

        ★ 这才是「买家没选座位时要给他报的那个区」。
          别用 member_areas()（那是"有会员折扣价"，范围更宽，
          会把特惠区/优选区/普通区也算进来 —— 它们的座位不是会员座位）。
        """
        return [a for a in self.areas if a.is_member_area]

    def has_any_member_price(self) -> bool:
        return any(a.has_member_price for a in self.areas)

    def has_member_seats(self) -> bool:
        """这场有没有会员座位（可售的）。"""
        return any(a.available_member_seats() for a in self.areas)


def _seat_available(seat: dict) -> bool:
    """
    座位可不可售。

    ★ 万达用 status 表达：1 = 可售（实测 seat[].status）。
      认不出的值一律当【不可售】—— 宁可少报，不可错卖。
    """
    v = seat.get("status")
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return v == 1
    if isinstance(v, str):
        return v.strip() in ("1", "可售", "available", "true", "True")
    return False


def _is_member_seat(seat: dict) -> bool:
    """
    这个座位是不是【会员座位】（W+专享）。

    ★ 判据：seat[].payMemberSeatStatus == 1
      实测：W+专享区的 10 个座位全是 1；其他三区全是 0。
    """
    v = seat.get("payMemberSeatStatus")
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v == 1
    if isinstance(v, str):
        return v.strip() in ("1", "true", "True", "是")
    return False


def _norm_seat(value: Any) -> str:
    """座位名归一：去掉空格，全角括号统一。★ 只做保守归一，不做模糊匹配。"""
    s = str(value or "").strip()
    return s.replace(" ", "").replace("（", "(").replace("）", ")")


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        v = value.strip()
        if v.isdigit():
            return int(v)
    return None


# ============================================================ 一条纪律
#
# ★ 读不到就返回 None，【绝不猜】。
#   调用方拿到 None 必须显式处置（报错 / 问买家 / 降级），
#   不许 `or 0` / `or ""` 把"不知道"吞成"零"或"空"。
#
#   ★ 这是从原来的 integrations/wanda/fields.py 搬过来的 ——
#     那个模块是给"直连万达官方"那套做【字段别名容错】用的
#     （万达同一个东西有好几种写法：id/seatId、name/seatName/…），
#     已随那套一起删除。
#     我们走桥拿到的字段路径是【实测确定】的
#     （area[].wPlusActivity.price、seat[].payMemberSeatStatus …），
#     不需要别名表；但这条纪律必须留下：
#     ★ 良票那边踩过这个坑 —— settleAmount=null 被吞成 0，
#       结果算出"退回了全部冻结"的假账。
#   ⇒ 所以本模块里 _as_int / _is_member_seat / _seat_available
#     读不出值一律返回 None / False，绝不返回 0。


# ============================================================ 客户端
class WandaBridgeClient:
    """
    出票系统插件桥的只读客户端。

    ★ 查询使用 GET，以及只读 POST /api/order/match。这个类里【没有】任何写万达的路径 ——
      锁座/下单/支付不在这一层，故意的。
    """

    def __init__(self, config: BridgeConfig | None = None, *,
                 timeout: float = 40.0, transport=None):
        self.config = config or BridgeConfig.from_env()
        self.timeout = timeout
        # transport 可注入，便于测试不打网络
        self._transport = transport or _UrllibTransport()

    # ------------------------------------------------------------ 号池
    def list_accounts(self) -> list[dict]:
        """
        号池：GET /api/auth/internal/wplus-accounts

        返回 [{"phone": "...", "available": true}, ...]
        ★ 只有手机号 —— 后端不给 token（这是设计，见模块头注释）。
        """
        body = self._get(PATH_ACCOUNTS)
        accts = body.get("accounts")
        if not isinstance(accts, list):
            raise WandaBridgeError(
                "BRIDGE_PAYLOAD_INVALID",
                "号池响应里没有 accounts 数组", detail=body)
        return [a for a in accts if isinstance(a, dict) and a.get("phone")]

    def require_account(self, phone: str) -> dict:
        """
        确认这个手机号在号池里且可用，返回那条记录。

        ★★ 为什么必须做这一步（而不是直接把 phone 丢给 /seats）：
          如果 phone 不在号池 / 没有 token，后端会【静默回退】到别的号，
          而且响应里不会告诉我们它用了谁。
          ⇒ 我们先自己确认这个号在池子里，才能说"这一单用的是它"。
            否则一旦它被淘汰或掉线，我们会在不知情的情况下用别人的号。
        """
        want = str(phone or "").strip()
        if not want:
            raise WandaBridgeError("ACCOUNT_NOT_IN_POOL", "没传手机号")
        accts = self.list_accounts()
        if not accts:
            raise WandaBridgeError("ACCOUNT_POOL_EMPTY", "号池里没有可用账号")
        for a in accts:
            if str(a.get("phone")) == want:
                return a
        raise WandaBridgeError(
            "ACCOUNT_NOT_IN_POOL",
            f"手机号 {_mask_phone(want)} 不在号池里"
            f"（池中 {len(accts)} 个）")

    def pick_account(self, phone: str | None = None) -> str:
        """没指定就用池里第一个；指定了则必须在池里。"""
        if phone:
            self.require_account(phone)
            return str(phone).strip()
        accts = self.list_accounts()
        if not accts:
            raise WandaBridgeError("ACCOUNT_POOL_EMPTY", "号池里没有可用账号")
        return str(accts[0]["phone"])

    # ------------------------------------------------------------ 影院映射
    def wanda_lookup(self, *, city_id: str, cinema_id: str) -> CinemaRoute:
        """
        影院渠道映射：GET /api/cinema/wanda-lookup?cityId=&cinemaId=

        ★ 传的是【良票侧】的 cityCode + cinemaId（识别结果里就有）。

        ★★ 为什么必须按 ID 而不是按名字：
          两套命名完全不同 ——
              良票：万达影城（宁德万达广场激光IMAX店）
              万达：宁德万达广场店
          我实测按名字归一化匹配只有 35.9% 命中率，
          而按 ID 查这张表 100% 准（843 条人工维护的映射）。

        ★ 出票系统的设计（见他们的答复文档）：
          · mapped=true                       → 有映射
          · mapped=false, NO_MAPPING          → 没映射（★ 正常，走良票）
          · mapped=false, IDENTITY_STORE_...  → 映射库不可用（★ 异常）
          后两种都返回 HTTP 200。
          ⇒ 所以这里【不抛异常】表示"没映射"，
            只把 reason 原样带回去，让调用方决定。
            唯一会抛的是：库不可用（要能区分出来，否则会静默退化）。
        """
        cid = str(city_id or "").strip()
        nid = str(cinema_id or "").strip()
        if not cid or not nid:
            raise WandaBridgeError(
                "BRIDGE_PAYLOAD_INVALID",
                "wanda_lookup 需要 cityId 和 cinemaId（良票侧的值）")
        body = self._get(PATH_CINEMA_MAP, {"cityId": cid, "cinemaId": nid})
        data = body.get("data")
        if not isinstance(data, dict):
            raise WandaBridgeError(
                "BRIDGE_PAYLOAD_INVALID", "映射响应缺 data", detail=body)
        route = CinemaRoute(
            mapped=bool(data.get("mapped")),
            wanda_city_id=str(data.get("wandaCityId") or ""),
            wanda_cinema_id=str(data.get("wandaCinemaId") or ""),
            wanda_cinema_name=str(data.get("wandaCinemaName") or ""),
            source_cinema_name=str(data.get("sourceCinemaName") or ""),
            reason=str(data.get("reason") or ""),
        )
        # ★★ 映射库不可用要【抛】—— 不能和"没映射"混成一个结果。
        #    混了以后，所有万达单会静默退化成良票单，且没有任何告警。
        if route.store_unavailable:
            raise WandaBridgeError(
                "IDENTITY_STORE_UNAVAILABLE",
                "影院映射库不可用（副本可能没同步成功）—— "
                "无法判断这单走万达还是良票，不能瞎猜",
                detail=route.reason)
        return route

    # ------------------------------------------------------------ 座位图
    def seat_map(self, showtime_id: str, *, phone: str) -> SeatMapResult:
        """
        座位图：GET /api/showtime/seats?showtimeId=&phone=

        ★★ phone 是【必填】—— 不是可选参数。
          这样调用方不可能"忘了传"从而触发后端的静默回退。
          （我用 keyword-only 强制它。）
        """
        sid = str(showtime_id or "").strip()
        if not sid:
            raise WandaBridgeError("BRIDGE_PAYLOAD_INVALID", "没传 showtimeId")
        want_phone = str(phone or "").strip()
        if not want_phone:
            raise WandaBridgeError(
                "ACCOUNT_NOT_IN_POOL",
                "seat_map 必须显式传 phone —— 不传会触发后端的静默回退，"
                "导致用了别的账号而无人知晓")

        body = self._get(PATH_SEATS, {"showtimeId": sid, "phone": want_phone})
        data = body.get("data")
        if not isinstance(data, dict):
            raise WandaBridgeError(
                "BRIDGE_PAYLOAD_INVALID", "座位图响应缺 data", detail=body)
        rs = data.get("realtimeSeats")
        if not isinstance(rs, dict):
            # ★ 到这里 bizCode 已经被 _get 检查过了（非 0 会在那里抛）。
            #   所以 realtimeSeats 为空 = 这个场次的座位图确实拿不到。
            raise WandaBridgeError(
                "BRIDGE_PAYLOAD_INVALID",
                "座位图没有 realtimeSeats（该场次可能已过期/已满）",
                detail={"bizCode": data.get("bizCode"),
                        "bizMsg": data.get("bizMsg")})
        areas = self._parse_areas(rs)
        return SeatMapResult(showtime_id=sid, phone=want_phone,
                             areas=areas, raw=body)

    @staticmethod
    def _parse_areas(realtime_seats: dict) -> list[MemberArea]:
        """
        解析 realtimeSeats.area[]。

        ★★ wPlusActivity 为 null = 【这个区域没有会员价】，
          不是"这个区域没座位"。两者必须分开 ——
          我第一版就是差点把它们混起来（见模块头「不吞异常」那段）。

        字段来源（实测）：
            area[].areaId
            area[].seat[]                      座位（seatId/name/row/column/status）
            area[].areaPrice.salesPrice        原价
            area[].wPlusActivity.price         ★ 会员价
            area[].wPlusActivity.activityCode  活动 ID
            area[].seat[].payMemberSeatStatus  该座位是不是会员座
        """
        out: list[MemberArea] = []
        for raw_area in (realtime_seats.get("area") or []):
            if not isinstance(raw_area, dict):
                continue
            price = raw_area.get("areaPrice")
            price = price if isinstance(price, dict) else {}
            act = raw_area.get("wPlusActivity")
            act = act if isinstance(act, dict) else {}
            seats = [s for s in (raw_area.get("seat") or [])
                     if isinstance(s, dict)]
            out.append(MemberArea(
                area_id=str(raw_area.get("areaId") or ""),
                area_name=str(price.get("areaName")
                              or raw_area.get("areaName") or ""),
                sales_price_fen=_as_int(price.get("salesPrice")),
                # ★ 会员价只认 wPlusActivity.price；拿不到就是 None
                member_price_fen=_as_int(act.get("price")),
                member_activity_present=raw_area.get("wPlusActivity") is not None,
                activity_code=str(act.get("activityCode") or ""),
                activity_name=str(act.get("activityName") or ""),
                user_limit_num=_as_int(act.get("userLimitNum")),
                seats=seats,
                # ★★ 会员座位：座位级 payMemberSeatStatus==1
                member_seats=[s for s in seats if _is_member_seat(s)],
            ))
        return out

    # ------------------------------------------------------------ 通用只读
    def get_json(self, path: str, params: dict | None = None) -> dict:
        """
        通用只读 GET（★ 只允许白名单里那些路径）。

        ★ 为什么不把每个接口都写成一个方法：
          号池/座位图/映射的形状差异大（都有各自的解析和防护），
          写成方法值；但像"按影院取场次"这种返回结构由调用方解析的，
          给一个通用入口就够，免得桥里堆一堆解析逻辑。

        ★ 仍然只发 GET，仍然走 _get 的全部检查
          （HTTP / code / bizCode）。
        """
        return self._get(path, params)

    def match_facts(self, facts: dict) -> dict:
        """Read-only matching POST: never creates an order or selects seats."""
        path = '/api/order/match'
        payload = json.dumps({'text': '', 'mode': 'screenshot', 'auto_select_seats': False,
                              'phone': self.pick_account(), 'hints': facts},
                             ensure_ascii=False, separators=(',', ':'))
        headers, _ = self._auth_headers(path, payload)
        req = urllib.request.Request(self.config.base + path, data=payload.encode('utf-8'),
                                     headers={**headers, 'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            body = json.load(response)
        if not isinstance(body, dict):
            raise WandaBridgeError('BRIDGE_PAYLOAD_INVALID', '万达匹配响应不是对象')
        return body

    # ------------------------------------------------------------ 内部
    def _auth_headers(self, path: str, body_text: str) -> tuple:
        """
        选出这个路径该用的鉴权头，并说明用的是哪种方式。

        ★★ 策略：**应用签名优先，不支持就自动回退桥密钥。**
          理由见文件头「鉴权实况」：账号池和 5.1/5.2 到不了应用签名分支
          （路由级依赖 / 中间件提前 return），拿签名去调只会得到 403/401。
        ★ 回退是【按路径白名单】做的（NO_APP_SIGNATURE_PATHS），
          不是"失败了再试一次" —— 不给服务端产生无意义的失败请求。
        """
        if self.config.has_app_signature and path not in NO_APP_SIGNATURE_PATHS:
            return (build_app_signature_headers(
                self.config.app_key, self.config.app_secret, body_text),
                "app")
        return ({HEADER_KEY: self.config.key_for_path(path)}, "bridge_key")

    def _get(self, path: str, params: dict | None = None) -> dict:
        self.config.require_key()
        url = self.config.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        # ★ GET 的 canonical body 固定是 "{}"（文档 §2.2，实测确认）
        headers, auth_mode = self._auth_headers(path, "{}")
        try:
            status, raw = self._transport.get(url, headers, self.timeout)
        except WandaBridgeError:
            raise
        except Exception as exc:                              # noqa: BLE001
            # ★ 连不上是最常见的部署问题，报错里要给出【能自己排查的信息】：
            #   地址是什么、怎么改。
            #   实测踩过：桥在后端的 127.0.0.1:8000 上，从别的机器直接连会
            #   ConnectionRefused，而默认 base 恰好就是 127.0.0.1:8000 ——
            #   不看地址会以为是密钥错。
            hint = ""
            if "127.0.0.1" in self.config.base or "localhost" in self.config.base:
                hint = (f"（当前 base 是 {self.config.base}，只能在【后端同一台机器】上"
                        f"访问。插件和桥不同机时必须设 {ENV_BASE} "
                        f"指向可达地址，例如内网 IP 或隧道）")
            raise WandaBridgeError(
                "BRIDGE_UNREACHABLE",
                f"连不上出票系统桥 {self.config.base}{path}："
                f"{type(exc).__name__}: {exc}{hint}") from exc

        if status in (401, 403):
            # ★★ 401/403 的排查提示要分清用的是哪种凭证 ——
            #    文档里 5.1/5.2 用通用密钥会 401，而错误信息只说
            #    "需要管理后台登录"，接入方会以为是会话问题。
            if auth_mode == "app":
                why = (f"应用签名被拒绝（AppKey={self.config.app_key[:8]}…）。"
                       f"检查：AppSecret 是否正确、时间戳是否在 ±300 秒内、"
                       f"nonce 是否被复用 —— 服务端会返回 reason 字段说明")
            else:
                why = (f"桥密钥被拒绝（{path}）。"
                       f"检查密钥是否正确；"
                       f"★ 注意 /api/order/internal/wplus-price 与 "
                       f"/api/order/internal/wplus-recovery 只认"
                       f"『集成专用密钥』，通用密钥会被 401")
            raise WandaBridgeError(
                "BRIDGE_FORBIDDEN",
                f"桥拒绝了请求（HTTP {status}，鉴权方式={auth_mode}）—— "
                f"{why}。原始响应：{raw[:200]}",
                detail=raw[:300])
        if status != 200:
            raise WandaBridgeError(
                f"BRIDGE_HTTP_{status}",
                f"桥返回 HTTP {status}", detail=raw[:300])
        try:
            body = json.loads(raw) if raw.strip() else {}
        except ValueError as exc:
            raise WandaBridgeError(
                "BRIDGE_PAYLOAD_INVALID",
                f"桥返回的不是 JSON：{raw[:160]!r}") from exc
        if not isinstance(body, dict):
            raise WandaBridgeError("BRIDGE_PAYLOAD_INVALID", "响应不是对象")

        # ★★ 纪律①：HTTP 200 也要看 code 和 data.bizCode
        code = body.get("code")
        if code not in (0, "0", None):
            raise WandaBridgeError(
                f"BRIDGE_CODE_{code}",
                f"桥业务失败 code={code} msg={body.get('msg')!r}",
                detail=body.get("msg"))
        data = body.get("data")
        if isinstance(data, dict):
            biz = data.get("bizCode")
            # 万达的 bizCode：0 才是成功。1000015 是"服务器繁忙"，
            # 但它真正常见的原因是【凭证为空/场次ID不对】，不是限流。
            if biz not in (0, "0", None):
                raise WandaBridgeError(
                    f"WANDA_BIZ_{biz}",
                    f"万达业务失败 bizCode={biz} "
                    f"bizMsg={data.get('bizMsg')!r}"
                    f"（注意：这个 code 也出现在【凭证无效】时，"
                    f"不一定是限流）",
                    detail=data.get("bizMsg"))
        return body


class _UrllibTransport:
    """默认 transport：用标准库发 GET（不引第三方依赖）。"""

    def get(self, url: str, headers: dict, timeout: float):
        req = urllib.request.Request(url, method="GET",
                                     headers={**headers,
                                              "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")


def _mask_phone(p: str) -> str:
    p = str(p or "")
    return p[:3] + "****" + p[-2:] if len(p) >= 6 else "***"


# ============================================================ 租约
@dataclass
class AccountLease:
    """
    账号租约（本地记账用）。

    ★★ 重要区分：
      真正的并发防撞在【出票系统后端】——
        store.acquire_account_lease(phone, order_id, operator_id, ttl≤1800)
        冲突返回 {"acquired": False, "reason": "account_busy"}
        释放：release_account_lease(phone, order_id)
      那两个函数【不在桥的白名单里】，我们调不到。

      ⇒ 所以这里只是【我们这边的记账】：记录"这一单打算用哪个号"，
        用于：
          ① 同一单重试时不换号（换号会让上游看到两个账号在抢同一次操作）
          ② 事后审计"这单用了谁的号"
        ★ 它【不能】替代后端租约，也不能用来判断"这个号现在没被别人用"。
    """
    phone: str
    scope: str                 # 用 order_id / chat_id 标识这一单
    acquired_at: float
    ttl_seconds: float

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.acquired_at > self.ttl_seconds


class LeaseBook:
    """
    进程内的租约台账。★ 单进程有效；多进程部署要换成共享存储。

    ★ 我不把它做成文件持久化，理由：
      它只是"同一单别换号"的记忆，重启后第一件事本来就是重新选号；
      落盘反而会留下一个需要清理、可能过期的状态。
      （真正的落盘租约在后端那侧，不归我们管。）
    """

    # ★ 出处：出票系统的租约接口约定
    #   store.acquire_account_lease(phone, order_id, operator_id, ttl≤1800s)
    #   —— 你给的接口说明里写明上限 1800 秒。这里照抄同一个上限，
    #   免得本地记账的有效期比后端还长（那会出现"后端已释放、
    #   我们还以为占着"的假状态）。
    MAX_TTL = 1800.0

    def __init__(self) -> None:
        self._by_scope: dict[str, AccountLease] = {}

    def acquire(self, scope: str, phone: str,
                ttl_seconds: float = 1800.0) -> AccountLease:
        ttl = min(float(ttl_seconds), self.MAX_TTL)
        old = self._by_scope.get(scope)
        if old and not old.expired:
            # ★ 同一单不换号 —— 换了会让上游看到两个号在抢同一次操作
            return old
        lease = AccountLease(phone=phone, scope=scope,
                             acquired_at=time.monotonic(),
                             ttl_seconds=ttl)
        self._by_scope[scope] = lease
        return lease

    def get(self, scope: str) -> AccountLease | None:
        lease = self._by_scope.get(scope)
        if lease and lease.expired:
            self._by_scope.pop(scope, None)
            return None
        return lease

    def release(self, scope: str) -> None:
        self._by_scope.pop(scope, None)

    def active(self) -> list[AccountLease]:
        return [l for l in list(self._by_scope.values()) if not l.expired]
