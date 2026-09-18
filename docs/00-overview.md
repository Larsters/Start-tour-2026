# 00 · Leash — team overview (read this first)

One page on what we are building for the Viseca "Agent on a Leash" challenge,
so engine, chatbot, and pitch stay aligned. Details live in the numbered docs.

| Doc | What it holds |
| --- | --- |
| `01-brief.md` | The challenge decoded, constraints, API surface |
| `02-scenario-analysis.md` | All 45 fixture purchases, the trap in each, our lean |
| `03-market-landscape.md` | Visa / Mastercard / AP2 / Stripe / YC startups, and the gap we fill |
| `04-solution-design.md` | Engine internals: quarantine, clauses, trust score, receipts |
| `05-requirements.md` | Numbered requirements + decision log from the design session |
| `06-event-inventory.md` | Every field the API sends, what we derive from it, what we must load ourselves |

---

## What we are building, in one paragraph

A **policy engine** that an AI shopping agent must consult before spending a
customer's money. The customer states what is allowed in plain language; the
engine compiles that into a contract, asks the customer only the questions only
they can answer, and then judges every proposed purchase on its facts:
**approve, decline, or ask**. It never trusts the agent or the merchant, it
explains every decision clause by clause, and it does more than block: it
warns about sizing, delivery dates, subscription terms and suspicious prices,
and for unverified sellers it proposes a bounded yes via a one-time capped card.

We do **not** build the shopping agent or checkout. We build the leash.

## Why this wins (pitch spine)

1. **Issuer's seat.** In Mastercard Agent Pay and Visa Intelligent Commerce the
   *issuer* holds the consent policy, runs step-up, and revokes. Viseca is the
   issuer. This is the engine that seat needs.
2. **Everyone else does limits.** Allowance (YC 2026), Nekuda, Stripe, Mastercard
   all ship amount cap, period cap, merchant list, expiry, revoke. We add what
   they lack: **cart-versus-intent verification**, **injection-proof by
   construction**, **issuer-wide merchant trust**, and **advice**.
3. **Explainable and controllable.** Every decision is a receipt; every
   uncertainty becomes a question; the customer can tighten or revoke anytime.

## Architecture

```
 Customer ──chat──► Chatbot UI (team)  ──MCP tools──► ┌───────────────────────────────┐
                        │  question / step-up cards   │  Leash engine (Python)        │
                        └───────HTTP (direct)────────►│  · compiler  (OpenAI mid)     │
                                                      │  · extractor (OpenAI small)   │
 Viseca simulator ◄──long-poll / decision / resolve──►│  · clauses, trust, ledger     │
                                                      │  · merchant score, advisory   │
                                                      │  · per-customer md/jsonl state│
                                                      └───────────────────────────────┘
```

- **One Python process**: FastAPI (HTTP), MCP server (agent tools), background
  worker (Viseca long-poll). Start with one command.
- **Trust boundary**: the LLM inside the chatbot is *untrusted*. Questions and
  step-ups are rendered by the chat UI as **cards fetched straight from the
  engine** and answered **straight to the engine**. The LLM only learns "a
  question is pending" / "answered".
- **Hot path** (≤ 8 s, target ≤ 2 s): deterministic clauses + one small-model
  extraction call with regex fallback. **Cold path** (no deadline): compiler,
  web lookups, advice, dry-run.

## The three checks every purchase goes through

| Layer | Question it answers | Fails to |
| --- | --- | --- |
| **Contract clauses** | Is this within what the customer allowed? (amount, rolling period, basket purpose, item + attributes, return terms, add-ons, retailer type, familiarity, one-time vs standing) | decline (hard) / ask (soft or missing fact) |
| **Trust signals** | Is anything about *how* this arrived suspicious? (injection in merchant text, lookalike seller, unknown merchant, new device, night burst, velocity, duplicate) | ask, containment proposal, or decline for lookalikes/injection-plus-failure |
| **Advice** | Is there something the customer would want to know? (brand sizing, delivery after the occasion, subscription minimum term, price far below market → counterfeit, high return rate) | ask under "ask when uncertain"; otherwise attached to approve |

Combine rule: any hard fail → `decline`; any soft fail → `step_up`; any
unknown → the customer's `uncertainty_policy`; trust escalated → `step_up`;
else `approve`.

## Merchant trust bands (Q22)

Score from issuer-wide history (distinct cards, approval/decline/refund rates,
first seen), card-level familiarity, lookalike-of-known-ID, new country, and a
web-reputation adapter when a real domain resolves.

| Band | Score | Effect when clauses pass |
| --- | --- | --- |
| trusted | ≥ 0.7 | approve |
| unknown | 0.4 – 0.7 | `step_up` + **containment proposal**: "Seller could not be verified. Approve with a one-time virtual card capped at CHF <amount>, this seller only, expiring <date>? The agent must follow the one-time-card protocol." |
| risky / lookalike | < 0.4 or name-lookalike with different ID | decline |

## MCP tools exposed to the agent

