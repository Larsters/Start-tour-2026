import os, shutil
from pathlib import Path

os.environ.setdefault("LEASH_STATE_DIR", str(Path(__file__).parent / "_state"))
os.environ["TEAM_API_KEY"] = ""            # keep these tests offline

from leash import config  # noqa: E402
from leash.compiler import apply_answer, compile_rules  # noqa: E402


def test_apply_answer_rules():
    m = compile_rules("My girlfriend's birthday is in a week. I want Adidas running shoes and a jersey, around CHF 250 total. Ask me when uncertain.")
    m, note = apply_answer(m, "By which date must it arrive? I will refuse anything that ships later.", "2026-09-25")
    assert str(m.intent.deliver_by) == "2026-09-25" and "deliver by" in note
    from leash.models import RequestedItem
    m.intent.requested_items = [RequestedItem(type="adidas running shoes", attributes={"brand": "adidas"})]
    m, note = apply_answer(m, "What size should the running shoes be?", "41")
    assert m.intent.requested_items[0].attributes["size"] == "41"


def test_dry_run_on_history():
    from leash import service
    shutil.rmtree(config.STATE_DIR / "CU0001", ignore_errors=True)
    d = service.draft_mandate("CU0001", "Order our household groceries for delivery. Keep each order at or below CHF 120 including delivery, "
                              "and keep the total across any seven days at or below CHF 300. Ask me when uncertain.", card_id="CA0001", prefer_llm=False)
    out = service.dry_run("CU0001", d["draft_id"])
    n = sum(out["counts"].values())
    assert n > 0 and out["counts"]["approve"] > 0
    assert "would have approved" in out["summary"]
    # the real ledger was not touched by the dry-run
    assert not (config.STATE_DIR / "CU0001" / "ledger.jsonl").exists()


def test_sizing_clause_asks_when_brand_runs_small():
    from leash.clauses import sizing_advice
    from leash.models import IntentSpec
    from leash.replay import build_events
    from leash.models import AuthorizationEvent
    from leash.data import ref
    ev = build_events("SCEN0002", ref(), "TM_X")[0]
    ev["deadline_at"] = "2026-09-18T00:00:08Z"; ev["runtime"]["received_at"] = "2026-09-18T00:00:00Z"
    a = AuthorizationEvent.model_validate(ev).authorization
    c = sizing_advice(a, {"brand": "adidas", "item_type": "running shoes", "runs": "runs_small", "recommendation": "size up half to one size",
                          "sources": ["https://www.adidas.com/x"], "verified": True, "confidence": "high", "requested_size": "43"}, IntentSpec())
    assert c.status == "fail" and c.severity == "step_up" and "runs small" in c.summary
