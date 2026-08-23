from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


DEFAULT_REPLY_TEMPLATES: dict[str, str] = {
    "first_contact_notice": "您好，请发送已标记购买位置的完整选座页截图\n并说明需要几张\n\n收到报价后请回复“确认”\n再提交订单并保持待付款\n收到“价格已修改”后再付款",
    "quote_confirmation_instruction": "接受本次报价请回复“确认”。",
    "quote_processing_notice": "收到，正在按当前信息核对万达实时场次和优惠，请稍等。",
    "quote_replaced_by_official_selection": "您这次发送的是官方已选座截图，已按具体座位重新核价；上一版未选座试价已失效。",
    "quote_exact": "※{城市标记}| {影院}\n电影：{影片}\n影厅：{影厅}\n场次：{日期} {场次}\n座位：{座位}\n\n{单价}元/张，{张数}张合计{合计}元。",
    "quote_area": "※{城市标记}| {影院}\n电影：{影片}\n影厅：{影厅}\n场次：{日期} {场次}\n\n{单价}元/张，{张数}张合计{合计}元。\n出票时按您原图圈选的位置操作，无需提供具体座位号；若该位置届时不可选，会先联系您确认，不会擅自换座。",
    "quote_need_count": "※{城市标记}| {影院}\n电影：{影片}\n影厅：{影厅}\n场次：{日期} {场次}\n\n实时单价{单价}元/张，请告诉我需要几张。\n出票时按您原图圈选的位置操作，无需提供具体座位号；若该位置届时不可选，会先联系您确认，不会擅自换座。",
    "quote_count_completed": "已收到{张数}张需求\n实时单价{单价}元/张，{张数}张合计{合计}元。\n请提交订单后先不要付款\n等待系统改价\n仅在收到“价格已修改”后付款。",
    "quote_buyer_app_better_price": "您现在用的APP有合适的优惠价，可以自行购买。",
    "available_wplus_seats": "可以选。当前万达实时座位图中，{排数}排可选W+座位：{可选座位}。",
    "manual_delivery_preference": "已记录：出票时按您原图圈选的位置操作，无需提供具体座位号。若该位置届时不可选，会先联系您确认，不会擅自换座。",
    "wplus_area_unavailable": "当前无法核验 W+ 区域，请人工确认后处理。",
    "wplus_seats_unavailable": "当前场次没有W+位置可选择，您看到的位置可能是维修状态无法购买。",
    "wplus_price_unavailable": "当前场次未查到可用的W+会员专属优惠，暂不能自动报价，请人工确认。",
    "quote_price_conflict": "当前场次会员优惠不足，按当前规则暂无法形成安全报价，请人工确认。",
    "insufficient_available_seats": "当前没有足够同类可用座位，请人工确认。",
    "cinema_catalog_not_unique": "请问这是哪个城市的万达影城？已识别的影片、日期、场次和座位信息会保留，无需重发截图。",
    "showtime_not_unique": "截图信息无法唯一匹配场次，请补充影院和开场时间。",
    "showtime_not_found": "当前万达官方场次中未找到该日期和开场时间，请刷新万达选座页后发送最新截图。",
    "official_selection_unverifiable": "截图中的官方已选座当前并非全部实时可选，请在购票平台重新选择当前可选座位后发送最新完整截图；请勿付款。",
    "image_not_seat_map": "截图价格仅供参考，实际价格以万达实时核价结果为准。",
    "order_paid": "订单已付款，后续由人工出票或售后处理，不会重新核价。",
    "need_image": "请发送万达电影票座位图截图，并补充需要的张数。",
    "text_quote_missing_fields": "为了实时核价，请发送已标记需要购买位置的完整选座页截图，并说明需要几张。截图需要补全：{缺失信息}。图片标记仅供人工出票，不代表官方选座。",
    "non_wanda_cinema": "暂时只代订万达影院的电影票。",
    "ticket_count_conflict": "文字张数与官方已选座张数不一致，请人工确认。",
    "wplus_account_unavailable": "W+ 核价账号暂不可用，请稍后人工确认。",
    "temporary_lock_failed": "实时优惠核验暂未完成，请稍后人工确认。",
    "temporary_lock_release_unverified": "本次临时试价已尝试取消，但试价座位尚未在万达实时座位图中确认恢复；这不代表该场会员座都不可售。为避免重复占座，本次已停止自动报价，请勿付款，稍后重新发送最新完整选座页。",
    "wanda_gateway_unavailable": "万达实时核价暂不可用，请稍后重试。",
    "quote_verification_failed": "暂未核到该场实时价格，请补充完整影院名、影片和开场时间。",
    "recognition_failed": "选座截图暂未识别成功，请重新发送清晰完整的选座图，并补充影院、影片、场次和需要张数。",
    "order_submit_before_payment": "点击右上角立即购买~确定购买，先不输密码再返回（如果有免密付款页面修改为普通支付）\n提交订单后请先不要付款，等待系统确认改价成功后再付款。",
    "order_price_change_failed": "当前订单金额无法自动修改，请先不要付款，已转人工处理。",
    "price_change_authorization_failed": "平台订单改价授权失败，请先不要付款，已转人工核查。",
    "paid_amount_mismatch": "订单金额与本次核验报价不一致；如已支付，请勿重复下单，联系人工处理。",
    "paid_quote_unconfirmed": "订单已付款，但未找到有效确认报价；请勿重复下单，联系人工处理。",
    "paid_manual_delivery": "已收到付款，请稍等人工出票。订单已付款，不会重新核价。",
}

