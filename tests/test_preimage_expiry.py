"""Expired pre-image drafts stay blocked and recover through a fresh image."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
import pytest
from tests.test_request_batch import actual_flow
from tests.test_request_resolution_edges import tool
from application.request_state import project

@pytest.mark.parametrize("age,expired", [(1,False),(25,True),(-1,True)])
@pytest.mark.parametrize("kind", ["ticket_details","awaiting_image_request","agent_request_plan"])
def test_preimage_pending_age(actual_flow,age,expired,kind):
    make,s,quotes,service,_=actual_flow
    c=s.conversation
    ctx={k:getattr(c,k,"" if k=="demand_id" else 0 if k=="demand_rev" else None) for k in ("tenant_id","shop_id","buyer_id","chat_id","demand_id","demand_rev")}
    ctx["created_at"]=(datetime.now(timezone.utc)-timedelta(hours=age)).isoformat()
    ctx[kind]=dict(ctx,pending=True,source_image=None,city="广州")
    state=project(c,ctx)
    assert state["blocked"] and state["expired"] is expired
    assert state["status"] == ("REQUEST_REVALIDATION_REQUIRED" if expired else "REQUEST_FIELDS_INCOMPLETE")

def test_expired_preimage_edit_and_recovery_do_not_revive_quote(actual_flow):
    make,s,quotes,service,_=actual_flow
    old=s.conversation.quote_id
    s.image_context={}
    assert tool(make,"广州").execute("supplement_city",dict(city="广州",request_fields=["city"]))["ok"]
    s.image_context["created_at"]=(datetime.now(timezone.utc)-timedelta(hours=25)).isoformat()
    before=deepcopy(s.image_context)
    for name,args in [("supplement_city",dict(city="深圳",request_fields=["city"])),("recover_image_context",{}),("request_latest_quote_reprice",{})]:
        t=tool(make,"深圳")
        t.semantic_intent="revise"
        r=t.execute(name,args)
        assert not r["ok"] and r["status"]=="REQUEST_REVALIDATION_REQUIRED"
        assert s.image_context==before
    assert not tool(make,"多少钱").read_quote().get("tradable", False)
    assert quotes.get(old).status=="SUPERSEDED"  # No silent mutation or permission revival.
    service.flow.api.ch4_show_list.assert_not_called()

def test_fresh_image_does_not_inherit_expired_preimage_fields():
    from tests.test_buyer_assistant import BuyerAssistantTests
    from application.buyer_assistant import BuyerAssistantTools
    f=BuyerAssistantTests();f.setUp()
    try:
        f.session.image_context={}
        t=BuyerAssistantTools(f.svc,f.session,"广州",f.cfg,None)
        t.enable_semantic_routing();t.request_plan_enabled=True;t.semantic_intent="revise"
        assert t.execute("supplement_city",dict(city="广州",request_fields=["city"]))["ok"]
        f.session.image_context["created_at"]=(datetime.now(timezone.utc)-timedelta(hours=25)).isoformat()
        from application.ticket_flow import FlowResult
        def recovered(service, session, previous, image_url, result, cfg):
            assert not result.ok
            assert not any(previous.get(k) for k in ("awaiting_image_request", "ticket_details", "agent_request_plan"))
            return FlowResult(ok=True)
        f.svc.flow.on_screenshot.return_value = FlowResult(ok=False)
        with patch.object(f.svc,"_blacklist_gate",return_value=None), patch("application.image_sequence.recover_sequence",side_effect=recovered) as recovery:
            f.svc.on_message("c",image_url="https://example.test/new.jpg",config=f.cfg)
        assert recovery.call_count == 1
        args=f.svc.flow.on_screenshot.call_args.kwargs
        assert not args.get("buyer_request") and args.get("city_name")!="广州"
        assert not f.session.image_context.get("awaiting_image_request")
        assert not project(f.session.conversation,f.session.image_context)["expired"]
    finally:f.tearDown()

def test_completed_noimage_context_does_not_expire(actual_flow):
    _,s,_,_,_=actual_flow
    ctx=deepcopy(s.image_context);ctx.pop("url",None)
    ctx["created_at"]=(datetime.now(timezone.utc)-timedelta(hours=25)).isoformat()
    ctx["ticket_details"]={"pending":False}
    assert not project(s.conversation,ctx)["blocked"]


