import assert from 'node:assert/strict';
import test from 'node:test';
import { createImageLoader } from '../src/image-loader.mjs';

test('image loader accepts only allowed public HTTPS image responses', async () => {
  const loader = createImageLoader({
    allowlist: ['*.alicdn.com'],
    lookupImpl: async () => [{ address: '203.0.113.10', family: 4 }],
    fetchImpl: async () => new Response(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), {
      status: 200,
      headers: { 'content-type': 'image/png', 'content-length': '8' },
    }),
  });
  const image = await loader.load('https://img.alicdn.com/example.png');
  assert.equal(image.contentType, 'image/png');
  assert.deepEqual([...image.bytes], [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
});

test('image loader rejects spoofed content types', async () => {
  const loader = createImageLoader({
    allowlist: ['*.alicdn.com'],
    lookupImpl: async () => [{ address: '203.0.113.10', family: 4 }],
    fetchImpl: async () => new Response(Buffer.from('not an image'), {
      status: 200,
      headers: { 'content-type': 'image/png' },
    }),
  });
  await assert.rejects(loader.load('https://img.alicdn.com/example.png'), /do not match/u);
});

test('image loader rejects untrusted hosts and private DNS resolutions', async () => {
  const fetchImpl = async () => { throw new Error('must not fetch'); };
  const untrusted = createImageLoader({
    allowlist: ['*.alicdn.com'],
    fetchImpl,
    lookupImpl: async () => [{ address: '203.0.113.10', family: 4 }],
  });
  await assert.rejects(untrusted.load('https://example.com/image.png'), /not allowed/u);

  const privateHost = createImageLoader({
    allowlist: ['*.alicdn.com'],
    fetchImpl,
    lookupImpl: async () => [{ address: '127.0.0.1', family: 4 }],
  });
  await assert.rejects(privateHost.load('https://img.alicdn.com/image.png'), /private/u);
});
