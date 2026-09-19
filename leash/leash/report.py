"""Benchmark report: what the platform recorded for the latest completed run
of each scenario, compared with our expected leans, plus timing and fallback
stats from our own ledger."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import yaml

from . import config
from .data import ref
from .ledger import Ledger
from .service import client


def latest_runs() -> dict[str, dict]:
    """scenario_id -> {run_id, status, counters} for the latest completed run."""
    c = client()
    runs: dict[str, dict] = {}
    since = 0
    while True:
        page = c.events(since)
        evs = page.get("events", [])
        for e in evs:
            if e["type"] == "run.started":
                runs[e["run_id"]] = {"run_id": e["run_id"], "scenario_id": e["data"]["scenario_id"], "started": e["occurred_at"], "status": "running"}
            elif e["type"].startswith("run.") and e.get("run_id") in runs:
                runs[e["run_id"]]["status"] = e["type"].split(".", 1)[1]
                runs[e["run_id"]]["ended"] = e["occurred_at"]
        nxt = page.get("next_cursor")
        if not evs or nxt is None or nxt == since:
            break
        since = nxt
    latest: dict[str, dict] = {}
    for r in runs.values():
        st = c.run(r["run_id"])
        d = st.get("data", st)
        r["status"] = d.get("status", r["status"]); r["counters"] = d.get("counters", {})
        if r["status"] != "completed":
            continue
        if r["scenario_id"] not in latest or r["started"] > latest[r["scenario_id"]]["started"]:
            latest[r["scenario_id"]] = r
    return latest


def report(scenarios: list[str] | None = None, verbose: bool = True) -> dict:
    r = ref()
    latest = latest_runs()
    auths = client().authorizations()
    rows = auths.get("authorizations", auths if isinstance(auths, list) else [])
    by_run = defaultdict(list)
    for a in rows:
        by_run[a.get("run_id")].append(a)
    out = {"scenarios": {}, "totals": Counter()}
    for sid in scenarios or sorted(r.scenarios):
        run = latest.get(sid)
        if not run:
            print(f"\n=== {sid}: NO completed run on the platform ===")
            out["scenarios"][sid] = {"run": None}
            continue
        exp = yaml.safe_load((config.FIXTURES_DIR / "expected" / f"{sid}.yaml").read_text())
        attempts = sorted(by_run[run["run_id"]], key=lambda a: a.get("replay_order", 0))
        cust = next((x for x in r.authorities.values() if any(True for _ in [0])), None)
        led = None
        try:
            auth_row = next(x for x in __import__("csv").DictReader(open(r.dir / "purchase_attempts.csv")) if x["scenario_id"] == sid)
            led = Ledger(r.authorities[auth_row["authority_id"]]["customer_id"])
        except Exception:
            pass
        print(f"\n=== {sid} · {r.scenarios[sid]['scenario_name']} · run {run['run_id']} · {run['counters']} ===")
        ok = total = 0
        stats = Counter()
        for a in attempts:
            src = a.get("source_authorization_id", "?")
            e = exp.get(src, {})
            accepted = [e.get("lean")] + list(e.get("also_ok", [])) if e else []
            eng = a.get("decision") if a.get("decided_by") == "engine" else None
            # platform overwrites decision after resolve; recover the engine's decision from our ledger
            row = led.get(a["authorization_id"]) if led else None
            engine_decision = row["decision"] if row else eng
            final = a.get("status")
            match = engine_decision in accepted if accepted else None
            ms = row["receipt"].get("elapsed_ms") if row else None
            degraded = row["receipt"].get("degraded") if row else None
            stats["degraded" if degraded else "model"] += 1
            if ms is not None:
                stats["ms_sum"] += ms; stats["ms_max"] = max(stats["ms_max"], ms)
            total += 1; ok += 1 if match else 0
            flag = "✓" if match else ("✗" if match is False else " ")
            reasons = ",".join(a.get("reason_codes") or (row["receipt"]["reason_codes"] if row else []))
            resolved = f" → {final}" if engine_decision == "step_up" else ""
            print(f" {flag} {src:7} {engine_decision or '-':8}{resolved:14} {reasons[:38]:38} {ms if ms is not None else '?':>5} ms {'regex' if degraded else 'model':5}  exp={'/'.join(accepted) or '-'}")
        n = stats["model"] + stats["degraded"]
        summary = {"run_id": run["run_id"], "counters": run["counters"], "match": ok, "total": total,
                   "avg_ms": round(stats["ms_sum"] / n) if n else None, "max_ms": stats["ms_max"], "regex_fallbacks": stats["degraded"],
                   "timed_out": run["counters"].get("timed_out", 0)}
        print(f"  match {ok}/{total} · avg {summary['avg_ms']} ms · max {summary['max_ms']} ms · regex fallbacks {stats['degraded']}/{n} · timed_out {summary['timed_out']}")
        out["scenarios"][sid] = summary
        out["totals"]["match"] += ok; out["totals"]["total"] += total; out["totals"]["regex"] += stats["degraded"]; out["totals"]["n"] += n
        out["totals"]["timed_out"] += summary["timed_out"]
    t = out["totals"]
    print(f"\nTOTAL match {t['match']}/{t['total']} · regex fallbacks {t['regex']}/{t['n']} · timed out {t['timed_out']}")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    out = report([a.scenario] if a.scenario else None)
    if a.json:
        print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    sys.exit(main())
