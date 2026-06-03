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

from .event_classifier import classify_events, ChessEvent, _null_move_fen
from .llm_selector import select_meme, MemeSelection
from .meme_overlay import OverlaySpec
from . import cache as _cache

if TYPE_CHECKING:
    import anthropic


def _find_sound(gif_path: Path) -> Optional[Path]:
    """
    Look for a .mp3 with the same stem next to the GIF.
    E.g.  assets/memes/crying.gif  →  assets/memes/crying.mp3
    Returns None if the file does not exist.
    """
    mp3 = gif_path.with_suffix(".mp3")
    return mp3 if mp3.exists() else None


def build_overlay_specs(
    game_states: List[Dict[str, Any]],
    eval_map: Dict[str, Optional[Dict]],
    move_timestamps: List[float],
    memes_dir: Path,
    llm_client: Optional["anthropic.Anthropic"] = None,
    blunder_threshold: float = 0.20,
    gif_duration: float = 2.0,
    include_sounds: bool = True,
) -> List[OverlaySpec]:
    """
    Classify events, select memes (with FEN-keyed LLM cache), return overlay specs.

    Args:
        game_states:      [{ply, san, fen_before, fen_after}, ...]
        eval_map:         {fen -> {pvs: [{cp, mate}]}}
                          May include null-move FENs for aggression detection:
                          call build_null_move_eval_map() to extend eval_map first.
        move_timestamps:  seconds at which each move's delay phase starts
                          (index i → game_states[i])
        memes_dir:        directory containing *.gif (and optionally *.mp3) files
        llm_client:       anthropic.Anthropic() instance (optional)
        blunder_threshold: win-probability drop to call a move a blunder
        gif_duration:     how long each GIF plays on screen (seconds)
        include_sounds:   if True, attach matching .mp3 alongside each GIF (if found)
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

        sound_path = _find_sound(gif_path) if include_sounds else None

        specs.append(OverlaySpec(
            gif_path=gif_path,
            square=selection.square,
            start_sec=move_timestamps[event.ply],
            duration_sec=gif_duration,
            sound_path=sound_path,
        ))

    return specs


def build_null_move_eval_map(
    game_states: List[Dict[str, Any]],
    engine_path: str,
    existing_eval_map: Optional[Dict] = None,
    depth: int = 12,
) -> Dict:
    """
    Extend eval_map with null-move FEN evaluations so the classifier can
    detect "aggression" events.

    For each move in game_states, computes the FEN that results from the
    opponent passing their turn, then evaluates it with Stockfish if it is
    not already in existing_eval_map.

    Returns a dict that merges existing_eval_map with new null-move entries.
    Requires Stockfish to be available at engine_path.
    """
    import chess
    import chess.engine

    result = dict(existing_eval_map or {})

    # Collect null-move FENs that need evaluation
    nm_fens: List[str] = []
    for state in game_states:
        nm = _null_move_fen(state["fen_after"])
        if nm and nm not in result:
            nm_fens.append(nm)

    if not nm_fens or not engine_path:
        return result

    try:
        with chess.engine.SimpleEngine.popen_uci(engine_path) as eng:
            for fen in nm_fens:
                board = chess.Board(fen)
                info = eng.analyse(board, chess.engine.Limit(depth=depth), multipv=1)
                if isinstance(info, list):
                    info = info[0]
                score = info["score"].white()
                if score.is_mate():
                    pvs = [{"mate": score.mate()}]
                else:
                    pvs = [{"cp": score.score(mate_score=32000)}]
                result[fen] = {"pvs": pvs}
    except Exception:
        pass  # engine unavailable — aggression detection silently skipped

    return result
