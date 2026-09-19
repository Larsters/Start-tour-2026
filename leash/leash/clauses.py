"""Contract clauses: pure functions (event facts + state) -> ClauseResult.
Each clause knows its own severity; the engine only combines."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from .config import THRESHOLDS
from .data import CardBaseline, ReferenceData, chf
from .ledger import Ledger
from .models import Authorization, ClauseResult, IntentSpec, Item
from .quarantine import ExtractedFacts
from .trust import MerchantTrust

_STOP = {"a", "an", "the", "of", "and", "for", "my", "new", "pair", "computer"}
_SYN = {"shoes": "shoe", "trainers": "shoe", "sneakers": "shoe", "runners": "shoe",
        "vouchers": "voucher", "voucher": "voucher", "giftcard": "voucher", "monitors": "monitor",
        "display": "monitor", "screen": "monitor"}


def _tokens(s: str) -> list[str]:
    out = []
    for t in re.split(r"[\s\-_/,]+", s.lower()):
        t = re.sub(r"[^a-z0-9]", "", t)
        if not t or t in _STOP:
            continue
        t = _SYN.get(t, t)
        if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.append(t)
    return out


def _fail(name: str, sev: str, summary: str, **kw) -> ClauseResult:
    return ClauseResult(clause=name, status="fail", severity=sev, summary=summary, **kw)


def _pass(name: str, summary: str = "", **kw) -> ClauseResult:
    return ClauseResult(clause=name, status="pass", severity="info", summary=summary, **kw)


def _unknown(name: str, summary: str, **kw) -> ClauseResult:
    return ClauseResult(clause=name, status="unknown", severity="step_up", summary=summary, **kw)


def _info(name: str, summary: str, **kw) -> ClauseResult:
    return ClauseResult(clause=name, status="info", severity="info", summary=summary, **kw)


# ------------------------------------------------------------------ gates
def card_gates(a: Authorization, b: CardBaseline) -> list[ClauseResult]:
    out = []
    if a.authority_status != "active" or a.card_status_at_attempt != "active":
        out.append(_fail("card_state", "decline", f"authority {a.authority_status}, card {a.card_status_at_attempt}"))
    if a.channel in ("ecommerce", "recurring") and not b.online_enabled:
        out.append(_fail("card_online", "decline", "card is not enabled for online use"))
    if a.merchant.merchant_country != "CH" and not b.international_enabled:
        out.append(_fail("card_international", "decline", "card is not enabled for international use"))
    if b.per_transaction_limit_chf and a.billing_amount_chf > b.per_transaction_limit_chf:
        out.append(_fail("issuer_limit", "decline", f"CHF {a.billing_amount_chf:.2f} exceeds the account's per-transaction limit",
                         value=a.billing_amount_chf, limit=b.per_transaction_limit_chf))
    return out or [_pass("card_state", "card and authority active")]


# ------------------------------------------------------------------ money
def amount(a: Authorization, i: IntentSpec) -> ClauseResult | None:
    if i.per_order_cap_chf is None:
        return None
    v, cap = a.billing_amount_chf, i.per_order_cap_chf
    if v > cap:
        return _fail("amount", "decline", f"order total CHF {v:.2f} exceeds the CHF {cap:.2f} per-order limit (delivery included)",
                     value=v, limit=cap, counterfactual=f"would pass if the total were ≤ CHF {cap:.2f}")
    return _pass("amount", f"CHF {v:.2f} ≤ CHF {cap:.2f} (items {a.items_subtotal:.2f} + delivery {a.delivery_fee:.2f})", value=v, limit=cap)


def period(a: Authorization, i: IntentSpec, ledger: Ledger) -> ClauseResult | None:
    if not i.period:
        return None
    spent, rows = ledger.approved_in_window(a.mandate_id, a.timestamp, i.period.days)
    total = round(spent + a.billing_amount_chf, 2)
    extra = {"window_days": i.period.days, "approved_before": spent, "counted": [r["authorization_id"] for r in rows]}
    if total > i.period.cap_chf:
        return _fail("period", "decline",
                     f"CHF {spent:.2f} already approved in the last {i.period.days} days; adding CHF {a.billing_amount_chf:.2f} makes CHF {total:.2f} > CHF {i.period.cap_chf:.2f}",
                     value=total, limit=i.period.cap_chf, extra=extra,
                     counterfactual=f"would pass if this order were ≤ CHF {max(0.0, i.period.cap_chf - spent):.2f}")
    return _pass("period", f"CHF {spent:.2f} + {a.billing_amount_chf:.2f} = {total:.2f} ≤ {i.period.cap_chf:.2f} over {i.period.days} days",
                 value=total, limit=i.period.cap_chf, extra=extra)


def split_order(a: Authorization, i: IntentSpec, ledger: Ledger) -> ClauseResult | None:
    if i.per_order_cap_chf is None:
        return None
    recent = [r for r in ledger.recent(a.mandate_id, a.timestamp, 1.0)
              if r["merchant_id"] == a.merchant.merchant_id and r["final_status"] in ("approved", "pending")]
    if not recent:
        return None
    combined = round(sum(r["amount_chf"] for r in recent) + a.billing_amount_chf, 2)
    if combined > i.per_order_cap_chf:
        return _fail("split_order", "step_up",
                     f"second order at the same shop within an hour; together CHF {combined:.2f} exceeds the CHF {i.per_order_cap_chf:.2f} per-order limit",
                     value=combined, limit=i.per_order_cap_chf, extra={"related": [r["authorization_id"] for r in recent]})
    return _pass("split_order", f"combined with recent orders at this shop: CHF {combined:.2f} ≤ {i.per_order_cap_chf:.2f}")


# ------------------------------------------------------------------ basket
def basket_categories(a: Authorization, i: IntentSpec) -> list[ClauseResult]:
    out = []
    forbidden = [it for it in a.items if it.item_category in i.forbidden_item_categories]
    if forbidden:
        names = ", ".join(f"{it.item_name} ({it.item_category})" for it in forbidden)
        out.append(_fail("forbidden_category", "decline", f"basket contains a forbidden category: {names}", value=[it.item_category for it in forbidden]))
    if i.allowed_item_categories:
        outside = [it for it in a.items if it.item_category not in i.allowed_item_categories and it.item_category not in i.forbidden_item_categories]
        if outside:
            names = ", ".join(f"{it.item_name} ({it.item_category})" for it in outside)
            out.append(_fail("basket_purpose", "step_up", f"basket contains items outside the stated purpose: {names}",
                             value=[it.item_category for it in outside], limit=i.allowed_item_categories))
        else:
            out.append(_pass("basket_purpose", f"all {len(a.items)} line(s) within {', '.join(i.allowed_item_categories)}"))
    return out


def _requested(i: IntentSpec) -> list:
    return ([i.requested_item] if i.requested_item else []) + list(i.requested_items)


_GENERIC = {"set", "item", "product", "thing", "pair", "one", "kit", "piece", "unit", "gift", "present", "model"}


def _match(have: list[str], want: list[str]) -> str:
    """'exact' | 'substitute' | 'none' for one cart line vs one requested item.
    The last token is the head noun unless it is generic ('set', 'item'); the
    rest are qualifiers. Head present + all qualifiers present → exact; head
    present but a qualifier missing (road vs trail) → substitute; head absent →
    substitute only if most qualifiers still match, else none."""
    if not have or not want:
        return "none"
    head = want[-1]
    quals = [t for t in want[:-1] if t not in _GENERIC]
    head_ok = head in have or head in _GENERIC
    missing = [t for t in quals if t not in have]
    if head_ok:
        return "substitute" if missing else "exact"
    if quals and len(missing) / len(quals) <= 0.5:
        return "substitute"
    return "none"


def requested_item(a: Authorization, facts: dict[int, ExtractedFacts], i: IntentSpec) -> list[ClauseResult]:
    wants = [(ri, _tokens(ri.type)) for ri in _requested(i) if _tokens(ri.type)]
    if not wants:
        return []
    out: list[ClauseResult] = []
    for it in a.items:
        f = facts[it.line_no]
        if f.is_addon_service:
            continue
        have = _tokens(f.item_type or it.item_name) + _tokens(it.item_name)
        scored = sorted(((_match(have, w), ri, w) for ri, w in wants), key=lambda x: {"exact": 0, "substitute": 1, "none": 2}[x[0]])
        kind, ri, w = scored[0]
        label = " / ".join(ri.type for ri, _ in wants)
        if kind == "none":
            out.append(_fail("requested_item", "decline", f"'{it.item_name}' is not the requested {label}", value=it.item_name, limit=label))
            continue
        if kind == "substitute":
            missing = [t for t in w if t not in have and t not in _GENERIC] or [w[-1]]
            out.append(_fail("requested_item", "step_up", f"'{it.item_name}' looks like a substitute for the requested {ri.type} (missing: {', '.join(missing)})",
                             value=it.item_name, limit=ri.type))
            continue
        out.append(_pass("requested_item", f"'{it.item_name}' matches the requested {ri.type}", value=it.item_name))
        for k, v in ri.attributes.items():
            got = getattr(f, k, None)
            if got is None:
                out.append(_unknown("item_attribute", f"{k} not stated for '{it.item_name}' (you asked for {k} {v})", value=None, limit=v))
            elif str(got).lower().strip() != str(v).lower().strip():
                out.append(_fail("item_attribute", "decline", f"{k} {got} offered, you asked for {k} {v}", value=str(got), limit=v))
            else:
                out.append(_pass("item_attribute", f"{k} {got} as requested", value=str(got), limit=v))
    return out


def return_terms(a: Authorization, facts: dict[int, ExtractedFacts], i: IntentSpec) -> list[ClauseResult]:
    if i.min_return_days is None:
        return []
    out = []
    for it in a.items:
        f = facts[it.line_no]
        if f.is_addon_service:
            continue
        if f.final_sale or a.order_returnable == "false":
            out.append(_fail("return_terms", "decline", f"'{it.item_name}' is final sale / not returnable; you require {i.min_return_days}+ days",
                             value=0, limit=i.min_return_days))
        elif f.return_days is None:
            out.append(_unknown("return_terms", f"return window for '{it.item_name}' is not stated (order_returnable={a.order_returnable})", value=None, limit=i.min_return_days))
        elif f.return_days < i.min_return_days:
            out.append(_fail("return_terms", "decline", f"returns accepted for only {f.return_days} days; you require {i.min_return_days}+",
                             value=f.return_days, limit=i.min_return_days, counterfactual=f"would pass with a {i.min_return_days}-day return window"))
        else:
            out.append(_pass("return_terms", f"returns accepted within {f.return_days} days ≥ {i.min_return_days}", value=f.return_days, limit=i.min_return_days))
    return out


def hidden_fee(a: Authorization, facts: dict[int, ExtractedFacts]) -> ClauseResult | None:
    """A one-off product whose merchant text enrols the buyer in a recurring
    charge. Not a separate cart line, so the customer would never see it."""
    for it in a.items:
        f = facts[it.line_no]
        if it.item_category in ("subscriptions", "membership") or (f.is_addon_service and len(a.items) > 1):
            continue
        if f.recurring_billing or f.minimum_term_months:
            term = f" (minimum term {f.minimum_term_months} months)" if f.minimum_term_months else ""
            return _fail("hidden_fee", "step_up", f"'{it.item_name}' comes with a recurring charge buried in the product text{term}; you did not ask for a subscription",
                         value=f.minimum_term_months or True, extra={"line_no": it.line_no}, counterfactual="would pass without the recurring charge")
    return None


def addons(a: Authorization, facts: dict[int, ExtractedFacts], i: IntentSpec) -> ClauseResult | None:
    if not (i.no_addons or _requested(i)):
        return None
    extras = [it for it in a.items if (facts[it.line_no].is_addon_service and len(a.items) > 1)
              or it.item_category in ("subscriptions", "membership")]
    want_qty = sum(ri.quantity for ri in _requested(i)) or 1
    mains = [it for it in a.items if it not in extras]
    if extras:
        names = ", ".join(f"{it.item_name} (CHF {chf(it.unit_price * it.quantity, it.currency, _FX):.2f})" for it in extras)
        return _fail("addons", "step_up", f"unrequested add-on in the cart: {names}", value=[it.item_name for it in extras],
                     counterfactual="would pass without the add-on")
    if len(mains) > want_qty or sum(it.quantity for it in mains) > want_qty:
        return _fail("addons", "step_up", f"{sum(it.quantity for it in mains)} main item(s) in the cart, {want_qty} requested", value=len(mains), limit=want_qty)
    return _pass("addons", "only the requested item(s) in the cart")


_FX = {"CHF": 1.0, "EUR": 0.95, "GBP": 1.12, "USD": 0.87}


def price_sanity(a: Authorization, ref: ReferenceData, market: dict | None = None) -> list[ClauseResult]:
    out = []
    for it in a.items:
        rng = ref.item_range(it.item_id)
        src = "catalogue"
        mk = (market or {}).get(it.item_name) if isinstance(market, dict) else None
        if not rng and mk:
            rng, src = (mk["low"], (mk["low"] + mk["high"]) / 2, mk["high"]), "market: " + ", ".join(mk.get("sources") or [])[:120]
        if not rng:
            continue
        lo, typ, hi = rng
        unit_chf = chf(it.unit_price, it.currency, ref.fx)
        if unit_chf < lo * THRESHOLDS["price_sanity_low_factor"]:
            out.append(_fail("price_sanity", "step_up", f"'{it.item_name}' at CHF {unit_chf:.2f} is far below the usual CHF {lo:.0f}–{hi:.0f} ({src}); could be counterfeit or a bait listing",
                             value=unit_chf, limit=[lo, hi], extra={"source": src}))
        elif unit_chf > hi * 3:
            out.append(_fail("price_sanity", "step_up", f"'{it.item_name}' at CHF {unit_chf:.2f} is several times the usual CHF {lo:.0f}–{hi:.0f} ({src})", value=unit_chf, limit=[lo, hi], extra={"source": src}))
        elif unit_chf > hi * 1.5:
            out.append(_info("price_sanity", f"'{it.item_name}' at CHF {unit_chf:.2f} is above the usual CHF {lo:.0f}–{hi:.0f} ({src}); worth a look", value=unit_chf, limit=[lo, hi], extra={"source": src}))
        else:
            out.append(_info("price_sanity", f"'{it.item_name}' CHF {unit_chf:.2f} within usual CHF {lo:.0f}–{hi:.0f} (typical {typ:.0f})", value=unit_chf, limit=[lo, hi]))
    return out


def subscription_term(a: Authorization, facts: dict[int, ExtractedFacts], i: IntentSpec) -> ClauseResult | None:
    if not i.max_subscription_term_months:
        return None
    for it in a.items:
        f = facts[it.line_no]
        if f.minimum_term_months and f.minimum_term_months > i.max_subscription_term_months:
            return _fail("subscription_term", "step_up", f"'{it.item_name}' has a minimum term of {f.minimum_term_months} months; you allowed {i.max_subscription_term_months}",
                         value=f.minimum_term_months, limit=i.max_subscription_term_months)
    return _pass("subscription_term", "no minimum term beyond your limit")


def deliver_by(a: Authorization, i: IntentSpec) -> ClauseResult | None:
    if not i.deliver_by:
        return None
    if a.delivery_by is None:
        if a.fulfillment_method == "digital":
            return _pass("deliver_by", "digital delivery, no shipping date needed")
        return _unknown("deliver_by", f"no delivery date given; you need it by {i.deliver_by}", value=None, limit=str(i.deliver_by))
    d = date.fromisoformat(a.delivery_by)
    if d > i.deliver_by:
        return _fail("deliver_by", "step_up", f"earliest delivery {d} is after the {i.deliver_by} you need it by" +
                     (" (shipping from abroad)" if a.merchant.merchant_country != "CH" else ""), value=str(d), limit=str(i.deliver_by))
    return _pass("deliver_by", f"delivery by {d} ≤ {i.deliver_by}", value=str(d), limit=str(i.deliver_by))


def fulfillment(a: Authorization, i: IntentSpec) -> ClauseResult | None:
    if i.fulfillment == "any":
        return None
    if a.fulfillment_method != i.fulfillment:
        return _fail("fulfillment", "step_up", f"fulfilment '{a.fulfillment_method}' differs from expected '{i.fulfillment}'", value=a.fulfillment_method, limit=i.fulfillment)
    return _pass("fulfillment", f"fulfilment {a.fulfillment_method}")


# ------------------------------------------------------------------ merchant
def retailer_type(a: Authorization, i: IntentSpec) -> ClauseResult | None:
    if not i.retailer_categories:
        return None
    m = a.merchant
    if m.merchant_category not in i.retailer_categories:
        return _fail("retailer_type", "decline", f"{m.merchant_name} is a {m.merchant_category} shop (MCC {m.merchant_mcc}), not {', '.join(i.retailer_categories)}",
                     value=m.merchant_category, limit=i.retailer_categories)
    return _pass("retailer_type", f"{m.merchant_name} is a {m.merchant_category} retailer (MCC {m.merchant_mcc})", value=m.merchant_category)


def seller_familiarity(a: Authorization, i: IntentSpec, b: CardBaseline) -> ClauseResult | None:
    if not i.seller_familiarity_min:
        return None
    n = b.merchant_counts.get(a.merchant.merchant_id, 0)
    if n < i.seller_familiarity_min:
        sev = THRESHOLDS["familiarity_violation"]
        return _fail("seller_familiarity", sev, f"{a.merchant.merchant_name}: {n} earlier approved purchase(s) on this card, you require {i.seller_familiarity_min}+",
                     value=n, limit=i.seller_familiarity_min, counterfactual="would pass at a shop you have used before")
    return _pass("seller_familiarity", f"{a.merchant.merchant_name}: {n} earlier approved purchase(s) on this card", value=n, limit=i.seller_familiarity_min)


def merchant_trust_clause(mt: MerchantTrust, a: Authorization, i: IntentSpec) -> ClauseResult:
    m = a.merchant
    if mt.band == "lookalike":
        other = mt.lookalike_of
        return _fail("merchant_trust", "decline", f"'{m.merchant_name}' ({m.merchant_id}) is a near-identical name to a known merchant ({other}) but a different seller; never seen on this card",
                     value=mt.band, extra=mt.inputs)
    if mt.band == "risky":
        web = mt.inputs.get("web_reputation") or {}
        if web.get("verdict") == "suspicious":
            srcs = ", ".join(web.get("sources") or []) or "no sources"
            return _fail("merchant_trust", "decline", f"{m.merchant_name} is flagged as suspicious online (scam / counterfeit / non-delivery reports; sources: {srcs})",
                         value=mt.score, extra=mt.inputs)
        return _fail("merchant_trust", "decline", f"{m.merchant_name} has a poor record across the issuer's cards (approval {mt.inputs['approval_rate']:.0%}, refunds {mt.inputs['refund_rate']:.0%})",
                     value=mt.score, extra=mt.inputs)
    if mt.band == "unknown":
        if i.seller_familiarity_min:   # already a contract clause; keep as evidence only
            return _info("merchant_trust", f"{m.merchant_name}: unverified (score {mt.score})", value=mt.score, extra=mt.inputs)
        return _fail("merchant_trust", "step_up", f"{m.merchant_name} could not be verified (score {mt.score}: {mt.inputs['distinct_cards']} cards, {mt.inputs['approved']} approvals across the issuer)",
                     value=mt.score, extra=mt.inputs)
    return _pass("merchant_trust", f"{m.merchant_name} trusted (score {mt.score}: {mt.inputs['distinct_cards']} cards, approval {mt.inputs['approval_rate']:.0%})", value=mt.score, extra=mt.inputs)


# ------------------------------------------------------------------ manipulation
def injection(a: Authorization, facts: dict[int, ExtractedFacts]) -> ClauseResult:
    worst = max(a.items, key=lambda it: facts[it.line_no].instruction_likeness)
    f = facts[worst.line_no]
    if f.instruction_likeness >= THRESHOLDS["injection_step_up_level"]:
        return _fail("injection", "step_up", "the product text contains an instruction aimed at automated buyers; it was ignored, but this is a manipulation signal",
                     value=f.instruction_likeness, extra={"span": f.quoted_span, "line_no": worst.line_no, "extractor": f.source})
    return _pass("injection", "no instructions hidden in merchant text", value=f.instruction_likeness, extra={"extractor": f.source})


def duplicate(a: Authorization, ledger: Ledger, i: IntentSpec) -> ClauseResult:
    if a.related_authorization_id and a.related_authorization_status == "declined":
        return _pass("duplicate", f"re-quote of declined {a.related_authorization_id}; judged on its own facts", extra={"related": a.related_authorization_id})
    item_ids = sorted(it.item_id for it in a.items)
    tol = THRESHOLDS["duplicate_amount_tolerance"]
    for r in ledger.recent(a.mandate_id, a.timestamp, THRESHOLDS["duplicate_window_hours"]):
        if r["final_status"] not in ("approved", "pending"):
            continue
        same_items = r["item_ids"] == item_ids
        close_amount = abs(r["amount_chf"] - a.billing_amount_chf) <= tol * max(r["amount_chf"], 1)
        if r["merchant_id"] == a.merchant.merchant_id and same_items and close_amount:
            return _fail("duplicate", "step_up", f"looks like a repeat of {r['authorization_id']} (same shop, same items, CHF {r['amount_chf']:.2f}) {int((a.timestamp - datetime.fromisoformat(r['sim_ts'])).total_seconds() // 60)} min earlier",
                         value=r["authorization_id"], counterfactual="would pass if the earlier order was cancelled")
    if i.one_time and ledger.approved_count(a.mandate_id) > 0:
        return _fail("duplicate", "step_up", "you asked for a one-time purchase and one was already approved under this contract", value="one_time")
    return _pass("duplicate", "no matching recent order")


def sizing_advice(a: Authorization, advice: dict, i: IntentSpec) -> ClauseResult:
    """Brand sizing knowledge vs the size the customer asked for (Q3: contradiction → ask)."""
    runs, rec = advice.get("runs"), advice.get("recommendation", "")
    src = ", ".join(advice.get("sources") or []) or "unverified"
    wanted = advice.get("requested_size")
    if runs in ("runs_small", "runs_large") and wanted:
        return ClauseResult(clause="item_sizing", status="fail", severity="step_up",
                            summary=f"{advice['brand']} {advice['item_type']} {runs.replace('_', ' ')} — you asked for size {wanted}. {rec}",
                            value=wanted, extra={"sources": advice.get("sources"), "verified": advice.get("verified"), "confidence": advice.get("confidence")})
    return _info("item_sizing", f"{advice['brand']}: {rec} (source: {src})", extra={"sources": advice.get("sources")})
