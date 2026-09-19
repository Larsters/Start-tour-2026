# Leash — issuer-side policy engine for agentic payments

Decides whether an AI shopping agent may spend a customer's money:
**approve / decline / step_up**, explained clause by clause. Built for the
Viseca "Agent on a Leash" challenge (START Hack × Swiss {ai} Weeks 2026).
Design docs: `../docs/00-overview.md`.

## Run

```bash
uv venv && uv pip install -e ".[dev]"

# offline: replay all 45 fixture purchases through the engine
.venv/bin/python -m leash.replay                # add --model to use the OpenAI extractor
.venv/bin/pytest -q

# HTTP API + customer cards (+ worker if TEAM_API_KEY is set)
# Credentials live in leash/.env (gitignored), read automatically by leash.config:
#   LEASH_BASE_URL=https://leash-api-production.up.railway.app
#   TEAM_API_KEY=leash_...           # enables the sandbox worker
#   OPENAI_API_KEY=...               # optional: compiler + extractor use models, else rules/regex
#   OPENAI_API_KEY_2=...             # optional: tried automatically if the primary key fails (rate limit/quota/auth)
.venv/bin/uvicorn leash.api:app --reload --port 8080
# GET / → service index; GET /health; customer cards GET /cards?customer_id=CU0001

# live connection check against the sandbox (SCEN0000)
.venv/bin/python -c "from leash.viseca import VisecaClient; print(VisecaClient().bootstrap()['team'])"

# MCP server for the shopping agent (stdio)
.venv/bin/python -m leash.mcp_server
```

## Layout

| Module | Role |
| --- | --- |
| `models.py` | Strict pydantic models of the sponsor's event schema, IntentSpec, receipt |
| `data.py` | CSV pack, card baselines, issuer-wide merchant stats, FX |
| `compiler.py` | Instruction → mandate (rules always; OpenAI when a key is set) |
| `quarantine.py` | Untrusted merchant text → typed facts + injection score (regex fallback, model optional) |
| `clauses.py` | Contract clauses: amount, period, split order, basket, item, terms, add-ons, retailer, familiarity, price sanity, delivery, subscription |
| `trust.py` | Merchant trust bands, lookalike detection, session thermostat |
| `engine.py` | Combine → receipt; idempotent via the ledger |
| `ledger.py` / `state.py` / `cards.py` | Per-customer JSONL ledger, markdown mandate/knowledge, customer cards |
| `viseca.py` / `worker.py` | Sandbox client and long-poll worker |
| `advisor.py` | Cold-path web lookups (brand sizing, market price, merchant reputation) → hints + `knowledge.md` |
| `service.py` | Operations shared by HTTP and MCP: draft, answers, dry-run, confirm, precheck, propose, cards |
| `api.py` | FastAPI: decide, precheck, propose, mandates, dry-run, cards, ledger |
| `live.py` | Live sandbox runner (observe the API worker, or poll here) |
| `report.py` | Platform report: latest completed run per scenario vs our leans, timing, fallbacks |
| `agent.py` / `demo_catalogue.py` / `mockshop.py` | The demo shopping agent (OpenAI tool loop) and its mock shop: nine fixed products + LLM-generated listings for any other query |
| `events.py` / `web/index.html` | Event bus and the split-screen demo page (`/app`) |
| `mcp_server.py` | MCP tools for the agent |
| `replay.py` | Offline replay + expected-lean diff (`fixtures/expected/*.yaml`) |

## Decision rule

any hard-fail clause → `decline` · any soft-fail → `step_up` · any unknown →
customer's `uncertainty_policy` · session escalated → `step_up` · else `approve`.
Injection is never an input, only a signal; combined with another failing
clause it declines.

Unknown-but-clean merchants get a `step_up` with a **containment proposal**
(one-time virtual card capped at the order amount, this seller only).
