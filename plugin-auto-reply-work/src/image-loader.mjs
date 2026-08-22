import { lookup } from 'node:dns/promises';
import { isIP } from 'node:net';

const MAX_IMAGE_BYTES = 8 * 1024 * 1024;
const ALLOWED_TYPES = new Set(['image/jpeg', 'image/png', 'image/webp']);

export function createImageLoader({ allowlist, fetchImpl = fetch, lookupImpl = lookup, timeoutMs = 15_000 }) {
  if (!Array.isArray(allowlist) || allowlist.length === 0) throw new TypeError('image host allowlist is required');
  if (typeof fetchImpl !== 'function') throw new TypeError('fetch implementation is required');

  async function load(sourceUrl) {
    let url = validateUrl(sourceUrl, allowlist);
    for (let redirects = 0; redirects <= 3; redirects += 1) {
      await rejectPrivateResolution(url.hostname, lookupImpl);
      const response = await fetchImpl(url, {
        method: 'GET',
        redirect: 'manual',
        headers: {
          accept: 'image/jpeg,image/png,image/webp',
          'user-agent': 'wanda-plugin-image-fetch/1.0',
        },
        signal: AbortSignal.timeout(timeoutMs),
      });
      if ([301, 302, 303, 307, 308].includes(response.status)) {
        if (redirects === 3) throw new Error('image redirect limit exceeded');
        const location = response.headers.get('location');
        if (!location) throw new Error('image redirect is missing location');
        url = validateUrl(new URL(location, url).toString(), allowlist);
        continue;
      }
      if (!response.ok) throw new Error(`image download failed with HTTP ${response.status}`);
      const contentType = String(response.headers.get('content-type') ?? '').split(';')[0].trim().toLowerCase();
      if (!ALLOWED_TYPES.has(contentType)) throw new TypeError('image response has an unsupported content type');
      const declaredLength = Number(response.headers.get('content-length'));
      if (Number.isFinite(declaredLength) && declaredLength > MAX_IMAGE_BYTES) {
        throw new RangeError('image response is too large');
      }
      const bytes = await readLimitedBody(response.body, MAX_IMAGE_BYTES);
      if (!matchesImageSignature(bytes, contentType)) throw new TypeError('image bytes do not match the declared content type');
      return Object.freeze({ bytes, contentType, sourceUrl: url.toString() });
    }
    throw new Error('image download failed');
  }

  return Object.freeze({ load });
}

function validateUrl(value, allowlist) {
  const text = String(value ?? '').trim();
  if (!text || text.length > 2_048) throw new TypeError('image URL is invalid');
  const url = new URL(text);
  if (url.protocol !== 'https:' || url.username || url.password || url.port) {
    throw new TypeError('image URL must be credential-free HTTPS on the default port');
  }
  const hostname = url.hostname.toLowerCase().replace(/\.$/u, '');
  if (!allowlist.some((rule) => hostMatches(hostname, rule))) {
    throw new TypeError('image host is not allowed');
  }
  url.hash = '';
  return url;
}

function hostMatches(hostname, rule) {
  const normalized = String(rule).toLowerCase();
  if (!normalized.startsWith('*.')) return hostname === normalized;
  const suffix = normalized.slice(1);
  return hostname.endsWith(suffix) && hostname.length > suffix.length;
}

async function rejectPrivateResolution(hostname, lookupImpl) {
  if (isIP(hostname) && isPrivateAddress(hostname)) throw new TypeError('private image address is not allowed');
  const addresses = await lookupImpl(hostname, { all: true, verbatim: true });
  if (!addresses.length || addresses.some((entry) => isPrivateAddress(entry.address))) {
    throw new TypeError('private image address is not allowed');
  }
}

function isPrivateAddress(address) {
  const value = String(address).toLowerCase();
  if (value.includes(':')) {
    if (value.startsWith('::ffff:') && value.slice(7).includes('.')) return isPrivateAddress(value.slice(7));
    return value === '::1'
      || value === '::'
      || value.startsWith('fc')
      || value.startsWith('fd')
      || value.startsWith('fe8')
      || value.startsWith('fe9')
      || value.startsWith('fea')
      || value.startsWith('feb')
      || value.startsWith('::ffff:127.')
      || value.startsWith('::ffff:10.')
      || value.startsWith('::ffff:192.168.');
  }
  const octets = value.split('.').map(Number);
  if (octets.length !== 4 || octets.some((part) => !Number.isInteger(part))) return true;
  return octets[0] === 0
    || octets[0] === 10
    || octets[0] === 127
    || (octets[0] === 169 && octets[1] === 254)
    || (octets[0] === 172 && octets[1] >= 16 && octets[1] <= 31)
    || (octets[0] === 192 && octets[1] === 168)
    || octets[0] >= 224;
}

function matchesImageSignature(bytes, contentType) {
  if (contentType === 'image/png') {
    return bytes.length >= 8 && [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]
      .every((value, index) => bytes[index] === value);
  }
  if (contentType === 'image/jpeg') {
    return bytes.length >= 3 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff;
  }
  return bytes.length >= 12
    && String.fromCharCode(...bytes.slice(0, 4)) === 'RIFF'
    && String.fromCharCode(...bytes.slice(8, 12)) === 'WEBP';
}

async function readLimitedBody(stream, limit) {
  if (!stream) throw new Error('image response body is empty');
  const chunks = [];
  let total = 0;
  for await (const chunk of stream) {
    total += chunk.length;
    if (total > limit) throw new RangeError('image response is too large');
    chunks.push(chunk);
  }
  if (total === 0) throw new Error('image response body is empty');
  return new Uint8Array(Buffer.concat(chunks));
}
