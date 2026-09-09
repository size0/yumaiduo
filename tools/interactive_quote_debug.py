from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from uuid import uuid4
from pathlib import Path
from typing import Any, Mapping

# Make the selected checkout explicit without baking a worktree path into the
# module. ``--backend-path`` can point at another checkout for comparison.
DEFAULT_BACKEND = Path(__file__).resolve().parents[1] / "backend"
SELECTED_BACKEND = Path(os.environ.get("V4_DEBUG_BACKEND_PATH", DEFAULT_BACKEND)).resolve()
if str(SELECTED_BACKEND) not in sys.path:
    sys.path.insert(0, str(SELECTED_BACKEND))

from app.canonical_buyer_reply import CanonicalBuyerReplyRenderer  # noqa: E402
from app.canonical_conversation_agent import (  # noqa: E402
    AgentContextBuilder,
    CanonicalAgentToolBackend,
    CanonicalConversationAgent,
)
from app.cinema_route_v2.models import CinemaRouteResult  # noqa: E402
from app.pricing.models import PricingRulesSnapshot  # noqa: E402
from app.quote_record_store import QuoteRecordStore  # noqa: E402
from app.quote_v2.service import CanonicalQuoteRequest, CanonicalQuoteRuntime, QuoteV2Service  # noqa: E402
from app.reply_template_store import ReplyTemplateStore  # noqa: E402
from app.rule_state_coordinator import RuleStateCoordinator  # noqa: E402
from app.rules_first_state_store import SqliteTransactionStateStore  # noqa: E402
from app.rules_first_store import RulesFirstStore  # noqa: E402
from app.seat_facts_v2.models import SeatFactsResult  # noqa: E402
from app.show_resolve_v2.models import ShowResolutionResult  # noqa: E402
from app.wanda_cost_v2.models import WandaCostFacts, WandaCostItem  # noqa: E402
from app.wanda_pricing_v2.service import WandaPricingV2Service  # noqa: E402
from app.recognition_v2.models import RecognitionResult  # noqa: E402


TENANT = "107"
SHOP = "2313315754"
BUYER = "2464035965"
CHAT = "66050332180"
CONTEXT = "debug-purchase-context"


class FakeModel:
    """Only the model boundary is fake; all quote/state code remains real."""

    def __init__(self, ticket_count: int) -> None:
        self.ticket_count = ticket_count
        self.calls: list[dict[str, Any]] = []

    async def complete(self, messages: list[dict[str, Any]], tools: tuple[dict[str, Any], ...]) -> Mapping[str, Any]:
        self.calls.append({"message_count": len(messages), "tool_count": len(tools)})
        if len(self.calls) == 1:
            return {
                "tool_calls": [{
                    "id": "debug-call-1",
                    "type": "function",
                    "function": {
                        "name": "update_purchase_request",
                        "arguments": json.dumps({"ticket_count": self.ticket_count}),
                    },
                }],
            }
        return {"reply": f"已记录{self.ticket_count}张。"}


class FakeRecognition:
    async def recognize(self, _image_url: str, **_kwargs: Any) -> RecognitionResult:
        return RecognitionResult(
            city_text="惠州",
            cinema_text="万达影城（港惠激光IMAX店）",
            movie="杀死比尔：血色全传",
            show_date="2026-09-12",
            start_time="16:10",
            hall="激光IMAX厅",
            selected_seats=[],
            has_selected_seats=False,
            has_manual_mark=None,
            confidence=0.99,
        )


class FakeCinemaRoute:
    async def resolve(self, _recognition: RecognitionResult) -> CinemaRouteResult:
        return CinemaRouteResult(
            route="WANDA_SELF", wanda_city_id="惠州", wanda_city_name="惠州",
            wanda_store_id="wanda-store-港惠", wanda_cinema_name="万达影城（港惠激光IMAX店）",
            wanda_cinema_address=None, resolution_reason="fixture_verified",
            verification_status="VERIFIED", verification_level="FIXTURE",
        )


