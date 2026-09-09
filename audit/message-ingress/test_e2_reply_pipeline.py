"""Isolated production-snapshot composition; only external responses are fake."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

from app.canonical_conversation_agent import (
    AgentContextBuilder, CanonicalAgentToolBackend, CanonicalConversationAgent,
)
from app.canonical_event_handler import CanonicalEventHandler
from app.rule_state_coordinator import RuleStateCoordinator
from app.rules_first_runtime import RulesFirstRuntime
from app.rules_first_state_store import SqliteTransactionStateStore
from app.rules_first_store import RulesFirstStore
from app.shop_automation_store import ShopAutomationStore


class PlainProtector:
    def protect(self, value):
        return 'isolated:' + value

    def unprotect(self, value):
        return value.removeprefix('isolated:')


class NoLegacy:
    async def process_event(self, body):
        raise AssertionError('Legacy event handler must not be called')


class ToolModel:
    def __init__(self):
        self.calls = 0

    async def complete(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return {'tool_calls': [{'id': 'city-count', 'type': 'function', 'function': {
                'name': 'update_purchase_request',
                'arguments': json.dumps({'city': '广州', 'ticket_count': 2}),
            }}]}
        return {'reply': '广州测试万达，测试电影，13:35，2张合计89.80元。'}


@pytest.mark.asyncio
@pytest.mark.parametrize('e1_has_command', [True, False])
async def test_e2_quote_has_own_durable_reply_and_survives_restart(tmp_path, e1_has_command):
    fixture_path = Path(__file__).parents[1] / 'quote_fixtures.py'
    spec = importlib.util.spec_from_file_location('quote_fixtures_ingress', fixture_path)
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    quote_runtime = fixtures.runtime(tmp_path)

    class ImageProvider:
        async def recognize(self, *args, **kwargs):
            return fixtures.recognition()

    quote_runtime._recognition = ImageProvider()
    db = tmp_path / 'events.sqlite3'
    protector = PlainProtector()
    inbox = RulesFirstStore(db, protector=protector)
    states = SqliteTransactionStateStore(db, protector=protector)
    quotes = quote_runtime._quotes.store
    shops = ShopAutomationStore(tmp_path / 'shops.json')
    shops.sync('isolated', [{'accountUnb': 'shop'}])
    shops.set_canonical_enabled('isolated', 'shop', True)
    shops.set_canonical_conversation_enabled('isolated', 'shop', True)
    model = ToolModel()
    agent = CanonicalConversationAgent(
        AgentContextBuilder(quote_store=quotes), model,
        tool_backend=CanonicalAgentToolBackend(quote_runtime=quote_runtime, quote_store=quotes),
        max_tool_rounds=1,
    )
    handler = CanonicalEventHandler(agent=agent, shop_store=shops, inbox=inbox)
    durable = RulesFirstRuntime(
        inbox, NoLegacy(), RuleStateCoordinator(states, quote_store=quotes), states,
        canonical_text_handler=handler.process_event,
    )
    e1 = {'envelope': {'id': 'E1', 'tenantId': 'isolated', 'event': 'im.message.received',
        'payload': {'accountUnb': 'shop', 'peerUnb': 'buyer', 'chatId': 'chat',
                    'itemId': 'this-image-context', 'messageType': 2,
                    'imageUrls': ['https://fixture.invalid/image']}}}
    image_result = await quote_runtime.process_image_event(e1)
    assert image_result['status'] == 'QUOTED'
    if not e1_has_command:
        # Exercise an existing image-result-without-reply shape; do not mock persistence.
        image_result = {**image_result, 'current_runtime_reply': ''}
    durable.accept_canonical_result(e1, image_result)
    old_commands = durable.claim_commands()
    if e1_has_command:
        assert len(old_commands) == 1
        old = old_commands[0]
        inbox.record_command_result(old['command_id'], old['lease_token'],
            {'status': 'skipped', 'reason': 'buyer_message_arrived_before_send'})
    else:
        assert not old_commands
    e2 = copy.deepcopy(e1)
    e2['envelope']['id'] = 'E2'
    e2['envelope']['payload'].update(messageType=1, imageUrls=[], content='广州，2张')
    durable.accept(e2)
    assert await durable.drain_once()
    commands = durable.claim_commands()
    assert len(commands) == 1, commands
    cmd = commands[0]
    assert cmd['event_id'] == 'E2'
    assert cmd['action']['text'] == (
        '影片：测试电影；影院：广州测试万达；场次：2026-09-05 13:35；'
        'W+这场44.9/张，共2张89.8。麻烦确认一下影院和场次，确认后直接拍就行哈'
    )
    assert cmd['action']['quote_record_id']
    records = quotes.list('isolated')
    successor = next(q for q in records if q['ticket_count'] == 2)
    assert successor['purchase_context_id'] == 'this-image-context'
    assert successor['total_sell_price_fen'] == 8980
    assert successor['record_id'] == cmd['action']['quote_record_id']
    assert successor['supersedes_quote_id'] == image_result['quote']['quote_id']
    before = len(records)
    reopened = RulesFirstStore(db, protector=protector)
    assert reopened.enqueue_event(e2)['duplicate'] is True
    assert await durable.drain_once() is False
    assert len(quotes.list('isolated')) == before
    assert model.calls == 2
    print(json.dumps({'E1_command': e1_has_command, 'E2_event': cmd['event_id'],
        'quote_id': successor['quote_id'], 'command_id': cmd['command_id'],
        'reply': cmd['action']['text'], 'external_send': 'NOT_EXECUTED'}, ensure_ascii=False))
