# Canonical / Legacy Hard Isolation

Scope: `tenant=107` / `shop=2313315754`

## Canonical image graph

`buyer image -> CanonicalQuoteRuntime -> RecognitionV2 -> CinemaRouteV2 -> ShowResolveV2 -> SeatFactsV2 -> CostFactsV2 -> V4PricingEngine -> Canonical QuoteRecord -> CanonicalBuyerReplyRenderer -> RulesFirst outbox -> Plugin send`

Canonical image failure is terminal. It may return a safe no-quote / manual-task result, but it must not enter legacy recognition, legacy quote, or legacy reply code.

## Canonical text graph

`buyer text -> AgentContextBuilder -> CanonicalConversationAgent -> canonical tools -> canonical quote/state authority -> RulesFirst outbox -> Plugin send`

Canonical text failure is terminal. It may return safe failure or manual-task output, but it must not enter legacy NLP / keyword / regex / chat-service code.

## Legacy graph

`legacy image/text event -> RulesFirstDecisionEngine -> legacy recognition / legacy quote / legacy agent helpers -> RulesFirst outbox -> Plugin send`

This graph remains for non-canonical shops only.

## Legacy owners

### OLD_RECOGNITION

`LEGACY_RECOGNITION_FILES =`
- `backend/app/service.py`
- `backend/app/cli.py`
- `backend/app/plugin_automation.py`
- `backend/app/main.py` (legacy `/api/chat/image-messages` only)

`LEGACY_RECOGNITION_ENTRYPOINTS =`
- `MovieImageRecognitionService.recognize_from_url`
- `MovieImageRecognitionService.recognize`

`LEGACY_RECOGNITION_CALLERS =`
- `backend/app/main.py`
- `backend/app/plugin_automation.py`
- `backend/app/cli.py`
- legacy image-related tests under `backend/tests/test_service.py`, `backend/tests/test_plugin_automation.py`

### OLD_QUOTE

`LEGACY_QUOTE_FILES =`
- `backend/app/wanda_direct_quote.py`
- `backend/app/chat.py`
- `backend/app/plugin_automation.py`
- `backend/app/main.py` (legacy `/api/chat/image-messages` reply path)

`LEGACY_QUOTE_ENTRYPOINTS =`
- `WandaDirectQuoteService.quote`
- `build_recognition_reply`

`LEGACY_QUOTE_CALLERS =`
- `backend/app/main.py`
- `backend/app/plugin_automation.py`
- legacy seat-display tests under `backend/tests/test_seat_display.py`, `backend/tests/test_plugin_automation.py`

`LEGACY_PRICING_CALLERS =`
- `backend/app/plugin_automation.py`
- `backend/app/main.py` (legacy image chat endpoint)

### OLD_AGENT

`LEGACY_AGENT_FILES =`
- `backend/app/chat_service.py`
- `backend/app/plugin_automation.py`
- `backend/app/main.py` (legacy `/api/chat/text-messages` fallback path)

`LEGACY_AGENT_ENTRYPOINTS =`
- `CustomerServiceChatService.reply`
- `CustomerServiceChatService.sync_platform_history`

`LEGACY_AGENT_CALLERS =`
- `backend/app/main.py`
- `backend/app/plugin_automation.py`
- legacy chat tests under `backend/tests/test_chat_ai.py`, `backend/tests/test_plugin_automation.py`

## Boundary tests

- `backend/tests/test_legacy_boundary_imports.py`
- `backend/tests/test_legacy_isolation_hard_fence.py`

## Current isolation result

- `CANONICAL_TO_LEGACY_EDGES = 0`
- Canonical image failures do not call legacy recognition / quote entrypoints.
- Canonical text failures do not call legacy agent / chat entrypoints.
- Legacy entrypoints still exist for non-canonical shops and shared deterministic infrastructure.