class FakeShowResolver:
    async def resolve(self, _request: Mapping[str, Any]) -> ShowResolutionResult:
        return ShowResolutionResult(
            status="RESOLVED", wanda_store_id="wanda-store-港惠", wanda_show_id="show-20260912-1610",
            wanda_film_id="film-kill-bill", movie_name="杀死比尔：血色全传",
            show_date="2026-09-12", start_time="16:10", hall_name="激光IMAX厅",
            # 4,490 fen provider cost + the real pricing rule (official
            # original 6,410 fen, -290 adjustment) yields 6,120 fen.
            sales_price_fen=6410,
        )


class FakeSeatFacts:
    async def resolve(self, _request: Mapping[str, Any], **_kwargs: Any) -> SeatFactsResult:
        return SeatFactsResult(
            status="WPLUS_AREA_RESOLVED", seat_request_type="WPLUS_AREA",
            wanda_store_id="wanda-store-港惠", wanda_show_id="show-20260912-1610",
            has_manual_mark=False, has_selected_seats=False,
        )


class FakeCostResolution:
    def resolve(self, _show: ShowResolutionResult, _seats: SeatFactsResult) -> WandaCostFacts:
        return WandaCostFacts(
            status="COST_READY", request_type="WPLUS_AREA",
            cost_items=[WandaCostItem(
                zone_type="W+", cost_fen=4490, cost_source="SHOWTIME_WPLUS",
            )],
        )


class PlainProtector:
    def protect(self, value: str) -> str:
        return value

    def unprotect(self, value: str) -> str:
        return value


@dataclass
class DebugHarness:
    root: Path
    runtime: CanonicalQuoteRuntime
    agent: CanonicalConversationAgent
    outbox: RulesFirstStore
    rules_runtime: Any
    renderer: CanonicalBuyerReplyRenderer
    fake_model: FakeModel

    async def close(self) -> None:
        await self.runtime.aclose()


def build_harness(ticket_count: int = 2, root: Path | None = None) -> DebugHarness:
    directory = root or Path(tempfile.mkdtemp(prefix="wanda-v4-quote-debug-"))
    directory.mkdir(parents=True, exist_ok=True)
    quote_store = QuoteRecordStore(directory / "quote-records.json", protector=PlainProtector())
    quote_service = QuoteV2Service(quote_store, ttl_seconds=1800)
    renderer = CanonicalBuyerReplyRenderer(
        ReplyTemplateStore(directory / "reply-templates.json").current,
    )
    runtime = CanonicalQuoteRuntime(
        recognition_service=FakeRecognition(),
        cinema_route_service=FakeCinemaRoute(),
        show_resolve_service=FakeShowResolver(),
        seat_facts_service=FakeSeatFacts(),
        cost_resolution_service=FakeCostResolution(),
        wanda_pricing_service=WandaPricingV2Service(),
        selected_seat_quote_service=None,
        pricing_rules_provider=lambda: PricingRulesSnapshot(
            enabled=True, revision=12, rule_version="pricing-r12",
        ),
        quote_service=quote_service,
        liangpiao_facts_adapter=None,
        reply_renderer=renderer,
    )
    outbox = RulesFirstStore(directory / "rules.sqlite3", protector=PlainProtector())
    states = SqliteTransactionStateStore(directory / "rules.sqlite3", protector=PlainProtector())
    coordinator = RuleStateCoordinator(states, quote_store=quote_store)
    rules_runtime = _RulesRuntimeForDebug(outbox, coordinator, states)
    fake_model = FakeModel(ticket_count)
    agent = CanonicalConversationAgent(
        AgentContextBuilder(quote_store=quote_store, transaction_store=states),
        fake_model,
        tool_backend=CanonicalAgentToolBackend(
            quote_runtime=runtime, quote_store=quote_store, transaction_store=states,
        ),
    )
    return DebugHarness(directory, runtime, agent, outbox, rules_runtime, renderer, fake_model)