DEFAULT_REPLY_TEMPLATE_IMAGES: dict[str, str] = {key: "" for key in DEFAULT_REPLY_TEMPLATES}

DEFAULT_RUNTIME_SETTINGS: dict[str, Any] = {
    "automation_enabled": False,
    "recognition_enabled": True,
    "quote_enabled": True,
    "auto_price_change": False,
    "ai_reply_enabled": False,
    "shadow_evaluation_enabled": False,
    "conversation_agent_mode": "shadow",
    "agent_canary_enabled": False,
    "agent_canary_kill_switch": True,
    "agent_canary_percentage": 0,
    "agent_canary_approved": False,
    "agent_canary_runtime_version": "",
    "agent_canary_approved_at": None,
    "ai_reply_system_prompt": "仅基于已确认的会话事实和已启用知识库回复；不编造价格、库存、订单或承诺。只追问当前缺失的最少字段，不得要求买家重复已提供的信息；文字座位仅作偏好，不得声称正在核对其库存或逐座价格。",
    "ai_reply_shop_background": "",
    "ai_reply_precautions": "",
    "ai_reply_style": "",
    "ai_reply_memory_hours": 24,
    "ai_reply_memory_depth": 20,
    "ai_reply_delay_seconds": 3,
    "ai_reply_manual_takeover_seconds": 20,
    "low_confidence_threshold": 0.9,
    "reply_templates": DEFAULT_REPLY_TEMPLATES,
    "reply_template_images": DEFAULT_REPLY_TEMPLATE_IMAGES,
    "updated_at": None,
}

