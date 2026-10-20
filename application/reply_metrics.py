"""Content-free, per-turn reply diagnostics shared by live traces and replay."""
from copy import deepcopy

VERSION = "reply-metrics-v1"


def _state(tools):
    if not hasattr(tools, "reply_metrics"):
        tools.reply_metrics = {"version": VERSION, "candidate_count": 0,
                               "needs_repair_count": 0, "invalid_metadata_count": 0,
                               "missing_metadata_count": 0, "final_fallback_reason": ""}
    return tools.reply_metrics


def observe(tools, decision):
    """Count each model text candidate once, never the final boundary recheck."""
    if not getattr(tools, "semantic_mode", False):
        return
    state = _state(tools)
    metadata = getattr(tools, "reply_metadata", {})
    state["candidate_count"] += 1
    state["needs_repair_count"] += int(decision.reason == "NEEDS_REPAIR")
    state["invalid_metadata_count"] += int(bool(metadata.get("invalid_response_metadata")))
    state["missing_metadata_count"] += int(not metadata)


def fallback(tools, reason):
    if getattr(tools, "semantic_mode", False):
        _state(tools)["final_fallback_reason"] = reason


def snapshot(tools):
    """A recorded zero is distinct from an old trace without instrumentation."""
    return deepcopy(_state(tools))


def aggregate(rows):
    """Report candidate and turn denominators; never treat missing data as zero."""
    totals = dict(rows=0, observed_turns=0, unobserved_turns=0, candidate_count=0,
                  needs_repair_count=0, invalid_metadata_count=0, missing_metadata_count=0,
                  needs_repair_turns=0, invalid_metadata_turns=0, missing_metadata_turns=0,
                  final_fallback_turns=0)
    reasons = {}
    for row in rows:
        totals["rows"] += 1
        data = row.get("reply_metrics") or (row.get("last_ai_trace") or {}).get("reply_metrics")
        if not data or data.get("version") != VERSION:
            totals["unobserved_turns"] += 1
            continue
        totals["observed_turns"] += 1
        for key in ("candidate_count", "needs_repair_count", "invalid_metadata_count", "missing_metadata_count"):
            totals[key] += data.get(key, 0)
        for name in ("needs_repair", "invalid_metadata", "missing_metadata"):
            totals[name + "_turns"] += int(data.get(name + "_count", 0) > 0)
        reason = data.get("final_fallback_reason")
        if reason:
            totals["final_fallback_turns"] += 1
            reasons[reason] = reasons.get(reason, 0) + 1
    rates = {}
    for name in ("needs_repair", "invalid_metadata", "missing_metadata"):
        rates[name + "_candidate_rate"] = (totals[name + "_count"] / totals["candidate_count"]
                                             if totals["candidate_count"] else None)
        rates[name + "_turn_rate"] = (totals[name + "_turns"] / totals["observed_turns"]
                                        if totals["observed_turns"] else None)
    rates["final_fallback_turn_rate"] = (totals["final_fallback_turns"] / totals["observed_turns"]
                                           if totals["observed_turns"] else None)
    return {"version": VERSION, **totals, "rates": rates, "fallback_reasons": reasons}
