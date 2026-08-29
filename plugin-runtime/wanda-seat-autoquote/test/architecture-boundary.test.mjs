import assert from 'node:assert/strict';
import { readFile, readdir } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const root = dirname(dirname(fileURLToPath(import.meta.url)));

async function sourceFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const nested = await Promise.all(entries.map((entry) => {
    const path = join(directory, entry.name);
    return entry.isDirectory() ? sourceFiles(path) : [path];
  }));
  return nested.flat();
}

test('production runtime remains a platform executor with no old-plugin or AI business implementation', async () => {
  const paths = [join(root, 'index.mjs'), ...(await sourceFiles(join(root, 'src')))].filter((path) => path.endsWith('.mjs'));
  const source = (await Promise.all(paths.map((path) => readFile(path, 'utf8')))).join('\n').toLowerCase();
  const forbidden = [
    '闲鱼插件旧版归档',
    '万达报价插件',
    'ticket-system-local',
    '_ticket_reply',
    'plugin_bridge',
    'wplus_adjustment_cents',
    'regular_adjustment_cents',
    'ai_api_key',
    'vision_model',
    'prompt_version',
    'knowledge_base',
  ];
  for (const marker of forbidden) assert.equal(source.includes(marker), false, `forbidden production marker: ${marker}`);
});

test('production event path accepts durable inbox receipts and executes only claimed commands', async () => {
  const processor = await readFile(join(root, 'src', 'runtime', 'event-processor.mjs'), 'utf8');
  const client = await readFile(join(root, 'src', 'backend', 'client.mjs'), 'utf8');
  assert.match(processor, /accepted\?\.accepted !== true/u);
  assert.match(processor, /backend\.claimCommands/u);
  assert.match(processor, /backend\.reportCommand/u);
  assert.doesNotMatch(processor, /normalizeDecision|reportAction|decision\?\.actions|shadow/iu);
  assert.match(client, /plugin\/commands\/claim/u);
  assert.match(client, /plugin\/commands\/\$\{encodeURIComponent\(commandId\)\}\/result/u);
  assert.doesNotMatch(client, /actions\/result|reportAction/iu);
});

test('manifest keeps the plugin id and declares the documented FishMore panel contract', async () => {
  const manifest = JSON.parse(await readFile(join(root, 'yumaiduo.plugin.json'), 'utf8'));
  assert.equal(manifest.id, 'wanda-seat-autoquote');
  assert.equal(manifest.entrypoint.web, '/ui');
  assert.equal(manifest.entrypoint.crossOriginUi, true);
  assert.equal(manifest.permissions.includes('order.write.price_change'), true);
  assert.equal(manifest.permissions.includes('order.write.ship'), true);
  assert.equal(manifest.permissions.includes('order.rate.write'), false);
  assert.equal(manifest.extensionPoints.subscribes.includes('order.rated'), false);
  assert.equal(manifest.extensionPoints.subscribes.includes('order.logistics.changed'), false);
  assert.deepEqual(manifest.extensionPoints.menu, [{
    section: 'plugins',
    label: '万达AI客服',
    title: '万达AI客服 V4',
    to: '/console/plugins/wanda-seat-autoquote/dashboard',
    icon: 'ticket',
  }]);
  assert.deepEqual(manifest.extensionPoints.routes, [{
    path: '/console/plugins/wanda-seat-autoquote/dashboard',
    title: '万达AI客服 V4',
    component: '/ui',
    entry: '/ui',
  }]);
});

test('package allowlist includes the production panel but excludes local state and development material', async () => {
  const packageJson = JSON.parse(await readFile(join(root, 'package.json'), 'utf8'));
  const files = packageJson.files.join('\n');
  for (const required of ['ui/index.html', 'ui/styles.css', 'ui/app.js', 'ui/sdk.js']) {
    assert.equal(files.includes(required), true, `release allowlist is missing ${required}`);
  }
  for (const marker of ['data', 'datasets', 'evaluation', 'tools', 'test', '图片学习样本']) {
    assert.equal(files.includes(marker), false, `release allowlist contains ${marker}`);
  }
});

