import assert from 'node:assert/strict';
import { access } from 'node:fs/promises';
import http from 'node:http';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright-core';
import { createUiHandler } from '../src/ui-handler.mjs';

const defaultBrowser = process.platform === 'win32'
  ? 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'
  : '/usr/bin/chromium';

test('operator sees safety state, prioritized work and sample blockers on desktop and mobile', async (t) => {
  const executablePath = process.env.PLAYWRIGHT_EXECUTABLE_PATH || defaultBrowser;
  await access(executablePath);
  const handler = createUiHandler({
    config: {
      projectRoot: fileURLToPath(new URL('..', import.meta.url)), coreUrl: 'https://core.example.com',
      maxUiBodyBytes: 64 * 1024, allowLocalUiBypass: true, manifest: { id: 'wanda-seat-autoquote' },
    },
    platformRuntime: { verifyGateway() { return false; } },
    api: {
      async overview() { return { plugin: { status: 'running' } }; },
      async getSettings() {
        return {
          automation_enabled: true, recognition_enabled: true, quote_enabled: false, price_change_enabled: true,
          ai_reply_enabled: false, shadow_evaluation_enabled: true, conversation_agent_mode: 'shadow',
        };
      },
      async listOperations() {
        return [
          { event_id: 'event-paid', buyer_label: '买家A', stage: 'exception_review', exception_reason: 'paid_amount_mismatch', order_id: 'order-1', updated_at: '2026-08-23T07:01:00Z' },
          { event_id: 'event-quoted', buyer_label: '买家B', stage: 'quoted', next_action: '等待买家确认报价', updated_at: '2026-08-23T07:00:00Z' },
        ];
      },
      async listTicketOrders() { return [{ order_id: 'order-1', buyer_label: '买家A', stage: 'exception_review', exception_reason: 'paid_amount_mismatch', updated_at: Date.now() }]; },
      async listManualTasks() { return [{ task_id: 'manual-1', buyer_label: '买家C', status: 'open', priority: 'high', summary: '人工核价', updated_at: '2026-08-23T07:02:00Z' }]; },
      async getAgentCanaryReadiness() { return { runtime_version: 'runtime-v33', audited_sample_count: 91, minimum_sample_count: 100, tool_selection_accuracy: 92.3, blockers: ['insufficient_audited_samples', 'tool_selection_accuracy_below_95'] }; },
      async getAgentOfflineEvaluation() { return { runtime_version: 'runtime-v33', sample_count: 32, minimum_sample_count: 100, full_path_pass_rate: 85.7, blockers: ['insufficient_image_samples', 'full_path_pass_rate_below_95'] }; },
      async listAgentHumanComparisons() { return [{ review: { status: 'unreviewed' } }, { review: { status: 'reviewed' } }]; },
    },
    logger: { error() {} },
  });
  const server = http.createServer((req, res) => void handler(req, res, new URL(req.url, 'http://localhost').pathname));
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const browser = await chromium.launch({ executablePath, headless: true });
  t.after(() => browser.close());
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  await page.goto(`http://127.0.0.1:${server.address().port}/ui/workbench`);
  await page.getByText('本地预览').waitFor();

  assert.equal(await page.getByText('自动报价').locator('..').getByText('已关闭').count(), 1);
  assert.equal(await page.getByText('待付款改价').locator('..').getByText('已开启').count(), 1);
  assert.equal(await page.locator('#urgent-count').textContent(), '2');
  assert.match(await page.locator('#sample-cards').textContent(), /91\s*\/\s*100/u);
  assert.match(await page.locator('#sample-cards').textContent(), /32\s*\/\s*100/u);
  assert.match(await page.locator('#blocker-list').textContent(), /工具选择正确率低于95%/u);
  assert.match(await page.locator('#action-queue').textContent(), /实付金额与确认金额不一致/u);
  assert.equal(await page.locator('.queue-item').count(), 2);

  await page.getByRole('button', { name: '人工任务' }).click();
  assert.equal(await page.locator('.queue-item').count(), 1);
  assert.match(await page.locator('#action-queue').textContent(), /人工核价/u);

  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
  await page.screenshot({ path: '../.tmp/workbench-e2e.png', fullPage: true });
});
