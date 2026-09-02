from __future__ import annotations

import httpx
import pytest
import asyncio

from app.chat_service import CustomerServiceChatService
from app.config import Settings
from app.errors import ProviderError


@pytest.mark.asyncio
async def test_customer_chat_uses_legacy_runtime_for_scoped_context() -> None:
    requests = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "已查到场次。"}}]},
        )

    settings = Settings(chat_api_key="key", chat_base_url="https://example.test/v1")
    context = {
        "tenant_id": "tenant-1",
        "shop_id": "shop-1",
        "buyer_id": "buyer-1",
        "chat_id": "chat-1",
        "event_id": "event-1",
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = CustomerServiceChatService(settings, client=client)
        reply = await service.reply("查场次", "chat-1", runtime_context=context)

    assert requests == 1
    assert reply == "已查到场次。"


@pytest.mark.asyncio
async def test_runtime_delegate_failure_is_not_replayed() -> None:
    requests = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(503, json={"error": "unavailable"})

    settings = Settings(chat_api_key="key", chat_base_url="https://example.test/v1")
    context = {
        "tenant_id": "tenant-1",
        "shop_id": "shop-1",
        "buyer_id": "buyer-1",
        "chat_id": "chat-2",
        "event_id": "event-2",
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = CustomerServiceChatService(settings, client=client)
        with pytest.raises(ProviderError):
            await service.reply("查场次", "chat-2", runtime_context=context)

    # LegacyAgentRuntime propagates the provider failure; the adapter must not
    # call the provider a second time while trying to recover its result.
    assert requests == 1


@pytest.mark.asyncio
async def test_new_scoped_message_interrupts_old_runtime_reply() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    requests = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            first_started.set()
            await release_first.wait()
            text = "旧回复"
        else:
            text = "新回复"
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": text}}]})

    settings = Settings(chat_api_key="key", chat_base_url="https://example.test/v1")
    context = {
        "tenant_id": "tenant-1", "shop_id": "shop-1", "buyer_id": "buyer-1", "chat_id": "chat-3",
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = CustomerServiceChatService(settings, client=client)
        old_task = asyncio.create_task(service.reply("旧问题", "chat-3", runtime_context={**context, "event_id": "event-old"}))
        await first_started.wait()
        new_task = asyncio.create_task(service.reply("新问题", "chat-3", runtime_context={**context, "event_id": "event-new"}))
        new_reply = await new_task
        release_first.set()
        old_reply = await old_task

    assert new_reply == "新回复"
    assert old_reply == "本次请求已被新消息中断，请以最新消息为准。"
