"""
application/wanda_cost.py —— 万达渠道的成本解析

2026-10-16 店主确认：wPlusActivity 缺失/null 时用对应区域 salesPrice。
有会员活动但价格无效时不回退；普通售价来源标记 WANDA_SALES_PRICE。
未选座时按可售会员区域取价；无活动用该会员区域salesPrice。
合并区域余座须成本和官方原价均一致，否则要求明确座位。

════════════════════════════════════════════════════════════════
★ 这一层只干一件事：**给出「成本」**，不管加价。

    成本侧（本模块）：座位图 area[].wPlusActivity.price   ← 会员价
    定价侧（规则引擎）：domain/quote_rules.py 的分档表      ← 你配好的

★ 为什么要单独一层、不塞进 bridge.py：
    bridge 只管"怎么跟出票系统说话"（HTTP/签名/解析），
    本模块管"这一单的成本该取谁"（渠道判定 + 场次解析 + 座位→区域）。
    混在一起以后换个上游就要动传输层。

════════════════════════════════════════════════════════════════
★★ 三条已经踩过的纪律（都来自实测）

  ① **渠道按 ID 判，不按名字。**
     良票「万达影城（宁德万达广场激光IMAX店）」
     万达「宁德万达广场店」  ——  两套命名完全不同。
     按名字归一化匹配实测只有 35.9% 命中率；按 ID 查映射表 100% 准。
     ★ 而且万达侧的名字可能【不含"万达"】——
       实测有「儒意影城(CBD万达广场店)原万达影城」（ID 595 仍是万达的）。
       所以名字只能进日志，不能参与判断。

  ② **「会员座位」= seat[].payMemberSeatStatus == 1，不是"有 wPlusActivity"。**
     实测：W+专享区 12 个座位全是 1；特惠/优选/普通区全是 0，
     但那三个区【也有】 wPlusActivity.price（那是会员折扣价）。
     ⇒ 用 member_seat_areas()，别用 member_areas()。

  ③ **买家没选座位 = 他要会员座位。**
     这时取 W+专享区（member_seat_areas）的会员价。
     ★ 绝不能退化成"随便挑个最便宜的区"——那是报错价。

════════════════════════════════════════════════════════════════
★★ 为什么直接读座位图，而不调 /api/order/internal/wplus-price

  出票系统有个官方报价接口 `wplus-price`（开放平台文档 §5.1），
  它的【默认模式】也是只读预览、读的也是座位图的 `area[].wPlusActivity`。
  ★ 实测两者数值完全一致（6 个场次交叉验证，漏收 0、不一致 0）。

  那我们为什么仍读座位图？三条理由：

  ① **报价要用座位→区域的归属。**
     买家选了「优选区 6排8座」，我必须知道那个座位属于 area 32
     才能取对应区域的价。座位图里 `area[].seat[]` 是嵌套结构，
     天然携带归属；而 wplus-price 只按 area_code 平铺返回区域列表，
     要么再拉一次座位图，要么自己按 area_code 反查 —— 反而更绕。
     （实测 area[].areaId == area[].areaPrice.areaCode，两条路都能对上，
       但我们直接看座位所在容器最不容易错。）

  ② **少一次 HTTP。** 报价链路里本来就要读座位图（座位可售性、
     会员座位数都从它来），再调一次 wplus-price 是纯重复。

  ③ **wplus-price 需要【集成专用密钥】**（LIANGPIAO_WANDA_BRIDGE_KEY），
     和其他接口的通用密钥不同；座位图两把都认。
     少依赖一把密钥，部署面更小。

  ★ 什么时候应该改走 wplus-price：
    如果出票系统把会员价口径改成"锁座后的活动分摊"（而不是座位图），
    两边就不再一致了。check_bridge_drift.py 每次跑都会交叉核对，
    一旦发现不一致就会报出来 —— 那时再切换。

════════════════════════════════════════════════════════════════
★ 与 V5 的差异（我们这一步比它省）

  V5 的 wanda/cost.py:65 要求"所有可用座位的会员价只有一个值"，
  但不同区域会员价本来就不同（实测 50.26/45.00/50.26/47.00），
  必然 >1 ⇒ 抛 WPLUS_MEMBER_SEAT_PRICE_AMBIGUOUS ⇒ 落到 probe（真下临时单）。

  ⇒ 我们按【买家选的那个座位所在区域】取价，区域内部价格一致，
    就不存在"跨区域歧义"，因此【不需要 probe】。
"""
from dataclasses import dataclass, field
from typing import Any

