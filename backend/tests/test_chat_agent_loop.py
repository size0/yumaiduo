from __future__ import annotations

import json
import re

import httpx
import pytest

from app.chat_service import CustomerServiceChatService
from app.config import Settings
from app.models import MovieImageInfo
from app.rule_contracts import AiAssistResult, AgentTurnPlan


def test_runtime_context_exposes_multi_image_and_pending_resolution_facts_to_agent() -> None:
    prompt = CustomerServiceChatService._runtime_context_prompt({
        "pending_image_url": "https://img.alicdn.com/one.png",
        "pending_image_urls": [
            "https://img.alicdn.com/one.png", "https://img.alicdn.com/two.png",
        ],
        "pending_cinema_candidates": [
            {"cinema_id": 1001, "name": "南山万达影城"},
            {"cinema_id": 1002, "name": "宝安万达影城"},
        ],
        "pending_image_conflicts": ["showtime_start"],
        "pending_image_missing_fields": ["showtime_start"],
        "pending_show_candidates": [
            {"show_id": "show-1", "showtime_start": "19:30"},
            {"show_id": "show-2", "showtime_start": "20:30"},
        ],
        "pending_recognition_targets": [{
            "target_id": "image-target-2",
            "snapshot_id": "rs-2",
            "snapshot_revision": 3,
            "candidate_shows": [{"show_id": "show-2", "start_time": "20:30"}],
        }],
        "pending_image_recognition": {"movie_name": "测试电影", "showtime_start": None},
        "image_quote_targets": [{
            "target_id": "image-target-1",
            "recognition": {"movie_name": "测试电影", "selected_seats": [{"seat_number": "5排6座"}]},
            "quote": {"total_quote_cents": 4900, "ticket_count": 1},
        }],
    })

    assert '\"pending_image_urls\":[\"https://img.alicdn.com/one.png\",\"https://img.alicdn.com/two.png\"]' in prompt
    assert '\"pending_image_conflicts\":[\"showtime_start\"]' in prompt
    assert '\"pending_image_missing_fields\":[\"showtime_start\"]' in prompt
    assert '\"pending_show_candidates\":[{\"show_id\":\"show-1\"' in prompt
    assert '\"pending_recognition_targets\":[{\"target_id\":\"image-target-2\"' in prompt
    assert '\"pending_cinema_candidates\":[{\"cinema_id\":1001' in prompt
    assert '\"pending_image_recognition\":{\"movie_name\":\"测试电影\"' in prompt
    assert '\"image_quote_targets\":[{\"target_id\":\"image-target-1\"' in prompt
    assert "已有权威预报价时直接按目标逐项回复" in prompt
    assert "不得再次询问座位、张数或要求重复确认" in prompt
    assert "必须一次传给 recognize_screenshot" in prompt
    assert "调用 resolve_image_conflict" in prompt
    assert "【这一轮的实时上下文】" in prompt
    assert "阶段、订单状态、报价和待补字段不能根据话术或历史推测" in prompt
    assert "銆" not in prompt
    assert "鍥" not in prompt


@pytest.mark.asyncio
async def test_explicit_buyer_seat_overrides_screenshot_seat_for_quote_tool() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(200, json={
                "choices": [{"message": {
                    "role": "assistant", "content": None,
                    "tool_calls": [{
                        "id": "quote-1", "type": "function",
                        "function": {
                            "name": "quote.preflight_current",
                            "arguments": json.dumps({
                                "selected_seats": [{"seat_number": "10排15座"}],
                            }, ensure_ascii=False),
                        },
                    }],
                }}],
            })
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "已完成核验。"}}],
        })

    calls: list[tuple[str, dict[str, object]]] = []

    async def execute(name: str, arguments: dict[str, object]) -> dict[str, object]:
        calls.append((name, arguments))
        return {"ok": True, "quote": {
            "seat_quotes": [{"seat_number": "9排15座"}],
            "unit_quote_cents": 7040,
        }}

    settings = Settings(chat_api_key="key", chat_base_url="https://example.test/v1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = CustomerServiceChatService(
            settings, client=client, tool_executor=execute,
            tool_schemas=[{"type": "function", "function": {
                "name": "quote.preflight_current", "parameters": {"type": "object"},
            }}],
        )
        recognition = MovieImageInfo.model_validate({
            "cinema_name": "合肥天鹅湖万达广场店", "movie_name": "奥德赛",
            "showtime_start": "13:05", "selected_seats": [{"seat_number": "10排15座"}],
            "selected_count_visible": 1,
        })
        service.remember_image_context("conversation-seat-override", recognition, None, None)
        reply = await service.reply(
            "9排15多少钱", "conversation-seat-override",
            runtime_context={"_agent_tool_executor": execute},
        )

    assert reply == "已完成核验。"
    assert calls[0][0] == "quote.preflight_current"
    assert calls[0][1]["selected_seats"] == [{"seat_number": "9排15座"}]


