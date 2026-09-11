# V4 Production Baseline — 2026-09-11

## Purpose

This document records the non-sensitive identity of the source snapshot used to reconstruct this canonical repository. It is evidence metadata, not a deployment instruction.

## Captured production releases

| Component | Actual production release | Canonical path |
|---|---|---|
| V4 Backend | `quote-delivery-compat-20260911-0105` | `backend/app/` |
| wanda-seat-autoquote | `quote-records-ui-20260910-r3` | `plugin-runtime/wanda-seat-autoquote/` |
| Ticket Backend | `ticket-payment-qr-20260910-r2` | Not included in this repository |

Captured: `2026-09-11`

V4 production manifest build identifier:

```text
f6b35186bc0f4c7d648d4282ffc59aa6087c2151
```

This identifier came from the V4 production manifest. The V4 release directory on the server had no `.git` metadata, and this identifier could not be resolved in the previous local `E:/鱼麦多/v4` Git object database. The canonical commit created from the verified production snapshot is therefore the formal traceable Git baseline.

## Source evidence

- V4 Backend source snapshot: `E:/鱼麦多/v4-worktrees/backend-release-20260910-quote-delivery/app`
- Plugin source snapshot: `E:/鱼麦多/v4-worktrees/quote-records-ui-20260910-r3`
- V4 Backend verification: 98 of 98 Python source files matched by SHA256.
- Plugin verification: 20 of 20 selected production source/UI files matched by SHA256.

## Ticket-system boundary

The target `size0/yumaiduo` repository before reconstruction contained the V3 backend gateway and plugin runtime, but no `ticket-system` tree. Ticket-system is a separate production service. Its running backend release was identified during the audit, but it was not copied into this repository because the nearest local snapshot was not exact and the component was outside the target repository's established scope.

The running ticket process was observed under:

```text
/opt/ticket-system/releases/ticket-payment-qr-20260910-r2/backend
```

No server files, services, release pointers, or databases were modified during this reconstruction.
