import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import { UnknownActionResultError, createActionExecutor } from '../src/action-executor.mjs';

test('production sources contain no executable shipping, receipt reminder, rating or legacy delivery queue calls', async () => {
  const [executor, backendClient, config, application] = await Promise.all([
    readFile(new URL('../src/action-executor.mjs', import.meta.url), 'utf8'),
    readFile(new URL('../src/backend-client.mjs', import.meta.url), 'utf8'),
    readFile(new URL('../src/config.mjs', import.meta.url), 'utf8'),
    readFile(new URL('../src/application.mjs', import.meta.url), 'utf8'),
  ]);
  for (const forbidden of ['core.orders.ship(', 'core.orders.fakeShip(', 'core.orders.remindReceipt(', 'core.orders.createRate(']) {
    assert.equal(executor.includes(forbidden), false, forbidden);
  }
  for (const forbidden of ['claimDelivery', 'ackDelivery', 'claimReminder', 'ackReminder']) {
    assert.equal(backendClient.includes(forbidden), false, forbidden);
  }
  for (const forbidden of ['BACKEND_DELIVERY_CLAIM_PATH', 'BACKEND_REMINDER_CLAIM_PATH', '/deliveries/claim', '/reminders/claim']) {
    assert.equal(config.includes(forbidden), false, forbidden);
  }
  assert.equal(application.includes('临时锁座核验失败'), false);
});

function registry(ownIds = []) {
  const ids = new Set(ownIds);
  return {
    wasSentMessage: async (_tenant, _chat, id) => ids.has(id),
    recordSentMessage: async (_tenant, _chat, id) => ids.add(id),
  };
}

function replyAction() {
  return {
    action_id: 'action-1',
    tenant_id: 'tenant-1',
    kind: 'reply',
    account_unb: 'shop-1',
    chat_id: 'chat-1',
    peer_unb: 'buyer-1',
    text: '请确认需要几张票。',
  };
}

function priceAction() {
  return {
    action_id: 'action-price-1',
    tenant_id: 'tenant-1',
    kind: 'change_price',
    account_unb: 'shop-1',
    order_id: 'order-1',
    price_fee: 8080,
    transport_fee: 0,
    expected_total_cents: 8080,
    expected_quantity: 2,
    gates: {
      feature_enabled: true,
      unique_showtime: true,
      quantity_confirmed: true,
      selection_confirmed: true,
      quote_valid: true,
      order_linked: true,
      human_takeover: false,
    },
  };
}

test('reply stops when latest outbound message belongs to a human or another plugin', async () => {
  let sends = 0;
  const executor = createActionExecutor({
    messageRegistry: registry(),
    coreFor: () => ({
      im: {
        listMessages: async () => ({ items: [{ direction: 'outbound', messageId: 'human-1' }] }),
        sendMessage: async () => { sends += 1; },
      },
    }),
  });

  const result = await executor.execute(replyAction());
  assert.deepEqual(result, { status: 'skipped', reason: 'human_takeover' });
  assert.equal(sends, 0);
});

test('reply pauses for twenty seconds after a human reply even when the buyer sends a newer message', async () => {
  let sends = 0;
  const now = Date.parse('2026-08-17T10:00:00Z');
  const executor = createActionExecutor({
    now: () => now,
    messageRegistry: registry(),
    coreFor: () => ({
      im: {
        listMessages: async () => ({ items: [
          { direction: 'inbound', messageId: 'buyer-2', sentAt: new Date(now).toISOString() },
          { direction: 'outbound', messageId: 'human-1', sentAt: new Date(now - 10_000).toISOString() },
        ] }),
        sendMessage: async () => { sends += 1; },
      },
    }),
  });
  assert.deepEqual(await executor.execute(replyAction()), { status: 'skipped', reason: 'human_takeover' });
  assert.equal(sends, 0);
});

