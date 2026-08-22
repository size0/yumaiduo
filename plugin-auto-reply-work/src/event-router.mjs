export const EVENT_ROUTE_KIND = Object.freeze({
  MESSAGE: 'message',
  ORDER_CREATED: 'order_created',
  ORDER_PAID: 'order_paid',
  ORDER_PRICE_CHANGED: 'order_price_changed',
  ORDER_STATUS: 'order_status',
  LEGACY: 'legacy',
});

const ROUTES = Object.freeze({
  'im.message.received': Object.freeze({
    kind: EVENT_ROUTE_KIND.MESSAGE,
    bridge_status: null,
    conversation_stage: null,
  }),
  'order.created': Object.freeze({
    kind: EVENT_ROUTE_KIND.ORDER_CREATED,
    bridge_status: null,
    conversation_stage: null,
  }),
  'order.paid': Object.freeze({
    kind: EVENT_ROUTE_KIND.ORDER_PAID,
    bridge_status: 'paid',
    conversation_stage: null,
  }),
  'order.price.changed': Object.freeze({
    kind: EVENT_ROUTE_KIND.ORDER_PRICE_CHANGED,
    bridge_status: 'awaiting_payment',
    conversation_stage: 'waiting_payment',
  }),
  'order.closed': Object.freeze({
    kind: EVENT_ROUTE_KIND.ORDER_STATUS,
    bridge_status: 'cancelled',
    conversation_stage: 'cancelled',
  }),
});

/**
 * Classify only the durable event envelope type. Business decisions remain in
 * their deterministic orchestrators; unknown events deliberately retain the
 * legacy fallback during the strangler migration.
 */
export function routeWorkflowEvent(eventName) {
  const event = typeof eventName === 'string' ? eventName.trim() : '';
  const route = ROUTES[event] ?? {
    kind: EVENT_ROUTE_KIND.LEGACY,
    bridge_status: null,
    conversation_stage: null,
  };
  return { event, ...route };
}
