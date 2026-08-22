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

## Sequential waves

### Wave 0 — completed safety baseline

- Git baseline and private remote.
- Isolated Pi worktree dispatcher.
- Characterization tests for reply deduplication, deterministic failure ownership, Shadow side-effect isolation and temporary-lock failure closure.

### Wave 1 — pure policy extraction

Extract pure classifiers and reply construction from `workflow.mjs` into:

- `conversation/message-classifier.mjs`
- `reply/reply-policy.mjs`
- `quote/quote-followup-policy.mjs`

No store or network dependency is allowed in these modules. Existing exports remain as compatibility delegates.

### Wave 2 — quote conversation flow

Create `quote/create-quote-conversation-flow.mjs` owning image receipt, quote draft fusion, recognition, showtime resolution, quote result persistence and quote follow-ups. It receives stores, clients and the single reply port through explicit dependencies.

### Wave 3 — order lifecycle

Create `orders/create-order-lifecycle.mjs` owning created/paid/price-changed transitions and deterministic price-change gates. It cannot send directly; it returns reply intents to the reply port.

### Wave 4 — Agent conversation flow

Move Agent scheduling/active execution glue from `workflow.mjs` into `agent/create-conversation-agent-flow.mjs`. Tool implementations remain typed and cannot import bootstrap or admin code.

### Wave 5 — application decomposition

Extract from `application.mjs`:

- `bootstrap/create-storage-bundle.mjs`
- `bootstrap/create-agent-runtime-bundle.mjs`
- `bootstrap/create-lifecycle-controller.mjs`
- `admin/create-operator-api.mjs`
- `admin/operator-presenters.mjs`

`application.mjs` must end as dependency assembly plus `start`, `stop`, `health`, `enqueueEvent` and `handleHttpRequest` delegation.

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

Every wave is one reviewable commit with an independent rollback point. Agent ownership changes are explicitly excluded until decomposition is complete.