@pytest.mark.asyncio
async def test_relative_buyer_seat_request_uses_latest_reference_row_for_seat_list() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(200, json={
                "choices": [{"message": {
                    "role": "assistant", "content": None,
                    "tool_calls": [{
                        "id": "seat-list-1", "type": "function",
                        "function": {"name": "seat.list", "arguments": "{}"},
                    }],
                }}],
            })
        if len(requests) == 2:
            return httpx.Response(200, json={
                "choices": [{"message": {
                    "role": "assistant", "content": None,
                    "tool_calls": [{
                        "id": "quote-1", "type": "function",
                        "function": {
                            "name": "quote.preflight_current",
                            "arguments": '{"selected_seats":[{"seat_number":"9排15座"}]}',
                        },
                    }],
                }}],
            })
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "已完成对应位置核价。"}}],
        })

    calls: list[tuple[str, dict[str, object]]] = []

    async def execute(name: str, arguments: dict[str, object]) -> dict[str, object]:
        calls.append((name, arguments))
        if name == "seat.list":
            return {"ok": True, "available_seats": [{"seat_number": "9排15座"}]}
        return {"ok": True, "quote": {
            "seat_quotes": [{"seat_number": "9排15座"}],
            "unit_quote_cents": 7040,
        }}

    settings = Settings(chat_api_key="key", chat_base_url="https://example.test/v1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = CustomerServiceChatService(
            settings, client=client, tool_executor=execute,
            tool_schemas=[{"type": "function", "function": {
                "name": "seat.list", "parameters": {"type": "object"},
            }}],
        )
        recognition = MovieImageInfo.model_validate({
            "cinema_name": "合肥天鹅湖万达广场店", "movie_name": "奥德赛",
            "showtime_start": "13:05", "selected_seats": [{"seat_number": "10排15座"}],
            "selected_count_visible": 1,
        })
        service.remember_image_context("conversation-relative-seat", recognition, None, None)
        reply = await service.reply("前面一排多少钱", "conversation-relative-seat")

    assert reply == "已完成对应位置核价。"
    assert calls == [
        ("seat.list", {"row_no": 9}),
        ("quote.preflight_current", {"selected_seats": [{"seat_number": "9排15座"}]}),
    ]


@pytest.mark.asyncio
async def test_agent_tool_safety_observer_failure_stops_the_loop() -> None:
    requests = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={
            "choices": [{"message": {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "call-1", "type": "function",
                    "function": {"name": "show.list", "arguments": "{}"},
                }],
            }}],
        })

    async def executor(_name: str, _arguments: dict[str, object]) -> dict[str, object]:
        return {"ok": True, "shows": [{"show_id": "show-1"}, {"show_id": "show-2"}]}

    def observer(*_args) -> None:
        raise OSError("durable candidate store unavailable")

    settings = Settings(chat_api_key="key", chat_base_url="https://example.test/v1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = CustomerServiceChatService(
            settings, client=client, tool_executor=executor,
            tool_schemas=[{"type": "function", "function": {"name": "show.list"}}],
        )
        reply = await service.reply(
            "查场次", "conversation-1",
            runtime_context={"_agent_tool_result_observer": observer},
        )

    assert requests == 1
    assert reply == "系统暂时没有完成核验，请稍后重试。"
    assert "人工" not in reply


