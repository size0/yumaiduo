import {
  ExecutorContractError,
  buildPriceChangeIdempotencyKey,
  normalizeAuthoritativeOrder,
  normalizeAuthoritativeSession,
  normalizeInboundEvent,
  normalizeQuoteSnapshot,
  orderChangeability,
  summarizeProviderResult,
} from './contracts.mjs';

const FINAL_STATUSES = new Set(['succeeded', 'skipped', 'failed']);

function stableCode(value) {
  const code = String(value ?? '').trim().toLowerCase();
  return /^[a-z0-9_.-]{1,80}$/.test(code) ? code : null;
}

function errorStatus(error) {
  if (Number.isInteger(error?.status)) return error.status;
  if (Number.isInteger(error?.response?.status)) return error.response.status;
  return null;
}

function errorCategory(error) {
  const name = String(error?.name ?? '').toLowerCase();
  const code = String(error?.code ?? '').toLowerCase();
  const status = errorStatus(error);
  if (name.includes('abort') || name.includes('timeout') || code.includes('abort') || code.includes('timeout') || code === 'etimedout') {
    return 'timeout';
  }
  if (code.includes('network') || ['econnreset', 'econnrefused', 'enotfound', 'eai_again'].includes(code)) {
    return 'network_error';
  }
  if (status >= 500) return 'http_5xx';
  if (status >= 400) return 'http_4xx';
  if (status !== null) return 'http_error';
  return 'unknown_error';
}

function errorSummary(error) {
  const category = errorCategory(error);
  return {
    schema_version: 'executor_next.provider_error_summary.v1',
    category,
    code: stableCode(error?.code),
    status: errorStatus(error),
    retryable: category === 'timeout' || category === 'network_error' || category === 'http_5xx',
    summary: `provider_${category}`,
  };
}

function isExplicitSafeRetry(error) {
  const code = stableCode(error?.code);
  return errorStatus(error) === 400 && ['e_oauth_flow_failed', 'cannot_modify_fee'].includes(code);
}

function isUnknownPlatformOutcome(error) {
  const name = String(error?.name ?? '').toLowerCase();
  const code = String(error?.code ?? '').toLowerCase();
  if (name.includes('abort') || name.includes('timeout') || code.includes('timeout') || code.includes('network')) return true;
  const status = errorStatus(error);
  if (!Number.isInteger(status)) return true;
  return status >= 500;
}

function same(left, right) {
  return String(left ?? '') === String(right ?? '');
}

function contractFailure(error) {
  return {
    status: 'skipped',
    reason_code: error instanceof ExecutorContractError ? error.code : 'invalid_executor_request',
    action_attempted: false,
  };
}

function validateCommandBindings(event, quote, sdk_tenant_id, now_iso = null) {
  const required_event_fields = ['order_id', 'shop_id', 'buyer_id', 'chat_id'];
  const missing_event_field = required_event_fields.find((field) => !event[field]);
  if (missing_event_field) return `event_${missing_event_field}_required`;
  if (!same(event.tenant_id, sdk_tenant_id) || !same(quote.tenant_id, sdk_tenant_id)) return 'tenant_ownership_mismatch';
  for (const field of ['order_id', 'shop_id', 'buyer_id', 'chat_id']) {
    if (!same(event[field], quote[field])) return `${field}_quote_binding_mismatch`;
  }
  if (quote.quote_expires_at && now_iso && Date.parse(quote.quote_expires_at) <= Date.parse(now_iso)) return 'quote_expired';
  return null;
}

function validateBindings({ event, quote, order, session, sdk_tenant_id }) {
  const command_error = validateCommandBindings(event, quote, sdk_tenant_id);
  if (command_error) return command_error;
  for (const field of ['order_id', 'tenant_id', 'shop_id']) {
    if (!order[field]) return `authoritative_order_${field}_missing`;
    if (!same(order[field], quote[field])) return `${field}_order_ownership_mismatch`;
  }
  if (order.buyer_id && !same(order.buyer_id, quote.buyer_id)) return 'buyer_id_order_ownership_mismatch';
  if (order.chat_id && !same(order.chat_id, quote.chat_id)) return 'chat_id_order_ownership_mismatch';
  if (!order.buyer_id || !order.chat_id) {
    if (!session) return 'authoritative_order_session_missing';
    for (const field of ['shop_id', 'buyer_id', 'chat_id']) {
      if (!session[field]) return `authoritative_session_${field}_missing`;
      if (!same(session[field], quote[field])) return `${field}_session_ownership_mismatch`;
    }
  }
  return null;
}

