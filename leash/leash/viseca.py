"""Thin client for the sponsor's sandbox API (technical_details.md)."""
from __future__ import annotations

from typing import Any

import httpx

from . import config


class VisecaClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, timeout: float = 30.0):
        self.base_url = (base_url or config.LEASH_BASE_URL).rstrip("/")
        self.api_key = api_key or config.TEAM_API_KEY
        self._c = httpx.Client(base_url=self.base_url, timeout=timeout,
                               headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _req(self, method: str, path: str, **kw) -> Any:
        r = self._c.request(method, path, **kw)
        if r.status_code == 204:
            return None
        try:
            body = r.json()
        except ValueError:
            body = {"raw": r.text}
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {body.get('error', body)}")
        return body

    # reference
    def healthz(self) -> Any: return self._c.get("/healthz").json()
    def bootstrap(self) -> Any: return self._req("GET", "/v1/bootstrap")
    def reference_data(self) -> Any: return self._req("GET", "/v1/reference-data")
    def history_csv(self) -> str: return self._c.get("/v1/reference-data/authorization-history.csv").text

    # mandates
    def create_mandate(self, instruction: str, hard_rules: list[dict], uncertainty_policy: str,
                       guidance: list[str], open_questions: list[str]) -> Any:
        return self._req("POST", "/v1/mandates", json={"instruction": instruction, "hard_rules": hard_rules,
                                                       "uncertainty_policy": uncertainty_policy, "guidance": guidance,
                                                       "open_questions": open_questions})
    def confirm_mandate(self, draft_id: str) -> Any: return self._req("POST", f"/v1/mandates/{draft_id}/confirm", json={"confirmed": True})
    def get_mandate(self, mandate_id: str) -> Any: return self._req("GET", f"/v1/mandates/{mandate_id}")
    def patch_mandate(self, mandate_id: str, **fields: Any) -> Any: return self._req("PATCH", f"/v1/mandates/{mandate_id}", json=fields)
    def revoke_mandate(self, mandate_id: str) -> Any: return self._req("DELETE", f"/v1/mandates/{mandate_id}")

    # runs + decisions
    def start_run(self, scenario_id: str, mandate_id: str) -> Any:
        return self._req("POST", "/v1/scenario-runs", json={"scenario_id": scenario_id, "mandate_id": mandate_id})
    def run(self, run_id: str) -> Any: return self._req("GET", f"/v1/scenario-runs/{run_id}")
    def next_request(self, wait: int = 25) -> Any | None:
        return self._req("GET", f"/v1/decision-requests/next?wait={wait}")
    def decision(self, authorization_id: str, payload: dict) -> Any:
        return self._req("POST", f"/v1/authorizations/{authorization_id}/decision", json=payload)
    def resolve(self, authorization_id: str, decision: str, customer_message: str, evidence: list | None = None) -> Any:
        return self._req("POST", f"/v1/authorizations/{authorization_id}/resolve",
                         json={"decision": decision, "customer_message": customer_message, "evidence": evidence or []})
    def authorizations(self) -> Any: return self._req("GET", "/v1/authorizations")
    def events(self, since: int = 0) -> Any: return self._req("GET", f"/v1/events?since={since}")
    def reset(self) -> Any: return self._req("POST", "/v1/team/reset")
