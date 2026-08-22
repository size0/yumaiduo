const IMAGE_URL = /https?:\/\/\S+\.(?:jpe?g|png|webp|gif)(?:\?\S*)?$/iu;
const REQUIRED_QUOTE_PATH = ['recognize_image', 'resolve_showtime', 'quote_realtime'];
const HIGH_RISK_TOOLS = new Set(['request_price_change', 'change_price', 'create_order', 'pay', 'issue_ticket', 'refund', 'ship']);
const UNRESOLVED_TEMPLATE_PLACEHOLDER = /(?:\{[^{}\r\n]{1,40}\}|｛[^｛｝\r\n]{1,40}｝|\$\{[^{}\r\n]{1,40}\})/u;

function hasImage(event) {
  const payload = event?.envelope?.payload ?? {};
  return (Array.isArray(payload.imageUrls) && payload.imageUrls.some((value) => typeof value === 'string' && value.trim()))
    || IMAGE_URL.test(String(payload.content ?? payload.text ?? ''));
}
function expectedOutcome(event) {
  if (event?.result?.preview_status === 'preview_ready') return 'quote_succeeded';
  if (event?.result?.quote_failure_code) return 'quote_failed';
  return 'not_available';
}
function percentile(values, ratio) {
  if (!values.length) return null;
  const sorted = [...values].sort((left, right) => left - right);
  return sorted[Math.max(0, Math.ceil(sorted.length * ratio) - 1)];
}
function percentage(numerator, denominator) {
  return denominator ? Number(((numerator / denominator) * 100).toFixed(1)) : null;
}
function orderedPathExists(tools, required) {
  let cursor = 0;
  for (const tool of tools) if (tool === required[cursor]) cursor += 1;
  return cursor === required.length;
}

