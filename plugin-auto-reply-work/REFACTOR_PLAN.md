# Plugin decomposition plan

Baseline: `d3bc430`. Characterization-test checkpoint: `3e4c09c`.

## Target boundaries

```text
src/
  bootstrap/       dependency assembly and lifecycle only
  channels/        Yumaiduo event ingress/egress
  conversation/    conversation facts and source-time snapshots
  quote/           recognition, showtime resolution and realtime quote orchestration
  orders/          order facts, price-change gates and manual tasks
  ai/              provider-neutral AI orchestration and future Dify adapter
  agent/           planner, policy, tools and durable runtime
  reply/           the single reply/outbox/deduplication boundary
  storage/         encrypted durable stores
  admin/           HTTP and operator presentation
```

The business path remains:

```text
event -> conversation facts -> Agent/deterministic router -> controlled tool -> reply boundary
```

## Non-negotiable invariants

- No extraction may change buyer-visible behavior, action IDs, event outcomes, quote state, timing gates, tool contracts or reply ownership.
- Price/order facts remain deterministic and authoritative.
- Shadow/evaluation never cause external writes.
- A successful quote has exactly one authoritative reply; deterministic failures cannot be overwritten.
- Temporary Wanda probes always cancel and require realtime release evidence.
- `application.mjs` remains the composition root until the extracted modules have characterization coverage.
- Do not edit stable UI assets during this refactor.
- `workflow.mjs` is frozen for new business behavior; changes there are limited to compatibility delegation into extracted modules.
- Dify is never an authority for price, seats, order state, price change, sending, or manual fulfillment.

## Sequential waves

### Wave 0 — completed safety baseline

- Git baseline and private remote.
- Isolated Pi worktree dispatcher.
- Characterization tests for reply deduplication, deterministic failure ownership, Shadow side-effect isolation and temporary-lock failure closure.

### Wave 1 — event routing and pure policy extraction

The first strangler seam is `event-router.mjs`: it classifies message and order lifecycle events while unknown events retain the legacy fallback. Extract remaining pure classifiers and reply construction from `workflow.mjs` into:

- `conversation/message-classifier.mjs`
- `reply/reply-policy.mjs`
- `quote/quote-followup-policy.mjs`

No store or network dependency is allowed in these modules. Existing exports remain as compatibility delegates.

### Wave 2 — unique reply boundary

Create `reply/reply-orchestrator.mjs` as the only interface allowed to submit buyer replies. It owns placeholder rejection and delegates platform addressing, action-ID deduplication, human-takeover checks and sent-message persistence to the existing action executor while those internals are migrated. Both the deterministic workflow and durable Agent outbox must use this boundary. During migration, existing action builders delegate without changing action payloads or timing.

### Wave 3 — quote orchestration

Create `quote/quote-orchestrator.mjs` owning image receipt, quote draft fusion, recognition, showtime resolution, quote result persistence and quote follow-ups. It receives stores, clients and the single reply port through explicit dependencies. Wanda pricing and lock rules remain in V3.

### Wave 4 — order orchestration

Create `orders/order-orchestrator.mjs` owning created/paid/price-changed transitions and deterministic price-change gates. It cannot send directly; it returns typed reply intents to `reply-orchestrator.mjs`.

### Wave 5 — provider-neutral AI boundary

Create `ai/ai-orchestrator.mjs` around the existing Agent implementation before introducing another provider. It accepts bounded source-time snapshots and returns only typed low-risk decisions or reply drafts. It cannot send messages or invoke transaction tools.

After that seam is stable, add Dify only as a Shadow provider:

- `ai/dify-client.mjs`
- `ai/dify-response-validator.mjs`
- a Dify provider registered with `ai/ai-orchestrator.mjs`

Use Dify's published Workflow service API in blocking mode for stateless FAQ, intent, draft, and handoff classification. Do not use Dify `conversation_id` as authoritative memory; local source-time conversation snapshots remain authoritative. App API keys stay server-side, request/response payloads are bounded and redacted, and timeout/invalid schema produces no buyer message. Dify outputs may contain only `intent`, `confidence`, `reply_draft`, `handoff_recommended`, bounded `reason_code`, and bounded `missing_fields`.

Do not expose Wanda tools to a Dify Workflow. Dify may not quote, lock, cancel, change price, send, create fulfillment tasks, or invent transaction facts. Promotion remains Shadow -> automatic safety audit -> optional human comparison -> low-risk Canary -> low-risk Active, with the existing unique execution-owner gates.

Dify's repository uses a modified Apache 2.0 license with additional multi-tenant and frontend branding conditions. Initial deployment must use one internal workspace without rebranding; any future multi-workspace SaaS use requires license review.

### Wave 6 — application decomposition

Extract from `application.mjs`:

- `bootstrap/create-storage-bundle.mjs`
- `bootstrap/create-agent-runtime-bundle.mjs`
- `bootstrap/create-lifecycle-controller.mjs`
- `admin/create-operator-api.mjs`
- `admin/operator-presenters.mjs`

`application.mjs` must end as dependency assembly plus `start`, `stop`, `health`, `enqueueEvent` and `handleHttpRequest` delegation.

Completed: storage, Agent runtime, lifecycle, operator presenters, and operator API now live behind these seams; `application.mjs` is the composition root only. Compatibility re-exports remain temporarily for existing tests and callers.

## Parallel-work rules

The following hotspots have one writer at a time:

- `workflow.mjs: processPreviewOnly/processClaimed/tick`
- order lifecycle blocks in `workflow.mjs`
- `application.mjs: createApplication/start/stop/operator API`
- runtime settings and reply action construction

Other agents may concurrently add tests, audit dependency direction or implement new leaf modules, but may not modify the same hotspot in one wave.

## Verification per wave

```text
node --check on changed modules
node --test targeted characterization tests
npm test (plugin full suite)
git diff --check
manual review of event outcomes/action IDs/reply ownership
```

Every wave is one reviewable commit with an independent rollback point. Agent ownership changes are explicitly excluded until decomposition is complete. Dify integration starts only after the event, reply, quote/order, and provider-neutral AI seams have passed the full characterization suite.
