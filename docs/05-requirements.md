# 05 · Requirements — the policy engine ("Leash")

Status: **draft**. Settled items come from the brief, the data pack, and the
stakeholder clarifications relayed on 2026-09-18. Items marked **Q#** are open
decisions being grilled in the design session; they are listed at the bottom.

## Stakeholder clarifications (2026-09-18)

1. **Advisory, not just gatekeeping.** The engine may hold knowledge the
   customer lacks (e.g. "this brand runs small, buy two sizes up") and should
   *remind* the customer rather than silently approve or block. → Requirement A.
2. **The shopping agent refers to the engine.** The customer's shopping agent
   calls our engine, can ask it questions, and receives decisions from it. The
   engine stays the enforcer; the agent never applies the policy itself. → Requirement I.
3. **Deliverable for now: the policy engine.** UI is secondary (extent: Q1).

## Actors

| Actor | Trust | Talks to engine via |
| --- | --- | --- |
| Customer (cardholder) | trusted | Control UI (compile, confirm, answer step-ups, tighten, revoke) |
| Shopping agent | **untrusted** | Agent interface (propose purchase, query policy, relay questions) |
| Viseca simulator | trusted transport | Long-poll decision requests; `/decision`, `/resolve` |
| Merchant (via `item_details`, names) | **hostile by default** | Never directly; only as data inside an authorization |
| Operator / Viseca ops | trusted | Knowledge base, thresholds, engine version |

## Functional requirements

### C — Compile (instruction → mandate)
- C1 Accept the customer's instruction verbatim; send it unchanged to `POST /v1/mandates`.
- C2 Produce `hard_rules` in the API format only (`field`, `operator`, `value`, optional `currency`, `scope`, `period_days`; no extra keys, no boolean/null/object values).
- C3 Produce an `IntentSpec` (item, attributes, categories, retailer type, familiarity, return terms, add-ons, one-time vs standing, session-integrity flag, period) serialised into `guidance` so it round-trips in the mandate snapshot.
- C4 Produce `open_questions` for every ambiguity the compiler could not resolve (window semantics, familiarity threshold, one-time vs standing, brand sizing advice, …).
- C5 Render a human-readable contract (clauses + plain-language explanation) for confirmation. Confirm only after explicit customer agreement (`/confirm`).
- C6 **Dry-run**: replay the card's approved history through the compiled clauses and report counts + would-ask / would-decline rows before confirmation.
- C7 Tighten via `PATCH` only (add rules, `uncertainty_policy → decline`); never weaken. Revoke via `DELETE`.
- C8 Compiler may use a large model; must degrade to a rule-based parser that pushes unparsed parts into `open_questions`.

