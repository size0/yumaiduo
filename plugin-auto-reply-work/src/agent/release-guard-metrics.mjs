const ZERO_CODES = Object.freeze({
  high_risk_action: 'high_risk_actions', false_transaction_fact: 'false_transaction_facts',
  duplicate_write: 'duplicate_writes', write_retry_blocked: 'unknown_result_retries',
  cross_tenant_access: 'cross_tenant_access', authoritative_inconsistency: 'authoritative_inconsistencies',
  unsafe_final_reply: 'unsafe_final_replies', agent_deadline_exceeded_after_effect: 'post_deadline_effects',
});

function percentile95(values) {
  if (!values.length) return 0;
  const sorted = [...values].sort((left, right) => left - right);
  return sorted[Math.max(0, Math.ceil(sorted.length * 0.95) - 1)];
}

export function releaseGuardMetricsFrom(runs = [], outboxEntries = [], releaseGeneration = null) {
  const generation = Number(releaseGeneration);
  const activeRuns = (Array.isArray(runs) ? runs : [])
    .filter((run) => run?.mode === 'active' && Number(run?.release_generation) === generation)
    .sort((left, right) => String(right?.updated_at).localeCompare(String(left?.updated_at)))
    .slice(0, 100);
  const completed = activeRuns.filter((run) => ['completed', 'failed', 'timed_out'].includes(String(run?.status)));
  const latencies = completed.map((run) => Date.parse(run?.updated_at) - Date.parse(run?.created_at)).filter((value) => Number.isFinite(value) && value >= 0);
  const result = Object.fromEntries(Object.values(ZERO_CODES).map((field) => [field, 0]));
  let consecutiveGatewayFailures = 0;
  for (const run of activeRuns) {
    const observations = Array.isArray(run?.observations) ? run.observations : [];
    for (const observation of observations) {
      const field = ZERO_CODES[String(observation?.code ?? '')];
      if (field) result[field] += 1;
    }
    const gatewayFailed = observations.some((item) => ['wanda_gateway_unavailable', 'tool_execution_failed'].includes(String(item?.code)));
    if (gatewayFailed && consecutiveGatewayFailures === activeRuns.indexOf(run)) consecutiveGatewayFailures += 1;
  }
  const unknownOutbox = (Array.isArray(outboxEntries) ? outboxEntries : [])
    .filter((entry) => Number(entry?.release_generation) === generation && entry?.status === 'unknown').length;
  result.unsafe_final_replies += unknownOutbox;
  return Object.freeze({
    ...result,
    completed_turns: completed.length,
    hard_failures: completed.filter((run) => ['failed', 'timed_out'].includes(String(run?.status))).length,
    p95_latency_ms: percentile95(latencies),
    consecutive_gateway_failures: consecutiveGatewayFailures,
  });
}
