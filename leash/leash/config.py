"""Runtime configuration. Everything is overridable by environment variable."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader: KEY=VALUE lines, existing env wins."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_dotenv(ROOT / ".env")

DATA_DIR = Path(os.environ.get("LEASH_DATA_DIR", ROOT.parent / "viseca-2026" / "data"))
STATE_DIR = Path(os.environ.get("LEASH_STATE_DIR", ROOT / "state"))
FIXTURES_DIR = ROOT / "fixtures"

# Viseca sandbox
LEASH_BASE_URL = os.environ.get(
    "LEASH_BASE_URL",
    "https://leash-api-production.up.railway.app",
)
TEAM_API_KEY = os.environ.get("TEAM_API_KEY", "")

# Models (verify names against the OpenAI model list on event day)
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_API_KEY_2 = os.environ.get("OPENAI_API_KEY_2", "")   # fallback if the primary key is rate-limited/out of quota
OPENAI_API_KEYS = [k for k in dict.fromkeys([OPENAI_API_KEY, OPENAI_API_KEY_2]) if k]  # primary first, de-duplicated
EXTRACTOR_MODEL = os.environ.get("LEASH_EXTRACTOR_MODEL", "gpt-4.1-nano")   # ~1 s, benchmarked 2026-09-18
COMPILER_MODEL = os.environ.get("LEASH_COMPILER_MODEL", "gpt-4.1")          # ~3 s; gpt-5 took 30 s
EXTRACTOR_TIMEOUT_S = float(os.environ.get("LEASH_EXTRACTOR_TIMEOUT_S", "1.8"))   # platform redelivers after 3 s
REASONING_EFFORT = os.environ.get("LEASH_REASONING_EFFORT", "low")   # only sent to gpt-5* models
MIN_BUDGET_FOR_MODEL_S = float(os.environ.get("LEASH_MIN_BUDGET_FOR_MODEL_S", "3.5"))

# Decision thresholds (all surfaced in receipts)
THRESHOLDS = {
    "familiar_before_min": 1,          # "a seller I have bought from before"
    "familiar_regular_min": 3,         # "a shop I use regularly"
    "duplicate_window_hours": 24,
    "duplicate_amount_tolerance": 0.05,
    "lookalike_min_similarity": 88,    # rapidfuzz ratio 0-100
    "merchant_trusted_score": 0.6,
    "merchant_risky_score": 0.4,
    "trust_escalate": 0.5,
    "trust_relax": 0.3,
    "price_sanity_low_factor": 0.5,    # below 50 % of catalogue minimum → suspicious
    "injection_step_up_level": 2,
    "familiarity_violation": "decline",  # or "step_up"
}
