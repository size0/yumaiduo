"""
application/store.py —— 持久化：配置、文案、规则、凭证、会话

════════════════════════════════════════════════════════════════
★ 为什么要单独一层

  审计抓到一个循环依赖：
      server  →  application.service  →  server.load_rules

  service 需要读规则，而读规则写在 server 里 —— 于是两边互相依赖。
  这类循环的共同原因都是"某个函数放错了地方"。
  这个文件把【所有落盘读写】收拢到一处，解开循环：

      server    → store   （只负责 HTTP）
      service   → store   （只负责接线）
      store     → settings / templates / qa / quote_rules

  store 不认识 HTTP，也不认识编排 —— 它只认识文件。

★ 一条边界规则

  凡是"读/写磁盘"的动作都放这里；
  凡是"业务判断"都不放这里。
  这样换存储（文件 → 数据库）时只需要改这一个文件。
════════════════════════════════════════════════════════════════
"""

import json
import os
import stat
import tempfile
import sys
from threading import Lock, RLock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import settings as settings_module                  # noqa: E402
import templates as templates_module                # noqa: E402
from domain import quote_rules as qr                # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
_REPLY_METRICS_LOCK = Lock()


def append_reply_metrics(row: dict) -> None:
    """Append content-free daily observations; session snapshots alone lose history."""
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc)
    data = {"observed_at": stamp.isoformat(), **row}
    path = DATA / "reply-metrics" / (stamp.strftime("%Y-%m-%d") + ".jsonl")
    with _REPLY_METRICS_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(data, ensure_ascii=False) + "\n")

CONFIG_FILE = DATA / "settings.json"       # AI助理 + 功能设置
TEMPLATES_FILE = DATA / "templates.json"   # 文案改动
RULES_FILE = DATA / "quote_rules.json"     # 报价规则
BRANDS_FILE = DATA / "brands.json"         # 院线名缓存（从 brand/list 拉）
CRED_FILE = DATA / "liangpiao.json"        # 良票凭证 + 模型 Key
SESSIONS_FILE = DATA / "sessions.json"     # 会话状态
QA_FILE = DATA / "qa.json"                 # 关键词回复（智能问答）
# ★★ 2026-10-14 新增：店铺级开关。
#   ★ 为什么需要它、以及它【不是】什么：
#     · 鱼麦多只给了「读店铺列表」一个能力（没有增删改），
#       所以平台上的「启用状态」是【只读】的，我们改不了。
#     · 但店主需要"关掉某个店的自动发送"（比如某个店在放假/在整改）。
#       ⇒ 那是【我们自己】的开关，不是平台的：关掉 ⇒ 我们不往那个店发消息。
#     · 存我们自己的 data/shops.json，和平台状态分开，绝不混在一起改。
SHOPS_FILE = DATA / "shops.json"           # 店铺级开关（自动发送）
# ★ 报价记录（对应线上 /var/lib/ticket-system-v5/quotes.sqlite3）
#   为什么用 sqlite 而不是 json：报价要按 quote_id 精确取，
#   而且要能按身份过滤（报价隔离）。json 每次全量读写扛不住。
QUOTES_FILE = DATA / "quotes.sqlite3"

# 页面导航用：哪些页做完了
PAGES = [
    {"path": "/", "name": "AI助理", "done": True},
    {"path": "/replies", "name": "回复文案", "done": True},
    {"path": "/qa", "name": "关键词回复", "done": True},
    {"path": "/features", "name": "功能设置", "done": True},
    {"path": "/rules", "name": "报价规则", "done": True},
    {"path": "/quotes", "name": "报价记录", "done": True},
    {"path": "/orders", "name": "订单管理", "done": True},
]


