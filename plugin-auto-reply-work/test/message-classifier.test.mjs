import assert from 'node:assert/strict';
import test from 'node:test';

import {
  isBareAcknowledgement,
  isCurrentQuoteQuestion,
  isExplicitTypedSeatChoice,
  isQuoteConfirmation,
  isQuotePurchaseIntent,
  isSeatClarificationQuestion,
} from '../src/conversation/message-classifier.mjs';

test('recognizes bounded current-quote references without treating arbitrary price text as one', () => {
  for (const text of ['多少钱', '这个呢？', '不是53？', '会员价可以优惠吗', '直接拍下是吧']) {
    assert.equal(isCurrentQuoteQuestion(text), true, text);
  }
  for (const text of ['深圳万达多少钱', '换成明天多少钱', '截图上写53']) {
    assert.equal(isCurrentQuoteQuestion(text), false, text);
  }
});

test('keeps acknowledgements and purchase intents distinct while allowing active-quote confirmation', () => {
  assert.equal(isBareAcknowledgement('好的。'), true);
  assert.equal(isBareAcknowledgement('好的，2张'), false);
  assert.equal(isQuotePurchaseIntent('已拍下，待付款'), true);
  assert.equal(isQuotePurchaseIntent('我想先看看'), false);
  assert.equal(isQuoteConfirmation('确认'), true);
  assert.equal(isQuoteConfirmation('已拍下'), true);
  assert.equal(isQuoteConfirmation('不确认'), false);
});

test('separates typed seat preferences from seat clarification questions', () => {
  assert.equal(isExplicitTypedSeatChoice('10排7座'), true);
  assert.equal(isExplicitTypedSeatChoice('10排 7 8 座'), true);
  assert.equal(isExplicitTypedSeatChoice('是10排7座吗？'), false);
  assert.equal(isSeatClarificationQuestion('是中间位置吗？'), true);
  assert.equal(isSeatClarificationQuestion('10排7座'), false);
});