| Tool | Does | Side effects |
| --- | --- | --- |
| `draft_mandate(instruction)` | Compile verbatim instruction → contract + open questions; creates cards for the customer | draft only |
| `get_open_questions(mandate_id)` | Pending follow-ups and their status | none |
| `request_confirmation(mandate_id)` | Creates the confirm card; confirmation itself happens on the card (HTTP) | none until customer acts |
| `get_policy(mandate_id)` | Human-readable contract + machine clauses | none |
| `get_remaining_budget(mandate_id)` | Per-order cap, period cap, remaining in window | none |
| `precheck_cart(mandate_id, cart)` | Full evaluation without recording; returns would-approve / would-ask / would-decline, blocking clauses, questions, advice | none |
| `propose_purchase(mandate_id, cart)` | Real decision, recorded in the ledger; step-ups create a card | ledger write |
| `get_merchant_trust(merchant)` | Band, score, inputs | none |
| `get_item_advice(item, brand?, size?)` | Sizing / term / price advice with source and confidence | may cache into `knowledge.md` |
| `explain_decision(authorization_id)` | The receipt | none |

The agent **cannot** confirm, patch, revoke, or resolve. Those are card actions
from the customer via HTTP.

## Per-customer state (`state/<customer_id>/`)

- `mandate.md` — YAML front matter (mandate_id, hard_rules, IntentSpec, thresholds, assumptions) + the prose contract the customer confirmed
- `questions.md` — follow-ups with status and answers
- `knowledge.md` — cached item/brand/merchant facts with source URL, date, confidence
- `ledger.jsonl` — one line per decision: live id, sim time, amount, decision, clauses, resolved outcome

## Decision receipt (what every response carries)

```json
{
  "decision": "step_up",
  "reason_codes": ["merchant_unverified", "price_below_market"],
  "customer_message": "Approve the CHF 25 jersey from Kitsworld (seller not verified, price far below the usual CHF 80–110)? Option: one-time card capped at CHF 25.",
  "evidence": [
    {"clause": "amount", "status": "pass", "value": 25.0, "limit": 150},
    {"clause": "delivery_deadline", "status": "fail", "value": "2026-09-30", "needed_by": "2026-09-25"},
    {"clause": "merchant_trust", "status": "unknown", "band": "unknown", "score": 0.52},
    {"clause": "price_sanity", "status": "fail", "value": 25.0, "market_range": [80, 110], "source": "knowledge.md#jersey"}
  ],
  "recommended_action": {"type": "one_time_card", "cap_chf": 25.0, "merchant_id": "…", "expires_in_hours": 24},
  "engine_version": "leash-0.1"
}
```

Step-up card template: **Action needed** · *what* for CHF *x* at *seller* ·
**Why**: one plain sentence per failing clause · **Options**: Approve /
Approve with one-time card capped at CHF *x* (when proposed) / Decline.

## Demo storyline (Q23)

**Customer to chatbot:** "My girlfriend's birthday is in a week. I want to get
her Adidas running shoes and a jersey, around CHF 250 total."

1. **Compile + follow-ups (cards):** "Her shoe size?" → "39". Engine checks the
   Adidas size guide (verified source, cached in `knowledge.md`): "Adidas
   running models typically run small; people usually take one to two sizes up.
   Order 40 or 41?" → customer picks. "Deliver by <birthday − 1 day>?" → yes.
   Contract shown: total ≤ 250, deliver by date, two items only, no add-ons,
   ask when uncertain. Customer confirms on the card.
2. **Agent proposes the shoes** from a trusted retailer, size 41, returnable
   30 days, delivery in 3 days → **approve**, receipt all green, advice note
   attached.
3. **Agent proposes the jersey** at CHF 25 from a seller unknown to the network,
   shipping from abroad with delivery after the birthday → **step_up**: price
   far below market (counterfeit risk), seller unverified → containment
   proposal (one-time card capped at 25), delivery deadline missed. Customer
   declines from the card, or approves with the capped card.
4. **Control:** customer tightens ("decline when uncertain") and finally
   revokes; the agent's next proposal is refused by the platform.

**Compliance proof:** the five official scenarios replayed live, with the
three moments the brief asks for (frictionless grocery, injected monitor
intervention, human approve/reject/revoke).

## Offline replay (regression suite)

`python -m leash.replay --scenario SCEN0004 --mandate fixtures/mandates/SCEN0004.md`
builds events from the CSVs in `replay_order` (fresh deadlines, related IDs
mapped, FX applied), runs `/decide`, prints a table of decision + top reason,
and diffs against `fixtures/expected/SCEN0004.yaml` (our leans from doc 02).

## Build order

1. Event parsing (strict schema) + normaliser + ledger + amount/period/basket clauses → SCEN0000/0001 offline
2. Quarantine extractor (OpenAI small, structured output) + regex fallback + item/terms/add-on/retailer clauses → SCEN0002/0004
3. Merchant score + lookalike + duplicate/re-quote + session trust → SCEN0003, rest of 0004
4. Compiler (OpenAI mid) → contract md + questions; per-customer files
5. Worker against live API; mandate create/confirm/patch/delete
6. MCP server with the tool list; card endpoints for the chatbot
7. Advisory + derived clauses + web adapters (size guide, price, merchant) → demo storyline
8. Dry-run against history; polish receipts and counterfactuals

## Definition of done for the hackathon

- All 45 fixture events answered before deadline with receipts; leans in doc 02 match or the diff is explained.
- Model outage simulated → engine still answers (fallback visible in logs).
- Redelivered event → same decision, no double count.
- Demo storyline runs end to end through the chatbot, with cards for questions, confirmation, step-up, and revoke.
- Judges can read any decision and see what was allowed, which facts, why, and how the customer stayed in control.