export function agentImageOfflineEvaluationFrom(runs, events, { minimumSamples = 100 } = {}) {
  const minimum = Number.isSafeInteger(Number(minimumSamples)) && Number(minimumSamples) > 0 ? Number(minimumSamples) : 100;
  const imageEvents = new Map((Array.isArray(events) ? events : []).filter(hasImage).map((event) => [String(event.key), event]));
  const isDurableImageSample = (run) => imageEvents.has(String(run?.event_key)) || run?.result?.source_snapshot?.has_image === true;
  const sourceOutcome = (run) => {
    const source = imageEvents.get(String(run?.event_key));
    if (source) return expectedOutcome(source);
    const durable = String(run?.result?.source_snapshot?.authoritative_outcome ?? '');
    return ['quote_succeeded', 'quote_failed', 'not_available'].includes(durable) ? durable : 'not_available';
  };
  const uniqueSamples = new Map();
  for (const run of (Array.isArray(runs) ? runs : [])) {
    if (!['shadow', 'evaluation'].includes(run?.mode) || !isDurableImageSample(run)) continue;
    const key = String(run.event_key);
    const previous = uniqueSamples.get(key);
    if (!previous || String(run.updated_at ?? '') > String(previous.updated_at ?? '')) uniqueSamples.set(key, run);
  }
  const samples = [...uniqueSamples.values()];
  let completed = 0; let fullPathPass = 0; let duplicateQuote = 0; let unknownTool = 0; let highRisk = 0; let mismatch = 0; let prerequisiteReplans = 0; let safeReplies = 0; let missingOrUnsafeReplies = 0;
  const latencies = [];
  for (const run of samples) {
    const calls = Array.isArray(run.tool_calls) ? run.tool_calls : [];
    const tools = calls.map((call) => String(call?.tool ?? ''));
    const quoteCalls = calls.filter((call) => call?.tool === 'quote_realtime');
    const traceActions = Array.isArray(run?.result?.trace) ? run.result.trace.map((item) => String(item?.action ?? '')) : [];
    const firstQuotePlan = traceActions.indexOf('quote_realtime'); const firstResolvePlan = traceActions.indexOf('resolve_showtime');
    if (firstQuotePlan >= 0 && (firstResolvePlan < 0 || firstQuotePlan < firstResolvePlan)) prerequisiteReplans += 1;
    const successfulSource = sourceOutcome(run) === 'quote_succeeded';
    const proposedReply = String(run?.result?.proposed_reply ?? '').trim();
    const safeFinalReply = run?.result?.status === 'reply'
      && run?.result?.reply_generated === true
      && run?.result?.authoritative_reply_used === true
      && proposedReply.length > 0
      && !UNRESOLVED_TEMPLATE_PLACEHOLDER.test(proposedReply);
    if (successfulSource && safeFinalReply) safeReplies += 1;
    if (successfulSource && !safeFinalReply) missingOrUnsafeReplies += 1;
    if (run.status === 'completed') completed += 1;
    if (quoteCalls.length > 1) duplicateQuote += 1;
    if (calls.some((call) => call?.status === 'pending')) unknownTool += 1;
    if (tools.some((tool) => HIGH_RISK_TOOLS.has(tool))) highRisk += 1;
    if (String(run?.result?.authoritative_outcome ?? 'not_available') !== sourceOutcome(run)) mismatch += 1;
    const completedSuccess = (tool) => calls.some((call) => call?.tool === tool && call?.status === 'completed' && call?.observation?.status === 'success');
    if (successfulSource && run.status === 'completed' && safeFinalReply && orderedPathExists(tools, REQUIRED_QUOTE_PATH)
      && REQUIRED_QUOTE_PATH.every(completedSuccess) && quoteCalls.length === 1) fullPathPass += 1;
    const started = Date.parse(run.created_at); const ended = Date.parse(run.updated_at);
    if (Number.isFinite(started) && Number.isFinite(ended) && ended >= started) latencies.push(ended - started);
  }
  const completionRate = percentage(completed, samples.length);
  const successfulSources = samples.filter((run) => sourceOutcome(run) === 'quote_succeeded').length;
  const fullPathRate = percentage(fullPathPass, successfulSources);
  const prerequisiteReplanRate = percentage(prerequisiteReplans, samples.length);
  const blockers = [];
  if (samples.length < minimum) blockers.push('insufficient_image_samples');
  if (completionRate == null || completionRate < 95) blockers.push('completion_rate_below_95');
  if (successfulSources > 0 && (fullPathRate == null || fullPathRate < 95)) blockers.push('full_path_pass_rate_below_95');
  if (missingOrUnsafeReplies > 0) blockers.push('missing_or_unsafe_final_reply_detected');
  if (duplicateQuote > 0) blockers.push('quote_realtime_duplicate_detected');
  if (unknownTool > 0) blockers.push('unknown_tool_result_detected');
  if (highRisk > 0) blockers.push('high_risk_tool_detected');
  if (prerequisiteReplanRate != null && prerequisiteReplanRate > 5) blockers.push('prerequisite_replan_rate_above_5');
  if (mismatch > 0) blockers.push('authoritative_outcome_mismatch');
  return Object.freeze({
    ready: blockers.length === 0, sample_count: samples.length, minimum_sample_count: minimum,
    completed_count: completed, completion_rate: completionRate,
    successful_quote_sample_count: successfulSources, full_path_pass_count: fullPathPass, full_path_pass_rate: fullPathRate,
    safe_reply_count: safeReplies, missing_or_unsafe_reply_count: missingOrUnsafeReplies,
    quote_duplicate_count: duplicateQuote, unknown_tool_result_count: unknownTool, high_risk_tool_count: highRisk,
    prerequisite_replan_count: prerequisiteReplans, prerequisite_replan_rate: prerequisiteReplanRate,
    authoritative_mismatch_count: mismatch, latency_p50_ms: percentile(latencies, 0.5), latency_p95_ms: percentile(latencies, 0.95),
    blockers: Object.freeze(blockers),
  });
}
