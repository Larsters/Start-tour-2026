"""Pydantic models: the live event exactly as the sponsor's schema defines it,
our IntentSpec, and the decision receipt."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Currency = Literal["CHF", "EUR", "GBP", "USD"]
Decision = Literal["approve", "decline", "step_up"]
Tri = Literal["true", "false", "unknown", "not_applicable"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- live event
class Merchant(Strict):
    merchant_id: str
    merchant_name: str
    merchant_category: str
    merchant_mcc: str
    merchant_country: str
    merchant_city: str
    availability: Literal["online", "store", "store_and_online", "atm"]
    recurring_capable: Literal["true", "false"]


class Item(Strict):
    line_no: int = Field(ge=1)
    item_id: str
    item_name: str
    item_category: str
    quantity: int = Field(ge=1)
    unit_price: float
    currency: Currency
    item_details: str


class Authorization(Strict):
    authorization_id: str
    source_authorization_id: str
    scenario_id: str
    replay_order: int
    mandate_id: str
    profile_id: str
    card_id: str
    initiator_type: Literal["agent"]
    merchant: Merchant
    timestamp: datetime
    amount: float
    currency: Currency
    billing_amount_chf: float
    items_subtotal: float
    delivery_fee: float
    channel: Literal["ecommerce", "in_store", "mobile_wallet", "recurring", "atm"]
    customer_device_id: str
    authority_status: Literal["active", "revoked", "expired"]
    card_status_at_attempt: Literal["active", "blocked"]
    spend_in_period_before_chf: float | None
    recent_attempt_count_10m: int = Field(ge=0)
    fulfillment_method: str
    delivery_by: str | None
    order_returnable: Tri
    order_cancellable: Tri
    related_authorization_id: str | None
    related_authorization_status: Literal["pending", "approved", "declined", "cancelled"] | None
    purchase_description: str
    items: list[Item] = Field(min_length=1)


class MandateRule(Strict):
    field: str
    operator: Literal["<", "<=", "=", "!=", ">", ">=", "in", "not_in"]
    value: float | int | str | list[str]
    currency: Currency | None = None
    scope: Literal["purchase", "period"] | None = None
    period_days: int | None = None


class MandateSnapshot(Strict):
    mandate_id: str
    status: Literal["active", "superseded", "revoked", "expired"]
    customer_id: str
    card_id: str
    instruction: str
    hard_rules: list[MandateRule]
    uncertainty_policy: Literal["ask", "decline", "approve"]
    profile_id: str


class RecentAuthorization(Strict):
    authorization_id: str
    timestamp: datetime
    merchant_id: str
    billing_amount_chf: float
    status: Literal["approved", "declined", "pending", "cancelled"]


class Context(Strict):
    approved_spend_in_period_chf: float | None
    recent_authorizations: list[RecentAuthorization]


class Runtime(Strict):
    received_at: datetime
    history_window_minutes: int
    context_basis: str


class AuthorizationEvent(Strict):
    type: Literal["authorization.request"]
    request_id: str
    deadline_at: datetime
    authorization: Authorization
    mandate: MandateSnapshot
    context: Context
    runtime: Runtime


# ---------------------------------------------------------------- intent spec
class RequestedItem(BaseModel):
    type: str                                   # "road-running shoes"
    attributes: dict[str, str] = Field(default_factory=dict)   # {"size": "43"}
    quantity: int = 1


class PeriodCap(BaseModel):
    days: int
    cap_chf: float
    sliding: bool = True


class IntentSpec(BaseModel):
    """What the API's rule format cannot hold. Stored in the customer's mandate.md."""
    purpose: str = ""
    per_order_cap_chf: float | None = None
    period: PeriodCap | None = None
    allowed_item_categories: list[str] = Field(default_factory=list)
    forbidden_item_categories: list[str] = Field(default_factory=lambda: ["gift_card"])
    requested_item: RequestedItem | None = None
    retailer_categories: list[str] = Field(default_factory=list)   # merchant_category values
    seller_familiarity_min: int | None = None                       # approved history rows
    min_return_days: int | None = None
    no_addons: bool = False
    one_time: bool = False
    session_integrity: bool = False
    deliver_by: date | None = None
    max_subscription_term_months: int | None = None
    fulfillment: Literal["delivery", "digital", "any"] = "any"


class CompiledMandate(BaseModel):
    instruction: str
    hard_rules: list[MandateRule]
    uncertainty_policy: Literal["ask", "decline", "approve"] = "ask"
    intent: IntentSpec
    guidance: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    mandate_id: str | None = None
    draft_id: str | None = None
    customer_id: str | None = None
    card_id: str | None = None
    compiled_by: str = "rules"


# ---------------------------------------------------------------- receipt
ClauseStatus = Literal["pass", "fail", "unknown", "info"]
Severity = Literal["decline", "step_up", "info"]


class ClauseResult(BaseModel):
    clause: str
    status: ClauseStatus
    severity: Severity = "info"
    summary: str = ""
    value: Any = None
    limit: Any = None
    counterfactual: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class RecommendedAction(BaseModel):
    type: Literal["one_time_card"]
    cap_chf: float
    merchant_id: str
    expires_in_hours: int = 24
    note: str = ""


class Receipt(BaseModel):
    authorization_id: str
    decision: Decision
    reason_codes: list[str]
    customer_message: str
    evidence: list[ClauseResult]
    advice: list[str] = Field(default_factory=list)
    recommended_action: RecommendedAction | None = None
    engine_version: str
    degraded: bool = False
    elapsed_ms: int = 0

    def to_api_payload(self) -> dict[str, Any]:
        """Body for POST /v1/authorizations/{id}/decision."""
        return {
            "authorization_id": self.authorization_id,
            "decision": self.decision,
            "reason_codes": self.reason_codes,
            "customer_message": self.customer_message,
            "evidence": [c.model_dump(mode="json") for c in self.evidence],
            "engine_version": self.engine_version,
        }
