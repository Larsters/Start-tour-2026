"""Application service: the operations shared by the HTTP API and the MCP
server. Keeps both interfaces thin and identical in behaviour."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from . import ENGINE_VERSION, config, events
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
    events.publish(customer_id, "compile.started", instruction=instruction)
    m = compile_instruction(instruction, prefer_llm=prefer_llm and bool(config.OPENAI_API_KEY))
    m.customer_id, m.card_id = customer_id, card_id
    key = f"draft_{abs(hash(instruction)) % 10**8}"
    m.draft_id = key
    state = CustomerState(customer_id)
    state.save_mandate(m, key)
    cards = Cards(customer_id)
    qcards = [cards.create("question", q, "Only you can answer this.", options=["answer"], ref={"draft_id": key})
              for q in m.open_questions]
    events.publish(customer_id, "compile.done", draft_id=key, contract_markdown=render_contract(m), intent=m.intent.model_dump(mode="json"),
                   questions=[q["title"] for q in qcards], assumptions=m.assumptions, compiled_by=m.compiled_by)
    return {"draft_id": key, "contract_markdown": render_contract(m), "mandate": m.model_dump(mode="json"),
            "questions": qcards, "compiled_by": m.compiled_by}


def apply_question_answer(customer_id: str, draft_id: str, question: str, answer: str) -> dict[str, Any]:
    from .compiler import apply_answer
    state = CustomerState(customer_id)
    m = state.load_mandate(draft_id)
    if not m:
        raise KeyError(draft_id)
    m, note = apply_answer(m, question, answer)
    state.save_mandate(m, draft_id)
    events.publish(customer_id, "contract.updated", draft_id=draft_id, note=note, contract_markdown=render_contract(m))
    return {"draft_id": draft_id, "note": note, "contract_markdown": render_contract(m), "intent": m.intent.model_dump(mode="json"),
            "remaining_questions": m.open_questions}


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
    if client().configured:
        resp = client().create_mandate(m.instruction, [r.model_dump(mode="json", exclude_none=True) for r in m.hard_rules],
                                       m.uncertainty_policy, m.guidance, m.open_questions)
        platform_draft = resp.get("data", resp)["draft_id"]
        conf = client().confirm_mandate(platform_draft)
        m.mandate_id = conf.get("data", conf).get("mandate_id")
        m.draft_id = platform_draft
    else:
        m.mandate_id = m.mandate_id or f"TM_LOCAL_{draft_id}"
    state.save_mandate(m, m.mandate_id)
    events.publish(customer_id, "mandate.confirmed", mandate_id=m.mandate_id, contract_markdown=render_contract(m))
    return {"mandate_id": m.mandate_id, "draft_id": m.draft_id, "contract_markdown": render_contract(m)}


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
    events.publish(customer_id, "mandate.revoked", mandate_id=mandate_id)
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


def _hints(customer_id: str, mandate_id: str, cart: dict[str, Any], advise: bool):
    if not advise or not config.OPENAI_API_KEY:
        return None
    from .advisor import hints_for_cart
    m = CustomerState(customer_id).find_mandate(mandate_id)
    has_item = all(it.get("item_id") in ref().items for it in cart.get("items", []))
    return hints_for_cart(CustomerState(customer_id), cart, m.intent, catalogue_has_item=has_item)


def precheck(customer_id: str, mandate_id: str, cart: dict[str, Any], advise: bool = True) -> dict[str, Any]:
    """Evaluate without recording: run the engine against a scratch ledger."""
    import shutil, tempfile
    from . import ledger as L
    ev = cart_to_event(customer_id, mandate_id, cart)
    hints = _hints(customer_id, mandate_id, cart, advise)
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
            r = decide(ev, allow_model=bool(config.OPENAI_API_KEY), hints=hints)
        finally:
            L.Ledger.__init__ = orig  # type: ignore
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    d = r.model_dump(mode="json")
    d["decision"] = {"approve": "would_approve", "decline": "would_decline", "step_up": "would_ask"}[r.decision]
    d["blocking_clauses"] = [c for c in d["evidence"] if c["status"] in ("fail", "unknown")]
    events.publish(customer_id, "decision", mode="precheck", receipt=d, purchase=_purchase_summary(ev))
    return d


def _purchase_summary(ev: AuthorizationEvent) -> dict[str, Any]:
    a = ev.authorization
    return {"authorization_id": a.authorization_id, "items": [f"{it.quantity}× {it.item_name}" for it in a.items], "item_name": a.items[0].item_name,
            "merchant": a.merchant.merchant_name, "merchant_country": a.merchant.merchant_country, "total_chf": a.billing_amount_chf,
            "amount": a.amount, "currency": a.currency, "delivery_by": a.delivery_by, "returnable": a.order_returnable,
            "details": a.items[0].item_details[:160], "scenario_id": a.scenario_id, "source_id": a.source_authorization_id}


def propose_purchase(customer_id: str, mandate_id: str, cart: dict[str, Any], advise: bool = True) -> dict[str, Any]:
    ev = cart_to_event(customer_id, mandate_id, cart)
    hints = _hints(customer_id, mandate_id, cart, advise)
    events.publish(customer_id, "decision.started", mode="propose", purchase=_purchase_summary(ev))
    r = decide(ev, allow_model=bool(config.OPENAI_API_KEY), hints=hints)
    card = make_step_up_card(customer_id, ev, r) if r.decision == "step_up" else None
    d = r.model_dump(mode="json")
    d["card"] = card
    events.publish(customer_id, "decision", mode="propose", receipt=d, purchase=_purchase_summary(ev), card=card)
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
    events.publish(customer_id, "card.answered", card=card)
    if card["kind"] == "step_up":
        out["resolution"] = resolve_card(client(), customer_id, card, answer, note)
    elif card["kind"] == "confirm_mandate" and answer == "confirm":
        out["mandate"] = confirm_mandate(customer_id, card["ref"]["draft_id"])
    elif card["kind"] == "question":
        CustomerState(customer_id).add_question({"id": card_id, "question": card["title"], "answer": answer, "note": note})
        if card["ref"].get("draft_id"):
            out["applied"] = apply_question_answer(customer_id, card["ref"]["draft_id"], card["title"], answer)
    return out


def status() -> dict[str, Any]:
    return {"engine_version": ENGINE_VERSION, "openai": bool(config.OPENAI_API_KEY), "sandbox": client().configured,
            "extractor_model": config.EXTRACTOR_MODEL, "compiler_model": config.COMPILER_MODEL, "state_dir": str(config.STATE_DIR)}


def dry_run(customer_id: str, draft_id: str, *, limit: int = 60) -> dict[str, Any]:
    """Replay the card's approved history purchases through the drafted clauses
    (scratch ledger, regex extractor only). Shows the customer what the policy
    would have done to their own recent shopping before they confirm it."""
    import shutil, tempfile
    from datetime import timedelta
    from pathlib import Path
    from . import ledger as L
    from .engine import decide as _decide
    from .models import AuthorizationEvent
    r = ref()
    state = CustomerState(customer_id)
    m = state.load_mandate(draft_id)
    if not m:
        raise KeyError(draft_id)
    card_id = m.card_id or next((c for c, b in r.baselines.items() if b.customer_id == customer_id), "")
    m = m.model_copy(deep=True)
    tested = ["amount", "period", "basket categories", "retailer type", "seller familiarity", "merchant trust", "session"]
    skipped = []
    for fld, blank in (("requested_item", None), ("requested_items", []), ("min_return_days", None), ("no_addons", False),
                       ("deliver_by", None), ("max_subscription_term_months", None), ("one_time", False)):
        if getattr(m.intent, fld) not in (None, [], False):
            skipped.append(fld); setattr(m.intent, fld, blank)
    rows = [h for h in r.history if h["card_id"] == card_id and h["status"] == "approved" and h["transaction_type"] == "purchase"
            and h["channel"] in ("ecommerce", "recurring", "mobile_wallet")][-limit:]
    tmp = Path(tempfile.mkdtemp(prefix="leash_dryrun_"))
    scratch_mandate_id = f"TM_DRYRUN_{draft_id}"
    orig = L.Ledger.__init__
    L.Ledger.__init__ = lambda self, cid, state_dir=tmp: orig(self, cid, state_dir)  # type: ignore
    counts = {"approve": 0, "step_up": 0, "decline": 0}
    samples: list[dict[str, Any]] = []
    try:
        for h in rows:
            mer = r.merchants.get(h["merchant_id"], {})
            now = datetime.now(timezone.utc)
            ev = AuthorizationEvent.model_validate({
                "type": "authorization.request", "request_id": f"req_dry_{h['authorization_id']}", "deadline_at": (now + timedelta(seconds=8)).isoformat(),
                "authorization": {
                    "authorization_id": f"DRY_{h['authorization_id']}", "source_authorization_id": h["authorization_id"], "scenario_id": "DRYRUN", "replay_order": 1,
                    "mandate_id": scratch_mandate_id, "profile_id": "PROFILE_DRY", "card_id": card_id, "initiator_type": "agent",
                    "merchant": {"merchant_id": h["merchant_id"], "merchant_name": h["merchant_name"], "merchant_category": h["merchant_category"],
                                 "merchant_mcc": h["merchant_mcc"], "merchant_country": h["merchant_country"], "merchant_city": h["merchant_city"],
                                 "availability": mer.get("availability", "online"), "recurring_capable": mer.get("recurring_capable", "false")},
                    "timestamp": h["timestamp"], "amount": float(h["amount"]), "currency": h["currency"], "billing_amount_chf": float(h["billing_amount_chf"]),
                    "items_subtotal": float(h["amount"]), "delivery_fee": 0.0, "channel": h["channel"] if h["channel"] in ("ecommerce", "recurring") else "ecommerce",
                    "customer_device_id": h["customer_device_id"] or "DVC-HIST", "authority_status": "active", "card_status_at_attempt": "active",
                    "spend_in_period_before_chf": None, "recent_attempt_count_10m": 0, "fulfillment_method": "delivery", "delivery_by": None,
                    "order_returnable": "unknown", "order_cancellable": "unknown", "related_authorization_id": None, "related_authorization_status": None,
                    "purchase_description": h["description"],
                    "items": [{"line_no": 1, "item_id": "IT_HIST", "item_name": h["description"], "item_category": h["merchant_category"], "quantity": 1,
                               "unit_price": float(h["amount"]), "currency": h["currency"], "item_details": h["description"]}]},
                "mandate": {"mandate_id": scratch_mandate_id, "status": "active", "customer_id": customer_id, "card_id": card_id, "instruction": m.instruction,
                            "hard_rules": [x.model_dump(mode="json", exclude_none=True) for x in m.hard_rules], "uncertainty_policy": m.uncertainty_policy, "profile_id": "PROFILE_DRY"},
                "context": {"approved_spend_in_period_chf": None, "recent_authorizations": []},
                "runtime": {"received_at": now.isoformat(), "history_window_minutes": 10, "context_basis": "run_decisions_and_scenario_timestamps"}})
            rc = _decide(ev, mandate=m, allow_model=False, now=now)
            counts[rc.decision] += 1
            if rc.decision != "approve" and len(samples) < 8:
                samples.append({"when": h["timestamp"][:10], "merchant": h["merchant_name"], "what": h["description"], "chf": float(h["billing_amount_chf"]),
                                "decision": rc.decision, "why": rc.reason_codes})
    finally:
        L.Ledger.__init__ = orig  # type: ignore
        shutil.rmtree(tmp, ignore_errors=True)
    n = sum(counts.values())
    summary = (f"Of your last {n} online purchases on this card, the money and merchant rules of this contract would have approved {counts['approve']}, "
               f"asked you about {counts['step_up']}, and declined {counts['decline']}.")
    return {"draft_id": draft_id, "card_id": card_id, "counts": counts, "summary": summary, "samples": samples,
            "tested": tested, "not_tested": skipped}


def current_state(customer_id: str) -> dict[str, Any]:
    """What the agent must know every turn: latest draft, active mandate, pending cards."""
    state = CustomerState(customer_id)
    mdir = state.dir / "mandates"
    drafts = sorted(mdir.glob("draft_*.md"), key=lambda p: p.stat().st_mtime)
    actives = sorted((p for p in mdir.glob("*.md") if not p.name.startswith("draft_")), key=lambda p: p.stat().st_mtime)
    active_id = actives[-1].stem if actives else None
    pending = Cards(customer_id).pending()
    out: dict[str, Any] = {"draft_id": drafts[-1].stem if drafts else None, "mandate_id": active_id,
                           "pending_cards": [{"kind": c["kind"], "title": c["title"]} for c in pending]}
    if active_id:
        m = state.load_mandate(active_id)
        if m:
            out["contract_summary"] = {"per_order_cap_chf": m.intent.per_order_cap_chf, "period": m.intent.period.model_dump() if m.intent.period else None,
                                       "requested_items": [ri.model_dump() for ri in ([m.intent.requested_item] if m.intent.requested_item else []) + list(m.intent.requested_items)],
                                       "deliver_by": str(m.intent.deliver_by) if m.intent.deliver_by else None, "seller_familiarity_min": m.intent.seller_familiarity_min}
    return out