from integrations.wanda.bridge import (
    CinemaRoute, MemberArea, SeatMapResult, WandaBridgeClient,
    WandaBridgeError)


class WandaCostError(RuntimeError):
    """
    万达成本解析失败。

    code：
      CHANNEL_UNKNOWN          渠道判不出来（映射库不可用）
      NOT_WANDA                这家没有万达映射 → 应走良票
      SHOWTIME_NOT_FOUND       万达侧找不到这个场次
      SEAT_NOT_IN_MAP          买家选的座位在座位图里找不到
      NO_MEMBER_PRICE          该区域没有会员价（≠ 没座位）
      NO_MEMBER_SEATS          这场没有会员座位（买家要会员座却买不到）
      SEAT_UNAVAILABLE         座位不可售
      MEMBER_PRICE_ABOVE_ORIGINAL
                               会员价高于原价（数据异常，不采信）
    """
    def __init__(self, code: str, message: str, *, detail: Any = None):
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {message}")


@dataclass
class WandaCost:
    """万达单的成本（给规则引擎用）。"""
    ok: bool
    # ★ 成本是【单座】的，报价时 × 张数（引擎要整单，见 as_engine_kwargs）
    unit_cost_fen: int = 0
    seat_count: int = 1
    original_unit_fen: int | None = None      # 官方原价（单座）
    area_id: str = ""
    area_name: str = ""
    wanda_cinema_id: str = ""
    showtime_id: str = ""
    source: str = ""                          # 成本来源，写进备注便于追溯
    notes: list[str] = field(default_factory=list)

    @property
    def total_cost_fen(self) -> int:
        return self.unit_cost_fen * max(1, self.seat_count)

    @property
    def original_total_fen(self) -> int | None:
        if self.original_unit_fen is None:
            return None
        return self.original_unit_fen * max(1, self.seat_count)

    def as_engine_kwargs(self) -> dict:
        """
        转成 QuoteRuleEngine.quote() 要的参数。

        ★ 引擎要的是【整单】成本（estimate_total_fen），
          而我们是【单座】会员价 → 这里 × 张数。
          （良票那边 preflight 直接给整单，所以那边不用乘。）
        """
        return {
            "estimate_total_fen": self.total_cost_fen,
            "seat_count": max(1, self.seat_count),
            "original_total_fen": self.original_total_fen,
        }


def _seats_from_names(seat_map: SeatMapResult, seat_names: list[str]) -> tuple:
    """
    把买家给的座位名映射到 (区域, 座位) 列表。

    返回 (areas_used, missing) —— missing 是找不到的座位名。
    """
    used: list[MemberArea] = []
    missing: list[str] = []
    for name in seat_names or []:
        area = seat_map.area_of_seat(name)
        if area is None:
            missing.append(str(name))
        else:
            used.append(area)
    return used, missing