test('Agent outbox reply is superseded by any newer buyer or human message after its source turn', async () => {
  const now = Date.parse('2026-08-24T00:25:35Z');
  for (const newer of [
    { direction: 'inbound', messageId: 'buyer-newer', sentAt: new Date(now - 1_000).toISOString(), expected: 'superseded_by_newer_buyer_message' },
    { direction: 'outbound', messageId: 'human-newer', sentAt: new Date(now - 70_000).toISOString(), expected: 'human_takeover' },
  ]) {
    let sends = 0;
    const executor = createActionExecutor({
      now: () => now,
      messageRegistry: registry(),
      coreFor: () => ({ im: {
        listMessages: async () => ({ items: [newer, { direction: 'inbound', messageId: 'buyer-source', sentAt: new Date(now - 80_000).toISOString() }] }),
        sendMessage: async () => { sends += 1; },
      } }),
    });
    const result = await executor.execute({ ...replyAction(), source_message_id: 'buyer-source', reply_origin: 'conversation_agent_outbox' });
    assert.deepEqual(result, { status: 'skipped', reason: newer.expected });
    assert.equal(sends, 0);
  }
});

test('verified plugin price-change reminder ignores the platform-generated order notice', async () => {
  let sends = 0;
  const now = Date.parse('2026-08-22T02:04:09Z');
  const executor = createActionExecutor({
    now: () => now,
    messageRegistry: registry(),
    coreFor: () => ({ im: {
      listMessages: async () => ({ items: [{
        direction: 'outbound', messageId: 'platform-price-notice', messageType: 26,
        content: '你已修改订单价格，等待买家付款', sentAt: new Date(now - 500).toISOString(),
      }] }),
      sendMessage: async () => { sends += 1; return { messageId: 'plugin-price-reminder' }; },
    } }),
  });

  const result = await executor.execute({
    ...replyAction(),
    text: '价格已修改为151.80元，请核对后付款。订单付款后不支持退改签。',
    ignore_platform_price_change_notice: true,
  });
  assert.deepEqual(result, { status: 'succeeded', message_id: 'plugin-price-reminder' });
  assert.equal(sends, 1);
});

test('verified plugin price-change reminder still stops for a real seller reply', async () => {
  let sends = 0;
  const now = Date.parse('2026-08-22T02:04:09Z');
  const executor = createActionExecutor({
    now: () => now,
    messageRegistry: registry(),
    coreFor: () => ({ im: {
      listMessages: async () => ({ items: [{
        direction: 'outbound', messageId: 'seller-text', messageType: 1,
        content: '稍等，我人工确认一下', sentAt: new Date(now - 500).toISOString(),
      }] }),
      sendMessage: async () => { sends += 1; },
    } }),
  });

  const result = await executor.execute({ ...replyAction(), ignore_platform_price_change_notice: true });
  assert.deepEqual(result, { status: 'skipped', reason: 'human_takeover' });
  assert.equal(sends, 0);
});

test('reply records its own platform message id', async () => {
  const sent = registry();
  const executor = createActionExecutor({
    messageRegistry: sent,
    coreFor: () => ({
      im: {
        listMessages: async () => ({ items: [{ direction: 'inbound', messageId: 'buyer-1' }] }),
        sendMessage: async () => ({ messageId: 'plugin-message-1' }),
      },
    }),
  });

  const result = await executor.execute(replyAction());
  assert.equal(result.status, 'succeeded');
  assert.equal(await sent.wasSentMessage('tenant-1', 'chat-1', 'plugin-message-1'), true);
});

test('reply with configured image sends text first and records both platform message ids', async () => {
  const sent = registry();
  const calls = [];
  const executor = createActionExecutor({
    messageRegistry: sent,
    coreFor: () => ({ im: {
      listMessages: async () => ({ items: [{ direction: 'inbound', messageId: 'buyer-1' }] }),
      sendMessage: async () => { calls.push('text'); return { messageId: 'plugin-text-1' }; },
      sendImage: async () => { calls.push('image'); return { messageId: 'plugin-image-1' }; },
    } }),
  });

  const result = await executor.execute({ ...replyAction(), kind: 'reply_with_image', image_url: 'https://cdn.example/reply.png' });
  assert.deepEqual(calls, ['text', 'image']);
  assert.equal(result.status, 'succeeded');
  assert.equal(result.image_status, 'succeeded');
  assert.equal(await sent.wasSentMessage('tenant-1', 'chat-1', 'plugin-text-1'), true);
  assert.equal(await sent.wasSentMessage('tenant-1', 'chat-1', 'plugin-image-1'), true);
});

test('configured HTTPS reply image is downloaded, uploaded to Xianyu IM, then sent', async () => {
  const calls = [];
  const sent = registry();
  const executor = createActionExecutor({
    messageRegistry: sent,
    imageLoader: {
      async load(url) {
        calls.push(['download', url]);
        return { bytes: new Uint8Array([0xff, 0xd8, 0xff]), contentType: 'image/jpeg' };
      },
    },
    coreFor: () => ({ im: {
      listMessages: async () => ({ items: [{ direction: 'inbound', messageId: 'buyer-1' }] }),
      sendMessage: async () => { calls.push(['text']); return { messageId: 'plugin-text-1' }; },
      uploadImage: async (input) => {
        calls.push(['upload', input.filename, input.contentType, input.data.byteLength]);
        return { imageUrl: 'https://xianyu-cdn.example/reply.jpg', width: 1080, height: 720 };
      },
      sendImage: async (input) => { calls.push(['image', input.imageUrl]); return { messageId: 'plugin-image-1' }; },
    } }),
  });

  const result = await executor.execute({ ...replyAction(), kind: 'reply_with_image', image_url: 'https://controlled.example/reply.jpg' });
  assert.deepEqual(calls, [
    ['text'],
    ['download', 'https://controlled.example/reply.jpg'],
    ['upload', 'wanda-reply-image.jpg', 'image/jpeg', 3],
    ['image', 'https://xianyu-cdn.example/reply.jpg'],
  ]);
  assert.equal(result.image_status, 'succeeded');
  assert.equal(result.image_message_id, 'plugin-image-1');
  assert.equal(await sent.wasSentMessage('tenant-1', 'chat-1', 'plugin-image-1'), true);
});

test('configured reply image reuses the account-scoped platform upload cache', async () => {
  let downloads = 0; let uploads = 0; let images = 0;
  const executor = createActionExecutor({
    messageRegistry: registry(),
    imageLoader: { async load() { downloads += 1; return { bytes: new Uint8Array([1, 2, 3]), contentType: 'image/png' }; } },
    coreFor: () => ({ im: {
      async listMessages() { return { items: [{ direction: 'inbound', messageId: 'buyer-1' }] }; },
      async sendMessage() { return { messageId: `text-${downloads}-${images}` }; },
      async uploadImage() { uploads += 1; return { imageUrl: 'https://xianyu-cdn.example/cached.png', width: 800, height: 600 }; },
      async sendImage(input) { images += 1; assert.equal(input.imageUrl, 'https://xianyu-cdn.example/cached.png'); return { messageId: `image-${images}` }; },
    } }),
  });
  const base = { ...replyAction(), kind: 'reply_with_image', image_url: 'https://controlled.example/reply.png' };
  assert.equal((await executor.execute(base)).image_status, 'succeeded');
  assert.equal((await executor.execute({ ...base, action_id: 'action-2', chat_id: 'chat-2' })).image_status, 'succeeded');
  assert.equal(downloads, 1);
  assert.equal(uploads, 1);
  assert.equal(images, 2);
});

test('reply does not send a duplicate after its own prior message is observed', async () => {
  let sends = 0;
  const executor = createActionExecutor({
    messageRegistry: registry(['plugin-message-1']),
    coreFor: () => ({
      im: {
        listMessages: async () => ({ items: [{ direction: 'outbound', messageId: 'plugin-message-1' }] }),
        sendMessage: async () => { sends += 1; },
      },
    }),
  });

  const result = await executor.execute(replyAction());
  assert.deepEqual(result, { status: 'skipped', reason: 'duplicate_reply' });
  assert.equal(sends, 0);
});

test('a concurrent verified quote does not bypass reply dedupe without its own recognition reply', async () => {
  let sends = 0;
  const executor = createActionExecutor({
    messageRegistry: registry(['plugin-recognition-1']),
    coreFor: () => ({ im: { listMessages: async () => ({ items: [{ direction: 'outbound', messageId: 'plugin-recognition-1' }] }), sendMessage: async () => { sends += 1; } } }),
  });
  const result = await executor.execute({ ...replyAction(), reply_origin: 'verified_quote' });
  assert.deepEqual(result, { status: 'skipped', reason: 'duplicate_reply' });
  assert.equal(sends, 0);
});

test('verified quote follow-up never repeats the exact same plugin text', async () => {
  let sends = 0;
  const action = { ...replyAction(), reply_origin: 'quote_follow_up', allow_plugin_followup: true };
  const executor = createActionExecutor({
    messageRegistry: registry(['plugin-failure-1']),
    coreFor: () => ({ im: {
      listMessages: async () => ({ items: [{ direction: 'outbound', messageId: 'plugin-failure-1', content: action.text }] }),
      sendMessage: async () => { sends += 1; },
    } }),
  });
  assert.deepEqual(await executor.execute(action), { status: 'skipped', reason: 'duplicate_reply' });
  assert.equal(sends, 0);
});

test('bounded quote progress may send after an earlier message from the same plugin', async () => {
  let sends = 0;
  const executor = createActionExecutor({
    messageRegistry: registry(['plugin-first-contact']),
    coreFor: () => ({ im: {
      listMessages: async () => ({ items: [{ direction: 'outbound', messageId: 'plugin-first-contact' }] }),
      sendMessage: async () => { sends += 1; return { messageId: 'plugin-progress-1' }; },
    } }),
  });
  const result = await executor.execute({
    ...replyAction(),
    reply_origin: 'quote_processing',
    allow_plugin_followup: true,
  });
  assert.deepEqual(result, { status: 'succeeded', message_id: 'plugin-progress-1' });
  assert.equal(sends, 1);
});

test('verified quote follow-up may send after the recognition message from the same plugin', async () => {
  let sends = 0;
  const executor = createActionExecutor({
    messageRegistry: registry(['plugin-recognition-1']),
    coreFor: () => ({
      im: {
        listMessages: async () => ({ items: [{ direction: 'outbound', messageId: 'plugin-recognition-1' }] }),
        sendMessage: async () => { sends += 1; return { messageId: 'plugin-quote-1' }; },
      },
    }),
  });

  const result = await executor.execute({
    ...replyAction(),
    reply_origin: 'quote_follow_up',
    allow_plugin_followup: true,
  });
  assert.deepEqual(result, { status: 'succeeded', message_id: 'plugin-quote-1' });
  assert.equal(sends, 1);
});

test('order event reply resolves the existing Yumaiduo session instead of inventing chat identifiers', async () => {
  const sent = [];
  const executor = createActionExecutor({
    coreFor: () => ({
      im: {
        async getSessionByOrder(orderId) {
          assert.equal(orderId, 'order-1');
          return { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' };
        },
        async listMessages() { return { items: [] }; },
        async sendMessage(input) { sent.push(input); return { messageId: 'message-1' }; },
      },
    }),
    messageRegistry: registry(),
  });

  await executor.execute({
    action_id: 'reply-order-1',
    kind: 'reply',
    tenant_id: 'tenant-1',
    order_id: 'order-1',
    text: '已经改好价格，可以付款了',
  });
  assert.deepEqual(sent[0], {
    accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1', text: '已经改好价格，可以付款了',
  });
});

test('price change is submitted only after order state and gates pass', async () => {
  const calls = [];
  const executor = createActionExecutor({
    messageRegistry: registry(),
    coreFor: () => ({
      orders: {
        get: async () => ({ orderStatus: 1, accountUnb: 'shop-1', quantity: 2, payment: '9999' }),
        changePrice: async (orderId, body) => calls.push({ orderId, body }),
      },
    }),
  });

  const result = await executor.execute(priceAction());
  assert.equal(result.status, 'submitted');
  assert.deepEqual(calls, [{ orderId: 'order-1', body: { priceFee: 8080, transportFee: 0 } }]);
});

test('a one-unit listing may receive the verified multi-ticket quote total', async () => {
  const calls = [];
  const executor = createActionExecutor({
    messageRegistry: registry(),
    coreFor: () => ({ orders: { get: async () => ({ orderStatus: 1, accountUnb: 'shop-1', quantity: 1, payment: '2000' }), changePrice: async (orderId, body) => calls.push({ orderId, body }) } }),
  });
  const result = await executor.execute({ ...priceAction(), order_quantity_policy: 'listing_unit' });
  assert.equal(result.status, 'submitted');
  assert.deepEqual(calls, [{ orderId: 'order-1', body: { priceFee: 8080, transportFee: 0 } }]);
});

test('a just-created order retries CANNOT_MODIFY_FEE after an authoritative unpaid reread', async () => {
  let reads = 0;
  let writes = 0;
  const waits = [];
  const executor = createActionExecutor({
    messageRegistry: registry(),
    sleep: async (milliseconds) => { waits.push(milliseconds); },
    coreFor: () => ({ orders: {
      async get() { reads += 1; return { orderStatus: 1, accountUnb: 'shop-1', quantity: 2, payment: '2000', postFee: '0' }; },
      async changePrice() {
        writes += 1;
        if (writes === 1) throw Object.assign(new Error('not ready'), { status: 400, code: 'E_OAUTH_FLOW_FAILED', body: { message: 'CANNOT_MODIFY_FEE' } });
      },
    } }),
  });

  const result = await executor.execute(priceAction());

  assert.equal(result.status, 'submitted');
  assert.equal(writes, 2);
  assert.equal(reads, 2);
  assert.deepEqual(waits, [2000]);
});

test('a readiness retry stops if the authoritative order becomes paid', async () => {
  let reads = 0;
  let writes = 0;
  const executor = createActionExecutor({
    messageRegistry: registry(),
    sleep: async () => {},
    coreFor: () => ({ orders: {
      async get() { reads += 1; return { orderStatus: reads === 1 ? 1 : 2, accountUnb: 'shop-1', quantity: 2, payment: '2000', postFee: '0' }; },
      async changePrice() { writes += 1; throw Object.assign(new Error('not ready'), { status: 400, code: 'E_OAUTH_FLOW_FAILED', body: { message: 'CANNOT_MODIFY_FEE' } }); },
    } }),
  });

  const result = await executor.execute(priceAction());

  assert.equal(result.status, 'skipped');
  assert.equal(result.reason, 'price_change_gate_failed');
  assert.deepEqual(result.failures, ['order_not_unpaid']);
  assert.equal(writes, 1);
});

test('terminal price rejection retains only the safe preflight amount review', async () => {
  const upstream = Object.assign(new Error('rejected'), {
    status: 400,
    code: 'E_OAUTH_FLOW_FAILED',
    body: { message: 'CANNOT_MODIFY_FEE' },
  });
  const executor = createActionExecutor({
    messageRegistry: registry(),
    sleep: async () => {},
    coreFor: () => ({
      orders: {
        get: async () => ({ orderStatus: 1, accountUnb: 'shop-1', quantity: 1, payment: '2000', postFee: '0' }),
        changePrice: async () => { throw upstream; },
      },
    }),
  });

  await assert.rejects(executor.execute({ ...priceAction(), order_quantity_policy: 'listing_unit' }), (error) => {
    assert.equal(error, upstream);
    assert.deepEqual(error.priceChangeReview, {
      current_total_cents: 2000,
      target_total_cents: 8080,
      current_transport_cents: 0,
      direction: 'increase',
    });
    return true;
  });
});

test('price change stops when a human has replied after the plugin quote', async () => {
  let changed = false;
  const executor = createActionExecutor({
    messageRegistry: registry(),
    coreFor: () => ({
      im: {
        async getSessionByOrder() { return { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' }; },
        async listMessages() { return { items: [{ direction: 'outbound', messageId: 'human-2', sentAt: new Date().toISOString() }] }; },
      },
      orders: {
        async get() { return { orderStatus: 1, accountUnb: 'shop-1', quantity: 2, payment: '9999' }; },
        async changePrice() { changed = true; },
      },
    }),
  });
  const result = await executor.execute(priceAction());
  assert.equal(result.status, 'skipped');
  assert.deepEqual(result.failures, ['human_takeover']);
  assert.equal(changed, false);
});

test('price change is not blocked by an undated historical outbound message', async () => {
  let changed = false;
  const executor = createActionExecutor({
    messageRegistry: registry(),
    coreFor: () => ({
      im: {
        async getSessionByOrder() { return { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' }; },
        async listMessages() { return { items: [{ direction: 'outbound', messageId: 'legacy-human-message' }] }; },
      },
      orders: { async get() { return { orderStatus: 1, accountUnb: 'shop-1', quantity: 2, payment: '9999' }; }, async changePrice() { changed = true; } },
    }),
  });
  assert.equal((await executor.execute(priceAction())).status, 'submitted');
  assert.equal(changed, true);
});

test('unknown price change result is reconciled before any retry', async () => {
  let reads = 0;
  const executor = createActionExecutor({
    messageRegistry: registry(),
    coreFor: () => ({
      orders: {
        get: async () => ({ orderStatus: 1, accountUnb: 'shop-1', quantity: 2, payment: String(++reads === 1 ? 9999 : 8080) }),
        changePrice: async () => { throw new TypeError('fetch failed'); },
      },
    }),
  });

  const result = await executor.execute(priceAction());
  assert.equal(result.status, 'succeeded');
  assert.equal(result.reconciled, true);
});

test('unreconciled timeout is marked unknown instead of blindly retried', async () => {
  const executor = createActionExecutor({
    messageRegistry: registry(),
    coreFor: () => ({
      orders: {
        get: async () => ({ orderStatus: 1, accountUnb: 'shop-1', quantity: 2, payment: '9999' }),
        changePrice: async () => { throw new TypeError('fetch failed'); },
      },
    }),
  });

  await assert.rejects(executor.execute(priceAction()), UnknownActionResultError);
});

test('send image uploads bytes to Xianyu CDN before sending', async () => {
  const calls = [];
  const sent = registry();
  const executor = createActionExecutor({
    messageRegistry: sent,
    coreFor: () => ({
      im: {
        async listMessages() { return { items: [] }; },
        async uploadImage(input) {
          calls.push(['upload', input.accountUnb, input.filename, input.contentType, Buffer.byteLength(input.data)]);
          return { imageUrl: 'https://cdn.test/ticket.jpg', width: 640, height: 960 };
        },
        async sendImage(input) {
          calls.push(['send-image', input]);
          return { messageId: 'image-message-1' };
        },
      },
    }),
  });

  const result = await executor.execute({
    action_id: 'image-1',
    kind: 'send_image',
    tenant_id: 'tenant-1',
    account_unb: 'shop-1',
    chat_id: 'chat-1',
    peer_unb: 'buyer-1',
    image_base64: Buffer.from('jpg').toString('base64'),
    filename: 'ticket.jpg',
    content_type: 'image/jpeg',
  });
  assert.equal(result.status, 'succeeded');
  assert.equal(await sent.wasSentMessage('tenant-1', 'chat-1', 'image-message-1'), true);
  assert.equal(calls[0][0], 'upload');
  assert.equal(calls[1][0], 'send-image');
});

test('shipping, receipt reminders and buyer ratings are hard-disabled', async () => {
  const executor = createActionExecutor({ messageRegistry: registry(), coreFor: () => ({}) });
  for (const kind of ['ship_order', 'remind_receipt', 'rate_buyer', 'create_buyer_rate']) {
    await assert.rejects(
      executor.execute({ action_id: `${kind}-1`, kind, tenant_id: 'tenant-1', order_id: 'order-1' }),
      /unsupported backend action/,
    );
  }
});
