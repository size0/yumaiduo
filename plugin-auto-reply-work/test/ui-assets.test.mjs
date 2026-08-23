import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);
const appPath = fileURLToPath(new URL('../ui/app.js', import.meta.url));
const htmlPath = fileURLToPath(new URL('../ui/index.html', import.meta.url));
const stylesPath = fileURLToPath(new URL('../ui/styles.css', import.meta.url));

test('dashboard consolidates settings and diagnostics without removing their controls', async () => {
  const html = await (await import('node:fs/promises')).readFile(htmlPath, 'utf8');
  const script = await (await import('node:fs/promises')).readFile(appPath, 'utf8');
  assert.match(html, /data-tab="ai-settings"/u);
  assert.match(html, /data-tab="automation"/u);
  assert.match(html, /data-tab="quote-records">报价记录/u);
  assert.match(html, /data-tab="order-management">订单管理/u);
  assert.match(html, /data-tab="diagnostics">异常记录/u);
  assert.match(html, /改价前后金额、上游拒绝原因和人工接管/u);
  assert.match(html, /可操作诊断/u);
  assert.doesNotMatch(html, /data-tab="knowledge-base"/u);
  assert.doesNotMatch(html, /data-tab="quote-preview"/u);
  assert.doesNotMatch(html, /data-panel="developer-tools"|图片测试|待报价预览|闲鱼改价金额核验/u);
  assert.doesNotMatch(script, /pendingQuotes|imageTest|priceChangeValidation|api\/recognition\/test|api\/quotes\/pending|api\/price-changes\/validate/u);
  assert.doesNotMatch(script, /quoteTasks:/u);
  assert.match(script, /operations:\s*'api\/operations'/u);
  assert.match(script, /ticketOrders:\s*'api\/orders'/u);
  assert.match(script, /function renderQuoteRecords/u);
  assert.match(script, /function renderOrderManagement/u);
  assert.match(script, /replyTemplateImages:\s*'api\/reply-template-images'/u);
  assert.match(script, /function uploadReplyTemplateImage/u);
  assert.match(script, /function settingsPatchMatches/u);
  assert.match(script, /const confirmed = await requestJson\(API\.settings\)/u);
  assert.match(script, /已保存，但页面状态刷新失败/u);
  assert.match(script, /已从后端复核/u);
  assert.match(script, /上传并保存图片/u);
  assert.match(script, /图片上传后立即保存/u);
  assert.match(html, /图片单独上传；文字统一保存/u);
  assert.match(html, /客服内容中心/u);
  assert.match(html, /id="quote-formula-summary"/u);
  assert.match(html, /报价数据采集中（不是AI自动学习）/u);
  assert.match(html, /场次结果统计与当前报价规则/u);
  assert.match(html, /影子会调用智能体分析真实对话/u);
  assert.match(html, /会话观察与经验提炼状态/u);
  assert.match(html, /Agent 上线前抽查/u);
  assert.match(html, /您认为应该怎么做/u);
  assert.match(script, /agentEvaluations:\s*'api\/agent-evaluations'/u);
  assert.match(script, /agentOfflineEvaluation:\s*'api\/agent-offline-evaluation'/u);
  assert.match(html, /id="agent-offline-evaluation-status"/u);
  assert.match(script, /agentHumanComparisons:\s*'api\/agent-human-comparisons'/u);
  assert.match(html, /id="human-comparison-body"/u);
  assert.match(script, /function renderAgentEvaluations/u);
  assert.match(html, /目前仍是影子模式/u);
  assert.match(html, /对应买家 \/ 这一轮/u);
  assert.match(html, /客服 Agent 接管状态/u);
  assert.doesNotMatch(html, /模型自主训练（不可开启）/u);
  assert.match(html, /id="agent-takeover-status"/u);
  assert.match(html, /value="active" disabled>接管：尚未达到上线门槛/u);
  assert.match(html, /保存成功后仍会回读后端逐字段复核/u);
  assert.match(html, /id="learning-observed-turns"/u);
  assert.match(script, /conversationLearningSummary:\s*'api\/conversation-learning-summary'/u);
  assert.match(script, /function renderConversationLearningSummary/u);
  assert.match(script, /影子观察不会发送AI生成的回复/u);
  assert.match(script, /这是后台当前设置的确定性规则，不是AI学习结果/u);
  assert.doesNotMatch(script, /learning_status|minimum_learning_samples/u);
  assert.doesNotMatch(html, /可用于安全学习|场次转化与报价公式/u);
  assert.doesNotMatch(script, /window\.confirm/u);
  assert.doesNotMatch(script, /button\.disabled = !record\.chat_id/u);
  assert.match(script, /PRICE_CHANGE_AUTHORIZATION_FAILED/u);
  assert.match(script, /已订阅服务.*重新授权.*订单改价权限/u);
  assert.match(html, /订单管理/u);
  assert.match(html, /闲鱼订单状态/u);
  assert.match(html, /出票状态/u);
  assert.match(html, /不会发送票码、不会调用平台发货/u);
  assert.match(script, /platform_order_status_text/u);
  assert.match(script, /platform_ticket_status/u);
  assert.match(html, /只有完成万达出票后才会在闲鱼点击发货/u);
  assert.doesNotMatch(script, /function updateFulfillment|function fulfillmentActions|api\/orders\/.*fulfillment/u);
  assert.doesNotMatch(html, /标记已出票|标记票码已发送|标记异常/u);
  assert.match(html, /id="ai-reply-system-prompt"/u);
  assert.match(html, /id="ai-reply-shop-background"/u);
  assert.match(html, /id="ai-reply-precautions"/u);
  assert.match(html, /id="ai-reply-style"/u);
  assert.match(html, /id="ai-reply-operations-title"/u);
  assert.match(html, /id="ai-reply-context-title"/u);
  assert.match(html, /id="ai-recognition-advanced"/u);
  assert.match(html, /id="knowledge-base-form"/u);
  assert.match(html, /会话经验草稿/u);
  assert.match(script, /会话提炼 · .*次一致证据/u);
  assert.doesNotMatch(html, /样本评测|图片审核工作台|panel-evaluation/u);
  assert.doesNotMatch(script, /evaluation-review|evaluation:/u);
  assert.match(script, /TAB_ALIASES/u);
  assert.match(script, /knowledgeBase:\s*'api\/knowledge-base'/u);
  assert.match(script, /wplus_seats_unavailable/u);
  assert.match(script, /quote_processing_notice/u);
  assert.match(script, /official_selection_unverifiable/u);
  assert.match(script, /function replyTemplateKeys/u);
  assert.match(script, /Object\.entries\(currentReplyTemplateDrafts\(\)\)/u);
  assert.match(script, /quote_count_completed/u);
  assert.match(script, /等待系统改价/u);
  assert.match(script, /非万达影院/u);
  assert.doesNotMatch(script, /本次仅核价、不锁座/u);
  assert.doesNotMatch(html, /待审核回复|pending-replies/u);
  assert.doesNotMatch(script, /pendingReplies|api\/replies\/pending/u);
});