test('panel follows the FishMore iframe SDK and CSP rules', async () => {
  const html = await readFile(join(root, 'ui', 'index.html'), 'utf8');
  const app = await readFile(join(root, 'ui', 'app.js'), 'utf8');
  const styles = await readFile(join(root, 'ui', 'styles.css'), 'utf8');
  assert.match(html, /<link rel="stylesheet" href="\.\/ui\/styles\.css">/u);
  assert.match(html, /<script type="module" src="\.\/ui\/app\.js"><\/script>/u);
  assert.doesNotMatch(html, /<script(?![^>]*\bsrc=)[^>]*>/iu);
  assert.doesNotMatch(html, /\son[a-z]+\s*=/iu);
  assert.match(app, /createPluginSdk\(\)/u);
  assert.match(app, /fishMoreSdk\.ready\(\)/u);
  assert.match(app, /fishMoreSdk\.authedFetch\(`ui\/v4\/\$\{normalized\}`/u);
  assert.match(app, /serializeFormData/u);
  assert.match(app, /maxSourceBytes=68_000/u);
  assert.match(app, /image\/webp/u);
  assert.match(app, /copyTemplateVariable/u);
  assert.match(app, /document\.execCommand\('copy'\)/u);
  assert.match(app, /v4ImageFetch/u);
  assert.match(app, /ui\/v4\/jobs\/image-message/u);
  assert.match(app, /wanda_release_verification:'判定座位释放结果'/u);
  assert.match(app, /目标座位仍不可选/u);
  assert.match(styles, /--nav-accent:#20c7c7/iu);
  assert.match(html, /data-workspace="chat"/u);
  assert.match(html, /data-workspace="shops"/u);
  assert.match(html, /data-workspace="templates"/u);
  assert.match(html, /data-workspace="conversation"/u);
  assert.match(html, /id="memoryHours"/u);
  assert.match(html, /id="memoryDepth"/u);
  assert.match(html, /id="humanTakeoverDelay"/u);
  assert.match(html, /id="aiReplyEnabled"/u);
  assert.match(html, /AI 自动回复/u);
  assert.match(app, /\$\('aiReplyEnabled'\)\.checked=data\.ai_reply_enabled!==false/u);
  assert.match(app, /ai_reply_enabled:\$\('aiReplyEnabled'\)\.checked/u);
  assert.match(html, /自动回复文案/u);
  assert.match(html, /data-template-filter="recognition-quote-flow"/u);
  assert.match(html, /data-template-filter="order-payment-flow"/u);
  assert.match(html, /data-template-filter="fulfillment-flow"/u);
  assert.match(html, /data-template-filter="other-flow"/u);
  assert.match(html, /识别报价文案/u);
  assert.match(html, /拍下付款文案/u);
  assert.match(html, /出票结果文案/u);
  assert.match(html, /其他文案/u);
  assert.doesNotMatch(html, /识别等待文案（停用）/u);
  assert.match(html, /id="recognitionFailureOtherTemplate"/u);
  assert.doesNotMatch(html, /id="quoteAboveFanPriceTemplate"/u);
  assert.match(html, /id="paymentSuccessPendingTicketTemplate"/u);
  assert.doesNotMatch(html, /id="orderPendingWithQuoteTemplate"/u);
  assert.match(html, /id="orderPendingWithoutQuoteTemplate"/u);
  assert.doesNotMatch(html, /拍下后识别成功重新改价（停用）/u);
  assert.match(html, /id="priceChangeFailureTemplate"/u);
  assert.match(html, /id="quoteQuantityOrderGuidanceTemplate"/u);
  assert.match(html, /id="quoteQuantityMarkedSeatTemplate"/u);
  assert.match(html, /id="quoteQuantityFlexibleSeatTemplate"/u);
  assert.match(html, /id="quoteQuantityDefaultSeatTemplate"/u);
  assert.doesNotMatch(html, /确认后下单引导/u);
  assert.doesNotMatch(html, /id="pendingOrderImageQuoteUnavailableTemplate"/u);
  assert.match(app, /quote_quantity_order_guidance_template:'quoteQuantityOrderGuidanceTemplate'/u);
  assert.match(app, /quote_quantity_default_seat_template:'quoteQuantityDefaultSeatTemplate'/u);
  assert.doesNotMatch(app, /order_pending_with_quote_template:/u);
  assert.doesNotMatch(app, /recognition_waiting_template:/u);
  assert.doesNotMatch(app, /post_order_recognition_reprice_template:/u);
  assert.doesNotMatch(app, /order_submit_unpaid_template:/u);
  assert.doesNotMatch(app, /pending_order_image_quote_unavailable_template:/u);
  assert.doesNotMatch(html, /id="orderDetectedHoldPaymentTemplate"/u);
  assert.match(html, /id="paymentManualReviewTemplate"/u);
  assert.match(html, /id="orderBeforeQuoteConfirmationTemplate"/u);
  assert.match(html, /data-template-variable="\{城市\}"/u);
  assert.match(html, /data-template-variable="\{订单金额\}"/u);
  assert.match(styles, /\.template-section\[hidden\]\s*\{\s*display:none/u);
  assert.match(styles, /\.template-card textarea[^}]*font-size:14px/u);
  assert.match(html, /data-workspace="pricing"/u);
  assert.match(html, /data-workspace="model"/u);
});
