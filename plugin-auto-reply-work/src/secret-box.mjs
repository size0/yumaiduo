import { createCipheriv, createDecipheriv, randomBytes } from 'node:crypto';

export function encryptText(plaintext, key) {
  assertKey(key);
  const iv = randomBytes(12);
  const cipher = createCipheriv('aes-256-gcm', key, iv);
  const ciphertext = Buffer.concat([cipher.update(String(plaintext), 'utf8'), cipher.final()]);
  return {
    version: 1,
    iv: iv.toString('base64'),
    tag: cipher.getAuthTag().toString('base64'),
    ciphertext: ciphertext.toString('base64'),
  };
}

export function decryptText(value, key) {
  assertKey(key);
  if (value?.version !== 1) throw new Error('unsupported encrypted secret');
  const decipher = createDecipheriv('aes-256-gcm', key, Buffer.from(value.iv, 'base64'));
  decipher.setAuthTag(Buffer.from(value.tag, 'base64'));
  return Buffer.concat([
    decipher.update(Buffer.from(value.ciphertext, 'base64')),
    decipher.final(),
  ]).toString('utf8');
}

function assertKey(key) {
  if (!(key instanceof Uint8Array) || key.byteLength !== 32) {
    throw new Error('a 32-byte CONFIG_ENCRYPTION_KEY is required');
  }
}