test('dashboard exposes operational quote and order records without restoring a message-content conversation center', async () => {
  const html = await (await import('node:fs/promises')).readFile(htmlPath, 'utf8');
  const script = await (await import('node:fs/promises')).readFile(appPath, 'utf8');
  assert.doesNotMatch(html, /会话中心|data-tab="conversations"|panel-reply-preview|chat-session|chat-history/u);
  assert.doesNotMatch(script, /conversations|chatSessions|chatHistory|chatSimulation|api\/im\//u);
  assert.match(html, /id="quote-records-body"/u);
  assert.match(html, /id="ticket-orders-body"/u);
  assert.match(script, /api\/operations/u);
  assert.doesNotMatch(html, /operations-queue-body|operations-detail/u);
  assert.doesNotMatch(script, /refreshOperationsSilently/u);
});

test('dashboard does not describe a retired seat-lock workflow', async () => {
  const html = await (await import('node:fs/promises')).readFile(htmlPath, 'utf8');
  const script = await (await import('node:fs/promises')).readFile(appPath, 'utf8');
  assert.doesNotMatch(html, /临时锁座|释放锁座/u);
  assert.doesNotMatch(script, /临时锁座读取/u);
});

test('quote stages have distinct visual states', async () => {
  const styles = await (await import('node:fs/promises')).readFile(stylesPath, 'utf8');
  for (const stage of ['quoted', 'quote_confirmed', 'waiting_payment', 'paid_manual_delivery', 'ticket_issued', 'ticket_sent', 'quote_expired', 'exception_review']) {
    assert.match(styles, new RegExp(`\\.status-${stage}\\b`, 'u'));
  }
});

test('dashboard browser module has valid JavaScript syntax', async () => {
  const result = await execFileAsync(process.execPath, ['--check', appPath]);
  assert.equal(result.stderr, '');
});
