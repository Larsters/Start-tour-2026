"""Shared OpenAI call helper. Every model call in this codebase goes through
`with_fallback` so a rate-limited, out-of-quota, or revoked primary key does
not take the whole model path down mid-event: it tries each key configured in
`config.OPENAI_API_KEYS` in order and returns the first success. Callers keep
their existing `except Exception: return None` (or similar) fallback to the
deterministic path (rules/regex) — this only widens what "the model path
failed" means from one key to all configured keys."""
from __future__ import annotations

import logging
from typing import Any, Callable, TypeVar

from . import config

log = logging.getLogger("leash.llm")
T = TypeVar("T")


def with_fallback(call: Callable[[Any], T], *, timeout: float, max_retries: int = 0) -> T:
    """Run `call(client)` against each configured OpenAI key in turn.

    Returns the first success. Raises the last exception if every key fails,
    or RuntimeError if no key is configured at all — callers already wrap
    this in a broad except, so either case degrades to the deterministic
    fallback the caller already has."""
    from openai import OpenAI

    keys = config.OPENAI_API_KEYS
    if not keys:
        raise RuntimeError("no OpenAI API key configured")
    last_exc: Exception | None = None
    for i, key in enumerate(keys):
        try:
            return call(OpenAI(api_key=key, timeout=timeout, max_retries=max_retries))
        except Exception as exc:
            last_exc = exc
            if i + 1 < len(keys):
                log.warning("OpenAI key #%d failed (%s: %s); trying fallback key", i + 1, type(exc).__name__, str(exc)[:200])
    raise last_exc  # type: ignore[misc]
