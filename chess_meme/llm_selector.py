"""
LLM meme selector.

The LLM does NOT analyse chess positions — it only receives a plain-language
description of what the engine already determined, then picks the best GIF from
a list of candidates.  The model returns JSON: {square, meme, reason}.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, TYPE_CHECKING

import chess

if TYPE_CHECKING:
    import anthropic
    from .event_classifier import ChessEvent

# Which GIFs are valid candidates for each event type.
# Files must exist in the memes directory (checked at call time).
MEME_CANDIDATES: dict[str, List[str]] = {
    "checkmate":  ["crying.gif", "facepalm.gif"],
    "check":      ["shock.gif", "surprise.gif"],
    "blunder":    ["facepalm.gif", "crying.gif"],
    "capture":    ["shock.gif", "surprise.gif"],
    "aggression": ["dog.gif", "shock.gif"],   # "пизда вам пацаны" energy
}

_DEFAULT_MODEL = "claude-haiku-4-5-20251001"

_SYSTEM = (
    "You are a creative assistant for a chess highlight video generator. "
    "You receive a description of what just happened on the board "
    "(already analysed by a chess engine — you are NOT playing chess). "
    "Pick the single most fitting meme GIF from the candidates list. "
    "Reply ONLY with valid JSON on one line: "
    '{"square": <0-63>, "meme": "<filename>", "reason": "<one sentence>"}. '
    "No extra text."
)


@dataclass
class MemeSelection:
    square: int   # chess square index (0-63), same as ChessEvent.square
    meme: str     # filename, e.g. "crying.gif"
    reason: str   # human-readable one-liner for debugging


def _describe_event(event: "ChessEvent") -> str:
    color = "White" if event.move_color == chess.WHITE else "Black"
    templates = {
        "checkmate":  f"{color} delivered checkmate with {event.san} on move {event.move_number}.",
        "check":      f"{color} gave check with {event.san} on move {event.move_number}.",
        "blunder":    (
            f"{color} blundered with {event.san} on move {event.move_number} "
            f"(win-probability dropped {event.eval_drop_wp:.0%})."
        ),
        "capture":    (
            f"{color} captured a piece worth {event.material_gain_cp} centipawns "
            f"with {event.san} on move {event.move_number}."
        ),
        "aggression": (
            f"{color} played the aggressive {event.san} on move {event.move_number}. "
            "Stockfish rates it as dubious, but it carries a devastating threat "
            "— if the opponent does nothing, it wins immediately."
        ),
    }
    return templates.get(event.event_type, f"{color} played {event.san} on move {event.move_number}.")


def select_meme(
    event: "ChessEvent",
    memes_dir: Path,
    llm_client: Optional["anthropic.Anthropic"],
    model: str = _DEFAULT_MODEL,
) -> Optional[MemeSelection]:
    """
    Ask the LLM to pick the best meme for this event.

    Returns None if no candidate GIFs exist on disk.
    If llm_client is None (no API key), falls back to the first candidate
    without calling the API. Also falls back if the LLM returns garbage.
    """
    candidates = [f for f in MEME_CANDIDATES.get(event.event_type, []) if (memes_dir / f).exists()]
    if not candidates:
        return None

    # Single candidate, or no LLM client → skip the API call entirely
    if len(candidates) == 1:
        return MemeSelection(square=event.square, meme=candidates[0], reason="Only candidate available.")
    if llm_client is None:
        return MemeSelection(square=event.square, meme=candidates[0], reason="No LLM client, using first candidate.")

    description = _describe_event(event)
    candidates_str = ", ".join(candidates)
    user_msg = f"{description}\n\nAvailable meme GIFs: [{candidates_str}]\n\nChoose the best one."

    response = llm_client.messages.create(
        model=model,
        max_tokens=128,
        system=_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )

    raw = response.content[0].text.strip()
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return MemeSelection(square=event.square, meme=candidates[0], reason="LLM parse failed, using fallback.")

    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return MemeSelection(square=event.square, meme=candidates[0], reason="LLM JSON invalid, using fallback.")

    chosen = data.get("meme", "")
    if chosen not in candidates:
        chosen = candidates[0]

    return MemeSelection(
        square=event.square,
        meme=chosen,
        reason=data.get("reason", ""),
    )
