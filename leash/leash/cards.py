"""Customer-facing cards: the trusted channel. A card is a question the
customer must answer (compile follow-up, mandate confirmation, step-up).
The chatbot renders cards fetched from here and posts answers here; the LLM
in the chat never carries an answer."""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from . import config

CardKind = Literal["question", "confirm_mandate", "step_up", "info"]


class Cards:
    def __init__(self, customer_id: str, state_dir: Path | None = None):
        state_dir = state_dir or config.STATE_DIR
        self.customer_id = customer_id
        self.path = state_dir / customer_id / "cards.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _all(self) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    r = json.loads(line)
                    rows[r["id"]] = r
        return rows

    def _append(self, row: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")

    def create(self, kind: CardKind, title: str, body: str, *, options: list[str] | None = None,
               ref: dict[str, Any] | None = None, deadline: str | None = None) -> dict[str, Any]:
        row = {"id": f"card_{uuid.uuid4().hex[:10]}", "kind": kind, "title": title, "body": body,
               "options": options or ["approve", "decline"], "ref": ref or {}, "status": "pending",
               "answer": None, "created_at": datetime.now().astimezone().isoformat(), "deadline": deadline}
        self._append(row)
        return row

    def answer(self, card_id: str, answer: str, note: str = "") -> dict[str, Any] | None:
        rows = self._all()
        row = rows.get(card_id)
        if not row or row["status"] != "pending":
            return None
        row = {**row, "status": "answered", "answer": answer, "note": note,
               "answered_at": datetime.now().astimezone().isoformat()}
        self._append(row)
        return row

    def pending(self) -> list[dict[str, Any]]:
        return [r for r in self._all().values() if r["status"] == "pending"]

    def get(self, card_id: str) -> dict[str, Any] | None:
        return self._all().get(card_id)

    def list(self) -> list[dict[str, Any]]:
        return list(self._all().values())
