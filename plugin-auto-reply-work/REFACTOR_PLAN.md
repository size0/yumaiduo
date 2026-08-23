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
  ai/              provider-neutral primary AI orchestration
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
- External advisory providers are not part of the production runtime; price, seats, order state, price change, sending and fulfillment remain within existing authoritative boundaries.

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

Completed: `quote/quote-orchestrator.mjs` owns recognition prefetch, bounded quote-draft persistence, duplicate-attempt claiming and the single realtime quote invocation. `quote-preview-client.mjs` has been reduced from935 lines to约365 lines and now delegates pure responsibilities to `quote-text-facts.mjs`, `quote-recognition-fusion.mjs`, `quote-failure-mapper.mjs` and `quote-response-presenter.mjs`. The compatibility export for `parseTextQuoteRequest` remains stable. Wanda pricing and lock rules remain inV3.

### Wave 4 — order orchestration

Create `orders/order-orchestrator.mjs` owning created/paid/price-changed transitions and deterministic price-change gates. It cannot send directly; it returns typed reply intents to `reply-orchestrator.mjs`.

### Wave 5 — provider-neutral AI boundary

Create `ai/ai-orchestrator.mjs` around the existing Agent implementation before introducing another provider. It accepts bounded source-time snapshots and returns only typed low-risk decisions or reply drafts. It cannot send messages or invoke transaction tools.

The optional secondary advisory-provider experiment was retired before activation. The production target keeps one primary planner behind `ai/ai-orchestrator.mjs`; local source-time snapshots remain authoritative and no external workflow provider receives conversation data.

Promotion remains Shadow -> automatic safety audit -> optional human comparison -> low-risk Canary -> low-risk Active, with the existing unique execution-owner gates.

### Wave 6 — application decomposition

Extract from `application.mjs`:

- `bootstrap/create-storage-bundle.mjs`
- `bootstrap/create-agent-runtime-bundle.mjs`
- `bootstrap/create-lifecycle-controller.mjs`
- `admin/create-operator-api.mjs`
- `admin/operator-presenters.mjs`

`application.mjs` must end as dependency assembly plus `start`, `stop`, `health`, `enqueueEvent` and `handleHttpRequest` delegation.

Completed: storage, Agent runtime, lifecycle, operator presenters, and operator API now live behind these seams; `application.mjs` is the composition root only. Compatibility re-exports remain temporarily for existing tests and callers.

### Wave 7 — V3 quote-domain decomposition

Completed: `wanda_quote.py` no longer owns pure realtime-seat parsing, official-seat selection, area-probe selection, offer uniqueness, quote-boundary arithmetic, safe failure diagnostics, official showtime matching or the legacy local gateway adapter. These live in `wanda_quote_domain.py`, `wanda_quote_diagnostics.py`, `wanda_showtime_matcher.py` and `wanda_quote_gateway.py`, with compatibility imports preserved for existing callers. The main service now retains only read-only seat lookup, temporary-offer orchestration and quote response assembly; direct Wanda temporary-order safety remains unchanged.

Completed: the V3 FastAPI composition root is reduced from roughly 950 lines to roughly 207 lines. Plugin Bridge administration, quote previews, Agent/reply endpoints, deterministic reply rendering and shared preview safety helpers now live in dedicated route/support modules. Existing paths, response models, authentication checks, fail-closed Active/Canary gates and compatibility imports remain unchanged.

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

Every wave is one reviewable commit with an independent rollback point. Agent ownership changes are explicitly excluded until decomposition is complete. External secondary providers remain out of scope after the event, reply, quote/order, and provider-neutral AI seams have passed the full characterization suite.
