import { hasUnresolvedReplyPlaceholder } from '../agent/response-composer.mjs';

const BUYER_REPLY_KINDS = new Set(['reply', 'send_message', 'reply_with_image', 'send_image']);

export function isBuyerReplyAction(action) {
  return Boolean(action && typeof action === 'object' && BUYER_REPLY_KINDS.has(String(action.kind ?? '').trim()));
}

/**
 * Single buyer-delivery boundary for the strangler migration.
 *
 * The existing action executor still owns platform addressing, deduplication,
 * human-takeover detection and message-ID persistence. This boundary prevents
 * quote/order/AI orchestrators from bypassing those controls and deliberately
 * has no transaction execution capability.
 */
export function createReplyOrchestrator({ actionExecutor }) {
  if (!actionExecutor || typeof actionExecutor.execute !== 'function') {
    throw new TypeError('actionExecutor.execute is required');
  }

  async function deliver(action) {
    if (!isBuyerReplyAction(action)) {
      throw new TypeError('reply orchestrator requires a buyer-facing reply action');
    }
    if (
      ['reply', 'send_message', 'reply_with_image'].includes(String(action.kind))
      && hasUnresolvedReplyPlaceholder(action.text)
    ) {
      return { status: 'skipped', reason: 'unresolved_reply_placeholder' };
    }
    return actionExecutor.execute(action);
  }

  return Object.freeze({ deliver });
}
