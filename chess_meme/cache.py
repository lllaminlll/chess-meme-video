"""
FEN-keyed cache for LLM meme selections.

Keyed by (fen_before, event_type) so identical positions in different games
don't re-pay for an API call.  Stored as a flat JSON file.

In-memory dict (_CACHE) is loaded once on first access and kept in sync with
the on-disk file.  Reads are O(1) dict lookups; writes update memory first,
then flush to disk atomically.
"""

import json
import time
from pathlib import Path
from typing import Any, Optional

_CACHE_FILE = Path("llm_meme_cache.json")

# In-memory store.  None means "not loaded yet".
_CACHE: Optional[dict] = None


def _ensure_loaded() -> dict:
    """Load cache from disk if not already in memory."""
    global _CACHE
    if _CACHE is None:
        _CACHE = _load_from_disk()
    return _CACHE


def _load_from_disk() -> dict:
    if not _CACHE_FILE.exists():
        return {}
    try:
        data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save(cache: dict) -> None:
    try:
        tmp = _CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(_CACHE_FILE)
    except (IOError, OSError):
        pass


def _key(fen_before: str, event_type: str) -> str:
    return f"{fen_before}|{event_type}"


def get(fen_before: str, event_type: str) -> Optional[dict]:
    """Return cached payload or None.  Reads from in-memory dict (O(1))."""
    entry = _ensure_loaded().get(_key(fen_before, event_type))
    if entry is None:
        return None
    return entry.get("data")


def put(fen_before: str, event_type: str, payload: dict) -> None:
    """Persist payload under (fen_before, event_type).

    Updates in-memory dict immediately, then flushes to disk atomically.
    """
    cache = _ensure_loaded()
    cache[_key(fen_before, event_type)] = {"data": payload, "ts": time.time()}
    _save(cache)


def invalidate() -> None:
    """Drop the in-memory cache (forces reload from disk on next access)."""
    global _CACHE
    _CACHE = None
