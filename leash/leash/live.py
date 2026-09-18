"""Drive a real sandbox scenario: create + confirm the mandate, start the run,
then either POLL and decide here (--poll) or OBSERVE a worker that is already
running (default; e.g. the API server's worker thread). Prints one line per
purchase with the platform's recorded decision."""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time

from .cards import Cards
from .compiler import compile_instruction
from .data import ref
from .replay import check
from .service import client
from .state import CustomerState
from .worker import handle_event, resolve_card

log = logging.getLogger("leash.live")


def setup_run(scenario_id: str, *, reset: bool, prefer_llm: bool) -> tuple[str, str, str]:
    r, c = ref(), client()
    if reset:
        print("reset:", c.reset())
    instruction = r.scenarios[scenario_id]["cardholder_instruction"]
    m = compile_instruction(instruction, prefer_llm=prefer_llm)
    d = c.create_mandate(instruction, [x.model_dump(mode="json", exclude_none=True) for x in m.hard_rules],
                         m.uncertainty_policy, m.guidance, m.open_questions)
    draft_id = d.get("data", d)["draft_id"]
    mandate_id = c.confirm_mandate(draft_id).get("data", {}).get("mandate_id") or c.get_mandate(draft_id).get("mandate_id")
    attempts = [x for x in csv.DictReader(open(r.dir / "purchase_attempts.csv")) if x["scenario_id"] == scenario_id]
    auth = r.authorities[attempts[0]["authority_id"]]
    m.draft_id, m.mandate_id, m.customer_id, m.card_id = draft_id, mandate_id, auth["customer_id"], auth["card_id"]
    CustomerState(auth["customer_id"]).save_mandate(m, mandate_id)
    run = c.start_run(scenario_id, mandate_id)
    run_id = run.get("data", run)["run_id"]
    print(f"{scenario_id}: mandate {mandate_id} · run {run_id} · customer {auth['customer_id']} · compiled_by {m.compiled_by}")
    return run_id, mandate_id, auth["customer_id"]


def observe(run_id: str, expected: int, *, max_seconds: int = 900, auto_resolve: str | None = None, customer_id: str | None = None) -> list[dict]:
    """Watch the platform's view until all purchases are final or awaiting a human."""
    c = client()
    seen: dict[str, dict] = {}
    resolved: set[str] = set()
    t0 = time.time()
    while time.time() - t0 < max_seconds:
        auths = c.authorizations()
        rows = [a for a in auths.get("authorizations", auths if isinstance(auths, list) else []) if a.get("run_id") == run_id]
        for a in sorted(rows, key=lambda x: x.get("replay_order", 0)):
            key = a["authorization_id"]
            if key in seen and seen[key].get("status") == a.get("status"):
                continue
            seen[key] = a
            print(f"{a.get('source_authorization_id', '?'):7} {a.get('decision') or '-':8} status={a.get('status'):18} by={a.get('decided_by') or '-':8} {','.join(a.get('reason_codes') or []):40}")
            if auto_resolve and customer_id and a.get("status") == "awaiting_customer" and key not in resolved:
                card = next((k for k in Cards(customer_id).pending() if k["ref"].get("authorization_id") == key), None)
                if card:
                    Cards(customer_id).answer(card["id"], auto_resolve, "auto-resolved by live runner")
                    print("        card answered:", resolve_card(c, customer_id, card, auto_resolve)["decision"])
                    resolved.add(key)
        st = c.run(run_id).get("data", {}) or c.run(run_id)
        counters = st.get("counters", {})
        if st.get("status") in ("completed", "finished") or (counters and counters.get("remaining", 1) == 0 and counters.get("pending", 1) == 0):
            break
        time.sleep(2)
    results = [{"authorization_id": a.get("source_authorization_id"),
                "decision": "step_up" if (a.get("decided_by") == "engine" and a.get("decision") == "step_up") or a.get("status") in ("awaiting_customer", "timed_out") or key in resolved else a.get("decision"),
                "reason_codes": a.get("reason_codes") or []} for key, a in seen.items()]
    print(f"\n{len(results)}/{expected} purchases seen on the platform in {time.time() - t0:.0f}s; run status: {st.get('status')} {counters}")
    return results


def poll_here(run_id: str, customer_id: str, expected: int, *, allow_model: bool, auto_resolve: str | None, max_seconds: int = 900) -> list[dict]:
    c = client()
    results: list[dict] = []
    t0 = time.time()
    while len(results) < expected and time.time() - t0 < max_seconds:
        env = c.next_request(25)
        if env is None:
            st = c.run(run_id).get("data", {})
            if st.get("status") in ("completed", "finished"):
                break
            continue
        payload = env if "data" in env else {"data": env}
        t1 = time.perf_counter()
        rcpt = handle_event(c, payload, allow_model=allow_model)
        if rcpt is None:
            continue
        src = payload["data"]["authorization"]["source_authorization_id"]
        results.append({"authorization_id": src, "decision": rcpt.decision, "reason_codes": rcpt.reason_codes})
        print(f"{src:7} {rcpt.decision:8} {','.join(rcpt.reason_codes):40} {int((time.perf_counter() - t1) * 1000):5d} ms {'(regex fallback)' if rcpt.degraded else ''}")
        if rcpt.decision == "step_up" and auto_resolve:
            card = Cards(customer_id).pending()[-1]
            Cards(customer_id).answer(card["id"], auto_resolve, "auto-resolved by live runner")
            print("        resolved as", resolve_card(c, customer_id, card, auto_resolve)["decision"])
    return results


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="SCEN0000")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--poll", action="store_true", help="decide in this process (only if no API worker is running)")
    ap.add_argument("--no-model", action="store_true")
    ap.add_argument("--llm-compiler", action="store_true")
    ap.add_argument("--auto-resolve", choices=["approve", "decline"], default=None)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    expected = int(ref().scenarios[a.scenario]["event_count"])
    run_id, mandate_id, customer_id = setup_run(a.scenario, reset=a.reset, prefer_llm=a.llm_compiler)
    res = poll_here(run_id, customer_id, expected, allow_model=not a.no_model, auto_resolve=a.auto_resolve) if a.poll \
        else observe(run_id, expected, auto_resolve=a.auto_resolve, customer_id=customer_id)
    ok, total, diffs = check(a.scenario, res)
    print(f"{ok}/{total} match expected leans")
    for d in diffs:
        print("   ✗", d)


if __name__ == "__main__":
    sys.exit(main())