def resolve_cost(bridge: WandaBridgeClient, *,
                 city_id: str, cinema_id: str,
                 showtime_resolver,
                 phone: str,
                 seat_names: list[str] | None = None,
                 seat_count: int | None = None,
                 require_available: bool = False,
                 allow_unavailable_area_quote: bool = False,
                 verified_route: CinemaRoute | None = None,
                 fixed_cost_resolver=None) -> WandaCost:
    """
    解析万达单的成本。

    参数：
      city_id / cinema_id   良票侧的识别结果（★ 不是万达的）
      showtime_resolver     可调用：万达影院ID → 万达 showtimeId
                            （签名 (wanda_cinema_id) -> str | None）
      phone                 账号池里的手机号（★ 显式传，防静默回退）
      seat_names            买家选了的座位（空/None = 他要会员座位）
      seat_count            张数（没选座位时必须给）

    返回 WandaCost；失败抛 WandaCostError（带 code）。

    ★ 为什么 showtime_resolver 是注入的而不是本模块自己调：
      取场次是【读万达接口】，而出票系统的桥【没有】这个接口
      （白名单里只有 /api/showtime/seats）。
      所以现阶段由调用方提供（可以先用本地直连万达 client）。
      等桥加了场次接口，这里换成一个桥调用即可，本模块不用动。
    """
    # ---- ① 渠道：按 ID 判（不按名字）----
    try:
        route: CinemaRoute = verified_route or bridge.wanda_lookup(city_id=city_id,
                                                                  cinema_id=cinema_id)
    except WandaBridgeError as exc:
        # ★ 映射库不可用必须报出来，不能当"没映射"处理
        if exc.code == "IDENTITY_STORE_UNAVAILABLE":
            raise WandaCostError(
                "CHANNEL_UNKNOWN",
                "影院映射库不可用，判不出这单走万达还是良票 —— 不能瞎猜",
                detail=exc.code) from exc
        raise WandaCostError(
            "CHANNEL_UNKNOWN", f"查影院映射失败：{exc}", detail=exc.code) from exc
    if not route.use_wanda:
        raise WandaCostError(
            "NOT_WANDA",
            f"这家影院没有万达映射（{route.reason or 'NO_MAPPING'}）"
            f" → 这单应走良票")

    # ---- ② 万达场次 ----
    showtime_id = showtime_resolver(route.wanda_cinema_id)
    if not showtime_id:
        raise WandaCostError(
            "SHOWTIME_NOT_FOUND",
            f"万达侧找不到这个场次（cinema={route.wanda_cinema_id}）"
            f" —— 可能今天没排片，或场次时间对不上")

    # ---- ③ 座位图 ----
    seat_map = bridge.seat_map(str(showtime_id), phone=phone)

    # ---- ④ 选哪个区的会员价 ----
    want = [str(s).strip() for s in (seat_names or []) if str(s).strip()]
    if want:
        if require_available or fixed_cost_resolver is not None:
            from integrations.wanda.bridge import _seat_available, _norm_seat
            for name in want:
                matches = [s for a in seat_map.areas for s in a.seats
                           if _norm_seat(s.get('name')) == _norm_seat(name)]
                if len(matches) != 1:
                    raise WandaCostError('SEAT_NOT_IN_MAP', f'{name}未核实为唯一座位，请重新核对')
                if not _seat_available(matches[0]) and not allow_unavailable_area_quote:
                    raise WandaCostError('SEAT_UNAVAILABLE', f'{name}当前不可售，请换座', detail={'seats':[name]})
        # 买家选了座位 → 取【他选的座位所在区】的会员价
        used, missing = _seats_from_names(seat_map, want)
        if missing:
            raise WandaCostError(
                "SEAT_NOT_IN_MAP",
                f"买家选的座位在座位图里找不到：{missing}",
                detail={"missing": missing})
        # 一单可能跨区（选了两个不同区的座位）——
        # ★ 跨区时会员价可能不同，不能取平均，也不能取最低。
        #   先取第一个区，并在备注里标出"跨区"让人能发现。
        area = used[0]
        notes = []
        fixed_cost = fixed_cost_resolver(sum(a.sales_price_fen or 0 for a in used)) if fixed_cost_resolver else None
        if fixed_cost is not None:
            from integrations.wanda.bridge import _norm_seat
            if len({_norm_seat(s) for s in want}) != len(want):
                raise WandaCostError("VIP_PRICE_FACTS_INVALID", "所选座位不能重复")
            prices={a.sales_price_fen for a in used}
            if len(prices)!=1 or any(not p or p<=0 for p in prices):
                raise WandaCostError('VIP_PRICE_FACTS_INVALID','所选区域官方原价不唯一或缺失，需要分别核价')
            return WandaCost(ok=True,unit_cost_fen=fixed_cost,seat_count=len(want),original_unit_fen=next(iter(prices)),
                area_id=area.area_id,area_name=area.area_name,wanda_cinema_id=route.wanda_cinema_id,showtime_id=str(showtime_id),
                source='WANDA_RULE_FIXED_COST',notes=['按已匹配的影厅规则取固定预估成本，不采用W+活动价'])
        if len({a.area_id for a in used}) > 1:
            if any(not a.has_member_price for a in used):
                raise WandaCostError('SEAT_NOT_IN_MAP', '所选座位跨区域且涉及普通售价，请分别核价，暂不自动报价')
            if require_available:
                raise WandaCostError('SEAT_NOT_IN_MAP', '本次座位跨价格区域，需要店家分别核价，暂不自动报价')
            notes.append(
                "★ 买家选的座位跨了多个区域（"
                + "/".join(sorted({f'{a.area_name}({a.area_id})' for a in used}))
                + f"）—— 按第一处「{area.area_name}」的会员价算，请人工确认")
        if not area.has_member_price and (area.member_activity_present or not area.sales_price_fen or area.sales_price_fen <= 0):
            raise WandaCostError(
                "NO_MEMBER_PRICE",
                f"买家选的座位在「{area.area_name}」，但该区域没有会员价",
                detail={"area_id": area.area_id})
        count = len(want)
    else:
        if fixed_cost_resolver and fixed_cost_resolver(None) is not None:
            raise WandaCostError('VIP_SEATS_REQUIRED','该影厅使用固定预估成本规则，请先确认具体座位及官方原价')
        # ★★ 没选座位 = 买家要【会员座位】→ 取 W+专享区
        #    绝不能退化成"随便挑个最便宜的区"（那是报错价）
        member_areas = seat_map.member_seat_areas()
        areas = [a for a in member_areas if a.available_member_seats()]
        sales_fallback = False
        if areas:
            # Membership and activity pricing are independent facts. Missing
            # activity uses the *member area's* salesPrice, not a cheaper zone.
            if any(not a.has_member_price and (a.member_activity_present or not a.sales_price_fen or a.sales_price_fen <= 0) for a in areas):
                raise WandaCostError('NO_MEMBER_PRICE', '会员区有余座，但活动价异常或区域售价缺失，请核价')
            costs = {a.member_price_fen if a.has_member_price else a.sales_price_fen for a in areas}
            originals = {a.sales_price_fen for a in areas}
            if len(costs) != 1 or (len(areas) > 1 and
                    (len(originals) != 1 or any(not p or p <= 0 for p in originals))):
                raise WandaCostError('MEMBER_AREA_PRICE_AMBIGUOUS', '可售会员区域价格不同，请确认具体座位后核价')
        elif not member_areas and seat_map.areas and all(not a.member_activity_present and not a.has_member_price for a in seat_map.areas):
            from integrations.wanda.bridge import _seat_available
            available = [a for a in seat_map.areas if any(_seat_available(s) for s in a.seats)]
            if (available and all(a.sales_price_fen and a.sales_price_fen > 0 for a in available)
                    and len({a.sales_price_fen for a in available}) == 1):
                areas = available
                sales_fallback = True
            elif available:
                raise WandaCostError('MEMBER_AREA_PRICE_AMBIGUOUS', '没有可确认的统一会员区域价格，请确认具体座位后核价')
        if not areas:
            raise WandaCostError('NO_MEMBER_SEATS', '当前没有可售会员座位',
                                 detail={'showtime_id': str(showtime_id)})
        area = areas[0]
        notes = ["买家未指定座位 → 按会员座位区「"
                 f"{area.area_name}」的会员价"]
        count = int(seat_count or 1)
        # ★ 张数不能超过可售会员座位数
        avail = (sum(sum(_seat_available(s) for s in a.seats) for a in areas)
                 if sales_fallback else sum(len(a.available_member_seats()) for a in areas))
        if count > avail:
            raise WandaCostError(
                "SEAT_UNAVAILABLE",
                f"要 {count} 张，但「{area.area_name}」只剩 {avail} 个可售会员座位",
                detail={"want": count, "available": avail})

    # ---- ⑤ 数据合理性：会员价不能高于原价 ----
    #
    # ★ 来源：V5 `providers/wanda/cost.py:62-64`
    #     if any(price > _money(row.get('priceFen')) ...):
    #         raise WandaProviderError('WANDA_COST_UNAVAILABLE',
    #                                  'activity cost exceeds original price', 'cost')
    #   会员价本该是折扣价；高于原价说明活动数据异常。
    #   ★ 这种时候【不能采信】—— 按它算出来的售价会高于原价，
    #     等于拿"折扣"报了比原价还贵的价。
    if (area.member_price_fen and area.sales_price_fen
            and area.member_price_fen > area.sales_price_fen):
        raise WandaCostError(
            "MEMBER_PRICE_ABOVE_ORIGINAL",
            f"「{area.area_name}」会员价 {area.member_price_fen/100:.2f} 元"
            f" 高于原价 {area.sales_price_fen/100:.2f} 元 —— 活动数据异常，不采信",
            detail={"member_fen": area.member_price_fen,
                    "sales_fen": area.sales_price_fen,
                    "area_id": area.area_id})

    use_member = area.has_member_price
    if not use_member:
        notes = [n for n in notes if '会员价' not in n]
        notes.append('无会员活动，按对应区域 salesPrice 普通售价核价')
    return WandaCost(
        ok=True,
        unit_cost_fen=int(area.member_price_fen if use_member else area.sales_price_fen),
        seat_count=max(1, count),
        original_unit_fen=area.sales_price_fen,
        area_id=area.area_id,
        area_name=area.area_name,
        wanda_cinema_id=route.wanda_cinema_id,
        showtime_id=str(showtime_id),
        source="WANDA_MEMBER_PRICE" if use_member else "WANDA_SALES_PRICE",
        notes=notes,
    )
