# 06 · What arrives from the API, and what we can do with it

Source: `viseca-2026/data/schemas/authorization_event.schema.json`,
`scenario_fixtures/example_authorization_request.json`, the CSVs. The schema
is strict: **every field is required and `additionalProperties: false`
everywhere**, so parse strictly and treat any deviation as `schema_invalid`.

## The live event, field by field

### Envelope (keep for run tracking, not part of the event)
`run_id`, `event_id`, `type`, `authorization_id`, `status`, `occurred_at`.

### `data` = the event
| Field | Type | What we do with it |
| --- | --- | --- |
| `type` | const `authorization.request` | assert |
| `request_id` | string | log |
| `deadline_at` | ISO real-clock | response budget; skip model calls when < 1.5 s left |

### `data.authorization`
| Field | Type | Use |
| --- | --- | --- |
| `authorization_id` | live ID | idempotency key; `/decision` and `/resolve` target |
| `source_authorization_id` | `AU…` | offline replay cross-reference only. **Never** a decision input |
| `scenario_id`, `replay_order` | | **never** a decision input |
| `mandate_id`, `profile_id`, `card_id` | | load per-customer state dir; card → history baselines |
| `initiator_type` | const `agent` | assert |
| `merchant.merchant_id` | `ME…` | familiarity (card-level history count), **issuer-wide merchant score**, lookalike check vs familiar IDs |
| `merchant.merchant_name` | string | untrusted; lookalike edit-distance vs familiar names; display only |
| `merchant.merchant_category`, `merchant_mcc` | | retailer-type clause (specialist sports = `sporting_goods`/5941) |
| `merchant.merchant_country`, `merchant_city` | | new-country signal vs card history; cross-border evidence |
| `merchant.availability` | online/store/store_and_online/atm | online-only + never seen = weaker trust |
| `merchant.recurring_capable` | "true"/"false" | subscription risk context |
| `timestamp` | simulated | period windows, velocity, hour-of-day, duplicate window |
| `amount`, `currency` | | line FX consistency check |
| `billing_amount_chf` | number | **the** order total for caps and period sums (delivery included) |
| `items_subtotal`, `delivery_fee` | | evidence ("CHF 118 + 8 delivery"); split-order detection |
| `channel` | enum | agent rows are ecommerce/recurring; anything else is anomalous |
| `customer_device_id` | `DVC-…` | device novelty vs card history (session trust) |
| `authority_status`, `card_status_at_attempt` | enums | always `active` in fixtures; hard gate anyway |
| `spend_in_period_before_chf` | **always null** | ignore; keep our own ledger |
| `recent_attempt_count_10m` | int | velocity signal (platform-computed, same definition offline) |
| `fulfillment_method` | free string (`delivery`, `digital`, …) | `digital` + gift card = cash-out pattern |
| `delivery_by` | date or null | **deadline clauses** ("present by next Friday"); null = missing fact. Populated on 11/45 fixtures (all grocery rows, next-day) |
| `order_returnable` | true/false/unknown/not_applicable | return-terms clause; `unknown` → uncertainty |
| `order_cancellable` | same | always `unknown` in fixtures; subscriptions clause in custom carts |
| `related_authorization_id`, `related_authorization_status` | live ID / enum | re-quote vs duplicate; the platform rewrites the ID to the live one |
| `purchase_description` | string | deliberately uninformative; display only |
| `items[]` | ≥ 1 | see below |

### `data.authorization.items[]`
| Field | Use |
| --- | --- |
| `line_no`, `item_id` | join to `items.csv` (catalogue price range, generic description) |
| `item_name` | untrusted; display and item-type hint |
| `item_category` | **basket-purpose clause** (groceries / cosmetics / gift_card / subscriptions / membership …) |
| `quantity`, `unit_price`, `currency` | per-line CHF; price-sanity vs catalogue `[min, typical, max]` |
| `item_details` | **untrusted text → quarantine extractor** → size, return days, final-sale, recurring billing, add-on, minimum term, instruction-likeness |

### `data.mandate` (snapshot at run start)
`mandate_id`, `status` (active/superseded/revoked/expired), `customer_id`,
`card_id`, `instruction` (verbatim), `hard_rules[]` (`field`, `operator`,
`value`, `currency?`, `scope?`, `period_days?`), `uncertainty_policy`,
`profile_id`. **`guidance` and `open_questions` are NOT in the live event** —
so the `IntentSpec` must be recoverable from `hard_rules` + our per-customer
`mandate.md`, keyed by `mandate_id`.

