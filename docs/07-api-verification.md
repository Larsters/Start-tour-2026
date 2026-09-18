# API key verification report

**Team:** LookingForVCs (`team_4bd47d52`)
**Date:** 2026-09-18
**Prepared for:** the maintainer of the Agent on a Leash sandbox API
**Credential:** team bearer key `leash_…` (redacted; identify us by `team_4bd47d52`)

We received our team key and verified it against the sandbox before the event.
Every documented endpoint works. This report records what we exercised, the
results, and **one documentation defect that will cost other teams time**.

---

## 1. Headline finding: the documented base URL rejects our key

`technical_details.md` (step 4, "Connect to the API") instructs teams to use:

```
export LEASH_BASE_URL="https://saw26api.ashyground-364e1d07.switzerlandnorth.azurecontainerapps.io"
```

That host is **alive and healthy** but **rejects the key we were issued**:

```
$ curl https://saw26api.ashyground-364e1d07.switzerlandnorth.azurecontainerapps.io/healthz
{"status":"ok","service":"saw26-sandbox","api_version":"0.1.0","pack_version":"saw26"}
HTTP 200

$ curl -H "Authorization: Bearer leash_…" \
    https://saw26api.ashyground-364e1d07.switzerlandnorth.azurecontainerapps.io/v1/bootstrap
{"error":{"code":"unauthorized","message":"A valid team bearer token is required"}}
HTTP 401
```

The host our key authenticates against is the one supplied alongside the key:

```
https://leash-api-production.up.railway.app
```

Both hosts self-report identically (`service: saw26-sandbox`, `api_version:
0.1.0`, `pack_version: saw26`), so `/healthz` gives teams no signal that they
are pointed at the wrong deployment.

**Why this matters.** A team following the written instructions gets a healthy
`/healthz`, then a `401` on the first authenticated call. The natural reading of
`"A valid team bearer token is required"` is *"my key is broken"* — not *"I am
talking to the wrong host"*. We expect support questions from this.

**Checked before reporting:** `START-Hack/viseca-2026` is at its single commit
`Initial commit` (2026-09-18T13:10:40Z), and our local `technical_details.md` is
byte-identical to the upstream copy. The document has not been revised.

**Suggested fix:** update the `LEASH_BASE_URL` export in step 4, or have the
Azure host return a distinguishable error (e.g. `wrong_environment`) rather than
a generic `unauthorized`.

---

## 2. Endpoint conformance

All calls against `https://leash-api-production.up.railway.app` with our key.

| Method | Path | Result |
| --- | --- | --- |
| `GET` | `/healthz` | 200 — `api_version 0.1.0`, `pack_version saw26` |
| `GET` | `/v1/bootstrap` | 200 — team, 5 scenarios, timeouts, limits, features |
| `GET` | `/v1/reference-data` | 200 — 8 catalogues |
| `GET` | `/v1/reference-data/authorization-history.csv` | 200 — 4,702 lines |
| `POST` | `/v1/mandates` | 201 — returns `draft_id` |
| `POST` | `/v1/mandates/{draft_id}/confirm` | 200 — returns `mandate_id`, `status: active` |
| `GET` | `/v1/mandates/{mandate_id}` | 200 |
| `PATCH` | `/v1/mandates/{mandate_id}` | 200 on tighten · 409 on weaken (see §3) |
| `DELETE` | `/v1/mandates/{mandate_id}` | 200 — `status: revoked` |
| `POST` | `/v1/scenario-runs` | 201 — returns `run_id` |
| `GET` | `/v1/scenario-runs/{run_id}` | 200 — counters advance to `completed` |
| `GET` | `/v1/decision-requests/next?wait=25` | 200 with work · 204 when idle |
| `POST` | `/v1/authorizations/{id}/decision` | 200 — decision recorded |
| `GET` | `/v1/authorizations` | 200 |
| `GET` | `/v1/events?since=0` | 200 — 15 events |

Reference-data catalogues returned: `pack_version`, `scenarios`,
`scenario_authorities`, `fx_rates`, `merchants`, `items`, `customers`,
`accounts`.

