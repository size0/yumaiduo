"""Stable observations over existing guarded tools; never an execution permit."""
from copy import deepcopy


def normalize(name: str, result: dict, tools, *, operation_ids: tuple = ()) -> dict:
    result = deepcopy(result)
    code = str(result.get("status") or "")
    operations = list(operation_ids)
    if (code.endswith("UNKNOWN") or (operations and not result.get("ok"))
            or (code in ("RECOVERY_REQUIRED", "REQUESTED") and not operations)):
        outcome = "UNKNOWN"
    elif code in ("NEEDS_INFORMATION", "POLICY_NOT_FOUND", "REQUEST_FIELDS_STAGED", "REQUEST_PLAN_STAGED", "REQUEST_PLAN_REQUIRED"):
        outcome = "NEEDS_INFORMATION"
    elif code in ("BLOCKED", "STALE_REFERENCE", "INTENT_REQUIRED", "REFERENCE_REQUIRED", "ORDER_AMBIGUOUS"):
        outcome = "BLOCKED"
    elif name == "read_quote" and result.get("ok") and result.get("quote") is None:
        outcome = "NEEDS_INFORMATION"
        result.setdefault("missing_fields", ["quote"])
    elif not result.get("ok"):
        outcome = "FAILED"
    elif operations:
        outcome = "REQUESTED"
    else:
        outcome = "COMPLETED"
    result["tool"] = name
    result["outcome"] = outcome
    result.setdefault("status", outcome)
    result.setdefault("summary", result.get("guidance") or result.get("reason")
                      or ("工具查询已完成。" if outcome == "COMPLETED" else "工具尚未取得明确结果。"))
    result.setdefault("missing_fields", [])
    actions = result.get("next_actions") or []
    result["next_actions"] = list(actions) if isinstance(actions, (list, tuple)) else [str(actions)]
    if outcome == "UNKNOWN" and not result["next_actions"]:
        result["next_actions"] = ["先查询原操作结果；结果未确认时不得重复提交或声称成功。"]
    result["operation_id"] = operations[0] if operations else None
    result["operation_ids"] = operations
    result["operation_state"] = "CREATED_NOT_EXECUTED" if operations else None
    quote = result.get("quote")
    result["quote_kind"] = quote.get("quote_kind") if isinstance(quote, dict) else None
    result["tradable"] = bool(outcome == "COMPLETED" and quote.get("tradable")) if isinstance(quote, dict) else False
    result["facts"] = {key: deepcopy(result[key]) for key in
                       ("quote", "orders", "quote_reference", "platform_status", "platform_amount_yuan",
                        "demand_id", "demand_rev", "updated_fields") if key in result}
    return result
