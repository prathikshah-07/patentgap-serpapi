"""File-based cache for SerpApi responses.

Keyed by SHA-256 of the normalized query string. 24-hour TTL.
Second run of the same query is instant, no SerpApi calls.
"""

import hashlib
import json
import time
from pathlib import Path
from typing import Any

CACHE_DIR = Path(".cache")
CACHE_DIR.mkdir(exist_ok=True)
TTL_SECONDS = 24 * 60 * 60  # 24 hours


def _key(namespace: str, query: str) -> str:
    normalized = f"{namespace}:{query.strip().lower()}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def get(namespace: str, query: str) -> Any | None:
    path = CACHE_DIR / f"{_key(namespace, query)}.json"
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > TTL_SECONDS:
        path.unlink(missing_ok=True)
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def set(namespace: str, query: str, value: Any) -> None:
    path = CACHE_DIR / f"{_key(namespace, query)}.json"
    path.write_text(json.dumps(value, default=str), encoding="utf-8")