Scenario inventory matches the data pack: `SCEN0000` 1, `SCEN0001` 10,
`SCEN0002` 12, `SCEN0003` 11, `SCEN0004` 11 — 45 purchases.

Bootstrap values observed: `decision_timeout_seconds 8.0`,
`human_timeout_seconds 120.0`, `long_poll_max_wait_seconds 25.0`,
`redelivery_after_seconds 3.0`; `max_active_runs 3`, `max_runs_per_team 300`,
`rate_limit_per_minute 1200`, `max_request_body_bytes 65536`,
`events_page_size 200`; `judging_mode false`, `team_reset_enabled true`,
`mandate_patch_tighten_only true`,
`revoked_mandate_cancels_remaining_purchases true`, `history_window_minutes 10`.

---

## 3. Guardrails confirmed working

These are the behaviours we most wanted to trust, and the platform enforces them
server-side rather than leaving them to us. Reporting them as **working as
documented**, verbatim:

**Uncertainty policy cannot be weakened** — after tightening `ask` → `decline`,
attempting `decline` → `ask`:

```
409 {'code': 'policy_cannot_be_weakened',
     'message': "uncertainty_policy can only change from 'approve' or 'ask' to 'decline' (current: 'decline')"}
```

**Hard rules cannot be removed** — `PATCH` with an empty `hard_rules`:

```
409 {'code': 'rules_cannot_be_removed',
     'message': 'Every existing hard rule must be kept unchanged; you may only add rules',
     'missing_rules': [{'field': 'authorization.billing_amount_chf', 'operator': '<=',
                        'value': 20, 'currency': 'CHF', 'scope': 'purchase'}]}
```

The `missing_rules` echo is a good touch — it names exactly which rule was
dropped, so the client can repair the request without guessing.

**Long-polling** — `/v1/decision-requests/next?wait=25` returns `204` on a
25-second cadence while no run is active, and returns `200` early once a run
queues work. Matches the documented contract.

---

## 4. Live scenario runs

Two runs of `SCEN0000` (connection check), both end-to-end through our worker:

| Run | Result |
| --- | --- |
| `RUN_bf7d23d013c084fb` | `completed` — 1 generated, 1 approved, 0 timed out |
| `RUN_2f957586e57f28c1` | `completed` — 1 generated, 1 approved, 0 timed out |

Authorization `LA_41dcbff8cdaf7709` (source `AU0001`) decided `approve` in
**46 ms** against the 8-second deadline, `decided_by: engine`, evidence attached.
Mandates created during verification: `TM_a0003f231810bc71`,
`TM_b65477398f298ca8`, plus two throwaway mandates created solely to probe
`PATCH`/`DELETE` and revoked immediately afterwards.

---

## 5. Not exercised

Recorded for completeness — absence of a result here is not a defect report:

- `POST /v1/authorizations/{id}/resolve` — the human answer path. Our
  step-up flow has not yet produced a live step-up to resolve.
- `POST /v1/team/reset` — deliberately not called; we did not want to clear
  state mid-verification.
- `SCEN0001`–`SCEN0004` — validated offline against the CSV pack, not yet run
  live.

---

## 6. Reproducing this

```bash
export LEASH_BASE_URL="https://leash-api-production.up.railway.app"
export TEAM_API_KEY="<our team key>"

api() {
  curl --fail-with-body --silent --show-error --max-time 30 \
    -H "Authorization: Bearer $TEAM_API_KEY" \
    -H "Content-Type: application/json" \
    "$LEASH_BASE_URL$1" "${@:2}"
}

curl --silent "$LEASH_BASE_URL/healthz"
api /v1/bootstrap
api /v1/reference-data
```

---

## Summary

The key works and the API behaves as documented on every endpoint we called. The
only action we would ask for is **§1** — the base URL in `technical_details.md`
points at a deployment that our key cannot authenticate against, and the failure
mode looks like a bad key rather than a wrong host.

Happy to re-run any of this or test `/resolve` against a scenario of your
choosing. Contact us as team `team_4bd47d52` (LookingForVCs).
