"""HTTP surface: the sponsor-shaped decision path, the customer cards (trusted
channel), and mandate lifecycle. The MCP server exposes the same service."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

from . import agent, events, service
from .cards import Cards
from .ledger import Ledger
from .worker import Worker

log = logging.getLogger("leash.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    app.state.worker = None
    if service.client().configured:
        app.state.worker = Worker(service.client())
        app.state.worker.start()
        log.info("sandbox worker started")
    else:
        log.info("TEAM_API_KEY not set: worker disabled, running offline")
    yield
    if app.state.worker:
        app.state.worker.stop_event.set()


app = FastAPI(title="Leash policy engine", version="0.1", lifespan=lifespan)


class DraftIn(BaseModel):
    customer_id: str
    instruction: str
    card_id: str | None = None
    prefer_llm: bool = True


class CartIn(BaseModel):
    customer_id: str
    mandate_id: str
    cart: dict[str, Any]


class EventIn(BaseModel):
    event: dict[str, Any]
    run_id: str | None = None


class AnswerIn(BaseModel):
    answer: str
    note: str = ""


class ChatIn(BaseModel):
    customer_id: str
    message: str
    card_id: str | None = None


class TightenIn(BaseModel):
    add_rules: list[dict[str, Any]] | None = None
    uncertainty_policy: str | None = None


def _404(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except KeyError as e:
        raise HTTPException(404, f"not found: {e}")


@app.get("/")
def index():
    """Service index. Without it a browser hitting / just logs a 404."""
    return {"service": "leash", "docs": "/docs", "health": "/health",
            "endpoints": sorted({r.path for r in app.routes if r.path not in ("/", "/openapi.json")}),
            **service.status()}


@app.get("/health")
def health():
    return {"ok": True, **service.status()}


# mandates
@app.post("/mandates/draft")
def draft(body: DraftIn):
    return service.draft_mandate(body.customer_id, body.instruction, body.card_id, body.prefer_llm)


@app.post("/mandates/{draft_id}/request-confirmation")
def request_confirmation(draft_id: str, customer_id: str):
    return _404(service.request_confirmation, customer_id, draft_id)


@app.post("/mandates/{draft_id}/dry-run")
def dry_run(draft_id: str, customer_id: str):
    return _404(service.dry_run, customer_id, draft_id)


@app.get("/mandates/{mandate_id}")
def get_policy(mandate_id: str, customer_id: str):
    return _404(service.get_policy, customer_id, mandate_id)


@app.get("/mandates/{mandate_id}/budget")
def budget(mandate_id: str, customer_id: str):
    return _404(service.remaining_budget, customer_id, mandate_id)


@app.patch("/mandates/{mandate_id}")
def tighten(mandate_id: str, customer_id: str, body: TightenIn):
    return _404(service.tighten_mandate, customer_id, mandate_id, add_rules=body.add_rules, uncertainty_policy=body.uncertainty_policy)


@app.delete("/mandates/{mandate_id}")
def revoke(mandate_id: str, customer_id: str):
    return service.revoke_mandate(customer_id, mandate_id)


# decisions
@app.post("/decide")
def decide(body: EventIn):
    return service.decide_event(body.event, body.run_id)


@app.post("/precheck")
def precheck(body: CartIn):
    return _404(service.precheck, body.customer_id, body.mandate_id, body.cart)


@app.post("/propose")
def propose(body: CartIn):
    return _404(service.propose_purchase, body.customer_id, body.mandate_id, body.cart)


@app.get("/authorizations/{authorization_id}")
def explain(authorization_id: str, customer_id: str):
    return _404(service.explain, customer_id, authorization_id)


@app.get("/ledger")
def ledger(customer_id: str, mandate_id: str | None = None):
    led = Ledger(customer_id)
    rows = led.rows_for_mandate(mandate_id) if mandate_id else sorted(led._rows.values(), key=lambda r: r["sim_ts"])
    return [{k: r[k] for k in ("authorization_id", "mandate_id", "sim_ts", "amount_chf", "merchant_id", "decision", "final_status")}
            | {"reason_codes": r["receipt"]["reason_codes"], "customer_message": r["receipt"]["customer_message"]} for r in rows]


# cards (trusted customer channel)
@app.get("/cards")
def cards(customer_id: str, pending_only: bool = True):
    c = Cards(customer_id)
    return c.pending() if pending_only else c.list()


@app.post("/cards/{card_id}/answer")
def answer(card_id: str, customer_id: str, body: AnswerIn):
    return _404(service.answer_card, customer_id, card_id, body.answer, body.note)


# sandbox helpers
@app.post("/sandbox/runs")
def start_run(scenario_id: str, mandate_id: str):
    if not service.client().configured:
        raise HTTPException(400, "TEAM_API_KEY not set")
    return service.client().start_run(scenario_id, mandate_id)


@app.get("/sandbox/runs/{run_id}")
def run_status(run_id: str):
    return service.client().run(run_id)


# ------------------------------------------------------------------ web app + chat + events
WEB = Path(__file__).resolve().parent.parent / "web"


@app.get("/")
def root():
    return RedirectResponse("/app")


@app.get("/app")
def web_app():
    return FileResponse(WEB / "index.html")


@app.post("/chat")
def chat(body: ChatIn):
    if not service.config.OPENAI_API_KEY:
        raise HTTPException(400, "OPENAI_API_KEY not set")
    return agent.chat(body.customer_id, body.message, body.card_id)


@app.post("/chat/reset")
def chat_reset(customer_id: str, wipe_state: bool = False):
    agent.reset(customer_id)
    if wipe_state:
        import shutil
        shutil.rmtree(service.config.STATE_DIR / customer_id, ignore_errors=True)
    events.publish(customer_id, "session.reset")
    return {"ok": True}


@app.get("/events")
def get_events(customer_id: str, since: int = 0):
    return {"events": events.since(customer_id, since), "latest": events.latest_seq()}


@app.get("/customers")
def customers():
    from .data import ref
    r = ref()
    out = []
    for cid, c in r.customers.items():
        cards = [k for k, b in r.baselines.items() if b.customer_id == cid]
        out.append({"customer_id": cid, "name": c["persona_name"], "card_id": cards[0] if cards else None, "preferences": c["shopping_preferences"]})
    return out