# ★★ 订单状态词典（给界面用）。
#
#   ★ 取值与中文名来自 domain/order/models.py 的 OrderState 和
#     PaymentState —— 不是在这里另发明一套。
#   ★ 桶（bucket）照线上 V5 application/wanda_ticketing.py 的
#     BUCKETS 分组：unpaid / awaiting_ticket / ticketed / delivered /
#     attention / closed。界面按桶分标签页，运营一眼知道哪些要管。
ORDER_STATE_LABELS = {
    # ★★★ 2026-10-14 补上 AWAITING_PAYMENT / 等别名。
    #   为什么必须补：订单库【真的】会写这个状态
    #     （application/ticket_flow.py:1683 `state="PAID" if paid else "AWAITING_PAYMENT"`
    #       以及 :1810 的本地待付款单），
    #     但这两张表里只有 "CREATED"。
    #   ⇒ 后果（订单页实测看到的）：
    #       中文名回落成原始英文 → 状态列显示 "AWAITING_PAYMENT"，
    #       而旁边的桶按钮写的是「待付款」，同一行两种叫法；
    #       而且它被归到默认桶（显示上算进 attention），分类是错的 ——
    #       待付款是【正常路径的第一格】，不是"要人管"。
    #   ★ 保留 CREATED 同时认这两个：库里两种都可能有（历史 + 新写），
    #     统一按"待付款"展示、都归 unpaid 桶。
    "CREATED": "待付款",
    "AWAITING_PAYMENT": "待付款",
    "PAID": "待出票",
    "TICKETED": "已出票",
    "DELIVERED": "已交付",
    "REPRICE_PENDING": "待改价",
    "FAILED": "出票失败",
    "CANCELLED": "已取消",
    "REFUNDED": "已退款",
}
ORDER_STATE_BUCKETS = {
    "CREATED": "unpaid",
    "AWAITING_PAYMENT": "unpaid",
    "PAID": "awaiting_ticket",
    "TICKETED": "ticketed",
    "DELIVERED": "delivered",
    "REPRICE_PENDING": "attention",
    "FAILED": "attention",
    "CANCELLED": "closed",
    "REFUNDED": "closed",
}
PAYMENT_STATE_LABELS = {
    "UNPAID": "未付款",
    "CONFIRMED": "已确认",
    "AMBIGUOUS": "状态不明",
    "REJECTED": "付款被拒",
    # ★★ 2026-10-14 新增（出票失败【全额退回】时置上）。
    #   为什么必须有这个值：出票失败后买家付的钱要还回去，
    #   那张单不能还留在"已确认"里 —— 否则对账会把它算成收入。
    #   ★ 只用于全额退回；有损关单（lossAmount>0）【不】置它 ——
    #     那种情况买家确实付了钱、损失由我们承担，要留在"需关注"里
    #     按 lossAmount 认账（详见 ticket_flow._on_failed）。
    "REFUNDED": "已退款",
}
# ★ 报价记录状态（domain/quote/models.py 的 QuoteStatus）
QUOTE_STATUS_LABELS = {
    "READY": "有效",
    "CONFIRMED": "已确认",
    "SUPERSEDED": "已作废",
    "EXPIRED": "已过期",
    "CANCELLED": "已取消",
}


def _read_json(path: Path, default):
    """读 JSON。坏了就返回默认值 —— 配置坏了不该让整个服务起不来。"""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")


# ==================================================================== 配置
def load_config() -> "settings_module.Settings":
    """读全部配置（AI助理 + 功能设置）。"""
    data = settings_module.Settings()
    saved = _read_json(CONFIG_FILE, None)
    if isinstance(saved, dict):
        data.apply_dict(saved)
    from .shop_scope import apply_settings
    return apply_settings(data)


def save_config(data: "settings_module.Settings") -> None:
    from .shop_scope import save_settings
    if not save_settings(data):
        _write_json(CONFIG_FILE, data.to_dict())


