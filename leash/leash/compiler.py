"""Policy compiler: verbatim customer instruction -> CompiledMandate.
`compile_rules` is deterministic and always available; `compile_llm` uses an
OpenAI model with a JSON schema and falls back to the rules on any failure."""
from __future__ import annotations

import json
import re
from datetime import date

from . import config, llm
from .config import THRESHOLDS
from .models import CompiledMandate, IntentSpec, MandateRule, PeriodCap, RequestedItem

_NUM = r"(\d+(?:[.,]\d+)?)"
_MONEY = re.compile(r"(?:chf|fr\.?|sfr)\s*" + _NUM + r"|" + _NUM + r"\s*(?:chf|francs?)", re.I)
_WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "ten": 10, "fourteen": 14, "thirty": 30}

_CATEGORY_HINTS = {
    "groceries": ["grocer", "groceries", "food shopping", "supermarket"],
    "clothing": ["clothing", "clothes", "apparel", "jacket", "jersey", "shirt"],
    "sporting_goods": ["running shoes", "road-running", "sports", "sporting", "trainers", "sneakers", "adidas", "nike"],
    "electronics": ["monitor", "laptop", "electronics", "headphones", "phone"],
    "books": ["book"], "pet_care": ["pet", "dog", "cat food"], "household": ["household goods"],
}
_RETAILER_HINTS = {"specialist sports retailer": ["sporting_goods"], "sports shop": ["sporting_goods"], "sports retailer": ["sporting_goods"],
                   "electronics retailer": ["electronics"], "supermarket": ["groceries"], "pharmacy": ["health"]}


def _money(m: re.Match) -> float:
    return float((m.group(1) or m.group(2)).replace(",", "."))


def _days(text: str) -> int | None:
    m = re.search(r"(\d+|" + "|".join(_WORDNUM) + r")\s+days?", text, re.I)
    if not m:
        return None
    w = m.group(1).lower()
    return int(w) if w.isdigit() else _WORDNUM[w]