function resultBase(command, idempotency_key) {
  return {
    idempotency_key,
    order_id: command.quote.order_id,
    quote_version: command.quote.quote_version,
    ...(command.quote.quote_id ? { quote_id: command.quote.quote_id } : {}),
    ...(command.quote.quote_hash ? { quote_hash: command.quote.quote_hash } : {}),
    ...(Number.isSafeInteger(command.quote.quote_generation)
      ? { quote_generation: command.quote.quote_generation } : {}),
    ...(Number.isSafeInteger(command.quote.binding_revision)
      ? { binding_revision: command.quote.binding_revision } : {}),
    ...(Number.isSafeInteger(command.quote.transaction_revision)
      ? { transaction_revision: command.quote.transaction_revision } : {}),
    ...(command.quote.flow_version ? { flow_version: command.quote.flow_version } : {}),
    target_amount_cents: command.quote.target_amount_cents,
    ...(Number.isSafeInteger(command.quote.observed_order_amount_cents)
      ? { observed_order_amount_cents: command.quote.observed_order_amount_cents }
      : {}),
  };
}

export function createPriceChangeExecutor({
  sdk, tenant_id, receipt_store, now = () => new Date().toISOString(),
  retry_delays = [2_000, 5_000], sleep = (delay) => new Promise((resolve) => setTimeout(resolve, delay)),
}) {
  const sdk_tenant_id = String(tenant_id ?? '').trim();
  if (!sdk_tenant_id) throw new ExecutorContractError('executor_tenant_id_required');
  if (typeof sdk?.orders?.get !== 'function' || typeof sdk?.orders?.changePrice !== 'function') {
    throw new ExecutorContractError('executor_sdk_orders_required');
  }
  if (typeof receipt_store?.claim !== 'function' || typeof receipt_store?.save !== 'function') {
    throw new ExecutorContractError('executor_receipt_store_required');
  }
  const explicit_retry_delays = Array.isArray(retry_delays)
    ? retry_delays.map(Number).filter((delay) => Number.isFinite(delay) && delay >= 0 && delay <= 10_000).slice(0, 2)
    : [];

  async function execute({ provider_event, quote_snapshot } = {}) {
    let command;
    let idempotency_key;
    try {
      command = {
        event: normalizeInboundEvent(provider_event),
        quote: normalizeQuoteSnapshot(quote_snapshot),
      };
      const command_error = validateCommandBindings(command.event, command.quote, sdk_tenant_id, now());
      if (command_error) throw new ExecutorContractError(command_error);
      idempotency_key = buildPriceChangeIdempotencyKey(command.quote);
    } catch (error) {
      return contractFailure(error);
    }

    const base = resultBase(command, idempotency_key);
    const initial_receipt = {
      schema_version: 'executor_next.price_change_receipt.v1',
      idempotency_key,
      idempotency_material: {
        ...(command.quote.flow_version === 'V4_NEW_FLOW_V2' ? {
          flow_version: command.quote.flow_version,
          quote_id: command.quote.quote_id,
          quote_hash: command.quote.quote_hash,
          quote_generation: command.quote.quote_generation,
          binding_revision: command.quote.binding_revision,
          transaction_revision: command.quote.transaction_revision,
        } : {}),
        tenant_id: command.quote.tenant_id,
        shop_id: command.quote.shop_id,
        buyer_id: command.quote.buyer_id,
        chat_id: command.quote.chat_id,
        order_id: command.quote.order_id,
        quote_version: command.quote.quote_version,
        target_amount_cents: command.quote.target_amount_cents,
        ...(Number.isSafeInteger(command.quote.observed_order_amount_cents)
          ? { observed_order_amount_cents: command.quote.observed_order_amount_cents }
          : {}),
        ...(command.quote.quote_record_id ? { quote_record_id: command.quote.quote_record_id } : {}),
        ...(command.quote.confirmation_version ? { confirmation_version: command.quote.confirmation_version } : {}),
      },
      status: 'started',
      phase: 'claimed',
      action_attempted: false,
      inbound_event: command.event,
      quote_snapshot: command.quote,
      before_order: null,
      authoritative_session: null,
      provider_receipt: null,
      provider_error: null,
      audit_reason: null,
      after_order: null,
      result: null,
      created_at: now(),
      updated_at: now(),
    };

    let claim;
    try {
      claim = await receipt_store.claim(initial_receipt);
    } catch (error) {
      return { ...base, status: 'failed', reason_code: 'receipt_claim_failed', action_attempted: false };
    }
    const receipt = claim?.receipt;
    if (!claim || typeof claim.created !== 'boolean' || !receipt) {
      return { ...base, status: 'failed', reason_code: 'receipt_claim_invalid', action_attempted: false };
    }
    if (!claim.created) return reconcileExisting(receipt, command, base);

    let before_order;
    let authoritative_session = null;
    try {
      before_order = normalizeAuthoritativeOrder(await sdk.orders.get(command.quote.order_id), { sdk_tenant_id });
      if ((!before_order.buyer_id || !before_order.chat_id) && typeof sdk?.im?.getSessionByOrder === 'function') {
        authoritative_session = normalizeAuthoritativeSession(await sdk.im.getSessionByOrder(command.quote.order_id));
      }
    } catch (error) {
      return finalize(receipt, {
        ...base,
        status: 'failed',
        reason_code: 'authoritative_order_read_failed',
        action_attempted: false,
        provider_error: errorSummary(error),
      });
    }

    receipt.before_order = before_order;
    receipt.authoritative_session = authoritative_session;
    const ownership_error = validateBindings({
      event: command.event,
      quote: command.quote,
      order: before_order,
      session: authoritative_session,
      sdk_tenant_id,
    });
    if (ownership_error) {
      return finalize(receipt, { ...base, status: 'skipped', reason_code: ownership_error, action_attempted: false });
    }

    const changeability = orderChangeability(before_order);
    if (!changeability.allowed) {
      const observedAmount = command.quote.observed_order_amount_cents;
      const amountProvenUnchanged = (
        changeability.reason_code === 'order_already_paid'
        && Number.isSafeInteger(observedAmount)
        && before_order.amount_cents === observedAmount
      );
      return finalize(receipt, {
        ...base,
        status: 'skipped',
        reason_code: changeability.reason_code,
        action_attempted: false,
        verified_amount_cents: before_order.amount_cents,
        auto_refund_eligible: amountProvenUnchanged,
        manual_price_change_suspected: Boolean(
          changeability.reason_code === 'order_already_paid'
          && Number.isSafeInteger(observedAmount)
          && before_order.amount_cents !== observedAmount
        ),
      });
    }
    if (before_order.amount_cents === command.quote.target_amount_cents) {
      return finalize(receipt, {
        ...base,
        status: 'succeeded',
        reason_code: 'amount_already_matches',
        action_attempted: false,
        reconciled: true,
        verified_amount_cents: before_order.amount_cents,
      });
    }

    const prewrite_error = validateCommandBindings(command.event, command.quote, sdk_tenant_id, now());
    if (prewrite_error) {
      return finalize(receipt, { ...base, status: 'skipped', reason_code: prewrite_error, action_attempted: false });
    }

    receipt.phase = 'validated';
    receipt.updated_at = now();
    await receipt_store.save(receipt);

    let platform_error = null;
    receipt.phase = 'provider_call_started';
    receipt.action_attempted = true;
    receipt.audit_reason = 'provider_receipt_pending';
    receipt.updated_at = now();
    await receipt_store.save(receipt);
    try {
      const provider_result = await sdk.orders.changePrice(command.quote.order_id, {
        priceFee: command.quote.target_amount_cents,
        transportFee: 0,
      });
      receipt.provider_receipt = summarizeProviderResult(provider_result, 'orders.change_price');
      receipt.phase = 'provider_receipt_saved';
      receipt.audit_reason = null;
      receipt.updated_at = now();
      await receipt_store.save(receipt);
    } catch (error) {
      platform_error = error;
      receipt.provider_error = errorSummary(error);
      receipt.phase = 'platform_result_unknown_or_rejected';
      receipt.audit_reason = 'provider_receipt_unavailable_after_platform_error';
      receipt.updated_at = now();
      try {
        await receipt_store.save(receipt);
      } catch {
        // Readback still has priority after a possibly committed platform action.
      }
    }

    let after_order;
    try {
      after_order = normalizeAuthoritativeOrder(await sdk.orders.get(command.quote.order_id), { sdk_tenant_id });
      receipt.after_order = after_order;
    } catch (error) {
      return finalize(receipt, {
        ...base,
        status: 'unknown',
        reason_code: 'post_change_read_failed',
        action_attempted: true,
        provider_error: errorSummary(error),
      });
    }

    if (after_order.amount_cents === command.quote.target_amount_cents) {
      return finalize(receipt, {
        ...base,
        status: 'succeeded',
        reason_code: platform_error ? 'reconciled_after_platform_error' : 'price_change_verified',
        action_attempted: true,
        reconciled: Boolean(platform_error),
        verified_amount_cents: after_order.amount_cents,
      });
    }

    if (platform_error && isExplicitSafeRetry(platform_error)) {
      for (const delay of explicit_retry_delays) {
        if (delay) await sleep(delay);
        let retry_order;
        let retry_session = null;
        try {
          retry_order = normalizeAuthoritativeOrder(await sdk.orders.get(command.quote.order_id), { sdk_tenant_id });
          if ((!retry_order.buyer_id || !retry_order.chat_id) && typeof sdk?.im?.getSessionByOrder === 'function') {
            retry_session = normalizeAuthoritativeSession(await sdk.im.getSessionByOrder(command.quote.order_id));
          }
        } catch (error) {
          return finalize(receipt, {
            ...base, status: 'unknown', reason_code: 'explicit_retry_order_read_failed',
            action_attempted: true, provider_error: errorSummary(error),
          });
        }
        receipt.after_order = retry_order;
        receipt.authoritative_session = retry_session;
        const retry_binding_error = validateBindings({
          event: command.event, quote: command.quote, order: retry_order,
          session: retry_session, sdk_tenant_id,
        });
        const retry_changeability = orderChangeability(retry_order);
        const retry_prewrite_error = validateCommandBindings(command.event, command.quote, sdk_tenant_id, now());
        if (retry_binding_error || !retry_changeability.allowed || retry_prewrite_error) {
          return finalize(receipt, {
            ...base, status: 'skipped',
            reason_code: retry_binding_error || retry_prewrite_error || retry_changeability.reason_code,
            action_attempted: true, verified_amount_cents: retry_order.amount_cents,
          });
        }
        if (retry_order.amount_cents === command.quote.target_amount_cents) {
          return finalize(receipt, {
            ...base, status: 'succeeded', reason_code: 'reconciled_before_explicit_retry',
            action_attempted: true, reconciled: true, verified_amount_cents: retry_order.amount_cents,
          });
        }
        receipt.phase = 'explicit_retry_provider_call_started';
        receipt.updated_at = now();
        await receipt_store.save(receipt);
        let retry_error = null;
        try {
          const provider_result = await sdk.orders.changePrice(command.quote.order_id, {
            priceFee: command.quote.target_amount_cents,
            transportFee: 0,
          });
          receipt.provider_receipt = summarizeProviderResult(provider_result, 'orders.change_price');
          receipt.provider_error = null;
          receipt.audit_reason = null;
          receipt.updated_at = now();
          await receipt_store.save(receipt);
        } catch (error) {
          retry_error = error;
          receipt.provider_error = errorSummary(error);
          receipt.audit_reason = 'provider_receipt_unavailable_after_explicit_retry';
          receipt.updated_at = now();
          await receipt_store.save(receipt).catch(() => undefined);
        }
        let retry_after;
        try {
          retry_after = normalizeAuthoritativeOrder(await sdk.orders.get(command.quote.order_id), { sdk_tenant_id });
          receipt.after_order = retry_after;
        } catch (error) {
          return finalize(receipt, {
            ...base, status: 'unknown', reason_code: 'explicit_retry_readback_failed',
            action_attempted: true, provider_error: errorSummary(error),
          });
        }
        if (retry_after.amount_cents === command.quote.target_amount_cents) {
          return finalize(receipt, {
            ...base, status: 'succeeded', reason_code: 'price_change_verified_after_explicit_retry',
            action_attempted: true, reconciled: Boolean(retry_error),
            verified_amount_cents: retry_after.amount_cents,
          });
        }
        platform_error = retry_error;
        after_order = retry_after;
        if (!retry_error || !isExplicitSafeRetry(retry_error)) break;
      }
    }

    if (platform_error && isUnknownPlatformOutcome(platform_error)) {
      return finalize(receipt, {
        ...base,
        status: 'unknown',
        reason_code: 'platform_result_unknown_after_readback',
        action_attempted: true,
        verified_amount_cents: after_order.amount_cents,
      });
    }
    return finalize(receipt, {
      ...base,
      status: 'failed',
      reason_code: platform_error ? 'platform_price_change_rejected' : 'price_change_verification_failed',
      action_attempted: true,
      verified_amount_cents: after_order.amount_cents,
    });
  }

  function semanticRepriceStatus(command, result) {
    if (command.quote.flow_version !== 'V4_NEW_FLOW_V2') return null;
    const reason = String(result.reason_code ?? '').trim();
    if (result.status === 'succeeded') {
      return ['amount_already_matches', 'idempotent_readback_verified', 'reconciled_before_explicit_retry'].includes(reason)
        ? 'ALREADY_PRICED' : 'REPRICE_CONFIRMED';
    }
    if (result.status === 'unknown') return 'REPRICE_UNKNOWN';
    if (result.status === 'skipped') {
      if (reason === 'order_already_paid' || reason === 'order_paid_or_closed'
        || reason === 'order_refund_in_progress_or_complete' || reason === 'order_status_not_changeable') {
        return 'ORDER_NOT_UNPAID';
      }
      if (reason.includes('ownership_mismatch') || reason.includes('binding_mismatch')
        || reason === 'tenant_ownership_mismatch' || reason.startsWith('authoritative_')
        || reason.startsWith('event_')) return 'IDENTITY_MISMATCH';
    }
    if (result.status === 'failed') {
      return reason === 'price_change_verification_failed' ? 'REPRICE_UNCONFIRMED' : 'PROVIDER_FAILED';
    }
    return null;
  }

  async function reconcileExisting(receipt, command, base) {
    const bindingFields = command.quote.flow_version === 'V4_NEW_FLOW_V2'
      ? ['flow_version', 'source', 'quote_id', 'quote_hash', 'quote_generation', 'binding_revision', 'transaction_revision', 'idempotency_key']
      : [];
    for (const field of [
      'quote_version', 'order_id', 'tenant_id', 'shop_id', 'buyer_id', 'chat_id',
      'target_amount_cents', ...bindingFields,
    ]) {
      if (!same(receipt.quote_snapshot?.[field], command.quote[field])) {
        return { ...base, status: 'skipped', reason_code: 'idempotency_receipt_binding_mismatch', action_attempted: false, deduplicated: true };
      }
    }
    if (FINAL_STATUSES.has(receipt.status) && receipt.result) {
      return { ...receipt.result, deduplicated: true };
    }
    const interrupted_provider_call = receipt.phase === 'provider_call_started'
      && receipt.action_attempted === true
      && !receipt.provider_receipt;
    const recovery_context = interrupted_provider_call ? {
      audit_reason: 'provider_receipt_unavailable_due_to_crash',
      prior_action_attempted: true,
    } : {};
    if (interrupted_provider_call) receipt.audit_reason = recovery_context.audit_reason;
    let order;
    let authoritative_session = null;
    try {
      order = normalizeAuthoritativeOrder(await sdk.orders.get(command.quote.order_id), { sdk_tenant_id });
      if ((!order.buyer_id || !order.chat_id) && typeof sdk?.im?.getSessionByOrder === 'function') {
        authoritative_session = normalizeAuthoritativeSession(await sdk.im.getSessionByOrder(command.quote.order_id));
      }
      receipt.after_order = order;
      receipt.authoritative_session = authoritative_session;
    } catch (error) {
      return finalize(receipt, {
        ...base,
        status: 'unknown',
        reason_code: 'idempotent_readback_failed',
        action_attempted: false,
        deduplicated: true,
        ...recovery_context,
        provider_error: errorSummary(error),
      });
    }
    const ownership_error = validateBindings({
      event: command.event,
      quote: command.quote,
      order,
      session: authoritative_session,
      sdk_tenant_id,
    });
    if (ownership_error) {
      return finalize(receipt, {
        ...base,
        status: 'skipped',
        reason_code: ownership_error,
        action_attempted: false,
        deduplicated: true,
        ...recovery_context,
      });
    }
    if (order.amount_cents === command.quote.target_amount_cents) {
      return finalize(receipt, {
        ...base,
        status: 'succeeded',
        reason_code: 'idempotent_readback_verified',
        action_attempted: false,
        deduplicated: true,
        ...recovery_context,
        reconciled: true,
        verified_amount_cents: order.amount_cents,
      });
    }
    return finalize(receipt, {
      ...base,
      status: 'unknown',
      reason_code: 'previous_price_change_result_unknown',
      action_attempted: false,
      deduplicated: true,
      ...recovery_context,
      verified_amount_cents: order.amount_cents,
    });
  }

  async function finalize(receipt, result) {
    const reprice_status = semanticRepriceStatus({ quote: receipt.quote_snapshot }, result);
    const finalResult = {
      ...result,
      ...(reprice_status ? { reprice_status } : {}),
    };
    receipt.status = finalResult.status;
    receipt.phase = 'finished';
    receipt.result = finalResult;
    receipt.updated_at = now();
    await receipt_store.save(receipt);
    return finalResult;
  }

  return Object.freeze({ execute });
}