# ==================================================================== 文案
def load_template_overrides() -> None:
    """
    把上次保存的文案读回来，盖到默认值上。

    ★ 存储形状（两种都要兼容）：
        扁平（老格式）  {"QUOTE_CARD": "文案…"}
        带渠道（新格式）{"QUOTE_CARD": {"text": "全局文案",
                                        "channel_text": {"wanda": "…", "liangpiao": "…"}}}

      ★ 为什么要兼容老格式：这个文件可能已经存着运营改过的文案，
        直接按新格式读会让所有改动失效 —— 那是数据丢失，不是升级。

    ★ 运营改的永远是【全局】那一份（前端点"全局"tab 时 channel='global'）——
      渠道覆盖是额外的一层，不是替换。
    """
    saved = _read_json(TEMPLATES_FILE, None)
    if not isinstance(saved, dict):
        return
    for row in templates_module.TEMPLATES:
        item = saved.get(row["key"])
        if isinstance(item, str):
            # 老格式：扁平字符串 = 全局文案
            row["text"] = item
        elif isinstance(item, dict):
            if isinstance(item.get("text"), str):
                row["text"] = item["text"]
            if isinstance(item.get("variants"), dict):
                row["variants"] = {str(k): v for k, v in item["variants"].items()
                                    if isinstance(v, str)}
            ch = item.get("channel_text")
            if isinstance(ch, dict):
                row["channel_text"] = {**templates_module.DEFAULT_CHANNEL_TEXT.get(row["key"], {}),
                                       **{str(k): v for k, v in ch.items() if isinstance(v, str)}}


def save_templates() -> None:
    """存文案。★ 全局 text + 每个渠道的覆盖，两层都存。"""
    out = {}
    for t in templates_module.TEMPLATES:
        item: dict = {"text": t["text"]}
        ch = t.get("channel_text") or {}
        item["channel_text"] = ch
        item["variants"] = t.get("variants") or {}
        out[t["key"]] = item
    _write_json(TEMPLATES_FILE, out)


# ==================================================================== 关键词回复
def load_qa_overrides() -> None:
    """
    把保存过的关键词回复读回来，【按 ID 合并】到代码里的默认值上。

    ★★ 为什么是"合并"而不是"替换"（这是我踩过的坑）：
      第一版写的是 `QA_RULES[:] = rules` —— 整体替换。
      于是：data/qa.json 里存着 18 条，代码里我新加了 7 条，
      【只要磁盘文件有内容，那 7 条代码里的新规则重启后就消失了】。
      表现：改代码没用（文件赢），而且不报错、很难查。

    ★ 合并规则：
        · 文件里有的 ID  → 用文件里的（运营改过，以文件为准）
        · 代码里有、文件没有的 ID → 保留（新加的代码规则不能丢）
        · 文件里有、代码没有的 ID → 也保留（运营在界面上新建的）
        · deleted_ids 记录主动删除的 ID，重启后不再恢复；重新添加可解除删除。
        · 顺序：先按代码的顺序（保持稳定），再补上文件里多出来的
    """
    saved = _read_json(QA_FILE, None)
    if not isinstance(saved, dict):
        return
    rows = saved.get("rules")
    if not isinstance(rows, list):
        return
    deleted = {value for value in saved.get('deleted_ids', []) if isinstance(value, str)}
    import qa as qa_module
    known = set(qa_module.QARule.__dataclass_fields__)

    def build(item):
        if not isinstance(item, dict):
            return None
        try:
            return qa_module.QARule(
                **{k: v for k, v in item.items() if k in known})
        except TypeError:
            return None

    by_file = {}
    for item in rows:
        rule = build(item)
        if rule is not None:
            by_file[rule.qa_id] = rule
    if not by_file and not deleted:
        return

    merged, seen = [], set()
    # ① 先走代码的顺序 —— 文件里有同 ID 就用文件里的
    for rule in qa_module.QA_RULES:
        if rule.qa_id in deleted and rule.qa_id not in by_file:
            continue
        merged.append(by_file.get(rule.qa_id, rule))
        seen.add(rule.qa_id)
    # ② 再补上文件里多出来的（界面上新建的）
    for qa_id, rule in by_file.items():
        if qa_id not in seen:
            merged.append(rule)
            seen.add(qa_id)

    qa_module.QA_RULES[:] = merged
    # ★ 必须重建索引，否则界面改了、匹配走的还是旧的
    qa_module.reindex()