def compile_rules(instruction: str) -> CompiledMandate:
    t = instruction.lower()
    intent = IntentSpec(purpose=instruction.strip().rstrip("."))
    rules: list[MandateRule] = []
    assumptions: list[str] = []
    questions: list[str] = []

    # money: per-order cap and period cap
    moneys = [(m.start(), _money(m)) for m in _MONEY.finditer(t)]
    per_order = None
    for pos, val in moneys:
        window = t[max(0, pos - 60): pos + 40]
        if re.search(r"(across|total|in any|every|per (week|month))", window) and not re.search(r"return", window):
            days = _days(window) or (7 if "week" in window else 30)
            intent.period = PeriodCap(days=days, cap_chf=val, sliding=True)
            rules.append(MandateRule(field="authorization.billing_amount_chf", operator="<=", value=val, currency="CHF", scope="period", period_days=days))
            assumptions.append(f"'{days} days' is read as a rolling window ending at each purchase (stricter than calendar days).")
        elif per_order is None:
            per_order = val
    if per_order is not None:
        intent.per_order_cap_chf = per_order
        rules.append(MandateRule(field="authorization.billing_amount_chf", operator="<=", value=per_order, currency="CHF", scope="purchase"))
        assumptions.append("The per-order limit includes delivery fees (the order total is what is charged).")

    # categories / requested item
    for cat, hints in _CATEGORY_HINTS.items():
        if any(h in t for h in hints):
            intent.allowed_item_categories.append(cat)
    if m := re.search(r"(\d{2}-inch\s+\w+|road-running shoes|running shoes|trail-running shoes|[\w-]+\s+shoes|jersey|jacket|monitor)", t):
        item = m.group(1)
        attrs = {}
        if s := re.search(r"size\s*(\d{2}|xs|s|m|l|xl)", t):
            attrs["size"] = s.group(1).upper()
        intent.requested_item = RequestedItem(type=item, attributes=attrs, quantity=1)
        intent.no_addons = True
        intent.allowed_item_categories = []          # item-level check replaces category gate
    if "do not add anything" in t or "nothing else" in t or "only the" in t:
        intent.no_addons = True
    if "groceries" in intent.allowed_item_categories:
        rules.append(MandateRule(field="items[].item_category", operator="in", value=["groceries"]))
    rules.append(MandateRule(field="items[].item_category", operator="not_in", value=["gift_card"]))

    # retailer type
    for phrase, cats in _RETAILER_HINTS.items():
        if phrase in t:
            intent.retailer_categories = cats
            rules.append(MandateRule(field="authorization.merchant.merchant_category", operator="in", value=cats))

    # familiarity
    if re.search(r"(shop|seller|store|merchant)s? i (use|shop at) regularly|regular shop", t):
        intent.seller_familiarity_min = THRESHOLDS["familiar_regular_min"]
        assumptions.append(f"'Regularly' = at least {THRESHOLDS['familiar_regular_min']} earlier approved purchases at that shop on this card.")
    elif re.search(r"(bought|purchased|used|shopped) (from|at|with)? ?before|have used before|i have used", t):
        intent.seller_familiarity_min = THRESHOLDS["familiar_before_min"]
        assumptions.append("'Before' = at least one earlier approved purchase at that seller on this card.")
    if intent.seller_familiarity_min:
        rules.append(MandateRule(field="history.approved_merchant_transaction_count", operator=">=", value=intent.seller_familiarity_min))

    # return terms
    if m := re.search(r"return\w*\s+within\s+(\d+)\s+days", t):
        intent.min_return_days = int(m.group(1))
        rules.append(MandateRule(field="items[].return_days", operator=">=", value=intent.min_return_days))
    # one-time vs standing
    if re.search(r"\b(replace|buy the|the .* i chose|one .* item)\b", t):
        # standing by default (Q9); the customer can make it one-time via the question
        questions.append("Is this a one-time purchase? I will pause any second matching order for your confirmation.")
    # session integrity
    if re.search(r"someone other than me|not me driving|session", t):
        intent.session_integrity = True
    # delivery date
    if m := re.search(r"by (\d{4}-\d{2}-\d{2})", t):
        intent.deliver_by = date.fromisoformat(m.group(1))
    elif re.search(r"birthday|anniversary|in a week|next week", t):
        questions.append("By which date must it arrive? I will refuse anything that ships later.")
    if "subscription" in t:
        questions.append("What is the longest minimum term you accept for a subscription (months)?")

    # uncertainty
    policy = "ask" if re.search(r"ask me", t) else ("decline" if re.search(r"decline when (unsure|uncertain)|never guess", t) else "ask")
    if policy == "ask" and not re.search(r"ask me", t):
        assumptions.append("No instruction for uncertain cases; defaulting to asking you.")
    if intent.per_order_cap_chf is None and intent.period is None:
        questions.append("What is the most a single order may cost?")

    return CompiledMandate(instruction=instruction, hard_rules=rules, uncertainty_policy=policy, intent=intent,
                           guidance=[f"intent:{json.dumps(intent.model_dump(mode='json'))}"], open_questions=questions,
                           assumptions=assumptions, compiled_by="rules")


