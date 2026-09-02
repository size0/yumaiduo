# Liangpiao Max Price Semantic Report

## Scope

This is a read-only audit for Pricing Phase P2.5. No provider calls, order writes, production path changes, or transaction behavior changes were made.

## Field lineage

| Layer | Field | Meaning | Current use |
|---|---|---|---|
| `/api/v1/order/preflight` | `estimateAmount` | Provider's estimated payable amount for LIMIT | Becomes `provider_base_amount_fen`; displayed as the buyer amount before operator pricing |
| `/api/v1/order/preflight` | `totalAmount` | Provider total/upper-limit amount for LIMIT; confirmed payable amount for FIXED | LIMIT: `max_price_fen`; FIXED: base payable amount and max price |
| `/api/v1/order/preflight` | `marketAmount` | Provider market/original comparison amount | Only used to select the operator Liangpiao markup band |
| `SelectedSeatQuoteResult` | `provider_amount_fen` | Provider-side base amount | Persisted in the quote snapshot and exposed in refresh/offer data |
| `SelectedSeatQuoteResult` | `buyer_amount_fen` | Buyer-facing amount after optional operator pricing | Used for `RealQuote.total_quote_cents`, reply totals, and quote amount checks |
| `SelectedSeatQuoteResult` | `max_price_fen` | Maximum amount sent to order creation | `LiangpiaoOrderService` sends `maxPrice = max_price_fen or buyer_amount_fen` |
| `RealQuote` | `total_quote_cents` | Buyer-facing quote total | Used by chat replies, exact quote validation, and order amount matching |
| `RealQuote` | `max_price_cents` | Provider/order ceiling | Carried through the quote and QuoteRecord; not used as the buyer-facing reply total |
| QuoteRecord | `buyer_amount_fen` / `total_quote_cents` | Buyer-facing quote snapshot | Used in public quote projection, reply grounding, and order amount checks |
| QuoteRecord | `max_price_cents` | Order ceiling | Preserved for offer refresh and downstream order binding |
| Order create payload | `maxPrice` | Provider order ceiling | Derived from the quote snapshot, not recalculated from display price |

## LIMIT semantics

```text
estimateAmount = provider base / buyer-facing preflight amount
operator markup → buyer_amount_fen

totalAmount = provider upper limit
max_price_fen = totalAmount
```

The current implementation can therefore produce:

```text
provider_amount_fen = 6500
buyer_amount_fen    = 6830
max_price_fen       = 7000
```

These are intentionally different values. The P2.5 adapter preserves all three facts.

## FIXED semantics

```text
totalAmount = confirmed provider payable amount
operator markup → buyer_amount_fen
max_price_fen = totalAmount
```

Example:

```text
provider_amount_fen = 7000
buyer_amount_fen    = 7350
max_price_fen       = 7000
```

The existing code rejects `FIXED + estimated=true`, so the adapter also rejects it before constructing verified PricingFacts.

## Usage by current V4

### Quote reply

`LiangpiaoExactQuoteAdapter` maps:

```text
buyer_amount_fen → RealQuote.total_quote_cents
max_price_fen   → RealQuote.max_price_cents
```

The reply uses `total_quote_cents` / buyer amount. `max_price_cents` is not presented as the buyer's quote total.

### QuoteRecord

`RulesFirstDecisionEngine._record_quote()` stores both:

```text
max_price_cents
unit_quote_cents
total_quote_cents
provider_quote_id
provider_quote_hash
pricing_rule_version
```

The persisted record also stores the provider preflight snapshot for Liangpiao projections.

### Order creation

`LiangpiaoOrderService.create()` sends:

```python
maxPrice = max_price_fen or buyer_amount_fen
```

This is the only current order-level use of `max_price_fen`. It is not recomputed from `total_quote_cents`.

### Transaction/order matching

Current matching logic compares the authoritative paid order amount to the buyer-facing quote total/offer total. It does not use `max_price_cents` as the displayed buyer price. `max_price_cents` is retained as an order ceiling and refreshed during authoritative preflight.

## Route compatibility note

The neutral P2.5 route names are `WANDA_SELF`, `LIANGPIAO_LIMIT`, and `LIANGPIAO_FIXED`. Existing transaction consumers currently branch on lowercase legacy values `wanda_self` and `liangpiao_exact`. The compatibility mapper preserves the legacy `quote_route`/`route` values and additionally writes `provider_route` and `pricing_quote_route` with the canonical neutral value. This avoids silently breaking the current order path while keeping the new route explicit.

## Conclusion

The three concepts should remain separate:

```text
provider_max       = provider-side ceiling / LIMIT totalAmount
buyer_quote        = buyer-facing amount after operator pricing
order_maxPrice     = amount supplied to provider order/create
```

For the current V4 behavior:

- `buyer_quote` is the amount used in messages and quote amount validation.
- `order_maxPrice` is sourced from the preflight quote snapshot's `max_price_fen`.
- `provider_max` and `order_maxPrice` are currently represented by the same numeric value in the Liangpiao order path, but they are different semantic concepts and should remain separately named in the neutral model.
- No silent normalization or correction was made in P2.5.

The new `PricingFacts` retains `estimate`, `total`, `market`, `provider_amount`, and `provider_max` independently. `QuoteResult` retains `base_total`, `total_quote`, `max_price`, and `provider_amount` independently, with `PRICING_SEMANTIC_REVIEW_REQUIRED` when max and buyer amounts differ.