def save_qa_rules(rules: list) -> None:
    import os
    import tempfile
    import qa as qa_module
    saved = _read_json(QA_FILE, {})
    deleted = set(saved.get('deleted_ids', [])) if isinstance(saved, dict) else set()
    current_ids = {r.qa_id for r in rules}
    deleted.update(r.qa_id for r in qa_module.QA_RULES if r.qa_id not in current_ids)
    deleted.difference_update(current_ids)
    payload = json.dumps({"rules": [r.__dict__ for r in rules],
                          "deleted_ids": sorted(deleted)}, ensure_ascii=False, indent=2)
    QA_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=QA_FILE.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, QA_FILE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def qa_payload() -> dict:
    """关键词回复页要的全部数据。"""
    import qa as qa_module
    return {
        "rules": [r.__dict__ for r in qa_module.QA_RULES],
        "count": len(qa_module.QA_RULES),
        "triggers": [qa_module.Trigger.REPEAT, qa_module.Trigger.ONCE],
        "message_types": ["文本消息", "图片消息"],
        "limits": ["不限制", "1", "3", "5", "10", "20"],
        # ★ 内容里可以写 [全局调用标识ID] 引用另一条（支持嵌套，带环检测）
        "ref_pattern": qa_module.REF_PATTERN.pattern,
        "pages": PAGES,
    }


# ==================================================================== 报价规则
def sync_brands_from_api(api) -> int:
    """
    ★ 从良票 `brand/list` 拉院线名，写进本地缓存。

    这是补上一个真实的缺口：
      报价规则里的院线条件必须用【良票的真实品牌名】，
      而票达人页面上的「万达电影（一线）A类」是它自己的分级，
      良票 brand/list 里根本没有 —— 照抄的话规则永远匹配不上。

    返回拉到多少个品牌。失败时返回 0（不让它影响启动）。
    """
    try:
        data = api.ch4_brand_list()
    except Exception:                                   # noqa: BLE001
        return 0
    items = (data or {}).get("list") if isinstance(data, dict) else data
    names = qr.brands_from_api(items or [])
    if names:
        _write_json(BRANDS_FILE, {"brands": names})
    return len(names)


def load_brands() -> list[str]:
    """
    取院线名全集：优先用接口拉到的缓存，没有就退回已知的两个。

    ★ 为什么要有缓存：brand/list 需要签名（要凭证），
      而规则页在没配凭证时也得能打开。所以缓存是必须的。
    """
    saved = _read_json(BRANDS_FILE, None)
    if isinstance(saved, dict) and isinstance(saved.get("brands"), list):
        names = [str(b) for b in saved["brands"] if b]
        if names:
            return names
    return list(qr.KNOWN_BRANDS)


# ★★ 缓存多久算过期。院线名基本不变，一天一次足够。
BRANDS_TTL_SECONDS = 24 * 3600


def brands_age_seconds() -> float | None:
    """缓存文件的年龄（秒）；没有文件返回 None。"""
    try:
        if not BRANDS_FILE.exists():
            return None
        import time
        return time.time() - BRANDS_FILE.stat().st_mtime
    except OSError:
        return None


