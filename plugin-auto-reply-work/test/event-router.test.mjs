import assert from 'node:assert/strict';
import test from 'node:test';
import { EVENT_ROUTE_KIND, routeWorkflowEvent } from '../src/event-router.mjs';

test('routes buyer messages without attaching order transitions', () => {
  assert.deepEqual(routeWorkflowEvent('im.message.received'), {
    event: 'im.message.received',
    kind: EVENT_ROUTE_KIND.MESSAGE,
    bridge_status: null,
    conversation_stage: null,
  });
});

test('routes deterministic quoted-order lifecycle events explicitly', () => {
  assert.deepEqual(routeWorkflowEvent('order.created'), {
    event: 'order.created',
    kind: EVENT_ROUTE_KIND.ORDER_CREATED,
    bridge_status: null,
    conversation_stage: null,
  });
  assert.deepEqual(routeWorkflowEvent('order.paid'), {
    event: 'order.paid',
    kind: EVENT_ROUTE_KIND.ORDER_PAID,
    bridge_status: 'paid',
    conversation_stage: null,
  });
  assert.deepEqual(routeWorkflowEvent('order.price.changed'), {
    event: 'order.price.changed',
    kind: EVENT_ROUTE_KIND.ORDER_PRICE_CHANGED,
    bridge_status: 'awaiting_payment',
    conversation_stage: 'waiting_payment',
  });
});

test('preserves existing bridge and conversation transitions for closed orders', () => {
  assert.deepEqual(routeWorkflowEvent('order.closed'), {
    event: 'order.closed',
    kind: EVENT_ROUTE_KIND.ORDER_STATUS,
    bridge_status: 'cancelled',
    conversation_stage: 'cancelled',
  });
});

test('keeps unknown events on the legacy fallback instead of dropping them', () => {
  assert.deepEqual(routeWorkflowEvent('order.shipped'), {
    event: 'order.shipped',
    kind: EVENT_ROUTE_KIND.LEGACY,
    bridge_status: null,
    conversation_stage: null,
  });
  assert.deepEqual(routeWorkflowEvent(null), {
    event: '',
    kind: EVENT_ROUTE_KIND.LEGACY,
    bridge_status: null,
    conversation_stage: null,
  });
});