### `data.context`
`approved_spend_in_period_chf` (platform's counter from run decisions; reconcile
with our ledger), `recent_authorizations[]` (`authorization_id`, `timestamp`,
`merchant_id`, `billing_amount_chf`, `status`) — the run's own recent history,
useful for duplicate detection and as a cross-check.

### `data.runtime`
`received_at`, `history_window_minutes` (10), `context_basis`.

## What does NOT arrive (we must load it ourselves)
- Customer persona and preferences (`customers.csv`: e.g. CU0001 "avoids gift vouchers", `budget_style`).
- Account limits (`accounts.csv`: `per_transaction_limit_chf`, `monthly_limit_chf`) and card flags (`cards.csv`: `online_enabled`, `international_enabled`).
- The 4,701-row history (`GET /v1/reference-data/authorization-history.csv`).
- Catalogue price ranges (`items.csv`), FX (`fx_rates.csv`), merchant catalogue (`merchants.csv`).
- Any merchant reputation. The merchant object has no "verified" flag.
- Our own `IntentSpec`, knowledge cache, ledger.

## Issuer-wide merchant score — the data supports it
Viseca sees every cardholder's transactions. From the history we can compute
per merchant: distinct cards, approvals, declines, refunds, first-seen date.
For the scenario merchants:

| Merchant | Cards | Approved | Declined | Refunds | First seen | Note |
| --- | --- | --- | --- | --- | --- | --- |
| ME0001 Alpine Basket | 38 | 218 | 4 | 4 | 2025-09-02 | broad, clean |
| ME0028 TrailSpark | 5 | 44 | 0 | 1 | 2025-09-16 | |
| ME0029 Summit Thread | 2 | 3 | 0 | 0 | 2025-11-16 | thin but clean → "unfamiliar but compliant" |
| ME0053 GreenLoop | 7 | 33 | 0 | 0 | 2025-09-02 | fine merchant, wrong *type* |
| ME0026 RainThread | 31 | 66 | 6 | 0 | 2025-09-02 | |
| ME0058 Cobalt Coatworks | 32 | 74 | 1 | 1 | 2025-09-13 | |
| ME0060 Thames Weave | 6 | 12 | 1 | 0 | 2025-09-13 | |
| ME0022 PixelHarbor | 9 | 31 | 5 | 0 | 2025-09-03 | |
| **ME0059 PixelHarbour** | — | — | — | — | **absent** | never seen by any card + lookalike name |
| ME0023 Circuit and Pine | 9 | 19 | 2 | 0 | 2025-10-14 | |
| ME0024 HarborByte (US) | 4 | 29 | 7 | 1 | 2025-09-10 | higher decline share, but familiar to this card |

Implication: "unknown to the network" and "lookalike of a known merchant" are
both computable from data we already have, with no internet. A web-reputation
adapter can sit alongside for real merchant names (the synthetic ones will
never resolve online).

## Item-level history — the data supports it
Refunds carry `related_transaction_id` → original purchase, so per
`(merchant_id, description)` we can compute return rate and per card "have I
bought/returned this before". `items.csv` gives `[min, typical, max]` CHF per
item for price sanity (e.g. monitor 140 / 270 / 650; road shoes 70 / 140 / 240).

## Derived (intent-driven) clauses — what the event can support
| Intent phrase | Derived clause | Event field(s) |
| --- | --- | --- |
| "present for X's birthday in a week" | `delivery_by ≤ date`; category plausible for a gift; not a gift card unless asked | `delivery_by` (null → ask), `items[].item_category` |
| "subscription" / "plan" | minimum term, renewal price, cancellable | `item_details` (extractor: `minimum_term_months`, `recurring_billing`), `order_cancellable`, `merchant.recurring_capable` |
| "jersey" at a price far below catalogue/market | price-sanity: `unit_price_chf < min` or ≪ typical → counterfeit risk, follow-up | `unit_price`, `currency`, `items.csv` range, optional web price |
| "replace my shoes" | one item, same type, attributes | `items[]` count, extractor `item_type`/`size` |
| "from a seller I've used" | card-level familiarity + issuer-wide score | history |