def refresh_brands_if_stale(api) -> int:
    """
    院线名缓存过期就重新拉一次；返回拉到的个数（0 = 没拉/失败）。

    ★★★ 为什么必须补这一步（2026-10-14 审计发现）：
      `sync_brands_from_api` 早就写好了，但【全仓没有任何调用者】——
      ⇒ data/brands.json 永远不会存在 ⇒ load_brands() 永远退回
        KNOWN_BRANDS（只有两个名字）。
      实测后果：
        · 良票 brand/list 真实有【140】个院线名，我们只认识 2 个
        · 规则的 brands 条件靠 name 精确比较（QuoteRule.__post_init__）
          ⇒ 除万达/CGV 外，其余 138 个院线的规则【永远匹配不上】
        · rules_payload 的 known_brands 只给 2 个选项
          ⇒ 店主在规则页里根本没法给别的院线配规则
      实测确认识别结果里的 brandName 与 brand/list 的 name 是【同一个值】
      （两张真图都返回 '万达影城'，与 brand/list 里的 '万达影城' 逐字一致），
      所以动态拉到之后可以直接用。

    ★ 为什么把"过期判断"放在这里、而不是无条件同步：
      规则页是 GET，可能被反复打开；brand/list 是要签名的外网调用。
      无条件同步 = 每刷一次页面打一次良票。加 TTL 之后一天最多一次。

    ★ 为什么失败要静默返回 0：
      规则页在没配凭证/断网时也必须能打开（load_brands 有 KNOWN_BRANDS 兜底）。
      为了刷新名称而让整个页面报错，是本末倒置。
    """
    age = brands_age_seconds()
    if age is not None and age < BRANDS_TTL_SECONDS:
        return 0
    if api is None:
        return 0
    try:
        return sync_brands_from_api(api)
    except Exception:                                        # noqa: BLE001
        return 0


def rules_path(tenant_id="", shop_id=""):
    """Use a digest of both identifiers; never interpolate identifiers into paths."""
    import hashlib
    if not tenant_id and not shop_id:
        return RULES_FILE
    if not tenant_id or not shop_id:
        raise ValueError("店铺规则必须同时指定租户和店铺")
    key = hashlib.sha256(json.dumps([str(tenant_id), str(shop_id)]).encode()).hexdigest()
    return RULES_FILE.parent / "shop_rules" / (key + ".json")


def load_rules(tenant_id="", shop_id="") -> list:
    """
    读报价规则。

    ★ 文件不存在时给一份【界面能看、也真能用】的初始规则 ——
      用良票的真实品牌名、按院线分档、带加价。

    ★ 这里踩过一次：中间版本用了 default_rules_for_brands()，
      那只生成 1 条兜底规则，规则页从 6 行变成 1 行，等于把功能做没了。
      界面"有东西可看"和"东西是对的"要同时满足。
    """
    path = rules_path(tenant_id, shop_id)
    if path != RULES_FILE and not path.exists():
        return load_rules()
    saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    if isinstance(saved, list):
        if not saved:
            return []
        known = qr.QuoteRule.__dataclass_fields__
        try:
            loaded = []
            for item in saved:
                # ★★ tiers 必须还原成 PricingTier。
                #   原来直接 QuoteRule(**item)，tiers 就留成一堆 dict ——
                #   算价时 getattr(row, "lower_bps") 拿不到值（dict 没这个属性），
                #   落档永远返回 None 或命中默认档，比例全不生效。
                #   ★ 而且【不报错】：价格照算，只是算错。最难查的一类。
                tiers = [
                    qr.PricingTier(
                        **{k: v for k, v in t.items()
                           if k in qr.PricingTier.__dataclass_fields__})
                    for t in (item.get("tiers") or [])
                    if isinstance(t, dict)
                ]
                loaded.append(qr.QuoteRule(
                    **{k: v for k, v in item.items()
                       if k in known and k != "tiers"},
                    tiers=tiers))
            if loaded:
                return loaded
        except (TypeError, AttributeError) as exc:
            raise ValueError("报价规则文件格式错误") from exc
    if saved is not None:
        raise ValueError("报价规则文件必须是数组")
    return qr.seed_rules(load_brands())