### D — Decide (authorization → approve / decline / step_up)
- D1 Answer within `deadline_at` (default 8 s from queueing); internal budget ≤ 2 s; skip model calls when < 1.5 s remain.
- D2 Validate `data` against `authorization_event.schema.json`; invalid → `step_up` with `schema_invalid`, never crash.
- D3 Idempotent on live `authorization_id`; redelivery returns the stored result without re-evaluation or double counting.
- D4 Evaluate clauses from the mandate snapshot in the event (not from our local copy).
- D5 Money: use `billing_amount_chf` as the order total (delivery included); convert lines with `fx_rates` by the row's `currency`; half-even rounding to 2 dp.
- D6 Period spend from **our own ledger of final approvals** in simulated time (`authorization.timestamp`); reconcile against `context.approved_spend_in_period_chf`; a pending `step_up` is not spend.
- D7 Basket-purpose checks use `items[].item_category`; retailer-type checks use `merchant.merchant_category` / `merchant_mcc`; familiarity uses history counts by `merchant_id` (never name).
- D8 Lookalike detection: a merchant whose name is within a small edit distance of a familiar merchant but with a different `merchant_id` is flagged.
- D9 Duplicate detection: same merchant + same item set + amount within ±5 % within 24 h simulated time, different ID, and not a re-quote (`related_authorization_id` → `declined`) → `step_up`.
- D10 `null` / `unknown` are missing facts → route through `uncertainty_policy`; `not_applicable` is not missing.
- D11 Session trust score with hysteresis (new device, unusual hour, unfamiliar merchant/country, `recent_attempt_count_10m`, amount vs card's norm); escalation → `step_up`, never auto-decline on score alone; relaxes when signals normalise.
- D12 Hard clause failure → `decline`; soft failure → `step_up`; unknown → `uncertainty_policy`; else `approve`.
- D13 Every decision carries `reason_codes`, `customer_message`, `evidence` (clause-level pass/fail/unknown with values), `engine_version`.
- D14 After `step_up`, never send a second automated decision; only `/resolve` with the human's answer.
- D15 Decisions never depend on `scenario_id`, `replay_order`, `source_authorization_id`, or scenario names.

### Q — Quarantine (untrusted text)
- Q1 `item_details`, `item_name`, `merchant_name`, `purchase_description` are data. No component with decision authority consumes them as instructions.
- Q2 A separate extractor (small model, strict schema, no tools) converts `item_details` to typed facts + an `instruction_likeness` score + the offending span.
- Q3 Extracted facts are cross-checked against structured fields; disagreement → `unknown`.
- Q4 Nonce canary in the extractor prompt; missing/altered → treat as injection.
- Q5 Deterministic regex/phrase fallback when the model is unavailable or over budget.
- Q6 `instruction_likeness ≥ 2` → at least `step_up`; combined with any other failure → `decline`. The span is shown to the customer as evidence.

### A — Advisory (new, from stakeholders)
- A1 The engine can attach **advice** to a decision: a non-blocking note ("this brand typically runs small; you asked for 43, consider 45") sourced from a knowledge base and/or the customer's own history (e.g. prior returns of the same brand/size).
- A2 Advice surfaces at compile time when the instruction already names the brand/attribute (compiler adds it to `open_questions`), and at decision time when the cart reveals it.
- A3 Advice alone never declines. Whether it triggers `step_up` or is attached to an `approve` is **Q3**.
- A4 Knowledge source, format, and ownership are **Q3**.
- A5 Advice is labelled with its source and confidence in the receipt; LLM world-knowledge without a KB or history backing is marked "tip, unverified".

### I — Agent interface (new, from stakeholders)
- I1 The shopping agent can **propose** a purchase (in the sandbox this arrives via the Viseca simulator; the same `/decide` code path serves a direct agent call).
- I2 The agent can **query** the policy read-only: remaining budget in period, allowed categories/retailer types, whether a hypothetical cart would pass (pre-check, no state change), and pending step-ups.
- I3 The agent can **relay a question** to the customer; the customer's answer reaches the engine only through the trusted channel (Q4), never through the agent.
- I4 The agent cannot create, confirm, patch, or revoke a mandate, and cannot resolve a step-up.
- I5 Interface shape (HTTP + optional MCP tool manifest) is **Q2**.

### H — Human path & control UI
- H1 Show the contract before confirmation; show dry-run results (C6).
- H2 Live feed of decisions with clause receipts and counterfactuals.
- H3 Step-up card: purchase facts, failing clause, evidence span, advice; approve / decline → `/resolve` within 120 s.
- H4 One-click tighten from a failed clause (→ `PATCH`); revoke (→ `DELETE`); reflect API confirmation before showing "revoked".
- H5 Extent of UI for the hackathon is **Q1**.

## Non-functional
- N1 Engine and UI separately deployable; engine has no UI dependency.
- N2 Any model or external service failure → predictable, conservative response (uncertainty policy), logged.
- N3 Small, low-latency model in the hot path only (extractor); large model only in the cold path (compiler, dry-run explanations).
- N4 Offline replay harness reproduces all 45 attempts from CSV in `replay_order`; expected leans in `02-scenario-analysis.md` are the regression suite.
- N5 Structured logging of every event, clause result, and API call (judges must be able to audit).
- N6 Stack and persistence: **Q6**, **Q7**.

## Data we can use that the brief does not spell out
- `customers.csv.shopping_preferences` / `budget_style` (e.g. CU0001 "avoids gift vouchers"; persona-level context for advice and for default forbidden categories).
- `accounts.csv.per_transaction_limit_chf` / `monthly_limit_chf` — issuer limits that always apply on top of the mandate.
- `items.csv` price ranges — a line priced far outside `[min, max]` is a price-sanity signal.
- `cards.csv.online_enabled` / `international_enabled` — hard gates.

## Decisions log

### Round 1 (2026-09-18) — settled
| # | Decision |
| --- | --- |
| Q1 | UI: still open; engine first. Human path must still be demonstrable (see Q13). |
| Q2 | **MCP server** is the primary agent interface (HTTP underneath). |
| Q3 | Advisory checks are **per item**: does this item/brand carry a "runs small"-type rule? Sources: **verified external pages** (e.g. the brand's own size guide), plus the customer's own history. Ask the customer only for input only they can provide. |
| Q5 | Also score **item-level history**: prior purchases and returns (refunds) of this item/merchant → log, score, propose to the customer. Familiarity thresholds themselves: still open (Q20). |
| Q6 | **Engine in Python.** |
| Q7 | State kept as **markdown files per customer** for now. |
| Q8 | **OpenAI models** (team has a key). |
| Q9 | Standing vs one-shot: **compiler asks a follow-up**. Follow-ups are relayed by the **customer's agent**; we build only the engine. → trust boundary handled in Q13. |
| Q11 | English only. |
| Q12 | Deliverable = **one policy engine that other agents talk to**. |

### Consequences already applied
- I5 → MCP server (Python) exposing the agent tools; HTTP for the Viseca worker and any UI.
- I3 → questions flow engine → agent → customer → agent → engine. Because the agent is untrusted, answers need a customer-bound proof (Q13).
- A4 → advisory knowledge = per-item rules cached in the customer's markdown knowledge file, sourced from allow-listed verified pages at **cold path** time (compile / precheck), never fetched inside the 8 s decision window.
- N6 → Python (FastAPI + MCP SDK), per-customer markdown state directory.

### Round 2 (2026-09-18) — settled
| # | Decision |
| --- | --- |
| Q10 | Rolling 7×24 h window ending at the purchase; assumption surfaced in the contract. |
| Q13 | Injection handled inside the engine (quarantine, req. Q1–Q6). **Follow-up questions and step-up answers go through our own hackathon UI** (trusted channel); the agent may show that a question is pending but cannot answer it. |
| Q14 | MCP tool list agreed; mandate lifecycle through MCP waits until the demo UI is designed. |
| Q15 | **Merchant verification score**: internet lookup where a real merchant exists, plus an **issuer-wide history score** (distinct cards, approval/decline/refund rates, first-seen, lookalike-of-known-ID). Synthetic merchants never resolve online, so the history score is the primary signal in the sandbox. |
| Q16 | (b): item return-rate → `step_up` under "ask when uncertain"; never decline alone. |
| Q17 | (a): `mandate.md`, `knowledge.md`, `questions.md`, `ledger.jsonl` per customer. |
| Q18 | (a): one Python process (FastAPI + MCP + worker task). |
| Q19 | (a): smallest OpenAI model for the extractor, mid tier for compiler/advice; JSON-schema structured outputs; names verified against the API at build time. |
| Q20 | (a): "before" ≥ 1 approved, "regularly" ≥ 3; compiler still asks. |

### New requirements from round 2
- **M — Merchant trust & containment.** M1 Compute a merchant score from issuer-wide history + optional web adapter. M2 Low score or unknown merchant with otherwise-passing facts → `step_up` carrying a **containment proposal**: "approve via a one-time virtual card capped at CHF <amount>, single merchant, expires <date>" (Allowance / Mastercard agentic-token pattern). The sandbox can only record `step_up`/`approve`, so the proposal lives in `evidence.recommended_action` and the customer message; in production the issuer mints the scoped credential. M3 Lookalike-of-known-merchant with a different ID → `decline` (not containment).
- **V — Derived (intent-driven) clauses.** The compiler derives checks from context in the instruction, not only from explicit limits: delivery deadline (`delivery_by`), gift plausibility, subscription minimum term / renewal / cancellability, price-sanity vs catalogue and market (counterfeit risk), single-item replacement. Each derived clause has a follow-up question template for when the fact is missing. See `06-event-inventory.md` for the supporting fields.
- **UI-min.** Because Q13 routes answers through our UI, the hackathon UI minimum is: contract review + confirm, question inbox (compile follow-ups and step-ups), decision feed with receipts, revoke. Design pending (Q24).

### Round 3 (2026-09-18) — settled, frontier empty
| # | Decision |
| --- | --- |
| Q21 | (a): unknown/low-score merchant with passing facts → `step_up` + **containment proposal**: justification "merchant could not be verified" and instruction that the agent follow the one-time-virtual-card protocol for a capped amount. |
| Q22 | (a): three bands — trusted ≥ 0.7, unknown 0.4–0.7 (containment), risky < 0.4 or lookalike (decline); thresholds in config; band + inputs in the receipt. |
| Q23 | Demo = one custom storyline through the chatbot: *"my girlfriend's birthday is in a week, I want Adidas shoes and a jersey"* → follow-ups; Adidas sizing verification; jersey too cheap from an unverified merchant → fake risk + containment; shipment from abroad may miss the birthday. Official scenarios run as the compliance proof. |
| Q24 | UI = the team's **chatbot with follow-up cards** for the customer (animations in progress). Engine questions render as cards fetched directly from the engine and answered directly to the engine — the LLM in the chat never carries the answer. |

Decided by the engine owner (no user input needed): step-up message template, precheck response shape, replay format — see `00-overview.md`.