@pytest.mark.asyncio
async def test_agent_tool_loop_executes_query_then_returns_final_reply() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(200, json={
                "choices": [{"message": {
                    "role": "assistant", "content": None,
                    "tool_calls": [{
                        "id": "call-1", "type": "function",
                        "function": {"name": "show.list", "arguments": '{"cinema_id":"c1"}'},
                    }],
                }}],
            })
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "已查到场次，请选择后我再报价。"}}],
        })

    calls: list[tuple[str, dict[str, object]]] = []

    async def execute(name: str, arguments: dict[str, object]) -> dict[str, object]:
        calls.append((name, arguments))
        return {"ok": True, "shows": [{"show_id": "s1"}]}

    settings = Settings(chat_api_key="key", chat_base_url="https://example.test/v1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = CustomerServiceChatService(
            settings,
            client=client,
            tool_executor=execute,
            tool_schemas=[{"type": "function", "function": {"name": "show.list"}}],
        )
        reply = await service.reply("今晚有什么场次？", "conversation-tool")

    assert reply == "已查到场次，请选择后我再报价。"
    assert calls == [("show.list", {"cinema_id": "c1"})]
    assert requests[0]["tool_choice"] == "auto"
    assert requests[0]["tools"][0]["function"]["name"] == "show_list"
    assert requests[1]["messages"][-1]["role"] == "tool"
    assert requests[1]["messages"][-1]["name"] == "show_list"


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_agent_tool_call_is_sent_to_the_audit_recorder() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if not any(item.get("role") == "tool" for item in payload["messages"]):
            content = json.dumps({
                "action": "tool_call", "tool": "get_order_state",
                "arguments": {"order_id": "order-1"},
            })
        else:
            content = json.dumps({"action": "reply", "message": "已核对订单。"})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})

    audits: list[dict[str, object]] = []
    async def execute(_: str, __: dict[str, object]) -> dict[str, object]:
        return {"ok": True, "order_status": "paid"}

    settings = Settings(chat_api_key="key", chat_base_url="https://example.test/v1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = CustomerServiceChatService(
            settings, client=client, tool_executor=execute,
            tool_call_recorder=lambda value: audits.append(dict(value)),
        )
        reply = await service.reply(
            "订单状态？", "conversation-audit",
            runtime_context={
                "tenant_id": "tenant-a", "shop_id": "shop-a", "buyer_id": "buyer-a",
                "chat_id": "chat-a", "event_id": "event-a",
                "_agent_tool_executor": execute,
            },
        )

    assert reply == "已核对订单。"
    assert audits[0]["call_id"] == "json-tool-0"
    assert audits[0]["tenant_id"] == "tenant-a"
    assert audits[0]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_json_agent_action_tool_call_is_executed_and_reprompted() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            content = json.dumps({
                "action": "tool_call", "tool": "get_order_state",
                "arguments": {"order_id": "order-1"}, "references": [],
            }, ensure_ascii=False)
        else:
            content = json.dumps({
                "action": "reply", "message": "订单目前还是待付款。", "references": ["order-1"],
            }, ensure_ascii=False)
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": content,
        }}]})

    calls: list[tuple[str, dict[str, object]]] = []

    async def execute(name: str, arguments: dict[str, object]) -> dict[str, object]:
        calls.append((name, arguments))
        return {"ok": True, "order_status": "pending_payment", "order_id": "order-1"}

    settings = Settings(chat_api_key="key", chat_base_url="https://example.test/v1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = CustomerServiceChatService(settings, client=client, tool_executor=execute)
        reply = await service.reply(
            "订单现在什么状态？", "conversation-json-tool",
            runtime_context={"_agent_tool_executor": execute},
        )

    assert reply == "订单目前还是待付款。"
    assert calls == [("get_order_state", {"order_id": "order-1"})]
    assert requests[0]["response_format"] == {"type": "json_object"}
    assert requests[1]["messages"][-2]["tool_calls"][0]["id"] == "json-tool-0"
    assert requests[1]["messages"][-2]["tool_calls"][0]["function"]["name"] == "get_order_state"
    assert requests[1]["messages"][-1]["role"] == "tool"
    assert requests[1]["messages"][-1]["tool_call_id"] == "json-tool-0"


@pytest.mark.asyncio
async def test_local_strict_openai_contract_accepts_json_tool_round_trip() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if any(
            re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", item["function"]["name"]) is None
            for item in payload.get("tools", [])
        ):
            return httpx.Response(400, json={"error": {"message": "invalid tool name"}})
        tool_messages = [item for item in payload["messages"] if item.get("role") == "tool"]
        if tool_messages:
            preceding = payload["messages"][-2]
            expected_ids = {
                item["id"] for item in preceding.get("tool_calls", [])
                if isinstance(item, dict) and item.get("id")
            }
            if not expected_ids or any(item.get("tool_call_id") not in expected_ids for item in tool_messages):
                return httpx.Response(400, json={"error": {"message": "tool message without tool_calls"}})
            content = json.dumps({"action": "reply", "message": "已完成本地协议核验。"})
        else:
            content = json.dumps({
                "action": "tool_call", "tool": "show.list", "arguments": {"cinema_id": "c1"},
            })
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": content,
        }}]})

    async def execute(name: str, _arguments: dict[str, object]) -> dict[str, object]:
        assert name == "show.list"
        return {"ok": True, "shows": [{"show_id": "s1"}]}

    settings = Settings(chat_api_key="key", chat_base_url="https://example.test/v1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = CustomerServiceChatService(
            settings, client=client, tool_executor=execute,
            tool_schemas=[{"type": "function", "function": {
                "name": "show.list", "parameters": {"type": "object"},
            }}],
        )
        reply = await service.reply("查询场次", "conversation-strict-provider")

    assert reply == "已完成本地协议核验。"
    assert len(requests) == 2
    assert requests[0]["tools"][0]["function"]["name"] == "show_list"


