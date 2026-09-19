"""The decision engine: event -> receipt. Deterministic; the only optional
model call is the quarantine extractor, time-boxed with a regex fallback."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from . import ENGINE_VERSION, clauses as C
from .data import ReferenceData, ref as default_ref
from .ledger import Ledger
from .models import (AuthorizationEvent, ClauseResult, CompiledMandate, Decision, Receipt, RecommendedAction)
from .quarantine import ExtractedFacts, extract
from .state import CustomerState
from .trust import merchant_trust, session_signals, thermostat
from .advisor import Hints

_UNCERTAINTY: dict[str, Decision] = {"ask": "step_up", "decline": "decline", "approve": "approve"}


def _resolve_mandate(ev: AuthorizationEvent, state: CustomerState, override: CompiledMandate | None) -> CompiledMandate:
    if override:
        return override
    m = state.find_mandate(ev.mandate.mandate_id, ev.mandate.instruction)
    if m:
        return m
    from .compiler import compile_rules   # fallback: derive intent from the verbatim instruction
    m = compile_rules(ev.mandate.instruction)
    m.hard_rules = ev.mandate.hard_rules or m.hard_rules
    m.uncertainty_policy = ev.mandate.uncertainty_policy
    m.mandate_id = ev.mandate.mandate_id
    return m


def combine(evidence: list[ClauseResult], uncertainty_policy: str) -> tuple[Decision, list[str]]:
    fails = [c for c in evidence if c.status == "fail"]
    unknowns = [c for c in evidence if c.status == "unknown"]
    hard = [c for c in fails if c.severity == "decline"]
    soft = [c for c in fails if c.severity == "step_up"]
    inj = [c for c in fails if c.clause == "injection"]
    if hard:
        return "decline", [c.clause for c in hard] + [c.clause for c in soft]
    if soft:
        return "step_up", [c.clause for c in soft] + [c.clause for c in unknowns]
    if unknowns:
        return _UNCERTAINTY[uncertainty_policy], [c.clause for c in unknowns]
    return "approve", []


def _message(decision: Decision, ev: AuthorizationEvent, evidence: list[ClauseResult], action: RecommendedAction | None) -> str:
    a = ev.authorization
    what = ", ".join(f"{it.quantity}× {it.item_name}" for it in a.items)
    head = f"{what} for CHF {a.billing_amount_chf:.2f} at {a.merchant.merchant_name}"
    problems = [c.summary for c in evidence if c.status in ("fail", "unknown")]
    if decision == "approve":
        return f"Approved: {head}."
    if decision == "decline":
        return f"Declined: {head}. " + " ".join(f"{p[0].upper()}{p[1:]}." for p in problems[:3])
    msg = f"Please confirm: {head}. " + " ".join(f"{p[0].upper()}{p[1:]}." for p in problems[:3])
    if action:
        msg += f" Option: approve with a one-time virtual card capped at CHF {action.cap_chf:.2f} for this seller only; the agent must follow the one-time-card protocol."
    return msg


def decide(ev: AuthorizationEvent, *, run_id: str | None = None, mandate: CompiledMandate | None = None,
           ref: ReferenceData | None = None, allow_model: bool = True, now: datetime | None = None,
           hints: "Hints | None" = None, ledger: Ledger | None = None, alternative: dict[str, Any] | None = None) -> Receipt:
    t0 = time.perf_counter()
    ref = ref or default_ref()
    a = ev.authorization
    state = CustomerState(ev.mandate.customer_id)
    ledger = ledger or Ledger(ev.mandate.customer_id)

    # 1. idempotency
    prev = ledger.get(a.authorization_id)
    if prev:
        r = Receipt.model_validate(prev["receipt"])
        r.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return r

    # 2. mandate + intent
    m = _resolve_mandate(ev, state, mandate)
    intent = m.intent
    baseline = ref.baseline(a.card_id)

    # 3. quarantine
    now = now or datetime.now(timezone.utc)
    budget = (ev.deadline_at - now).total_seconds()
    facts: dict[int, ExtractedFacts] = {}
    for it in a.items:
        facts[it.line_no] = extract(it.item_name, it.item_details, allow_model=allow_model, budget_s=budget)
    degraded = any(f.source == "regex" for f in facts.values()) and allow_model

    # 4. trust
    mt = merchant_trust(a.merchant.merchant_id, a.merchant.merchant_name, a.merchant.merchant_country, ref, baseline, a.timestamp)
    if hints and hints.merchant_reputation and mt.band != "lookalike":
        rep = hints.merchant_reputation
        mt.score = round(max(0.0, min(1.0, mt.score + rep["score_adj"])), 3)
        mt.inputs["web_reputation"] = {"verdict": rep["verdict"], "sources": rep.get("sources")}
        if rep["verdict"] == "suspicious" and rep.get("confidence") == "high":
            mt.band = "risky"
        elif rep["verdict"] == "suspicious":
            mt.inputs["web_reputation"]["note"] = "low-confidence suspicion; treated as unknown → containment"
        elif rep["verdict"] == "reputable" and mt.band == "unknown" and (mt.score >= 0.5 or rep.get("confidence") == "high"):
            mt.band = "trusted"
    signals = session_signals(
        device_id=a.customer_device_id, hour=a.timestamp.hour,
        merchant_familiar=baseline.merchant_counts.get(a.merchant.merchant_id, 0) > 0,
        country_new=a.merchant.merchant_country not in baseline.country_counts,
        recent_10m=a.recent_attempt_count_10m, amount_chf=a.billing_amount_chf, baseline=baseline)
    trust_score, escalated = thermostat(ledger.trust(a.mandate_id), signals)

    # 5. clauses
    ev_list: list[ClauseResult] = []
    ev_list += C.card_gates(a, baseline)
    for c in (C.amount(a, intent), C.period(a, intent, ledger), C.split_order(a, intent, ledger)):
        if c: ev_list.append(c)
    ev_list += C.basket_categories(a, intent)
    ev_list += C.requested_item(a, facts, intent)
    ev_list += C.return_terms(a, facts, intent)
    for c in (C.addons(a, facts, intent), C.subscription_term(a, facts, intent), C.deliver_by(a, intent),
              C.fulfillment(a, intent), C.retailer_type(a, intent), C.seller_familiarity(a, intent, baseline)):
        if c: ev_list.append(c)
    if (hf := C.hidden_fee(a, facts)):
        ev_list.append(hf)
    ev_list += C.price_sanity(a, ref, market=hints.market_range_chf if hints else None)
    if hints and hints.size_advice:
        ev_list.append(C.sizing_advice(a, hints.size_advice, intent))
    ev_list.append(C.merchant_trust_clause(mt, a, intent))
    ev_list.append(C.injection(a, facts))
    ev_list.append(C.duplicate(a, ledger, intent))
    sig_text = "; ".join(f"{s.name}: {s.detail}" for s in signals) or "no session anomalies"
    if intent.session_integrity and escalated:
        ev_list.append(ClauseResult(clause="session_integrity", status="fail", severity="step_up",
                                    summary=f"this session does not look like you ({sig_text}); trust score {trust_score}",
                                    value=trust_score, extra={"signals": [s.name for s in signals]}))
    else:
        ev_list.append(ClauseResult(clause="session_integrity", status="pass" if not signals else "info", severity="info",
                                    summary=sig_text + f"; trust score {trust_score}", value=trust_score, extra={"signals": [s.name for s in signals]}))

    # 6. combine
    decision, codes = combine(ev_list, m.uncertainty_policy)
    codes = list(dict.fromkeys(codes))
    if decision == "step_up" and not codes:
        codes = ["customer_confirmation"]

    # 7. containment + advice
    action = None
    if decision == "step_up" and mt.band == "unknown" and any(c.clause == "merchant_trust" and c.status == "fail" for c in ev_list):
        action = RecommendedAction(type="one_time_card", cap_chf=a.billing_amount_chf, merchant_id=a.merchant.merchant_id,
                                   note="seller could not be verified; contain exposure to this amount and this seller")
    advice: list[str] = []
    suspect = any(c.clause == "price_sanity" and c.status == "fail" for c in ev_list) and mt.band != "trusted"
    if suspect:
        advice.insert(0, "This item is most likely a fake: the price is far below market and the seller could not be verified.")
    if mt.inputs.get("refund_rate", 0) > 0.1:
        advice.append(f"{a.merchant.merchant_name} has a {mt.inputs['refund_rate']:.0%} refund rate across the issuer's cards.")
    prefs = ref.customer_preferences(ev.mandate.customer_id)
    if prefs and any(it.item_category == "gift_card" for it in a.items) and "gift voucher" in prefs.lower():
        advice.append("Your profile notes that you avoid gift vouchers.")

    msg = _message(decision, ev, ev_list, action)
    if suspect and decision != "approve":
        msg = "Warning: this is most likely a fake. " + msg
    if alternative and decision != "approve":
        msg += f" Alternative: {alternative['name']} from {alternative['seller']} for CHF {alternative['price_chf']:.2f}, delivered by {alternative['delivery_by']}."
    receipt = Receipt(
        authorization_id=a.authorization_id, decision=decision, reason_codes=codes,
        customer_message=msg, evidence=ev_list, advice=advice, alternative=alternative if decision != "approve" else None,
        recommended_action=action, engine_version=ENGINE_VERSION, degraded=degraded,
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
    )

    # 8. remember
    ledger.record_decision(authorization_id=a.authorization_id, mandate_id=a.mandate_id, run_id=run_id,
                           sim_ts=a.timestamp, amount_chf=a.billing_amount_chf, merchant_id=a.merchant.merchant_id,
                           item_ids=[it.item_id for it in a.items], decision=decision, receipt=receipt.model_dump(mode="json"))
    if intent.session_integrity:
        ledger.set_trust(a.mandate_id, trust_score)
    return receipt


def resolve(customer_id: str, authorization_id: str, decision: str, note: str = "") -> dict[str, Any] | None:
    """Record the customer's answer after a step_up."""
    return Ledger(customer_id).resolve(authorization_id, decision, note)
