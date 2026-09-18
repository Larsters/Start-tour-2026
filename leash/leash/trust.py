"""Trust signals: issuer-wide merchant score with bands, lookalike detection,
and the per-run session-trust thermostat with hysteresis."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from rapidfuzz import fuzz

from .config import THRESHOLDS
from .data import CardBaseline, ReferenceData


@dataclass
class MerchantTrust:
    merchant_id: str
    score: float
    band: str                    # trusted | unknown | risky | lookalike
    inputs: dict[str, Any] = field(default_factory=dict)
    lookalike_of: str | None = None


def _norm(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def lookalike(merchant_id: str, merchant_name: str, ref: ReferenceData, baseline: CardBaseline) -> str | None:
    """Return the ID of a *different* merchant whose name is near-identical.
    Checks the card's familiar merchants first, then the whole catalogue."""
    candidates = {mid: ref.merchants[mid]["merchant_name"] for mid in baseline.merchant_counts if mid in ref.merchants}
    candidates.update({mid: m["merchant_name"] for mid, m in ref.merchants.items()})
    target = _norm(merchant_name)
    for mid, name in candidates.items():
        if mid == merchant_id:
            continue
        sim = fuzz.ratio(target, _norm(name))
        if sim >= THRESHOLDS["lookalike_min_similarity"]:
            return mid
    return None


def merchant_trust(merchant_id: str, merchant_name: str, merchant_country: str,
                   ref: ReferenceData, baseline: CardBaseline, as_of: datetime) -> MerchantTrust:
    ms = ref.merchant(merchant_id)
    card_count = baseline.merchant_counts.get(merchant_id, 0)
    inputs: dict[str, Any] = {
        "distinct_cards": len(ms.cards),
        "approved": ms.approved, "declined": ms.declined, "refunds": ms.refunds,
        "approval_rate": round(ms.approval_rate, 3), "refund_rate": round(ms.refund_rate, 3),
        "months_seen": round((as_of - ms.first_seen).days / 30, 1) if ms.first_seen else 0,
        "card_prior_approved": card_count,
        "country_new_for_card": merchant_country not in baseline.country_counts,
        "in_catalogue": merchant_id in ref.merchants,
    }
    la = lookalike(merchant_id, merchant_name, ref, baseline)
    if la:
        inputs["lookalike_of"] = la
        return MerchantTrust(merchant_id, 0.0, "lookalike", inputs, la)

    score = 0.0
    score += min(len(ms.cards), 20) / 20 * 0.30                 # breadth across the issuer's book
    score += (ms.approval_rate if (ms.approved + ms.declined) else 0.0) * 0.25
    score += max(0.0, 1 - ms.refund_rate * 5) * 0.10           # 20 % refund rate → 0
    score += min(inputs["months_seen"], 6) / 6 * 0.10
    score += min(card_count, 3) / 3 * 0.20                       # this card's own experience
    score += 0.05 if not inputs["country_new_for_card"] else 0.0
    score = round(min(score, 1.0), 3)
    if score >= THRESHOLDS["merchant_trusted_score"]:
        band = "trusted"
    elif score >= THRESHOLDS["merchant_risky_score"]:
        band = "unknown"
    else:
        band = "risky"
    return MerchantTrust(merchant_id, score, band, inputs)


@dataclass
class SessionSignal:
    name: str
    weight: float
    detail: str


def session_signals(*, device_id: str, hour: int, merchant_familiar: bool, country_new: bool,
                    recent_10m: int, amount_chf: float, baseline: CardBaseline) -> list[SessionSignal]:
    s: list[SessionSignal] = []
    if device_id and device_id not in baseline.device_counts:
        s.append(SessionSignal("new_device", 0.35, f"device {device_id} never seen on this card"))
    if baseline.hour_counts and baseline.hour_share(hour) < 0.02:
        s.append(SessionSignal("unusual_hour", 0.20, f"{hour:02d}:xx is outside this card's normal hours"))
    if not merchant_familiar:
        s.append(SessionSignal("unfamiliar_merchant", 0.15, "merchant never used on this card"))
    if country_new:
        s.append(SessionSignal("new_country", 0.10, "country never used on this card"))
    if recent_10m:
        s.append(SessionSignal("velocity", min(0.3, 0.10 * recent_10m), f"{recent_10m} attempt(s) in the last 10 min"))
    if baseline.amounts and amount_chf > baseline.p90_amount():
        s.append(SessionSignal("high_amount", 0.10, f"above this card's 90th percentile (CHF {baseline.p90_amount():.2f})"))
    return s


def thermostat(previous: float, signals: list[SessionSignal]) -> tuple[float, bool]:
    """Returns (new_score, escalated). Hysteresis: escalate ≥ trust_escalate,
    stay escalated until < trust_relax."""
    add = sum(x.weight for x in signals)
    score = previous + add if signals else max(0.0, previous - 0.25)
    score = round(max(0.0, min(1.0, score)), 3)
    was_escalated = previous >= THRESHOLDS["trust_relax"] and previous >= THRESHOLDS["trust_escalate"] * 0.999
    if score >= THRESHOLDS["trust_escalate"]:
        return score, True
    if was_escalated and score >= THRESHOLDS["trust_relax"]:
        return score, True
    return score, False
