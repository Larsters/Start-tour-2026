"""Append-only per-customer ledger (JSONL). The only source of truth for
period spend, duplicates, idempotency and session-trust state."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import STATE_DIR


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class Ledger:
    def __init__(self, customer_id: str, state_dir: Path = STATE_DIR):
        self.customer_id = customer_id
        self.path = state_dir / customer_id / "ledger.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._rows: dict[str, dict[str, Any]] = {}
        self._trust: dict[str, float] = {}      # mandate_id -> session trust score
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("kind") == "trust":
                self._trust[row["mandate_id"]] = row["score"]
            else:
                self._rows[row["authorization_id"]] = row   # later lines override (resolve)

    def _append(self, row: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")

    # ---------------------------------------------------------------- writes
    def record_decision(self, *, authorization_id: str, mandate_id: str, run_id: str | None,
                        sim_ts: datetime, amount_chf: float, merchant_id: str, item_ids: list[str],
                        decision: str, receipt: dict[str, Any]) -> None:
        row = {
            "kind": "decision", "authorization_id": authorization_id, "mandate_id": mandate_id,
            "run_id": run_id, "sim_ts": sim_ts.isoformat(), "amount_chf": amount_chf,
            "merchant_id": merchant_id, "item_ids": sorted(item_ids), "decision": decision,
            "final_status": {"approve": "approved", "decline": "declined", "step_up": "pending"}[decision],
            "receipt": receipt, "recorded_at": datetime.now().astimezone().isoformat(),
        }
        self._rows[authorization_id] = row
        self._append(row)

    def resolve(self, authorization_id: str, decision: str, note: str = "") -> dict[str, Any] | None:
        row = self._rows.get(authorization_id)
        if row is None:
            return None
        row = {**row, "final_status": "approved" if decision == "approve" else "declined",
               "resolved": {"decision": decision, "note": note, "at": datetime.now().astimezone().isoformat()}}
        self._rows[authorization_id] = row
        self._append(row)
        return row

    def set_trust(self, mandate_id: str, score: float) -> None:
        self._trust[mandate_id] = score
        self._append({"kind": "trust", "mandate_id": mandate_id, "score": score})

    # ---------------------------------------------------------------- reads
    def get(self, authorization_id: str) -> dict[str, Any] | None:
        return self._rows.get(authorization_id)

    def trust(self, mandate_id: str) -> float:
        return self._trust.get(mandate_id, 0.0)

    def rows_for_mandate(self, mandate_id: str) -> list[dict[str, Any]]:
        return sorted((r for r in self._rows.values() if r["mandate_id"] == mandate_id), key=lambda r: r["sim_ts"])

    def approved_in_window(self, mandate_id: str, end: datetime, days: int) -> tuple[float, list[dict[str, Any]]]:
        start = end - timedelta(days=days)
        rows = [r for r in self.rows_for_mandate(mandate_id)
                if r["final_status"] == "approved" and start <= _ts(r["sim_ts"]) <= end]
        return round(sum(r["amount_chf"] for r in rows), 2), rows

    def approved_count(self, mandate_id: str) -> int:
        return sum(1 for r in self.rows_for_mandate(mandate_id) if r["final_status"] == "approved")

    def recent(self, mandate_id: str, end: datetime, hours: float) -> list[dict[str, Any]]:
        start = end - timedelta(hours=hours)
        return [r for r in self.rows_for_mandate(mandate_id) if start <= _ts(r["sim_ts"]) < end]
