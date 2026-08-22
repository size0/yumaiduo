import { normalizeAiShadowAdvisory } from './ai-shadow-advisory-schema.mjs';

/** Extract and validate Dify's advisory-only blocking Workflow result. */
export function validateDifyShadowResponse(value) {
  let outputs = value;
  if (value?.data && typeof value.data === 'object' && !Array.isArray(value.data)) {
    if (value.data.status !== 'succeeded') throw new TypeError('Dify workflow did not succeed');
    outputs = value.data.outputs;
  }
  return normalizeAiShadowAdvisory(outputs);
}