@pytest.mark.asyncio
async def test_request_scoped_tool_executor_has_priority_over_global_executor() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if sum(item.get("role") == "tool" for item in payload["messages"]) == 0:
            content = json.dumps({"action": "tool_call", "tool": "get_quote", "arguments": {"movie_name": "奥德赛"}})
        else:
            content = json.dumps({"action": "reply", "message": "已取得权威报价。"})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})

    async def global_executor(_: str, __: dict[str, object]) -> dict[str, object]:
        raise AssertionError("request executor should handle this tool")

    async def request_executor(name: str, arguments: dict[str, object]) -> dict[str, object]:
        assert name == "get_quote"
        assert arguments == {"movie_name": "奥德赛"}
        return {"ok": True, "quote_id": "quote-1", "unit_quote_cents": 5_000}

    settings = Settings(chat_api_key="key", chat_base_url="https://example.test/v1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = CustomerServiceChatService(settings, client=client, tool_executor=global_executor)
        reply = await service.reply(
            "多少钱？", "conversation-request-tool",
            runtime_context={"_agent_tool_executor": request_executor},
        )

    assert reply == "已取得权威报价。"


@pytest.mark.asyncio
async def test_request_scoped_tool_rejection_never_falls_back_to_global_executor() -> None:
    requests: list[dict[str, object]] = []
    global_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        tool_messages = [item for item in payload["messages"] if item.get("role") == "tool"]
        if not tool_messages:
            content = json.dumps({
                "action": "tool_call", "tool": "order.detail",
                "arguments": {"order_id": "other-order"},
            })
        else:
            tool_result = json.loads(tool_messages[-1]["content"])
            content = json.dumps({
                "action": "reply",
                "message": "作用域拒绝已保留" if tool_result.get("error") == "tool_not_allowed" else "发生了全局回退",
            })
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})

    async def global_executor(_name: str, _arguments: dict[str, object]) -> dict[str, object]:
        nonlocal global_calls
        global_calls += 1
        return {"ok": True, "order": {"order_id": "other-order"}}

    async def request_executor(_name: str, _arguments: dict[str, object]) -> dict[str, object]:
        return {"ok": False, "error": "tool_not_allowed"}

    settings = Settings(chat_api_key="key", chat_base_url="https://example.test/v1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = CustomerServiceChatService(settings, client=client, tool_executor=global_executor)
        reply = await service.reply(
            "查另一个订单", "conversation-request-rejection",
            runtime_context={"_agent_tool_executor": request_executor},
        )

    assert reply == "作用域拒绝已保留"
    assert global_calls == 0
    assert json.loads(requests[-1]["messages"][-1]["content"])["error"] == "tool_not_allowed"


@pytest.mark.asyncio
async def test_request_scoped_current_order_query_remains_available() -> None:
    requests: list[dict[str, object]] = []
    scoped_calls: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if not any(item.get("role") == "tool" for item in payload["messages"]):
            content = json.dumps({
                "action": "tool_call", "tool": "order.detail",
                "arguments": {"orderNo": "current-provider-order"},
            })
        else:
            content = json.dumps({"action": "reply", "message": "当前订单仍在出票。"})
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": content}}],
        })

    async def global_executor(_name: str, _arguments: dict[str, object]) -> dict[str, object]:
        raise AssertionError("scoped current-order query must not use the global executor")

    async def request_executor(name: str, arguments: dict[str, object]) -> dict[str, object]:
        scoped_calls.append((name, dict(arguments)))
        return {"ok": True, "status": "ticketing"}

    settings = Settings(chat_api_key="key", chat_base_url="https://example.test/v1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = CustomerServiceChatService(
            settings, client=client, tool_executor=global_executor,
            tool_schemas=[{
                "type": "function",
                "function": {
                    "name": "order.detail",
                    "parameters": {
                        "type": "object", "additionalProperties": False,
                        "properties": {"orderNo": {"type": "string"}},
                        "required": ["orderNo"],
                    },
                },
            }],
        )
        reply = await service.reply(
            "查当前订单", "conversation-current-order",
            runtime_context={"_agent_tool_executor": request_executor},
        )

    assert reply == "当前订单仍在出票。"
    assert scoped_calls == [("order.detail", {"orderNo": "current-provider-order"})]
    assert requests[0]["tools"][0]["function"]["name"] == "order_detail"
    assert requests[0]["tools"][0]["function"]["parameters"]["additionalProperties"] is False


