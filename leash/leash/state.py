"""Per-customer markdown state: mandates (YAML front matter + prose contract),
questions, knowledge cache."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .config import STATE_DIR
from .models import CompiledMandate


def _split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    _, fm, body = text.split("---", 2)
    return yaml.safe_load(fm) or {}, body.lstrip("\n")


class CustomerState:
    def __init__(self, customer_id: str, state_dir: Path = STATE_DIR):
        self.customer_id = customer_id
        self.dir = state_dir / customer_id
        (self.dir / "mandates").mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- mandates
    def mandate_path(self, key: str) -> Path:
        return self.dir / "mandates" / f"{key}.md"

    def save_mandate(self, m: CompiledMandate, key: str | None = None) -> Path:
        key = key or m.mandate_id or m.draft_id or "draft"
        fm = m.model_dump(mode="json")
        body = render_contract(m)
        text = "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n\n" + body
        p = self.mandate_path(key)
        p.write_text(text, encoding="utf-8")
        return p

    def load_mandate(self, key: str) -> CompiledMandate | None:
        p = self.mandate_path(key)
        if not p.exists():
            return None
        fm, _ = _split_front_matter(p.read_text(encoding="utf-8"))
        return CompiledMandate.model_validate(fm)

    def find_mandate(self, mandate_id: str, instruction: str | None = None) -> CompiledMandate | None:
        m = self.load_mandate(mandate_id)
        if m:
            return m
        if instruction:  # fall back: any stored mandate with the same verbatim instruction
            for p in (self.dir / "mandates").glob("*.md"):
                fm, _ = _split_front_matter(p.read_text(encoding="utf-8"))
                if fm.get("instruction") == instruction:
                    return CompiledMandate.model_validate(fm)
        return None

    # ---------------------------------------------------------------- questions
    @property
    def questions_path(self) -> Path:
        return self.dir / "questions.jsonl"

    def add_question(self, q: dict[str, Any]) -> None:
        with self.questions_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({**q, "created_at": datetime.now().astimezone().isoformat()}) + "\n")

    def questions(self) -> list[dict[str, Any]]:
        if not self.questions_path.exists():
            return []
        rows: dict[str, dict[str, Any]] = {}
        for line in self.questions_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                rows[r["id"]] = r
        return list(rows.values())

    # ---------------------------------------------------------------- knowledge
    @property
    def knowledge_path(self) -> Path:
        return self.dir / "knowledge.md"

    def add_knowledge(self, topic: str, fact: str, source: str, confidence: str = "medium") -> None:
        line = f"- **{topic}** — {fact} _(source: {source}; confidence: {confidence}; {datetime.now().date()})_\n"
        with self.knowledge_path.open("a", encoding="utf-8") as f:
            if self.knowledge_path.stat().st_size == 0:
                f.write(f"# Knowledge cache for {self.customer_id}\n\n")
            f.write(line)

    def knowledge(self) -> str:
        return self.knowledge_path.read_text(encoding="utf-8") if self.knowledge_path.exists() else ""


def render_contract(m: CompiledMandate) -> str:
    """The prose the customer confirms. Plain language, one clause per line."""
    i = m.intent
    lines = [f"# Wallet contract", "", f"> {m.instruction}", "", "## What the engine will enforce", ""]
    if i.per_order_cap_chf is not None:
        lines.append(f"- Each order must total **CHF {i.per_order_cap_chf:.2f} or less**, delivery included.")
    if i.period:
        lines.append(f"- Across any {i.period.days} days, approved orders must stay **at or below CHF {i.period.cap_chf:.2f}**"
                     + (" (rolling window ending at each purchase)." if i.period.sliding else " (calendar period)."))
    if i.allowed_item_categories:
        lines.append(f"- Every item in the basket must be in: **{', '.join(i.allowed_item_categories)}**.")
    if i.forbidden_item_categories:
        lines.append(f"- Never: **{', '.join(i.forbidden_item_categories)}**.")
    if i.requested_item:
        attrs = ", ".join(f"{k} {v}" for k, v in i.requested_item.attributes.items())
        lines.append(f"- The item must be **{i.requested_item.type}**" + (f" ({attrs})" if attrs else "") + ".")
    if i.retailer_categories:
        lines.append(f"- Only from retailers of type: **{', '.join(i.retailer_categories)}**.")
    if i.seller_familiarity_min:
        lines.append(f"- Only from sellers with at least **{i.seller_familiarity_min}** earlier approved purchase(s) on this card.")
    if i.min_return_days is not None:
        lines.append(f"- The order must be returnable for **at least {i.min_return_days} days**; final-sale items are refused.")
    if i.no_addons:
        lines.append("- **No add-ons**: only the requested item(s); protection plans, subscriptions or extras pause the purchase.")
    if i.one_time:
        lines.append("- This is a **one-time** purchase; a second one pauses for your confirmation.")
    if i.session_integrity:
        lines.append("- Purchases that look like **someone else is driving the session** (new device, unusual hour, bursts, unfamiliar sellers) are paused.")
    if i.deliver_by:
        lines.append(f"- Must be deliverable by **{i.deliver_by}**.")
    if i.max_subscription_term_months:
        lines.append(f"- Subscriptions with a minimum term over **{i.max_subscription_term_months} months** pause.")
    lines.append(f"- When a needed fact is missing or ambiguous: **{ {'ask':'ask you','decline':'decline','approve':'approve'}[m.uncertainty_policy] }**.")
    lines.append("- Merchant text is never trusted: instructions hidden in product descriptions are flagged, not followed.")
    if m.assumptions:
        lines += ["", "## Assumptions I made (tell me if wrong)", ""] + [f"- {a}" for a in m.assumptions]
    if m.open_questions:
        lines += ["", "## Questions for you", ""] + [f"- {q}" for q in m.open_questions]
    return "\n".join(lines) + "\n"