_LLM_SYSTEM = """You compile a cardholder's plain-language spending instruction into a machine-checkable wallet policy for an issuer-side control engine. Output JSON matching the schema. Rules:
- Keep the customer's meaning; never widen permissions. When unsure, leave the field null and add an open question.
- per_order_cap_chf: the maximum a single order may total, delivery included. 'around CHF X total' for a multi-item request -> per_order_cap_chf X AND period {days: 30, cap_chf: X}.
- period: a rolling cap over N days ('across any seven days' -> days 7).
- allowed_item_categories vocabulary: groceries, clothing, sporting_goods, electronics, books, pet_care, household, health, subscriptions, membership, cosmetics, gift_card, transport, dining, food_delivery, fuel, travel, hotel, entertainment, software, sustainable_goods, kids_family, home_improvement, photography. Leave empty when requested_items are given.
- requested_items: one entry per specific thing the customer names, e.g. [{type: 'road-running shoes', attributes: {size: '43'}}, {type: 'football jersey', attributes: {brand: 'adidas'}}]. `type` is a short natural phrase KEEPING qualifiers (road-running, 27-inch). `attributes` keys are ONLY size, brand, color; omit unknown ones (ask instead). Set no_addons true when items are named.
- one_time: true ONLY if the customer says once/one-time/only one; otherwise false.
- retailer_categories: merchant types the customer restricts to (same vocabulary), e.g. 'specialist sports retailer' -> ['sporting_goods'].
- seller_familiarity_min: 3 for 'regularly', 1 for 'bought from before / used before'; null if not mentioned.
- session_integrity: true if the customer wants pauses when the session looks like someone else.
- deliver_by: ISO date only if stated or exactly derivable; 'birthday in a week' -> null + open question for the date.
- open_questions: AT MOST 3, only facts that block a decision and that the customer alone can answer (size, deadline date, brand yes/no). Never ask about color, style, model preferences.
- assumptions: interpretations you made, at most 4, one sentence each.
- uncertainty_policy: 'ask' if they say ask me; 'decline' if they say decline when unsure; else 'ask'."""


def compile_llm(instruction: str, today: date | None = None) -> CompiledMandate | None:
    if not config.OPENAI_API_KEYS:
        return None
    try:
        schema = IntentSpec.model_json_schema()
        wrapper = {"type": "object", "additionalProperties": False,
                   "properties": {"intent": schema, "open_questions": {"type": "array", "items": {"type": "string"}},
                                  "assumptions": {"type": "array", "items": {"type": "string"}},
                                  "uncertainty_policy": {"type": "string", "enum": ["ask", "decline", "approve"]}},
                   "required": ["intent", "open_questions", "assumptions", "uncertainty_policy"]}
        extra = {"reasoning_effort": config.REASONING_EFFORT} if config.COMPILER_MODEL.startswith("gpt-5") else {}
        resp = llm.with_fallback(lambda c: c.chat.completions.create(
            model=config.COMPILER_MODEL, **extra,
            messages=[{"role": "system", "content": _LLM_SYSTEM + f"\nToday is {today or date.today()}."},
                      {"role": "user", "content": instruction}],
            response_format={"type": "json_schema", "json_schema": {"name": "policy", "schema": wrapper}},
        ), timeout=30, max_retries=1)
        data = json.loads(resp.choices[0].message.content or "{}")
        intent = IntentSpec.model_validate(data["intent"])
        banned = re.compile(r"colou?r|style|design|model preference|preferred brand|preferred model", re.I)
        data["open_questions"] = [q for q in data["open_questions"] if not banned.search(q)][:3]
        base = compile_rules(instruction)          # rules produce the API hard_rules; LLM refines intent
        merged = base.model_copy(update={"intent": intent, "open_questions": data["open_questions"],
                                         "assumptions": data["assumptions"], "uncertainty_policy": data["uncertainty_policy"],
                                         "guidance": [f"intent:{json.dumps(intent.model_dump(mode='json'))}"], "compiled_by": config.COMPILER_MODEL})
        return merged
    except Exception:
        return None


def compile_instruction(instruction: str, prefer_llm: bool = True) -> CompiledMandate:
    if prefer_llm:
        m = compile_llm(instruction)
        if m:
            return m
    return compile_rules(instruction)


_APPLY_SYSTEM = """You update a wallet policy after the customer answered a follow-up question. Given the current intent JSON, the question and the customer's answer, return the FULL updated intent JSON (same schema). Only change what the answer settles: e.g. a date -> deliver_by; a shoe size -> attributes.size on the matching requested item; 'yes it's one-time' -> one_time true; a brand -> attributes.brand. Never widen limits unless the answer explicitly raises them. Also return 'note': one sentence describing the change."""