def _merged_runtime(value: Any) -> dict[str, Any]:
    stored = value if isinstance(value, dict) else {}
    runtime = {**DEFAULT_RUNTIME_SETTINGS, **stored}
    templates = stored.get("reply_templates")
    runtime["reply_templates"] = {
        **DEFAULT_REPLY_TEMPLATES,
        **(templates if isinstance(templates, dict) else {}),
    }
    legacy_circle_copies = {
        "manual_delivery_preference": "已记录您圈选的位置为人工出票偏好，请人工确认。",
        "quote_area": "※{城市标记}| {影院}\n电影：{影片}\n影厅：{影厅}\n场次：{日期} {场次}\n\n{单价}元/张，{张数}张合计{合计}元。\n图中标记仅作座位偏好，不代表对应座位可售；最终座位以出票时万达实时可用为准。",
        "quote_need_count": "※{城市标记}| {影院}\n电影：{影片}\n影厅：{影厅}\n场次：{日期} {场次}\n\n实时单价{单价}元/张，请告诉我需要几张。\n图中标记仅作座位偏好，不代表对应座位可售；最终座位以出票时万达实时可用为准。",
    }
    legacy_safety_copies = {
        "temporary_lock_release_unverified": {
            "临时试价座位未确认释放，已停止自动报价并转人工处理。",
            "临时试价座位的释放状态暂未确认，已停止自动报价；请勿付款，并稍后刷新选座页后重试。",
        },
    }
    for copies in (legacy_circle_copies, legacy_safety_copies):
        for key, legacy_copy in copies.items():
            matches_legacy = (
                runtime["reply_templates"].get(key) in legacy_copy
                if isinstance(legacy_copy, set)
                else runtime["reply_templates"].get(key) == legacy_copy
            )
            if matches_legacy:
                runtime["reply_templates"][key] = DEFAULT_REPLY_TEMPLATES[key]
    images = stored.get("reply_template_images")
    runtime["reply_template_images"] = {
        **DEFAULT_REPLY_TEMPLATE_IMAGES,
        **(images if isinstance(images, dict) else {}),
    }
    return runtime


DEFAULT_QUOTE_POLICY: dict[str, int] = {
    "wplus_adjustment_cents": -290,
    "wplus_member_price_threshold_cents": 6000,
    "regular_adjustment_cents": 100,
    "max_auto_order_amount_cents": 200000,
}


class PluginBridgeStore:
    """Persist only the plugin bridge's non-secret runtime and quote settings."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def runtime(self) -> dict[str, Any]:
        with self._lock:
            return _merged_runtime(self._read_unlocked().get("runtime", {}))

    def update_runtime(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            data = self._read_unlocked()
            current = _merged_runtime(data.get("runtime", {}))
            current.update(patch)
            current["updated_at"] = datetime.now(UTC).isoformat()
            data["runtime"] = current
            self._write_unlocked(data)
            return current.copy()

    def shop_enabled(self, account_unb: str) -> bool:
        """Return the plugin-owned switch for one authorized shop.

        Missing overrides intentionally mean enabled: adding this feature must
        not silently stop an already-running private plugin.
        """
        account = str(account_unb).strip()
        if not account:
            return True
        with self._lock:
            runtime = _merged_runtime(self._read_unlocked().get("runtime", {}))
            overrides = runtime.get("shop_automation_overrides", {})
            if not isinstance(overrides, dict):
                return True
            return overrides.get(account) is not False

    def update_shop_enabled(self, account_unb: str, enabled: bool) -> bool:
        account = str(account_unb).strip()
        if not account:
            raise ValueError("account_unb is required")
        with self._lock:
            data = self._read_unlocked()
            current = _merged_runtime(data.get("runtime", {}))
            overrides = current.get("shop_automation_overrides", {})
            if not isinstance(overrides, dict):
                overrides = {}
            overrides[account] = enabled
            current["shop_automation_overrides"] = overrides
            current["updated_at"] = datetime.now(UTC).isoformat()
            data["runtime"] = current
            self._write_unlocked(data)
            return enabled

    def quote_policy(self, tenant_id: str) -> dict[str, int]:
        with self._lock:
            policies = self._read_unlocked().get("quote_policies", {})
            policy = policies.get(tenant_id, {}) if isinstance(policies, dict) else {}
            return {**DEFAULT_QUOTE_POLICY, **policy}

    def update_quote_policy(self, tenant_id: str, patch: dict[str, int]) -> dict[str, int]:
        with self._lock:
            data = self._read_unlocked()
            policies = data.get("quote_policies")
            if not isinstance(policies, dict):
                policies = {}
            current = {**DEFAULT_QUOTE_POLICY, **policies.get(tenant_id, {}), **patch}
            policies[tenant_id] = current
            data["quote_policies"] = policies
            self._write_unlocked(data)
            return current.copy()

    def _read_unlocked(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        loaded = json.loads(self._path.read_text(encoding="utf-8-sig"))
        return loaded if isinstance(loaded, dict) else {}

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self._path.with_suffix(".tmp")
        temporary_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary_path, self._path)
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass
