import assert from 'node:assert/strict';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { loadV2Config } from '../src/config.mjs';
import { createV2Runtime, isExternalOperatorMessage, isKnownSelfPluginMessage } from '../src/runtime/event-processor.mjs';
import { V2EventStore } from '../src/runtime/event-store.mjs';
import manifest from '../yumaiduo.plugin.json' with { type: 'json' };

const silent = { debug() {}, info() {}, warn() {}, error() {} };
const env = (key, value) => ({
  CORE_URL: 'https://core.test', PLUGIN_DEVELOPER_TOKEN: 'developer', PLUGIN_BASE_URL: 'http://plugin.test:4003',
  WANDA_AI_V2_BACKEND_URL: 'http://backend.test', WANDA_AI_V2_BRIDGE_KEY: 'bridge',
  CONFIG_ENCRYPTION_KEY: Buffer.alloc(32, 7).toString('base64'), [key]: value,
});

function command(eventId, action) {
  return {
    command_id: `cmd-${eventId}`, lease_token: `lease-${eventId}`, tenant_id: 'tenant-1', event_id: eventId,
    action, context: {
      envelope: { id: eventId, tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: {} },
      session: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' }, recent_messages: [],
    },
  };
}

async function runWithConfig(config) {
  const dataDir = await mkdtemp(join(tmpdir(), 'wanda-write-gate-'));
  const calls = { sent: 0, reports: [] };
  const client = {
    shops: { list: async () => [] },
    im: {
      listMessages: async () => ({ items: [] }),
      sendMessage: async () => { calls.sent += 1; return { messageId: 'sent-1' }; },
    },
  };
  let offered = false;
  let processed = false;
  let claimed = false;
  const eventId = 'gate-event';
  const backend = {
    processEvent: async () => { processed = true; return { accepted: true, event_id: eventId }; },
    claimCommands: async () => {
      if (!processed || claimed) return { commands: [] };
      claimed = true;
      return { commands: [command(eventId, { id: 'reply', type: 'send_message', text: 'reply' })] };
    },
    reportCommand: async ({ result }) => { offered = false; calls.reports.push(result); },
  };
  const runtime = createV2Runtime({
    config: { ...config, dataDir }, platform: { createClient: () => client }, backend, logger: silent,
    store: new V2EventStore(join(dataDir, 'events.v2.json'), config.encryptionKey),
  });
  await runtime.start();
  await runtime.enqueue({ id: eventId, tenantId: 'tenant-1', event: 'im.message.received', ts: Date.now(), payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' } });
  for (let i = 0; i < 50 && !calls.reports.length; i += 1) await new Promise((resolve) => setTimeout(resolve, 10));
  await runtime.stop();
  return calls;
}

test('ordinary unknown outbound messages are external operators while known self messages are safe', () => {
  const sentIds = new Set(['self-1']);
  assert.equal(isExternalOperatorMessage({ id: 'human-1', direction: 'outbound', messageType: 1, content: '人工回复' }, sentIds), true);
  assert.equal(isExternalOperatorMessage({ id: 'self-1', direction: 'outbound', messageType: 1, content: 'AI回复' }, sentIds), false);
  assert.equal(isKnownSelfPluginMessage({ id: 'self-1', direction: 'outbound' }, sentIds), true);
  assert.equal(isExternalOperatorMessage({ id: 'card-1', direction: 'outbound', messageType: 26, content: '交易卡片' }, sentIds), false);
});

test('production config defaults every external action permit closed', () => {
  const config = loadV2Config({ env: env('EXTERNAL_WRITES_ENABLED', 'true'), manifest });
  assert.equal(config.externalWritesEnabled, true);
  assert.equal(config.messageSendEnabled, false);
  assert.equal(config.xianyuRepriceEnabled, false);
  assert.equal(config.refundEnabled, false);
  assert.equal(config.shipEnabled, false);
});

test('plugin final executor blocks message when its narrow permit is closed', async () => {
  const config = loadV2Config({ env: {
    ...env('EXTERNAL_WRITES_ENABLED', 'true'), MESSAGE_SEND_ENABLED: 'false',
  }, manifest });
  const calls = await runWithConfig(config);
  assert.equal(calls.sent, 0);
  assert.equal(calls.reports[0]?.status, 'skipped');
  assert.equal(calls.reports[0]?.reason, 'message_send_disabled');
});

test('plugin final executor permits only message when global and message gates are open', async () => {
  const config = loadV2Config({ env: {
    ...env('EXTERNAL_WRITES_ENABLED', 'true'), MESSAGE_SEND_ENABLED: 'true',
  }, manifest });
  const calls = await runWithConfig(config);
  assert.equal(calls.sent, 1);
  assert.equal(calls.reports[0]?.status, 'succeeded');
});