class _RulesRuntimeForDebug:
    """Use the production RulesFirstRuntime with a no-op event engine."""

    def __init__(self, outbox: RulesFirstStore, coordinator: RuleStateCoordinator, states: Any) -> None:
        from app.rules_first_runtime import RulesFirstRuntime

        self._runtime = RulesFirstRuntime(outbox, _NoopEngine(), coordinator, states)

    def accept_canonical_result(self, body: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
        return self._runtime.accept_canonical_result(body, result)

    def claim_commands(self) -> list[dict[str, Any]]:
        return self._runtime.claim_commands()


class _NoopEngine:
    async def process_event(self, _body: Mapping[str, Any]) -> dict[str, object]:
        return {"decision": {"mode": "debug", "actions": []}}

    def process_action_result(self, _body: Mapping[str, Any]) -> dict[str, object]:
        return {"actions": []}


def _event(event_id: str, *, text: str | None = None, image: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "accountUnb": SHOP, "peerUnb": BUYER, "chatId": CHAT,
        "itemId": CONTEXT, "messageType": 2 if image else 1,
    }
    if image:
        payload["imageUrls"] = ["fixture://buyer-image"]
    else:
        payload["content"] = text or ""
        payload["remoteMessageId"] = f"message-{event_id}"
    return {
        "envelope": {
            "id": event_id, "tenantId": TENANT, "event": "im.message.received",
            "payload": payload,
        },
        "session": {"accountUnb": SHOP, "peerUnb": BUYER, "chatId": CHAT},
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _show(label: str, value: Any) -> None:
    print(f"\n── {label} ──")
    print(_json(value))


async def run_demo(*, ticket_count: int = 2, verbose: bool = True) -> dict[str, Any]:
    harness = build_harness(ticket_count=ticket_count)
    try:
        started = time.perf_counter()
        image_body = _event("debug-image", image=True)
        image_result = await harness.runtime.process_image_event(image_body)
        image_reply = harness.rules_runtime.accept_canonical_result(image_body, image_result)
        image_commands = harness.rules_runtime.claim_commands()
        image_elapsed = round((time.perf_counter() - started) * 1000, 1)

        text_body = _event("debug-count", text=f"{ticket_count}张")
        started = time.perf_counter()
        agent_result = await harness.agent.process(text_body)
        quote_result = next(
            (
                item.get("result") for item in reversed(agent_result.get("tool_trace", []))
                if isinstance(item, Mapping) and isinstance(item.get("result"), Mapping)
                and item["result"].get("quote")
            ),
            None,
        )
        if not isinstance(quote_result, Mapping):
            raise RuntimeError("debugger_did_not_receive_quote_from_real_tool")
        rendered = harness.renderer.render(quote_result)
        durable_result = harness.rules_runtime.accept_canonical_result(text_body, {
            **dict(quote_result),
            "current_runtime_reply": rendered["text"],
            "current_runtime_replies": rendered.get("messages", []),
            "canonical_reply_kind": rendered["kind"],
        })
        text_commands = harness.rules_runtime.claim_commands()
        text_elapsed = round((time.perf_counter() - started) * 1000, 1)
        result = {
            "image": {"input": image_body, "quote_result": image_result, "durable": image_reply,
                      "commands": image_commands, "elapsed_ms": image_elapsed,
                      "end_reason": "quote_persisted_and_outbox_committed"},
            "continuation": {"input": text_body, "model_calls": harness.fake_model.calls,
                             "agent_result": agent_result, "quote_result": quote_result,
                             "rendered": rendered, "durable": durable_result,
                             "commands": text_commands, "elapsed_ms": text_elapsed,
                             "end_reason": "followup_quote_persisted_and_outbox_committed"},
        }
        if verbose:
            _show("本轮用户输入（图片）", {"event_id": "debug-image", "image_url": "fixture://buyer-image"})
            _show("图片识别→Quote→回复计划", {"quote": image_result.get("quote"), "reply": {
                "kind": image_result.get("canonical_reply_kind"),
                "messages": image_result.get("current_runtime_replies"),
            }})
            _show("图片 RulesFirst Outbox", image_commands)
            _show("本轮用户输入（文字）", f"{ticket_count}张")
            _show("模型提出的工具调用 / 原始参数 / 校验后的参数 / 工具返回", agent_result.get("tool_trace"))
            _show("后续模型回复", {"reply": agent_result.get("reply"), "model_calls": harness.fake_model.calls})
            _show("最终回复 / Outbox / 耗时 / 结束原因", {
                "messages": rendered.get("messages"), "commands": text_commands,
                "elapsed_ms": text_elapsed, "end_reason": result["continuation"]["end_reason"],
            })
        return result
    finally:
        await harness.close()


async def interactive_session() -> None:
    harness = build_harness()
    try:
        image_body = _event("interactive-image", image=True)
        image_result = await harness.runtime.process_image_event(image_body)
        image_durable = harness.rules_runtime.accept_canonical_result(image_body, image_result)
        _show("图片识别→Quote→两段回复", {
            "quote": image_result.get("quote"),
            "messages": image_result.get("current_runtime_replies"),
            "outbox_commands": image_durable.get("commands"),
        })
        print("\\n输入买家后续文字（例如 2张），输入 exit 退出。")
        while True:
            text = input("\\n买家> ").strip()
            if text.lower() in {"exit", "quit", "q"}:
                return
            match = re.search(r"(?<!\\d)([1-9]|1\\d|20)\\s*张", text)
            if not match:
                print("调试器当前只演示数量接续，请输入类似“2张”。")
                continue
            harness.fake_model.ticket_count = int(match.group(1))
            harness.fake_model.calls.clear()
            body = _event(f"interactive-text-{uuid4().hex[:8]}", text=text)
            started = time.perf_counter()
            agent_result = await harness.agent.process(body)
            quote_result = next(
                (item.get("result") for item in reversed(agent_result.get("tool_trace", []))
                 if isinstance(item, Mapping) and isinstance(item.get("result"), Mapping)
                 and item["result"].get("quote")), None,
            )
            if not isinstance(quote_result, Mapping):
                _show("Agent 失败 Trace", agent_result)
                continue
            rendered = harness.renderer.render(quote_result)
            durable = harness.rules_runtime.accept_canonical_result(body, {
                **dict(quote_result), "current_runtime_reply": rendered["text"],
                "current_runtime_replies": rendered.get("messages", []),
                "canonical_reply_kind": rendered["kind"],
            })
            _show("本轮用户输入", text)
            _show("模型工具调用 / 原始参数 / 校验参数 / 工具返回", agent_result.get("tool_trace"))
            _show("后续模型回复", agent_result.get("reply"))
            _show("最终回复 / Outbox / 耗时 / 结束原因", {
                "messages": rendered.get("messages"), "commands": durable.get("commands"),
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                "end_reason": "followup_quote_persisted_and_outbox_committed",
            })
    finally:
        await harness.close()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="真实 V4 报价链交互调试器（只 fake 外部边界）")
    parser.add_argument("--backend-path", type=Path, default=DEFAULT_BACKEND,
                        help="选择要运行的 backend checkout")
    parser.add_argument("--count", type=int, default=2, choices=range(1, 21))
    parser.add_argument("--json", action="store_true", help="只输出 JSON 结果")
    args = parser.parse_args()
    selected = args.backend_path.resolve()
    if selected != SELECTED_BACKEND:
        parser.error("--backend-path must be supplied through tools/cli.py so imports are selected before startup")
    if args.json:
        result = asyncio.run(run_demo(ticket_count=args.count, verbose=False))
        print(_json(result))
    else:
        asyncio.run(interactive_session())


if __name__ == "__main__":
    main()