def save_rules(rules: list, tenant_id="", shop_id="") -> None:
    # ★ 必须走 _rule_to_dict，不能直接 r.__dict__ ——
    #   tiers 里装的是 PricingTier dataclass，json.dumps 会报
    #   "Object of type PricingTier is not JSON serializable"。
    #   这个坑在 rules_payload（读）和 save_rules（写）各踩了一次。
    import os
    import tempfile
    path = rules_path(tenant_id, shop_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump([_rule_to_dict(r) for r in rules], stream, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _rule_to_dict(rule) -> dict:
    """
    规则 → 可 JSON 序列化的 dict。

    ★ 为什么不能直接 `rule.__dict__`：
      规则里现在有 `tiers` —— 里面装的是 `PricingTier` dataclass，
      `__dict__` 会把它原样塞进去，`json.dumps` 立刻报
      "Object of type PricingTier is not JSON serializable"。
      （我加 tiers 时就是这么炸的。）
    """
    out = dict(rule.__dict__)
    out["tiers"] = [_tier_to_dict(t)
                    for t in (getattr(rule, "tiers", None) or [])]
    return out


def _tier_to_dict(t) -> dict:
    """
    一行分档 → dict。★ 必须同时支持 PricingTier 和 dict。

    ★★ 踩过的坑（很难查，记在这）：
      原来这里只写了 `getattr(t, "lower_bps", 0)`。
      但【从 quote_rules.json 读回来的规则】其 tiers 已经是 dict 了，
      `getattr(dict, "lower_bps", 0)` 永远拿到默认值 0 ——
      于是每次保存都把整张分档表抹平成「0-10000 / 比例 0」，
      算价时落档永远命中第一档、比例恒为 0，价格看起来"正常"但全错。
    """
    if isinstance(t, dict):
        get = lambda k, d: t.get(k, d)                        # noqa: E731
    else:
        get = lambda k, d: getattr(t, k, d)                   # noqa: E731
    return {
        "lower_bps": int(get("lower_bps", 0) or 0),
        "upper_bps": int(get("upper_bps", 10000) or 10000),
        "adjustment_bps": int(get("adjustment_bps", 0) or 0),
        "fixed_add_fen": int(get("fixed_add_fen", 0) or 0),
        "upper_inclusive": bool(get("upper_inclusive", False)),
    }


def rules_payload(tenant_id="", shop_id="") -> dict:
    """报价规则页要的全部数据。"""
    rules = load_rules(tenant_id, shop_id)
    # ★ 院线全集 = 所有规则里出现过的院线【并集】，再加上缓存里的品牌名。
    #   早先写成"第一条超过 5 个院线的规则"，拿到的是局部列表 —— 界面上会少选项。
    all_brands: list[str] = []
    for rule in rules:
        for b in rule.brands:
            if b not in all_brands:
                all_brands.append(b)
    # 已知/拉到的良票品牌名也放进去，让运营能选到自己还没用过的品牌
    # ★ 这一段原先【写了两遍】（同一个 for 连着两次）——
    #   第二遍一个都加不进去（第一遍已经全加了），纯属多余。
    #   审计发现后合并成一次。
    for b in load_brands():
        if b not in all_brands:
            all_brands.append(b)
    return {
        "scope": "shop" if shop_id and rules_path(tenant_id, shop_id).exists() else "shared",
        "tenant_id": tenant_id, "shop_id": shop_id,
        "rules": [_rule_to_dict(r) for r in rules],
        "problems": qr.validate_rules(rules),
        "options": {
            "quote_modes": list(qr.QUOTE_MODES),
            "quote_mode_disabled": [qr.QUOTE_SCENE],
            "methods": list(qr.QUOTE_METHODS),
            "adjusts": list(qr.ADJUST_TYPES),
            "price_mode_of_quote": qr.PRICE_MODE_OF_QUOTE,
            # ★ 分档表的计价基数（对齐 V5 的 RoutePricing.basis）。
            #   它决定「折扣率」用谁算、以及比例乘在谁身上 —— 界面必须给出选择。
            "bases": list(qr.BASIS_TYPES),
            "basis_labels": {
                qr.BASIS_ORIGINAL: "原价",
                qr.BASIS_ESTIMATED: "预估成本",
                qr.BASIS_PER_TICKET: "每张固定加价",
                qr.BASIS_COST_PLUS: "整单固定加价",
            },
        },
        "brands": all_brands,
        # ★ 也把票达人的分级名单独列出来（23 条），
        #   让运营一眼看出"哪些名字不是良票的品牌名、选了也匹配不上"
        "tier_names_from_piaodaren": list(qr.TIER_NAMES_FROM_PIAODAREN),
        "known_brands": load_brands(),
        "pages": PAGES,
    }


# ==================================================================== 凭证
def load_credentials() -> dict:
    """
    读凭证。两类：

      良票（买票用）     app_key / app_secret / tel / base_url
      模型（说话用）     model_api_key

    ★ 为什么放一起：都是"不能进前端、不能进代码库"的东西，
      运维只要管一个文件。但用途完全不同，注释里写清楚，别再混用变量名。
    """
    saved = _read_json(CRED_FILE, None)
    if isinstance(saved, dict):
        saved.setdefault("model_api_key", "")
        # ★ 店铺属于哪个渠道（"wanda" / "liangpiao"）—— 决定用哪套文案。
        #   买家进来时还不知道影院，所以渠道只能由店铺决定。
        saved.setdefault("channel", "")
        return saved
    return {"app_key": "", "app_secret": "", "tel": "", "base_url": "",
            # ★ 对话模型的 Key 和良票的凭证放一起，但用途完全不同：
            #   一个是买票的凭证，一个是说话的凭证。别再混用同一个变量名。
            "model_api_key": "",
            "channel": ""}


CREDENTIAL_LOCK = RLock()


def save_credentials(data: dict) -> None:
    with CREDENTIAL_LOCK:
        _save_credentials_atomic(data)


def _save_credentials_atomic(data: dict) -> None:
    path = Path(CRED_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    fd, name = tempfile.mkstemp(prefix='.credentials-', dir=path.parent)
    temporary = Path(name)
    try:
        os.chmod(temporary, mode)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            fd = None
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if fd is not None:
            os.close(fd)
        if temporary.exists():
            temporary.unlink()


# ==================================================================== 店铺开关
def load_shop_prefs() -> dict:
    """
    读「店铺级开关」。返回 {"<shop_id>": {"send_enabled": bool}, ...}

    ★ 这是【我们自己的】开关，和平台上的"店铺启用状态"是两回事：
        平台状态   ：鱼麦多那边的（只读，我们改不了）
        send_enabled：我们这边的（关掉 ⇒ 不往这个店发任何自动消息）
      ★ 为什么不直接改平台状态：官方只给了「读店铺列表」一个能力，
        没有增删改。硬要改就是在做平台不允许的事。

    ★ 没配置过的店铺默认【开】（True）—— 保持原行为，
      不然上线一新增店铺就静默不发言了。
    """
    saved = _read_json(SHOPS_FILE, None)
    return saved if isinstance(saved, dict) else {}


def save_shop_prefs(data: dict) -> None:
    _write_json(SHOPS_FILE, data)


def shop_send_enabled(shop_id: str) -> bool:
    """
    这个店铺允许我们自动发消息吗。

    ★ 默认 True（没配过 = 开）：保持原行为最安全 ——
      新增一个店铺就"默认静音"会让人完全找不到原因。
    ★ 读不到配置也返回 True：宁可照常发（并在别处报错），
      也不要在配置故障时把所有店静默静音。
    """
    sid = str(shop_id or "").strip()
    if not sid:
        # ★ 没店铺号怎么办：不能"因为没有店铺号就不发" ——
        #   那会让所有历史命令静默失效。按"允许"处理，
        #   真正该拦的地方是插件里的 allowlist（按 shop_id 精确匹配）。
        return True
    try:
        prefs = load_shop_prefs()
        one = prefs.get(sid)
        if isinstance(one, dict):
            return bool(one.get("send_enabled", True))
        return True
    except Exception:                                     # noqa: BLE001
        return True


# ==================================================================== 会话
def load_sessions() -> dict:
    """读回会话状态。

    ★ 只存"状态 + 上一轮问的候选"，不存报价对象 ——
      报价有 30 分钟有效期，跨重启还留着反而是隐患。
    """
    saved = _read_json(SESSIONS_FILE, None)
    return saved if isinstance(saved, dict) else {}


def save_sessions(sessions: dict) -> None:
    _write_json(SESSIONS_FILE, sessions)
