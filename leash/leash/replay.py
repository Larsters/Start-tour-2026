"""Offline replay: build live-shaped events from the CSV pack (per
technical_details.md §3) and run them through the engine in replay_order."""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from . import config
from .compiler import compile_rules
from .data import ReferenceData, ref as default_ref
from .engine import decide
from .models import AuthorizationEvent, CompiledMandate
from .state import CustomerState


def _num(s: str) -> float:
    return float(s)


def build_events(scenario_id: str, ref: ReferenceData, mandate_id: str) -> list[dict]:
    attempts = sorted((r for r in _rows(ref.dir / "purchase_attempts.csv") if r["scenario_id"] == scenario_id),
                      key=lambda r: int(r["replay_order"]))
    lines = {}
    for r in _rows(ref.dir / "purchase_attempt_items.csv"):
        lines.setdefault(r["authorization_id"], []).append(r)
    events = []
    for r in attempts:
        auth = ref.authorities[r["authority_id"]]
        m = ref.merchants[r["merchant_id"]]
        items = [{
            "line_no": int(l["line_no"]), "item_id": l["item_id"], "item_name": l["item_name"],
            "item_category": l["item_category"], "quantity": int(l["quantity"]),
            "unit_price": _num(l["unit_price"]), "currency": l["currency"], "item_details": l["item_details"],
        } for l in sorted(lines[r["authorization_id"]], key=lambda l: int(l["line_no"]))]
        events.append({
            "type": "authorization.request",
            "request_id": f"req_offline_{r['authorization_id']}",
            "deadline_at": None,  # assigned at replay time
            "authorization": {
                "authorization_id": r["authorization_id"], "source_authorization_id": r["authorization_id"],
                "scenario_id": scenario_id, "replay_order": int(r["replay_order"]), "mandate_id": mandate_id,
                "profile_id": f"PROFILE_{auth['authority_id']}", "card_id": r["card_id"], "initiator_type": "agent",
                "merchant": {k: m[k] for k in ("merchant_id", "merchant_name", "merchant_category", "merchant_mcc",
                                               "merchant_country", "merchant_city", "availability", "recurring_capable")},
                "timestamp": r["timestamp"], "amount": _num(r["amount"]), "currency": r["currency"],
                "billing_amount_chf": _num(r["billing_amount_chf"]), "items_subtotal": _num(r["items_subtotal"]),
                "delivery_fee": _num(r["delivery_fee"]), "channel": r["channel"], "customer_device_id": r["customer_device_id"],
                "authority_status": r["authority_status"], "card_status_at_attempt": r["card_status_at_attempt"],
                "spend_in_period_before_chf": _num(r["spend_in_period_before_chf"]) if r["spend_in_period_before_chf"] else None,
                "recent_attempt_count_10m": int(r["recent_attempt_count_10m"]), "fulfillment_method": r["fulfillment_method"],
                "delivery_by": r["delivery_by"] or None, "order_returnable": r["order_returnable"],
                "order_cancellable": r["order_cancellable"], "related_authorization_id": r["related_authorization_id"] or None,
                "related_authorization_status": r["related_authorization_status"] or None,
                "purchase_description": r["purchase_description"], "items": items,
            },
            "mandate": {"mandate_id": mandate_id, "status": "active", "customer_id": auth["customer_id"], "card_id": auth["card_id"],
                        "instruction": ref.scenarios[scenario_id]["cardholder_instruction"], "hard_rules": [],
                        "uncertainty_policy": "ask", "profile_id": f"PROFILE_{auth['authority_id']}"},
            "context": {"approved_spend_in_period_chf": 0.0, "recent_authorizations": []},
            "runtime": {"received_at": None, "history_window_minutes": 10, "context_basis": "run_decisions_and_scenario_timestamps"},
        })
    return events


