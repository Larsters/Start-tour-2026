# 01 · The brief, decoded

Case: **Agent on a Leash** — Viseca × START Hack × Swiss {ai} Weeks 2026.
Source: `viseca-2026/challenge.md`, `viseca-2026/technical_details.md`.

## What Viseca is asking for (and what it is not)

**In scope:** an issuer-side *wallet control layer* that sits between an AI
shopping agent and the customer's card and answers, per proposed purchase,
`approve` / `decline` / `step_up` inside an 8-second deadline.

**Out of scope:** the shopping agent itself, merchant discovery, checkout.
The agent and the shop cannot touch the policy. We never see the agent's
reasoning; we only see the authorization it proposes.

Why this matters to Viseca: Visa Intelligent Commerce and Mastercard Agent Pay
both issue *agent-scoped tokens*. In Mastercard's model the **issuer** holds the
consent policy, runs step-up rules, and performs revocation. Viseca is one of
Switzerland's largest Visa/Mastercard issuers. The case is literally: *build the
issuer-side mandate engine we will need when agentic tokens land on our cards.*
That is the framing for the pitch.

## The two jobs

| Job | Input | Output | Latency |
| --- | --- | --- | --- |
| **Policy compiler** | Customer's natural-language instruction | Structured mandate (`hard_rules`, `uncertainty_policy`, `guidance`, `open_questions`), shown for confirmation; later tighten (`PATCH`) or revoke (`DELETE`) | Doesn't matter |
| **Decision engine** | Live `authorization.request` event (purchase + mandate snapshot + run context) | `approve` / `decline` / `step_up` + `reason_codes` + `customer_message` + `evidence` | < 8 s from queueing, must degrade gracefully if any model/service is down |

Plus a **worker** (long-poll loop, idempotent by live `authorization_id`) and a
**human path** (`/resolve` after `step_up`, 120 s window).

## Hard constraints from the brief

- No hard-coding to scenario names, IDs, or replay position.
- No answer key exists. Over-blocking ordinary shopping is a failure, same as under-blocking.
- All shop-provided text (`item_details`) is untrusted; extract facts, never obey.
- Count only *final approvals* toward spend limits; `step_up` is paused, not approved.
- Use simulated `authorization.timestamp` for spend windows/velocity; real clock only for deadlines.
- Recognise redelivery of the same live `authorization_id`; record once.
- Similar-but-distinct purchases can still be unwanted duplicates.
- `null` ≠ zero ≠ permission. `unknown` ≠ `not_applicable`.
- Join on IDs, never names (there is a deliberate lookalike merchant pair).
- Rule format is fixed (`field`, `operator`, `value`, optional `currency`, `scope`, `period_days`); no extra fields.
- PATCH can only add rules or tighten `uncertainty_policy` → `decline`. Never weaken.
- UI and engine should be separately deployable. Small, low-latency models preferred if any.

## What judges will score (from "What to show in your demo")

1. An ordinary purchase completes with little friction.
2. An ambiguous / unsafe / manipulated purchase gets a *useful* intervention.
3. The customer can approve, reject, or revoke.
4. For every result: what was allowed, which facts were used, why. Uncertainty visible. Customer visibly in control.

Implication: **explainability is not a feature, it is the product.** Every
decision needs a legible evidence trail a non-engineer judge can follow.

## API surface we depend on

```
GET  /healthz                                   liveness (no key)
GET  /v1/bootstrap                              timeouts, limits, features
GET  /v1/reference-data(.../authorization-history.csv)
POST /v1/mandates                               draft
POST /v1/mandates/{draft_id}/confirm            → mandate_id
GET/PATCH/DELETE /v1/mandates/{mandate_id}
POST /v1/scenario-runs                          {scenario_id, mandate_id} → run_id
GET  /v1/scenario-runs/{run_id}
GET  /v1/decision-requests/next?wait=25         200 envelope | 204
POST /v1/authorizations/{id}/decision
POST /v1/authorizations/{id}/resolve            human answer after step_up
GET  /v1/authorizations, GET /v1/events?since=
POST /v1/team/reset                             dev only
```

Base URL: `https://saw26api.ashyground-364e1d07.switzerlandnorth.azurecontainerapps.io`
Team key issued on event day.
