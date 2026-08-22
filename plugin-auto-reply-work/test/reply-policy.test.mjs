import assert from 'node:assert/strict';
import test from 'node:test';

import {
  configuredReply,
  configuredReplyImage,
  createBuyerReplyAction,
  createQuoteFollowUpAction,
  withConfiguredReplyImage,
} from '../src/reply/reply-policy.mjs';

const envelope = {
  id: 'event-1', tenantId: '107',
  payload: { accountUnb: 'shop-1', chatId: 'chat-1', peerUnb: 'buyer-1' },
};

test('creates stable buyer reply actions from authoritative envelope addressing', () => {
  assert.deepEqual(createBuyerReplyAction(envelope, ' 请确认。 ', 'verified_quote', 'quote-reply'), {
    action_id: 'event-1:quote-reply', kind: 'reply', tenant_id: '107',
    account_unb: 'shop-1', chat_id: 'chat-1', peer_unb: 'buyer-1',
    text: '请确认。', reply_origin: 'verified_quote',
  });
});

test('fails closed for missing addresses and unresolved placeholders', () => {
  assert.equal(createBuyerReplyAction({ ...envelope, payload: {} }, '请确认', 'test'), null);
  assert.equal(createBuyerReplyAction(envelope, '价格是{价格}元', 'test'), null);
});

test('quote follow-up action retains its stable suffix and bounded plugin-followup permission', () => {
  const action = createQuoteFollowUpAction(envelope, '请补充张数。');
  assert.equal(action.action_id, 'event-1:quote-conversation-follow-up');
  assert.equal(action.reply_origin, 'quote_follow_up');
  assert.equal(action.allow_plugin_followup, true);
});

test('template and HTTPS image selection remain bounded', () => {
  const settings = {
    reply_templates: { quote_exact: '自定义报价' },
    reply_template_images: {
      quote_exact: 'https://cdn.example/reply.png',
      unsafe: 'http://127.0.0.1/reply.png',
    },
  };
  assert.equal(configuredReply(settings, 'quote_exact', '默认报价'), '自定义报价');
  assert.equal(configuredReply(settings, 'missing', '默认报价'), '默认报价');
  assert.equal(configuredReplyImage(settings, 'quote_exact'), 'https://cdn.example/reply.png');
  assert.equal(configuredReplyImage(settings, 'unsafe'), '');
  assert.deepEqual(withConfiguredReplyImage({ kind: 'reply' }, settings, 'quote_exact'), {
    kind: 'reply_with_image', image_url: 'https://cdn.example/reply.png',
  });
});
