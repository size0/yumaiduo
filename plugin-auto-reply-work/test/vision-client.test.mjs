import assert from 'node:assert/strict';
import test from 'node:test';

import { VISION_PROMPT_VERSION, createVisionClient } from '../src/vision-client.mjs';

test('vision client validates structured seat classification', async () => {
  let requestBody;
  const client = createVisionClient({
    baseUrl: 'https://ai.example.com/v1',
    apiKey: 'test-key',
    model: 'vision-model',
    fetchImpl: async (_url, options) => {
      requestBody = JSON.parse(options.body);
      return new Response(JSON.stringify({
        choices: [{ message: { content: JSON.stringify({
          selection_mode: 'REGULAR_SELECTED',
          seat_names: ['6排7座', '6排6座'],
          selected_count: 2,
          has_annotation: false,
          annotation_bbox: null,
          confidence: 0.97,
          evidence: ['底部有两个原生座位卡片'],
        }) } }],
      }), { status: 200, headers: { 'content-type': 'application/json' } });
    },
  });

  const result = await client.classify({
    bytes: new Uint8Array([1, 2, 3]),
    contentType: 'image/png',
    buyerMessage: '这两个多少钱',
  });

  assert.equal(result.selectionMode, 'REGULAR_SELECTED');
  assert.deepEqual(result.seatNames, ['6排7座', '6排6座']);
  assert.equal(result.promptVersion, VISION_PROMPT_VERSION);
  assert.equal(requestBody.temperature, 0);
  assert.equal(requestBody.response_format.type, 'json_object');
});

test('vision client rejects non-image content before calling provider', async () => {
  let called = false;
  const client = createVisionClient({
    baseUrl: 'https://ai.example.com/v1',
    apiKey: 'test-key',
    model: 'vision-model',
    fetchImpl: async () => { called = true; },
  });

  await assert.rejects(
    client.classify({ bytes: new Uint8Array([1]), contentType: 'text/html' }),
    /only JPEG/,
  );
  assert.equal(called, false);
});

test('vision client rejects invalid JSON instead of guessing fields', async () => {
  const client = createVisionClient({
    baseUrl: 'https://ai.example.com/v1',
    apiKey: 'test-key',
    model: 'vision-model',
    fetchImpl: async () => new Response(JSON.stringify({
      choices: [{ message: { content: '大概是普通座' } }],
    }), { status: 200, headers: { 'content-type': 'application/json' } }),
  });

  await assert.rejects(
    client.classify({ bytes: new Uint8Array([1]), contentType: 'image/jpeg' }),
    /invalid JSON/,
  );
});

test('vision client accepts a no-selection response with selected_count zero', async () => {
  const client = createVisionClient({
    baseUrl: 'https://ai.example.com/v1', apiKey: 'test-key', model: 'vision-model',
    fetchImpl: async () => new Response(JSON.stringify({
      choices: [{ message: { content: JSON.stringify({
        selection_mode: 'NONE', seat_names: [], selected_count: 0,
        has_annotation: false, annotation_bbox: null, confidence: 0.99,
        evidence: ['未看到圈选或已选座卡片'],
      }) } }],
    }), { status: 200, headers: { 'content-type': 'application/json' } }),
  });
  const result = await client.classify({ bytes: new Uint8Array([1]), contentType: 'image/png' });
  assert.equal(result.selectionMode, 'NONE');
  assert.equal(result.selectedCount, null);
});
