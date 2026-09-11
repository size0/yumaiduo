# V4 Production Canonical Baseline

This repository is the canonical Git source of truth reconstructed from the source that was running in production on 2026-09-11.

## Components

- `backend/app/` — Wanda V4 Backend production application source.
- `plugin-runtime/wanda-seat-autoquote/` — production `wanda-seat-autoquote` plugin runtime and UI.
- `docs/` — production-baseline evidence and repository notes.
- `deploy/` — retained historical/supporting deployment material from the repository; it is not evidence of the currently running release.
- `BUSINESS_RULES.md` — business and safety boundaries retained from the repository.

The `ticket-system` is intentionally not included. The target repository's pre-existing structure contained the V3/V4 gateway and plugin, but no `ticket-system` component. Ticket-system is deployed as a separate service and was excluded rather than adding an unverified or unrelated component to this baseline.

## Production baseline

See [`docs/PRODUCTION_BASELINE_20260911.md`](docs/PRODUCTION_BASELINE_20260911.md) for release identifiers, source mappings, hash-verification scope, and the known ticket-system boundary.

## Local development

Backend:

```bash
cd backend
python -m pytest
```

Plugin:

```bash
cd plugin-runtime/wanda-seat-autoquote
npm test
```

Do not run tests or development commands against production credentials or production write APIs. Runtime configuration, secrets, user data, databases, logs, dependencies, and generated artifacts are excluded by `.gitignore`.
