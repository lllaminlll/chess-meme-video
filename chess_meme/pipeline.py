"""
Top-level meme pipeline.

Usage:
    from chess_meme import build_overlay_specs, apply_meme_overlays
    import anthropic

    client = anthropic.Anthropic()
    specs = build_overlay_specs(game_states, eval_map, move_timestamps, memes_dir, client)
    output = apply_meme_overlays(video_path, specs, board_offset, square_size)
"""

from pathlib import Path
from typing import Dict, List, Optional, Any, TYPE_CHECKING

from .event_classifier import classify_events, ChessEvent
from .llm_selector import select_meme, MemeSelection
from .meme_overlay import OverlaySpec
from . import cache as _cache

if TYPE_CHECKING:
    import anthropic


def build_overlay_specs(
    game_states: List[Dict[str, Any]],
    eval_map: Dict[str, Optional[Dict]],
    move_timestamps: List[float],
    memes_dir: Path,
    llm_client: "anthropic.Anthropic",
    blunder_threshold: float = 0.20,
    gif_duration: float = 2.0,
) -> List[OverlaySpec]:
    """
    Classify events, select memes (with FEN-keyed LLM cache), return overlay specs.

    Args:
        game_states:      [{ply, san, fen_before, fen_after}, ...]
        eval_map:         {fen -> {pvs: [{cp, mate}]}}
        move_timestamps:  seconds at which each move's delay phase starts
                          (index i → game_states[i])
        memes_dir:        directory containing *.gif files
        llm_client:       anthropic.Anthropic() instance
        blunder_threshold: win-probability drop to call a move a blunder
        gif_duration:     how long each GIF plays on screen (seconds)
    """
    events = classify_events(
        game_states, eval_map, blunder_threshold=blunder_threshold
    )

    specs: List[OverlaySpec] = []

    for event in events:
        # Check LLM cache before hitting the API
        cached = _cache.get(event.fen_before, event.event_type)
        if cached:
            d = cached["data"]
            selection = MemeSelection(square=d["square"], meme=d["meme"], reason=d["reason"])
        else:
            selection = select_meme(event, memes_dir, llm_client)
            if selection:
                _cache.put(event.fen_before, event.event_type, {
                    "square": selection.square,
                    "meme": selection.meme,
                    "reason": selection.reason,
                })

        if selection is None:
            continue

        gif_path = memes_dir / selection.meme
        if not gif_path.exists():
            continue

        if event.ply >= len(move_timestamps):
            continue

        specs.append(OverlaySpec(
            gif_path=gif_path,
            square=selection.square,
            start_sec=move_timestamps[event.ply],
            duration_sec=gif_duration,
        ))

    return specs
