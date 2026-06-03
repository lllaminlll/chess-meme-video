#!/usr/bin/env python3
"""
Example: render a chess game video and burn in LLM-selected meme GIFs.

Steps:
  1. Parse PGN → list of SAN moves + game states + FENs
  2. Analyse positions with Stockfish (or Lichess Cloud Eval)
  3. Classify events (check, checkmate, blunder, capture)
  4. Ask LLM to pick the right meme for each event (cached by FEN)
  5. Render the base video via the existing Renderer
  6. Post-process with ffmpeg to burn in GIF overlays

Run:
  python chess_meme_example.py game.pgn \
      --stockfish /usr/bin/stockfish \
      --memes ./assets/memes \
      --out ./output
"""

import argparse
import json
import os
import shutil
from pathlib import Path

import chess
import chess.pgn

try:
    import anthropic
except ImportError:
    anthropic = None

from chess_meme import build_overlay_specs, apply_meme_overlays, compute_move_timestamps


def parse_pgn(pgn_path: Path):
    """Return (moves_san, game_states, initial_fen)."""
    with open(pgn_path) as f:
        game = chess.pgn.read_game(f)

    board = game.board()
    initial_fen = board.fen()
    moves_san: list[str] = []
    game_states: list[dict] = []

    for ply, move in enumerate(game.mainline_moves()):
        fen_before = board.fen()
        san = board.san(move)
        board.push(move)
        fen_after = board.fen()

        moves_san.append(san)
        game_states.append({
            "ply": ply,
            "san": san,
            "fen_before": fen_before,
            "fen_after": fen_after,
        })

    return moves_san, game_states, initial_fen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pgn", help="Path to PGN file")
    ap.add_argument("--stockfish", default="stockfish")
    ap.add_argument("--memes", default="assets/memes")
    ap.add_argument("--out", default="output")
    ap.add_argument("--theme", default="green")
    ap.add_argument("--aspect", default="16:9")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--gif-duration", type=float, default=2.0)
    args = ap.parse_args()

    memes_dir = Path(args.memes)
    output_dir = Path(args.out)
    output_dir.mkdir(parents=True, exist_ok=True)

    print("Parsing PGN...")
    moves_san, game_states, initial_fen = parse_pgn(Path(args.pgn))

    # Import the existing Renderer and analysis functions
    from chess_video_gui import (
        Renderer,
        get_eval_for_fen_batch_local,
        annotate_moves,
    )

    # Stockfish опционален: без него шах/мат/взятие всё равно ловятся по
    # правилам доски, пропадают только зевки (blunder) и шкала оценки.
    stockfish = shutil.which(args.stockfish) or (args.stockfish if Path(args.stockfish).exists() else None)
    if stockfish:
        print("Analysing positions with Stockfish...")
        all_fens = list({s["fen_before"] for s in game_states} | {s["fen_after"] for s in game_states})
        eval_map = get_eval_for_fen_batch_local(all_fens, stockfish)
    else:
        print(f"Stockfish не найден ('{args.stockfish}') — анализ пропущен (шах/мат/взятие ловятся без него).")
        eval_map = {}

    print("Annotating moves...")
    annotated = annotate_moves(game_states, eval_map, engine_path=stockfish or "")
    script_map = {(i, s["san"]): ann for i, (s, ann) in enumerate(zip(game_states, annotated))}

    print("Rendering base video...")
    renderer = Renderer(
        theme=args.theme,
        aspect=args.aspect,
        fps=args.fps,
        output_dir=output_dir,
        eval_map=eval_map,
    )
    base_video = renderer.render_game_to_pipe(
        moves_san=moves_san,
        filename_base="game_base",
        display_title="Chess Game",
        fen=initial_fen,
        script={"moves": [{"ply": i, "san": s, "effects": script_map.get((i, s), {})}
                          for i, s in enumerate(moves_san)]},
        trailer_info=None,
    )

    if base_video is None:
        print("Rendering failed.")
        return

    print("Computing move timestamps...")
    timestamps = compute_move_timestamps(
        moves_san=moves_san,
        script_map=script_map,
        base_anim=renderer.base_anim_duration,
        base_delay=renderer.base_delay_duration,
        fps=args.fps,
    )

    # LLM опционален: без ключа берётся первый подходящий мем-кандидат.
    if anthropic is not None and os.environ.get("ANTHROPIC_API_KEY"):
        print("Selecting memes via LLM...")
        client = anthropic.Anthropic()
    else:
        print("ANTHROPIC_API_KEY не задан — мем выбирается по умолчанию (первый кандидат).")
        client = None
    specs = build_overlay_specs(
        game_states=game_states,
        eval_map=eval_map,
        move_timestamps=timestamps,
        memes_dir=memes_dir,
        llm_client=client,
        gif_duration=args.gif_duration,
    )
    print(f"  {len(specs)} meme overlay(s) selected.")

    if specs:
        print("Burning in GIF overlays...")
        final_video = apply_meme_overlays(
            input_video=base_video,
            overlays=specs,
            board_offset=renderer.board_offset,
            square_size=renderer.square_size,
            output_path=output_dir / "game_with_memes.mp4",
        )
        print(f"Done: {final_video}")
    else:
        print(f"No overlays to add. Base video: {base_video}")


if __name__ == "__main__":
    main()
