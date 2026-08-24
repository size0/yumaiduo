export function createAgentReplyOutboxDispatcher({ store, executeReply, commitDelivery = null, validateReply = null, logger = console, leaseMs = 30_000 } = {}) {
  if (!store || typeof executeReply !== 'function') throw new TypeError('agent reply outbox dispatcher dependencies are required');

  async function tick() {
    const entry = await store.claimDue({ leaseMs });
    if (!entry) return null;
    if (entry.status === 'committing') {
      try {
        if (entry.delivery?.type !== 'quote' || typeof commitDelivery !== 'function') throw new Error('delivery commit handler unavailable');
        await commitDelivery(entry);
        await store.markCommitted(entry.action_id, entry.lease_id);
        return { status: 'sent', action_id: entry.action_id, message_id: entry.platform_message_id };
      } catch (error) {
        logger.warn?.('[agent-outbox] delivery commit deferred without resending', { actionId: entry.action_id, error: String(error?.name ?? 'unknown').slice(0, 100) });
        try {
          await store.deferCommit(entry.action_id, entry.lease_id, 'delivery_commit_failed');
          return { status: 'commit_deferred', action_id: entry.action_id };
        } catch (leaseError) {
          if (/agent reply outbox lease mismatch/u.test(String(leaseError?.message ?? ''))) return { status: 'lease_lost', action_id: entry.action_id };
          throw leaseError;
        }
      }
    }
    if (typeof validateReply === 'function' && await validateReply(entry) !== true) {
      await store.markSkipped(entry.action_id, entry.lease_id, 'reply_projection_superseded');
      return { status: 'skipped', action_id: entry.action_id, reason: 'reply_projection_superseded' };
    }
    const action = {
      action_id: entry.action_id,
      kind: 'reply',
      tenant_id: entry.tenant_id,
      account_unb: entry.account_unb,
      chat_id: entry.chat_id,
      peer_unb: entry.peer_unb,
      source_message_id: entry.source_message_id,
      text: entry.text,
      reply_origin: 'conversation_agent_outbox',
      reply_provenance: entry.reply_provenance,
    };
    try {
      const result = await executeReply(action);
      if (result?.status === 'succeeded' && String(result.message_id ?? '').trim()) {
        const sent = await store.markSent(entry.action_id, entry.lease_id, result.message_id);
        return { status: sent.status, action_id: entry.action_id, message_id: result.message_id };
      }
      if (result?.status === 'skipped') {
        const reason = String(result.reason ?? 'safely_skipped').slice(0, 100);
        await store.markSkipped(entry.action_id, entry.lease_id, reason);
        return { status: 'skipped', action_id: entry.action_id, reason };
      }
      await store.markUnknown(entry.action_id, entry.lease_id, 'send_result_unknown');
      return { status: 'unknown', action_id: entry.action_id };
    } catch (error) {
      logger.warn?.('[agent-outbox] reply result unknown; not retrying', {
        actionId: entry.action_id,
        error: String(error?.code ?? error?.name ?? 'unknown').slice(0, 100),
      });
      try {
        await store.markUnknown(entry.action_id, entry.lease_id, 'send_result_unknown');
        return { status: 'unknown', action_id: entry.action_id };
      } catch (leaseError) {
        if (/agent reply outbox lease mismatch/u.test(String(leaseError?.message ?? ''))) return { status: 'lease_lost', action_id: entry.action_id };
        throw leaseError;
      }
    }
  }

  return Object.freeze({ tick });
}
