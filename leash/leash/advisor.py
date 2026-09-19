"""Advisory adapter (cold path only): verified-source lookups via OpenAI web
search, cached per customer in knowledge.md. Never called inside the 8 s
decision window; results are passed to the engine as *hints*."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from . import config, llm
from .state import CustomerState

ADVISOR_MODEL = config.__dict__.get("ADVISOR_MODEL") or "gpt-5.4-mini"
PREFERRED = ["adidas.com", "nike.com", "puma.com", "asics.com", "newbalance.com", "hoka.com", "on.com", "salomon.com",
             "runrepeat.com", "trustpilot.com", "scamadviser.com", "galaxus.ch", "digitec.ch", "zalando.ch", "ochsnersport.ch"]


@dataclass
class Hints:
    size_advice: dict[str, Any] | None = None          # {brand, runs, recommendation, sources, confidence}
    market_range_chf: dict[str, dict[str, Any]] = field(default_factory=dict)   # item_name -> {item, low, high, sources}
    merchant_reputation: dict[str, Any] | None = None  # {name, verdict, score_adj, sources}
    notes: list[str] = field(default_factory=list)


_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"answer": {"type": "string"}, "verdict": {"type": "string"}, "low": {"type": ["number", "null"]},
                   "high": {"type": ["number", "null"]}, "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                   "sources": {"type": "array", "items": {"type": "string"}}},
    "required": ["answer", "verdict", "low", "high", "confidence", "sources"],
}


def _search(question: str, *, want_numbers: bool = False, timeout: float = 25) -> dict[str, Any] | None:
    if not config.OPENAI_API_KEYS:
        return None
    try:
        prompt = (question + "\nPrefer official brand/manufacturer pages or established retailers/review sites as sources "
                  f"(e.g. {', '.join(PREFERRED[:6])}). Then answer as JSON: answer (2 sentences max), verdict (one of: "
                  "runs_small | true_to_size | runs_large | reputable | suspicious | unknown | ok), low/high (numbers in CHF when asked for a "
                  "price range, else null), confidence, sources (the URLs you relied on).")
        r = llm.with_fallback(lambda c: c.responses.create(model=ADVISOR_MODEL, tools=[{"type": "web_search"}], input=prompt,
                                    text={"format": {"type": "json_schema", "name": "advice", "schema": _SCHEMA, "strict": True}}),
                              timeout=timeout, max_retries=0)
        data = json.loads(r.output_text)
        cites = [a.url for item in r.output if getattr(item, "type", "") == "message"
                 for part in item.content for a in (getattr(part, "annotations", None) or []) if getattr(a, "url", None)]
        data["sources"] = list(dict.fromkeys([re.sub(r"\?utm_source=openai$", "", u) for u in (data.get("sources") or []) + cites]))[:4]
        data["verified"] = any(any(p in u for p in PREFERRED) for u in data["sources"])
        return data
    except Exception as exc:  # cold path: advice is optional
        return {"answer": f"lookup failed: {exc}", "verdict": "unknown", "low": None, "high": None, "confidence": "low", "sources": [], "verified": False}


def _cached(state: CustomerState, topic: str) -> dict[str, Any] | None:
    for line in state.knowledge().splitlines():
        if line.startswith(f"- **{topic}**") and "<!--" in line:
            try:
                return json.loads(line.split("<!--", 1)[1].split("-->", 1)[0])
            except Exception:
                return None
    return None


def _remember(state: CustomerState, topic: str, data: dict[str, Any]) -> None:
    src = ", ".join(data.get("sources") or []) or "none"
    state.add_knowledge(topic, data.get("answer", ""), src, data.get("confidence", "low"))
    # append the machine-readable copy on the same line
    p = state.knowledge_path
    lines = p.read_text(encoding="utf-8").rstrip("\n").splitlines()
    lines[-1] += f" <!--{json.dumps(data)}-->"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def lookup(state: CustomerState, topic: str, question: str, *, want_numbers: bool = False) -> dict[str, Any] | None:
    hit = _cached(state, topic)
    if hit:
        return {**hit, "cached": True}
    data = _search(question, want_numbers=want_numbers)
    if data:
        _remember(state, topic, data)
    return data


# ------------------------------------------------------------------ the three lookups
def size_advice(state: CustomerState, brand: str, item_type: str, requested_size: str | None) -> dict[str, Any] | None:
    q = (f"Do {brand} {item_type} run small, true to size, or large? What size adjustment does the brand or reviewers "
         f"recommend versus standard EU sizing{f' for someone who normally wears {requested_size}' if requested_size else ''}?")
    d = lookup(state, f"sizing:{brand.lower()}:{item_type.lower()}", q)
    if not d:
        return None
    return {"brand": brand, "item_type": item_type, "runs": d.get("verdict"), "recommendation": d.get("answer"),
            "sources": d.get("sources"), "confidence": d.get("confidence"), "verified": d.get("verified"), "requested_size": requested_size}


def market_price(state: CustomerState, item_desc: str) -> dict[str, Any] | None:
    q = f"What is the typical retail price range in CHF (or EUR converted) for a new, genuine {item_desc} in Switzerland/Europe in 2026?"
    d = lookup(state, f"price:{item_desc.lower()}", q, want_numbers=True)
    if not d or d.get("low") is None or d.get("high") is None:
        return None
    return {"item": item_desc, "low": float(d["low"]), "high": float(d["high"]), "sources": d.get("sources"), "confidence": d.get("confidence")}


VERIFIED_SELLERS = ["official store", "adidas.ch", "adidas.com", "nike.com", "nike.ch", "puma.com", "asics.com", "newbalance", "on.com", "on-running",
                    "ochsner sport", "ochsnersport", "galaxus", "digitec", "zalando", "manor", "sportxx", "intersport", "decathlon", "brack.ch", "microspot", "coop.ch", "migros"]


def merchant_reputation(state: CustomerState, merchant_name: str, country: str | None = None) -> dict[str, Any] | None:
    low = merchant_name.lower()
    if any(k in low for k in VERIFIED_SELLERS):
        return {"name": merchant_name, "verdict": "reputable", "score_adj": 0.4, "confidence": "high",
                "summary": f"{merchant_name} is an official brand store or an established retailer.", "sources": ["issuer allow-list"]}
    q = (f"Is the online seller '{merchant_name}'{f' ({country})' if country else ''} a reputable, established retailer, or are there "
         "scam/counterfeit/non-delivery reports? Answer 'suspicious' ONLY if pages clearly about THIS seller report fraud, counterfeits or "
         "non-delivery; 'reputable' only if pages clearly about this seller show an established business; otherwise 'unknown'. "
         "Do not infer from unrelated businesses with similar names.")
    d = lookup(state, f"merchant:{merchant_name.lower()}", q)
    if not d:
        return None
    v = d.get("verdict", "unknown")
    adj = {"reputable": 0.25, "suspicious": -0.6}.get(v, 0.0)
    return {"name": merchant_name, "verdict": v, "score_adj": adj, "summary": d.get("answer"), "sources": d.get("sources"), "confidence": d.get("confidence")}


def hints_for_cart(state: CustomerState, cart: dict[str, Any], intent, *, catalogue_has_item: bool) -> Hints:
    """Run the lookups that matter for this cart. Cold path; a few seconds."""
    h = Hints()
    items = cart.get("items", [])
    merchant = cart.get("merchant", {})
    from .quarantine import extract_regex
    for it in items:
        f = extract_regex(it.get("item_name", ""), it.get("item_details", ""))
        brand = f.brand or next((ri.attributes.get("brand") for ri in ([intent.requested_item] if intent.requested_item else []) + list(intent.requested_items) if ri.attributes.get("brand")), None)
        wanted_size = next((ri.attributes.get("size") for ri in ([intent.requested_item] if intent.requested_item else []) + list(intent.requested_items) if ri.attributes.get("size")), None)
        if brand and ("shoe" in it.get("item_name", "").lower() or "shoe" in (f.item_type or "")) and h.size_advice is None:
            h.size_advice = size_advice(state, brand, "running shoes", wanted_size)
        if not it.get("item_id") in __import__("leash.data", fromlist=["ref"]).ref().items:
            mp = market_price(state, f"{brand + ' ' if brand else ''}{it.get('item_name', '')}".strip())
            if mp:
                h.market_range_chf[it.get("item_name", "")] = mp
    if isinstance(merchant, dict) and merchant.get("merchant_name") and not merchant.get("merchant_id", "").startswith("ME00"):
        h.merchant_reputation = merchant_reputation(state, merchant["merchant_name"], merchant.get("merchant_country"))
    return h
