"""Application service: the operations shared by the HTTP API and the MCP
server. Keeps both interfaces thin and identical in behaviour."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from . import ENGINE_VERSION, config
from .cards import Cards
from .compiler import compile_instruction, compile_rules
from .data import ref
from .engine import decide
from .ledger import Ledger
from .models import AuthorizationEvent, CompiledMandate, MandateRule
from .state import CustomerState, render_contract
from .viseca import VisecaClient
from .worker import make_step_up_card, resolve_card

log = logging.getLogger("leash.service")
_client: VisecaClient | None = None


def client() -> VisecaClient:
    global _client
    if _client is None:
        _client = VisecaClient()
    return _client


# ------------------------------------------------------------------ mandates
def draft_mandate(customer_id: str, instruction: str, card_id: str | None = None, prefer_llm: bool = True) -> dict[str, Any]:
    m = compile_instruction(instruction, prefer_llm=prefer_llm and bool(config.OPENAI_API_KEY))
    m.customer_id, m.card_id = customer_id, card_id
    key = f"draft_{abs(hash(instruction)) % 10**8}"
    if client().configured:
        resp = client().create_mandate(instruction, [r.model_dump(mode="json", exclude_none=True) for r in m.hard_rules],
                                       m.uncertainty_policy, m.guidance, m.open_questions)
        m.draft_id = resp.get("draft_id") or (resp.get("data") or {}).get("draft_id")
        key = m.draft_id or key
    else:
        m.draft_id = key
    state = CustomerState(customer_id)
    state.save_mandate(m, key)
    cards = Cards(customer_id)
    qcards = [cards.create("question", q, "Only you can answer this.", options=["answer"], ref={"draft_id": key})
              for q in m.open_questions]
    return {"draft_id": key, "contract_markdown": render_contract(m), "mandate": m.model_dump(mode="json"),
            "questions": qcards, "compiled_by": m.compiled_by}


def request_confirmation(customer_id: str, draft_id: str) -> dict[str, Any]:
    m = CustomerState(customer_id).load_mandate(draft_id)
    if not m:
        raise KeyError(draft_id)
    pending = [c for c in Cards(customer_id).pending() if c["kind"] == "question" and c["ref"].get("draft_id") == draft_id]
    card = Cards(customer_id).create("confirm_mandate", "Confirm this wallet contract?", render_contract(m),
                                     options=["confirm", "reject"], ref={"draft_id": draft_id})
    return {"card": card, "unanswered_questions": len(pending)}


def confirm_mandate(customer_id: str, draft_id: str) -> dict[str, Any]:
    state = CustomerState(customer_id)
    m = state.load_mandate(draft_id)
    if not m:
        raise KeyError(draft_id)
    if client().configured and m.draft_id and not m.draft_id.startswith("draft_"):
        resp = client().confirm_mandate(m.draft_id)
        m.mandate_id = resp.get("mandate_id") or (resp.get("data") or {}).get("mandate_id")
    else:
        m.mandate_id = m.mandate_id or f"TM_LOCAL_{draft_id}"
    state.save_mandate(m, m.mandate_id)
    return {"mandate_id": m.mandate_id, "contract_markdown": render_contract(m)}


def get_policy(customer_id: str, mandate_id: str) -> dict[str, Any]:
    m = CustomerState(customer_id).find_mandate(mandate_id)
    if not m:
        raise KeyError(mandate_id)
    return {"mandate_id": mandate_id, "contract_markdown": render_contract(m), "intent": m.intent.model_dump(mode="json"),
            "hard_rules": [r.model_dump(mode="json", exclude_none=True) for r in m.hard_rules],
            "uncertainty_policy": m.uncertainty_policy, "assumptions": m.assumptions, "open_questions": m.open_questions}


def tighten_mandate(customer_id: str, mandate_id: str, *, add_rules: list[dict] | None = None,
                    uncertainty_policy: str | None = None) -> dict[str, Any]:
    state = CustomerState(customer_id)
    m = state.find_mandate(mandate_id)
    if not m:
        raise KeyError(mandate_id)
    if add_rules:
        m.hard_rules += [MandateRule.model_validate(r) for r in add_rules]
    if uncertainty_policy == "decline":
        m.uncertainty_policy = "decline"
    state.save_mandate(m, mandate_id)
    platform = None
    if client().configured and not mandate_id.startswith("TM_LOCAL_"):
        fields: dict[str, Any] = {}
        if add_rules:
            fields["hard_rules"] = [r.model_dump(mode="json", exclude_none=True) for r in m.hard_rules]
        if uncertainty_policy == "decline":
            fields["uncertainty_policy"] = "decline"
        platform = client().patch_mandate(mandate_id, **fields)
    return {"mandate_id": mandate_id, "contract_markdown": render_contract(m), "platform": platform}


def revoke_mandate(customer_id: str, mandate_id: str) -> dict[str, Any]:
    platform = client().revoke_mandate(mandate_id) if client().configured and not mandate_id.startswith("TM_LOCAL_") else None
    Cards(customer_id).create("info", "Contract revoked", f"Mandate {mandate_id} was revoked. The agent can no longer spend under it.",
                              options=[], ref={"mandate_id": mandate_id})
    return {"mandate_id": mandate_id, "revoked": True, "platform": platform}


# ------------------------------------------------------------------ budget & decisions
def remaining_budget(customer_id: str, mandate_id: str, as_of: datetime | None = None) -> dict[str, Any]:
    m = CustomerState(customer_id).find_mandate(mandate_id)
    if not m:
        raise KeyError(mandate_id)
    led = Ledger(customer_id)
    as_of = as_of or datetime.now(timezone.utc)
    out: dict[str, Any] = {"per_order_cap_chf": m.intent.per_order_cap_chf}
    if m.intent.period:
        spent, rows = led.approved_in_window(mandate_id, as_of, m.intent.period.days)
        out["period"] = {"days": m.intent.period.days, "cap_chf": m.intent.period.cap_chf, "approved_chf": spent,
                         "remaining_chf": round(m.intent.period.cap_chf - spent, 2), "counted": [r["authorization_id"] for r in rows]}
    out["pending_step_ups"] = [r["authorization_id"] for r in led.rows_for_mandate(mandate_id) if r["final_status"] == "pending"]
    return out


def cart_to_event(customer_id: str, mandate_id: str, cart: dict[str, Any], *, authorization_id: str | None = None) -> AuthorizationEvent:
    """Agent-supplied cart (merchant + items + optional terms) → a live-shaped event."""
    r = ref()
    m = CustomerState(customer_id).find_mandate(mandate_id)
    if not m:
        raise KeyError(mandate_id)
    now = datetime.now(timezone.utc)
    merchant = cart["merchant"]
    if isinstance(merchant, str):
        merchant = {**r.merchants[merchant]}
    items = []
    for n, it in enumerate(cart["items"], 1):
        cat = it.get("item_category") or (r.items.get(it.get("item_id", ""), {}).get("item_category", "other"))
        items.append({"line_no": n, "item_id": it.get("item_id", f"IT_AGENT_{n}"), "item_name": it["item_name"], "item_category": cat,
                      "quantity": int(it.get("quantity", 1)), "unit_price": float(it["unit_price"]), "currency": it.get("currency", "CHF"),
                      "item_details": it.get("item_details", "")})
    subtotal = round(sum(i["unit_price"] * i["quantity"] for i in items), 2)
    delivery = float(cart.get("delivery_fee", 0.0))
    currency = items[0]["currency"]
    amount = round(subtotal + delivery, 2)
    from .data import chf
    auth_id = authorization_id or f"AU_AGENT_{int(now.timestamp() * 1000)}"
    card_id = m.card_id or cart.get("card_id") or ""
    return AuthorizationEvent.model_validate({
        "type": "authorization.request", "request_id": f"req_{auth_id}", "deadline_at": (now + timedelta(seconds=8)).isoformat(),
        "authorization": {
            "authorization_id": auth_id, "source_authorization_id": auth_id, "scenario_id": "AGENT", "replay_order": 1,
            "mandate_id": mandate_id, "profile_id": "PROFILE_AGENT", "card_id": card_id, "initiator_type": "agent",
            "merchant": {k: merchant.get(k, d) for k, d in (("merchant_id", "ME_AGENT"), ("merchant_name", "?"), ("merchant_category", "other"),
                                                             ("merchant_mcc", "0000"), ("merchant_country", "CH"), ("merchant_city", ""),
                                                             ("availability", "online"), ("recurring_capable", "false"))},
            "timestamp": cart.get("timestamp", now.isoformat()), "amount": amount, "currency": currency,
            "billing_amount_chf": chf(amount, currency, r.fx), "items_subtotal": subtotal, "delivery_fee": delivery,
            "channel": "ecommerce", "customer_device_id": cart.get("customer_device_id", "DVC-AGENT"),
            "authority_status": "active", "card_status_at_attempt": "active", "spend_in_period_before_chf": None,
            "recent_attempt_count_10m": int(cart.get("recent_attempt_count_10m", 0)),
            "fulfillment_method": cart.get("fulfillment_method", "delivery"), "delivery_by": cart.get("delivery_by"),
            "order_returnable": cart.get("order_returnable", "unknown"), "order_cancellable": cart.get("order_cancellable", "unknown"),
            "related_authorization_id": cart.get("related_authorization_id"), "related_authorization_status": cart.get("related_authorization_status"),
            "purchase_description": cart.get("purchase_description", "agent purchase"), "items": items},
        "mandate": {"mandate_id": mandate_id, "status": "active", "customer_id": customer_id, "card_id": card_id, "instruction": m.instruction,
                    "hard_rules": [x.model_dump(mode="json", exclude_none=True) for x in m.hard_rules], "uncertainty_policy": m.uncertainty_policy,
                    "profile_id": "PROFILE_AGENT"},
        "context": {"approved_spend_in_period_chf": None, "recent_authorizations": []},
        "runtime": {"received_at": now.isoformat(), "history_window_minutes": 10, "context_basis": "run_decisions_and_scenario_timestamps"},
    })


def precheck(customer_id: str, mandate_id: str, cart: dict[str, Any]) -> dict[str, Any]:
    """Evaluate without recording: run the engine against a scratch ledger."""
    import shutil, tempfile
    from . import ledger as L
    ev = cart_to_event(customer_id, mandate_id, cart)
    real_ledger = Ledger(customer_id)
    tmp = tempfile.mkdtemp(prefix="leash_precheck_")
    try:
        # copy ledger so period/duplicate context is real, but nothing is written back
        scratch = L.Ledger(customer_id, state_dir=__import__("pathlib").Path(tmp))
        if real_ledger.path.exists():
            shutil.copy(real_ledger.path, scratch.path)
        orig = L.Ledger.__init__
        L.Ledger.__init__ = lambda self, cid, state_dir=__import__("pathlib").Path(tmp): orig(self, cid, state_dir)  # type: ignore
        try:
            r = decide(ev, allow_model=bool(config.OPENAI_API_KEY))
        finally:
            L.Ledger.__init__ = orig  # type: ignore
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    d = r.model_dump(mode="json")
    d["decision"] = {"approve": "would_approve", "decline": "would_decline", "step_up": "would_ask"}[r.decision]
    d["blocking_clauses"] = [c for c in d["evidence"] if c["status"] in ("fail", "unknown")]
    return d


def propose_purchase(customer_id: str, mandate_id: str, cart: dict[str, Any]) -> dict[str, Any]:
    ev = cart_to_event(customer_id, mandate_id, cart)
    r = decide(ev, allow_model=bool(config.OPENAI_API_KEY))
    card = make_step_up_card(customer_id, ev, r) if r.decision == "step_up" else None
    d = r.model_dump(mode="json")
    d["card"] = card
    return d


def decide_event(event: dict[str, Any], run_id: str | None = None) -> dict[str, Any]:
    ev = AuthorizationEvent.model_validate(event)
    r = decide(ev, run_id=run_id, allow_model=bool(config.OPENAI_API_KEY))
    if r.decision == "step_up":
        make_step_up_card(ev.mandate.customer_id, ev, r)
    return r.model_dump(mode="json")


def explain(customer_id: str, authorization_id: str) -> dict[str, Any]:
    row = Ledger(customer_id).get(authorization_id)
    if not row:
        raise KeyError(authorization_id)
    return row


# ------------------------------------------------------------------ cards
def answer_card(customer_id: str, card_id: str, answer: str, note: str = "") -> dict[str, Any]:
    cards = Cards(customer_id)
    card = cards.answer(card_id, answer, note)
    if not card:
        raise KeyError(card_id)
    out: dict[str, Any] = {"card": card}
    if card["kind"] == "step_up":
        out["resolution"] = resolve_card(client(), customer_id, card, answer, note)
    elif card["kind"] == "confirm_mandate" and answer == "confirm":
        out["mandate"] = confirm_mandate(customer_id, card["ref"]["draft_id"])
    elif card["kind"] == "question":
        CustomerState(customer_id).add_question({"id": card_id, "question": card["title"], "answer": answer, "note": note})
    return out


def status() -> dict[str, Any]:
    return {"engine_version": ENGINE_VERSION, "openai": bool(config.OPENAI_API_KEY), "sandbox": client().configured,
            "extractor_model": config.EXTRACTOR_MODEL, "compiler_model": config.COMPILER_MODEL, "state_dir": str(config.STATE_DIR)}
