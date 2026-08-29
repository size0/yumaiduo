from __future__ import annotations

import json
import time

import pytest

from app.liangpiao_callbacks import CallbackError, CallbackVerifier, LiangpiaoCallbackHandler
from app.rule_contracts import ReplyPlan
from app.rule_templates import validate_reply_plan


class FakeState:
    def __init__(self) -> None:
        self.revision = 0
        self.calls: list[dict[str, object]] = []

    def get(self, **_: object) -> object:
        return self

    def transition(self, **kwargs: object) -> None:
        self.calls.append(dict(kwargs))
        self.revision += 1


@pytest.mark.asyncio
async def test_callback_signature_replay_and_state_progression() -> None:
    verifier = CallbackVerifier("secret")
    state = FakeState()
    handler = LiangpiaoCallbackHandler(
        verifier, state_store=state,
        mapping_store={"out-1": {"tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c", "out_order_no": "out-1"}},
        enabled=True,
    )
    raw = json.dumps({"outOrderNo": "out-1", "status": "ticket_sent", "ticketCode": "TK-1"}).encode()
    timestamp = str(int(time.time()))
    signature = verifier.sign(raw, timestamp, "nonce-1")
    result = await handler.handle(raw, signature=signature, timestamp=timestamp, nonce="nonce-1")
    assert result["code"] == "LIANGPIAO_CALLBACK_APPLIED"
    assert result["reply_plan"]["template_key"] == "flow.fulfillment.ticket_sent"
    assert validate_reply_plan(ReplyPlan.model_validate(result["reply_plan"]), state="TICKET_SENT")
    assert state.calls[0]["flow_state"] == "TICKET_SENT"
    with pytest.raises(CallbackError) as error:
        await handler.handle(raw, signature=signature, timestamp=timestamp, nonce="nonce-1")
    assert error.value.code == "LIANGPIAO_CALLBACK_REPLAY"


@pytest.mark.asyncio
async def test_callback_missing_ticket_evidence_goes_manual_hold() -> None:
    verifier = CallbackVerifier("secret")
    state = FakeState()
    handler = LiangpiaoCallbackHandler(
        verifier, state_store=state,
        mapping_store={"out-2": {"tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c", "out_order_no": "out-2"}},
        enabled=True,
    )
    raw = json.dumps({"outOrderNo": "out-2", "status": "ticket_sent"}).encode()
    timestamp = str(int(time.time()))
    signature = verifier.sign(raw, timestamp, "nonce-2")
    result = await handler.handle(raw, signature=signature, timestamp=timestamp, nonce="nonce-2")
    assert result["code"] == "LIANGPIAO_TICKET_EVIDENCE_MISSING"
    assert state.calls[0]["flow_state"] == "MANUAL_HOLD"


@pytest.mark.asyncio
async def test_callback_amount_mismatch_goes_manual_hold() -> None:
    verifier = CallbackVerifier("secret")
    state = FakeState()
    handler = LiangpiaoCallbackHandler(
        verifier, state_store=state,
        mapping_store={"out-3": {"tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c",
                                 "out_order_no": "out-3", "payload": {"maxPrice": 8800}}},
        enabled=True,
    )
    raw = json.dumps({"outOrderNo": "out-3", "status": "paid", "amountFen": 7700}).encode()
    timestamp = str(int(time.time()))
    result = await handler.handle(raw, signature=verifier.sign(raw, timestamp, "nonce-3"), timestamp=timestamp, nonce="nonce-3")
    assert result["code"] == "LIANGPIAO_CALLBACK_AMOUNT_MISMATCH"
    assert state.calls[0]["flow_state"] == "MANUAL_HOLD"