@pytest.mark.asyncio
async def test_recoverable_tool_failure_asks_for_missing_choice_instead_of_handoff() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if not any(item.get("role") == "tool" for item in payload["messages"]):
            content = json.dumps({"action": "tool_call", "tool": "show.list", "arguments": {}})
        else:
            content = json.dumps({"action": "handoff", "message": ""})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})

    async def execute(_name: str, _arguments: dict[str, object]) -> dict[str, object]:
        return {"ok": False, "error": "showtime_choice_required"}

    settings = Settings(chat_api_key="key", chat_base_url="https://example.test/v1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = CustomerServiceChatService(settings, client=client, tool_executor=execute)
        reply = await service.reply("我要晚上的", "conversation-missing-show")

    tool_result = json.loads(requests[-1]["messages"][-1]["content"])
    assert tool_result["recovery"]["next_action"] == "ask_buyer"
    assert "具体场次" in reply
    assert "人工" not in reply


@pytest.mark.asyncio
async def test_transient_read_failure_exposes_one_safe_retry_then_recovers() -> None:
    requests: list[dict[str, object]] = []
    executions = 0

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        tool_messages = [item for item in payload["messages"] if item.get("role") == "tool"]
        if not tool_messages:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call-1", "type": "function",
                "function": {"name": "show.list", "arguments": "{}"},
            }]}
        elif len(tool_messages) == 1:
            first_result = json.loads(tool_messages[0]["content"])
            assert first_result["recovery"]["next_action"] == "retry_tool"
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call-2", "type": "function",
                "function": {"name": "show.list", "arguments": "{}"},
            }]}
        else:
            message = {"role": "assistant", "content": json.dumps({
                "action": "reply", "message": "已重新查询到场次。",
            })}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    async def execute(_name: str, _arguments: dict[str, object]) -> dict[str, object]:
        nonlocal executions
        executions += 1
        if executions == 1:
            return {"ok": False, "error": "provider_read_failed"}
        return {"ok": True, "shows": [{"show_id": "show-1"}]}

    settings = Settings(chat_api_key="key", chat_base_url="https://example.test/v1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = CustomerServiceChatService(settings, client=client, tool_executor=execute)
        reply = await service.reply("查今晚场次", "conversation-read-retry")

    assert reply == "已重新查询到场次。"
    assert executions == 2


@pytest.mark.asyncio
async def test_invalid_json_agent_action_is_not_sent_to_buyer() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": '{"action":"tool_call","tool":"change_order_price","arguments":[]}',
        }}]})

    settings = Settings(chat_api_key="key", chat_base_url="https://example.test/v1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = CustomerServiceChatService(settings, client=client)
        reply = await service.reply("请改价", "conversation-invalid-json")

    assert "系统" in reply
    assert "tool_call" not in reply
    assert "人工" not in reply


@pytest.mark.asyncio
async def test_agent_tool_failure_fails_closed_without_model_claim() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "call-1", "type": "function", "function": {
                "name": "show.list", "arguments": "{}",
            }}],
        }}]})

    async def execute(_: str, __: dict[str, object]) -> dict[str, object]:
        return {"ok": False, "error": "provider_read_failed"}

    settings = Settings(chat_api_key="key", chat_base_url="https://example.test/v1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = CustomerServiceChatService(
            settings, client=client, tool_executor=execute,
            tool_schemas=[{"type": "function", "function": {"name": "show.list"}}],
        )
        reply = await service.reply("今晚有什么场次？", "conversation-tool-failure")

    assert calls == 2
    assert "连续两次" in reply
    assert "人工" not in reply


def test_agent_contract_removes_free_form_intent_and_limits_writes() -> None:
    with pytest.raises(Exception):
        AiAssistResult.model_validate({"intent_candidate": "buy"})
    with pytest.raises(ValueError, match="agent_write_action_budget_exceeded"):
        AgentTurnPlan.model_validate({
            "tool_calls": [
                {"name": "order.create", "arguments": {}},
                {"name": "order.cancel", "arguments": {}},
            ],
        })
