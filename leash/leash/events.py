"""In-process event bus so the web UI can animate what the engine does.
Thread-safe; the sponsor worker thread and request handlers both publish."""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

_lock = threading.Lock()
_seq = 0
_buf: deque[dict[str, Any]] = deque(maxlen=2000)


def publish(customer_id: str | None, type: str, **payload: Any) -> dict[str, Any]:
    global _seq
    with _lock:
        _seq += 1
        ev = {"seq": _seq, "ts": time.time(), "customer_id": customer_id, "type": type, **payload}
        _buf.append(ev)
        return ev


def since(customer_id: str | None, seq: int = 0, limit: int = 200) -> list[dict[str, Any]]:
    with _lock:
        rows = [e for e in _buf if e["seq"] > seq and (customer_id is None or e["customer_id"] in (customer_id, None))]
    return rows[-limit:]


def latest_seq() -> int:
    return _seq
