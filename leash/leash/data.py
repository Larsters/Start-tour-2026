"""Reference data: the sponsor's CSV pack, card-level baselines from history,
issuer-wide merchant statistics, catalogue price ranges, FX."""
from __future__ import annotations

import csv
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from functools import lru_cache
from pathlib import Path

from .config import DATA_DIR


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def chf(amount: float | str, currency: str, fx: dict[str, float]) -> float:
    d = Decimal(str(amount)) * Decimal(str(fx[currency]))
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))


@dataclass
class CardBaseline:
    card_id: str
    customer_id: str
    approved_count: int = 0
    merchant_counts: Counter = field(default_factory=Counter)      # approved by merchant_id
    merchant_names: dict[str, str] = field(default_factory=dict)   # merchant_id -> name (familiar)
    device_counts: Counter = field(default_factory=Counter)
    country_counts: Counter = field(default_factory=Counter)
    hour_counts: Counter = field(default_factory=Counter)
    amounts: list[float] = field(default_factory=list)
    per_transaction_limit_chf: float | None = None
    monthly_limit_chf: float | None = None
    online_enabled: bool = True
    international_enabled: bool = True

    def p90_amount(self) -> float:
        if not self.amounts:
            return 0.0
        s = sorted(self.amounts)
        return s[min(len(s) - 1, int(0.9 * len(s)))]

    def hour_share(self, hour: int) -> float:
        total = sum(self.hour_counts.values()) or 1
        return self.hour_counts.get(hour, 0) / total


@dataclass
class MerchantStats:
    merchant_id: str
    cards: set = field(default_factory=set)
    approved: int = 0
    declined: int = 0
    refunds: int = 0
    first_seen: datetime | None = None

    @property
    def approval_rate(self) -> float:
        n = self.approved + self.declined
        return self.approved / n if n else 0.0

    @property
    def refund_rate(self) -> float:
        return self.refunds / self.approved if self.approved else 0.0


class ReferenceData:
    def __init__(self, data_dir: Path = DATA_DIR):
        self.dir = data_dir
        self.merchants = {r["merchant_id"]: r for r in _read(data_dir / "merchants.csv")}
        self.items = {r["item_id"]: r for r in _read(data_dir / "items.csv")}
        self.customers = {r["customer_id"]: r for r in _read(data_dir / "customers.csv")}
        self.accounts = {r["account_id"]: r for r in _read(data_dir / "accounts.csv")}
        self.cards = {r["card_id"]: r for r in _read(data_dir / "cards.csv")}
        self.fx = {r["from_currency"]: float(r["rate"]) for r in _read(data_dir / "fx_rates.csv")}
        self.scenarios = {r["scenario_id"]: r for r in _read(data_dir / "scenario_catalogue.csv")}
        self.authorities = {r["authority_id"]: r for r in _read(data_dir / "scenario_authorities.csv")}
        self.history = _read(data_dir / "authorization_history.csv")
        self.baselines: dict[str, CardBaseline] = {}
        self.merchant_stats: dict[str, MerchantStats] = {}
        self._build()

    # ---------------------------------------------------------------- build
    def _build(self) -> None:
        for card_id, c in self.cards.items():
            acc = self.accounts.get(c["account_id"], {})
            b = CardBaseline(
                card_id=card_id,
                customer_id=acc.get("customer_id", ""),
                per_transaction_limit_chf=float(acc["per_transaction_limit_chf"]) if acc.get("per_transaction_limit_chf") else None,
                monthly_limit_chf=float(acc["monthly_limit_chf"]) if acc.get("monthly_limit_chf") else None,
                online_enabled=c.get("online_enabled", "true") == "true",
                international_enabled=c.get("international_enabled", "true") == "true",
            )
            self.baselines[card_id] = b
        for r in self.history:
            mid = r["merchant_id"]
            ms = self.merchant_stats.setdefault(mid, MerchantStats(merchant_id=mid))
            ts = _ts(r["timestamp"])
            ms.cards.add(r["card_id"])
            ms.first_seen = ts if ms.first_seen is None or ts < ms.first_seen else ms.first_seen
            if r["transaction_type"] == "refund":
                ms.refunds += 1
                continue
            if r["status"] == "approved":
                ms.approved += 1
            else:
                ms.declined += 1
            if r["status"] != "approved" or r["transaction_type"] != "purchase":
                continue
            b = self.baselines.get(r["card_id"])
            if b is None:
                continue
            b.approved_count += 1
            b.merchant_counts[mid] += 1
            b.merchant_names[mid] = r["merchant_name"]
            if r["customer_device_id"]:
                b.device_counts[r["customer_device_id"]] += 1
            b.country_counts[r["merchant_country"]] += 1
            b.hour_counts[ts.hour] += 1
            b.amounts.append(float(r["billing_amount_chf"]))

    # ---------------------------------------------------------------- lookups
    def baseline(self, card_id: str) -> CardBaseline:
        return self.baselines.get(card_id) or CardBaseline(card_id=card_id, customer_id="")

    def merchant(self, merchant_id: str) -> MerchantStats:
        return self.merchant_stats.get(merchant_id) or MerchantStats(merchant_id=merchant_id)

    def item_range(self, item_id: str) -> tuple[float, float, float] | None:
        it = self.items.get(item_id)
        if not it:
            return None
        return (float(it["unit_price_min_chf"]), float(it["unit_price_typical_chf"]), float(it["unit_price_max_chf"]))

    def customer_preferences(self, customer_id: str) -> str:
        c = self.customers.get(customer_id)
        return c["shopping_preferences"] if c else ""

    def history_end(self) -> datetime:
        return max(_ts(r["timestamp"]) for r in self.history)


@lru_cache(maxsize=1)
def ref() -> ReferenceData:
    return ReferenceData()