def apply_answer(m: CompiledMandate, question: str, answer: str, today: date | None = None) -> tuple[CompiledMandate, str]:
    """Merge a customer's answer into the intent. Rules first; LLM for the rest."""
    i = m.intent
    a = answer.strip()
    ql = question.lower()
    note = ""
    if (d := re.search(r"(\d{4}-\d{2}-\d{2})", a)) and ("date" in ql or "deliver" in ql or "birthday" in ql or "by when" in ql):
        i.deliver_by = date.fromisoformat(d.group(1)); note = f"deliver by {i.deliver_by}"
    elif (sz := re.fullmatch(r"(?:eu\s*)?(\d{2}(?:[.,]5)?|xs|s|m|l|xl)", a, re.I)) and "size" in ql:
        all_items = ([i.requested_item] if i.requested_item else []) + list(i.requested_items)
        qtok = set(re.findall(r"[a-z]+", ql))
        named = [ri for ri in all_items if any(t in qtok for t in re.findall(r"[a-z]+", ri.type.lower()) if t not in ("adidas", "nike", "the", "a"))]
        targets = named or [ri for ri in all_items if "size" not in ri.attributes] or all_items
        for ri in targets:
            ri.attributes["size"] = sz.group(1).upper()
        note = f"size {sz.group(1).upper()} on {', '.join(ri.type for ri in targets)}"
    elif "one-time" in ql or "one time" in ql:
        i.one_time = a.lower().startswith(("y", "ja", "oui", "si"))
        note = f"one_time={i.one_time}"
    elif "months" in ql and re.fullmatch(r"\d{1,3}", a):
        i.max_subscription_term_months = int(a); note = f"max subscription term {a} months"
    elif ("most a single order" in ql or "maximum amount" in ql or "spend" in ql or "budget" in ql) and (mo := re.search(r"(\d+(?:[.,]\d+)?)\s*(chf|eur|usd|gbp|€|\$|£)?|(chf|eur|usd|gbp|€|\$|£)\s*(\d+(?:[.,]\d+)?)", a, re.I)):
        amt = float((mo.group(1) or mo.group(4)).replace(",", "."))
        cur = (mo.group(2) or mo.group(3) or "chf").lower().replace("€", "eur").replace("$", "usd").replace("£", "gbp")
        fx = {"chf": 1.0, "eur": 0.95, "usd": 0.87, "gbp": 1.12}[cur]
        i.per_order_cap_chf = round(amt * fx, 2); note = f"per-order cap CHF {i.per_order_cap_chf}" + (f" (from {amt:g} {cur.upper()})" if cur != "chf" else "")
    elif config.OPENAI_API_KEYS:
        try:
            schema = {"type": "object", "additionalProperties": False, "properties": {"intent": IntentSpec.model_json_schema(), "note": {"type": "string"}}, "required": ["intent", "note"]}
            r = llm.with_fallback(lambda c: c.chat.completions.create(model=config.COMPILER_MODEL, messages=[
                {"role": "system", "content": _APPLY_SYSTEM + f"\nToday is {today or date.today()}."},
                {"role": "user", "content": json.dumps({"intent": i.model_dump(mode="json"), "question": question, "answer": a})}],
                response_format={"type": "json_schema", "json_schema": {"name": "update", "schema": schema}}), timeout=30, max_retries=0)
            data = json.loads(r.choices[0].message.content or "{}")
            i = IntentSpec.model_validate(data["intent"]); note = data.get("note", "updated by model")
        except Exception as exc:
            note = f"answer recorded, not applied ({exc})"
    else:
        note = "answer recorded, not applied (no rule matched)"
    m.intent = i
    m.open_questions = [q for q in m.open_questions if q != question]
    m.assumptions.append(f"Customer answered '{question}' → {a} ({note}).")
    m.guidance = [f"intent:{json.dumps(i.model_dump(mode='json'))}"]
    return m, note
