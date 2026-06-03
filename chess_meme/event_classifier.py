"""
Rule-based event classifier.

Determines what happened on each move using board state + engine eval.
The LLM step is NOT involved here — all logic is deterministic.

Event priority (highest first): checkmate > check > blunder > aggression > capture

"aggression" — a move that looks bad by Stockfish (position quality drops)
but creates a huge threat if the opponent does nothing (null-move eval).
This captures the human emotional read of early queen attacks, sacrifices
that carry deadly threats, and similar "objectively risky but terrifying" plays.
"""

import math
from dataclasses import dataclass
from typing import List, Optional, Dict, Any

import chess

# centipawn value per piece type
_PIECE_CP: Dict[int, int] = {
    chess.PAWN: 100,
    chess.KNIGHT: 300,
    chess.BISHOP: 300,
    chess.ROOK: 500,
    chess.QUEEN: 900,
}


@dataclass
class ChessEvent:
    event_type: str        # "checkmate" | "check" | "blunder" | "aggression" | "capture"
    square: int            # move.to_square — where GIF lands
    san: str
    fen_before: str
    fen_after: str
    eval_drop_wp: float    # win-probability drop for the mover (0..1), positive = got worse
    material_gain_cp: int  # centipawns captured (0 if non-capture)
    move_color: chess.Color
    move_number: int
    ply: int


def _win_prob(cp: Optional[float]) -> float:
    """Lichess sigmoid formula: cp → win probability (0..1)."""
    try:
        return 1.0 / (1.0 + math.exp(-0.00368208 * float(cp or 0)))
    except (TypeError, OverflowError):
        return 0.5


def _pov_eval(eval_dict: Optional[Dict], color: chess.Color) -> Optional[Dict]:
    """Return {cp, mate} from the mover's point of view."""
    if not eval_dict:
        return None
    pvs = eval_dict.get("pvs", [])
    if not pvs:
        return None
    top = pvs[0]
    sign = 1 if color == chess.WHITE else -1
    if (m := top.get("mate")) is not None:
        return {"cp": None, "mate": int(m) * sign}
    cp = top.get("cp")
    return {"cp": int(cp) * sign if cp is not None else 0, "mate": None}


def _wp_from_pov(pov: Optional[Dict]) -> float:
    if not pov:
        return 0.5
    if (m := pov.get("mate")) is not None:
        return 1.0 if m > 0 else 0.0
    return _win_prob(pov.get("cp", 0))


def _material_gain(board: chess.Board, move: chess.Move) -> int:
    """Centipawns the mover gains from this move (0 for non-captures)."""
    if board.is_en_passant(move):
        return _PIECE_CP[chess.PAWN]
    captured = board.piece_at(move.to_square)
    return _PIECE_CP.get(captured.piece_type, 0) if captured else 0


def _null_move_fen(fen_after: str) -> Optional[str]:
    """
    Return the FEN after the opponent "passes" their turn (null move).

    Returns None if null move is illegal (e.g. the side to move is in check,
    which can happen right after a checking move — but check events are already
    handled by a higher-priority branch).
    """
    try:
        board = chess.Board(fen_after)
        if board.is_check():
            return None
        board.push(chess.Move.null())
        return board.fen()
    except Exception:
        return None


def classify_events(
    game_states: List[Dict[str, Any]],
    eval_map: Dict[str, Optional[Dict]],
    blunder_threshold: float = 0.20,
    capture_min_value_cp: int = 300,
    aggression_threat_wp: float = 0.80,
    aggression_quality_wp: float = 0.55,
) -> List[ChessEvent]:
    """
    Classify each move into exactly one event (or skip if unremarkable).

    Args:
        game_states:           [{ply, san, fen_before, fen_after}, ...]
        eval_map:              {fen_str -> {pvs: [{cp, mate}]}} from Stockfish / Lichess.
                               May optionally include null-move FENs pre-computed by the
                               caller to enable "aggression" detection.
        blunder_threshold:     win-probability drop at which we call a move a blunder
        capture_min_value_cp:  minimum piece value (cp) for a capture to be notable
        aggression_threat_wp:  null-move win-probability that qualifies as "huge threat"
        aggression_quality_wp: real win-probability ceiling — above this the move is
                               just good, not aggressively risky (no aggression tag)

    Returns:
        Sorted list of ChessEvent, one per notable move.

    "aggression" detection (null-move technique):
        After the move the position may look equal or bad for the attacker by
        Stockfish.  But if the opponent could be made to pass their turn, the
        attacker would be winning easily — meaning the move carries a devastating
        threat.  When eval_map contains the null-move FEN (see _null_move_fen),
        we compare:
            null_wp  = mover's win-prob if opponent passes
            real_wp  = mover's actual win-prob after opponent's best reply
        If null_wp >= aggression_threat_wp AND real_wp < aggression_quality_wp,
        the move is tagged "aggression".
    """
    events: List[ChessEvent] = []

    for state in game_states:
        board = chess.Board(state["fen_before"])
        try:
            move = board.parse_san(state["san"])
        except Exception:
            continue

        fen_before = state["fen_before"]
        fen_after = state["fen_after"]
        board_after = chess.Board(fen_after)

        # Win-probability from the mover's perspective
        ev_before = _pov_eval(eval_map.get(fen_before), board.turn)
        ev_after = _pov_eval(eval_map.get(fen_after), board.turn)
        wp_before = _wp_from_pov(ev_before)
        wp_after = _wp_from_pov(ev_after)
        eval_drop = wp_before - wp_after  # positive → position worsened for mover

        mat_gain = _material_gain(board, move)

        # Priority: checkmate > check > blunder > aggression > notable capture
        if board_after.is_checkmate():
            event_type = "checkmate"

        elif board_after.is_check():
            event_type = "check"

        elif eval_drop >= blunder_threshold:
            event_type = "blunder"

        else:
            # Check for "aggression": bad-looking move with a huge hidden threat.
            # Requires null-move FEN to be present in eval_map (pre-computed).
            nm_fen = _null_move_fen(fen_after)
            if nm_fen and nm_fen in eval_map:
                # Null-move eval is from opponent's turn, so flip perspective:
                # after null move it's the original mover's turn again.
                nm_eval = _pov_eval(eval_map.get(nm_fen), board.turn)
                null_wp = _wp_from_pov(nm_eval)
                if null_wp >= aggression_threat_wp and wp_after < aggression_quality_wp:
                    event_type = "aggression"
                elif board.is_capture(move) and mat_gain >= capture_min_value_cp:
                    event_type = "capture"
                else:
                    continue
            elif board.is_capture(move) and mat_gain >= capture_min_value_cp:
                event_type = "capture"
            else:
                continue

        events.append(ChessEvent(
            event_type=event_type,
            square=move.to_square,
            san=state["san"],
            fen_before=fen_before,
            fen_after=fen_after,
            eval_drop_wp=max(0.0, eval_drop),
            material_gain_cp=mat_gain,
            move_color=board.turn,
            move_number=board.fullmove_number,
            ply=state["ply"],
        ))

    return events
