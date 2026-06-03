"""
FEN-keyed cache for LLM meme selections.

Keyed by (fen_before, event_type) so identical positions in different games
don't re-pay for an API call.  Stored as a flat JSON file.
"""

import json
import time
from pathlib import Path
from typing import Any, Optional

_CACHE_FILE = Path("llm_meme_cache.json")


def _load() -> dict:
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
    """Return cached payload or None."""
    return _load().get(_key(fen_before, event_type))


def put(fen_before: str, event_type: str, payload: dict) -> None:
    """Persist payload under (fen_before, event_type)."""
    cache = _load()
    cache[_key(fen_before, event_type)] = {"data": payload, "ts": time.time()}
    _save(cache)
