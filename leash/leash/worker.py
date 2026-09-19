"""Long-poll worker against the sponsor's sandbox. Idempotent, deadline-aware,
keeps polling while step-ups wait for the customer."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from . import events
from .cards import Cards
from .engine import decide
from .models import AuthorizationEvent, Receipt
from .viseca import VisecaClient

log = logging.getLogger("leash.worker")


def make_step_up_card(customer_id: str, ev: AuthorizationEvent, r: Receipt) -> dict:
    a = ev.authorization
    problems = [c.summary for c in r.evidence if c.status in ("fail", "unknown")]
    options = ["approve", "decline"]
    if r.recommended_action:
        options = ["approve_one_time_card", "approve", "decline"]
    if r.alternative:
        options = ["buy_alternative"] + [o for o in options if o != "approve"]
    body = "\n".join(f"• {p}" for p in problems) or r.customer_message
    return Cards(customer_id).create(
        "step_up", f"Confirm CHF {a.billing_amount_chf:.2f} at {a.merchant.merchant_name}?",
        body, options=options,
        ref={"authorization_id": a.authorization_id, "mandate_id": a.mandate_id, "customer_message": r.customer_message,
             "recommended_action": r.recommended_action.model_dump() if r.recommended_action else None, "alternative": r.alternative,
             "advice": r.advice,
             "items": [f"{it.quantity}× {it.item_name}" for it in a.items]},
    )


def handle_event(client: VisecaClient, envelope: dict, *, allow_model: bool = True) -> Receipt | None:
    data = envelope.get("data")
    run_id = envelope.get("run_id")
    try:
        ev = AuthorizationEvent.model_validate(data)
    except Exception as exc:  # predictable answer for malformed input
        auth_id = (data or {}).get("authorization", {}).get("authorization_id") or envelope.get("authorization_id")
        log.error("schema invalid for %s: %s", auth_id, exc)
        if auth_id:
            client.decision(auth_id, {"authorization_id": auth_id, "decision": "step_up", "reason_codes": ["schema_invalid"],
                                      "customer_message": "The purchase request could not be validated; please review it."})
        return None
    now = datetime.now(timezone.utc)
    from .service import _purchase_summary
    events.publish(ev.mandate.customer_id, "decision.started", mode="sandbox", purchase=_purchase_summary(ev), run_id=run_id)
    r = decide(ev, run_id=run_id, allow_model=allow_model, now=now)
    remaining = (ev.deadline_at - datetime.now(timezone.utc)).total_seconds()
    log.info("%s -> %s [%s] in %d ms (%.1fs left)", r.authorization_id, r.decision, ",".join(r.reason_codes), r.elapsed_ms, remaining)
    client.decision(r.authorization_id, r.to_api_payload())
    card = make_step_up_card(ev.mandate.customer_id, ev, r) if r.decision == "step_up" else None
    events.publish(ev.mandate.customer_id, "decision", mode="sandbox", receipt=r.model_dump(mode="json"), purchase=_purchase_summary(ev), card=card, run_id=run_id)
    return r


def resolve_card(client: VisecaClient, customer_id: str, card: dict, answer: str, note: str = "") -> dict:
    """Customer answered a step-up card: record locally and tell the platform."""
    from .engine import resolve
    auth_id = card["ref"]["authorization_id"]
    decision = "approve" if answer.startswith("approve") else "decline"
    msg = {"buy_alternative": "The customer declined this seller and chose the official-store alternative.", "approve": "The customer confirmed this purchase.",
           "approve_one_time_card": "The customer approved this purchase with a one-time virtual card capped at the order amount for this seller only.",
           "decline": "The customer rejected this purchase."}.get(answer, note or "Customer answered.")
    resolve(customer_id, auth_id, decision, msg)
    evidence = []
    if answer == "approve_one_time_card" and card["ref"].get("recommended_action"):
        evidence = [{"clause": "containment", "status": "info", "summary": "one-time virtual card", **card["ref"]["recommended_action"]}]
    out = client.resolve(auth_id, decision, msg, evidence) if client.configured else {"offline": True}
    if decision == "approve":
        ref = card["ref"]
        how = "approved by you" + (" with a one-time virtual card capped at CHF %.2f" % ref["recommended_action"]["cap_chf"] if answer == "approve_one_time_card" and ref.get("recommended_action") else "")
        title = card["title"].replace("Confirm ", "Order placed · ").rstrip("?")
        body = "\n".join("• " + l.lstrip("• ") for l in card.get("body", "").split("\n") if l.strip())
        Cards(customer_id).create("info", title, f"{how}.\nAuthorization {auth_id}\nYou accepted these warnings:\n{body}" if body else f"{how}.\nAuthorization {auth_id}",
                                  options=[], ref={"kind": "receipt", "authorization_id": auth_id, "mandate_id": ref.get("mandate_id")})
        events.publish(customer_id, "order.placed", card={"title": title, "authorization_id": auth_id})
    return {"authorization_id": auth_id, "decision": decision, "platform": out}


class Worker(threading.Thread):
    def __init__(self, client: VisecaClient, *, allow_model: bool = True, wait: int = 25):
        super().__init__(daemon=True, name="leash-worker")
        self.client, self.allow_model, self.wait = client, allow_model, wait
        self.stop_event = threading.Event()
        self.handled = 0

    def run(self) -> None:
        log.info("worker polling %s", self.client.base_url)
        while not self.stop_event.is_set():
            try:
                env = self.client.next_request(self.wait)
            except Exception as exc:
                log.warning("poll error: %s", exc)
                time.sleep(2)
                continue
            if env is None:
                continue
            try:
                handle_event(self.client, env, allow_model=self.allow_model)
                self.handled += 1
            except Exception as exc:
                log.exception("failed handling event: %s", exc)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    w = Worker(VisecaClient())
    w.run()