def _rows(p: Path) -> list[dict]:
    with p.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def replay(scenario_id: str, *, ref: ReferenceData | None = None, mandate: CompiledMandate | None = None,
           allow_model: bool = False, fresh: bool = True, verbose: bool = True) -> list[dict]:
    ref = ref or default_ref()
    mandate_id = f"TM_OFFLINE_{scenario_id}"
    events = build_events(scenario_id, ref, mandate_id)
    customer_id = events[0]["mandate"]["customer_id"]
    if fresh:
        shutil.rmtree(config.STATE_DIR / customer_id, ignore_errors=True)
    m = mandate or compile_rules(ref.scenarios[scenario_id]["cardholder_instruction"])
    m.mandate_id = mandate_id
    m.customer_id, m.card_id = customer_id, events[0]["mandate"]["card_id"]
    CustomerState(customer_id).save_mandate(m, mandate_id)
    results = []
    for e in events:
        now = datetime.now(timezone.utc)
        e["deadline_at"] = (now + timedelta(seconds=8)).isoformat()
        e["runtime"]["received_at"] = now.isoformat()
        e["mandate"]["hard_rules"] = [r.model_dump(mode="json", exclude_none=True) for r in m.hard_rules]
        e["mandate"]["uncertainty_policy"] = m.uncertainty_policy
        ev = AuthorizationEvent.model_validate(e)
        r = decide(ev, run_id=f"run_offline_{scenario_id}", allow_model=allow_model, now=now)
        top = next((c for c in r.evidence if c.status in ("fail", "unknown")), None)
        results.append({"authorization_id": r.authorization_id, "decision": r.decision, "reason_codes": r.reason_codes,
                        "top": top.summary if top else "", "amount": ev.authorization.billing_amount_chf,
                        "merchant": ev.authorization.merchant.merchant_name, "ms": r.elapsed_ms,
                        "action": r.recommended_action.model_dump() if r.recommended_action else None})
        if verbose:
            print(f"{r.authorization_id:7} {r.decision:8} CHF {ev.authorization.billing_amount_chf:7.2f} {ev.authorization.merchant.merchant_name:18} "
                  f"{','.join(r.reason_codes):40} {(top.summary[:90] if top else '')}")
    return results


def check(scenario_id: str, results: list[dict]) -> tuple[int, int, list[str]]:
    p = config.FIXTURES_DIR / "expected" / f"{scenario_id}.yaml"
    if not p.exists():
        return 0, 0, [f"no expectations file {p}"]
    exp = yaml.safe_load(p.read_text())
    ok, total, diffs = 0, 0, []
    for r in results:
        e = exp.get(r["authorization_id"])
        if not e:
            continue
        total += 1
        accepted = [e["lean"]] + list(e.get("also_ok", []))
        if r["decision"] in accepted:
            ok += 1
        else:
            diffs.append(f"{r['authorization_id']}: got {r['decision']} ({','.join(r['reason_codes'])}), expected {accepted} — {e.get('why','')}")
    return ok, total, diffs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Replay scenarios offline through the Leash engine")
    ap.add_argument("--scenario", default="all")
    ap.add_argument("--model", action="store_true", help="allow the OpenAI extractor (needs OPENAI_API_KEY)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    ref = default_ref()
    ids = sorted(ref.scenarios) if a.scenario == "all" else [a.scenario]
    all_ok = all_total = 0
    all_diffs = []
    for sid in ids:
        print(f"\n=== {sid} · {ref.scenarios[sid]['scenario_name']} ===")
        print(f"instruction: {ref.scenarios[sid]['cardholder_instruction']}")
        res = replay(sid, ref=ref, allow_model=a.model, verbose=not a.json)
        if a.json:
            print(json.dumps(res, indent=1))
        ok, total, diffs = check(sid, res)
        all_ok += ok; all_total += total; all_diffs += diffs
        print(f"--- {ok}/{total} match expected leans")
        for d in diffs:
            print("   ✗", d)
    print(f"\nTOTAL {all_ok}/{all_total}")
    return 0 if not all_diffs else 1


if __name__ == "__main__":
    sys.exit(main())
