"""Read the existing V5 VIP policy; no shadow setting or default discount."""
from dataclasses import dataclass
from pathlib import Path
import json,os,sqlite3
from domain.quote_rules import QuoteResult


@dataclass(frozen=True)
class VipRule:
    discount_bps:int
    revision:int


def load_rule(db_path=None):
    path=Path(db_path or os.getenv('TICKET_AGENT_V5_SETTINGS_DB','/var/lib/ticket-system-v5/business-settings.sqlite3'))
    try:
        if not path.is_file():raise ValueError('missing settings')
        with sqlite3.connect('file:'+path.as_posix()+'?mode=ro',uri=True,timeout=2) as db:
            row=db.execute('SELECT value,revision FROM business_settings WHERE key=?',('vip_discount_bps',)).fetchone()
        value=json.loads(row[0]) if row else None
        if type(value) is not int or not 1<=value<=10000 or type(row[1]) is not int:raise ValueError('invalid VIP setting')
        return VipRule(value,row[1])
    except (OSError,sqlite3.Error,ValueError,TypeError) as exc:
        raise ValueError('VIP_PRICING_UNAVAILABLE') from exc


def quote_vip(cost,rule):
    if cost.source!='WANDA_VIP_FIXED_COST' or cost.unit_cost_fen!=5500 or not cost.original_unit_fen:
        raise ValueError('VIP_PRICE_FACTS_INVALID')
    unit=max(5500,(cost.original_unit_fen*rule.discount_bps+5000)//10000)
    return QuoteResult(unit_price_fen=unit,total_price_fen=unit*cost.seat_count,
        price_mode='FIXED',max_price_fen=None,rule_name='万达VIP厅报价',quote_mode='VIP_DISCOUNT',
        adjust='ROUND_HALF_UP',cost_basis='WANDA_VIP_FIXED_COST',cost_unit_fen=5500,
        cost_total_fen=cost.total_cost_fen,seat_count=cost.seat_count,
        original_total_fen=cost.original_total_fen,margin_unit_fen=unit-5500,
        margin_total_fen=(unit-5500)*cost.seat_count,pricing_rule_id='wanda_vip',
        pricing_rule_revision=f'v5-settings-{rule.revision}',
        notes=[f'V5 VIP独立规则：固定预估成本55元/张，原价×{rule.discount_bps}/10000，不低于55元；不是W+活动价'])
