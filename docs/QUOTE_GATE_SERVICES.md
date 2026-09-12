# Canonical quote gate inventory

This inventory is the Phase 1 boundary for the GateResult refactor. Existing
services keep their current return contracts until an explicit migration phase.

| Gate | Current owner | Entry point | Phase 1 status |
|---|---|---|---|
| RECOGNITION | `backend/app/recognition_v2/service.py` | `RecognitionV2Service.recognize` | adapter-ready |
| CINEMA_ROUTE | `backend/app/cinema_route_v2/service.py` | `CinemaRouteV2Service.resolve` | adapter-ready |
| SHOW | `backend/app/show_resolve_v2/service.py` | `ShowResolveV2Service.resolve` | adapter-ready |
| SEAT | `backend/app/seat_facts_v2/service.py` | `SeatFactsV2Service.resolve` | adapter-ready |
| COST | `backend/app/wanda_cost_v2/service.py` | `WandaCostResolutionService.resolve` | adapter-ready |
| PRICING | `backend/app/wanda_pricing_v2/service.py` | `WandaPricingV2Service.price` | adapter-ready |
| QUOTE | `backend/app/quote_v2/service.py` | `QuoteV2Service.persist` | adapter-ready |
| REPLY | `backend/app/canonical_buyer_reply.py` | `render` | adapter-ready |

The unified models, deterministic policy, dependency invalidation graph, and
legacy adapter are introduced with the feature flag disabled by default.
No existing orchestration service is switched to the new contract in Phase 1.
