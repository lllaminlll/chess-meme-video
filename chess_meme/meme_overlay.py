"""
GIF overlay pipeline.

Provides two things:
  1. compute_move_timestamps() — converts renderer speed params into wall-clock
     seconds so we know when each move's delay phase starts.
  2. apply_meme_overlays()    — post-processes the rendered .mp4 to burn in
     the selected GIFs via ffmpeg overlay filters.
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import chess


@dataclass
class OverlaySpec:
    gif_path: Path
    square: int       # chess square index (0-63)
    start_sec: float  # when to start showing the GIF
    duration_sec: float


def compute_move_timestamps(
    moves_san: List[str],
    script_map: Dict[Tuple[int, str], dict],
    base_anim: float = 0.4,
    base_delay: float = 0.8,
    fps: int = 30,
    speed: float = 1.0,
    trailer_duration: float = 0.0,
) -> List[float]:
    """
    Return a list of timestamps (seconds) at which the *delay phase* for
    each move starts.  This is when the position is settled and a GIF overlay
    makes the most sense visually.

    Index i corresponds to moves_san[i].
    """
    timestamps: List[float] = []
    t = trailer_duration
    for i, san in enumerate(moves_san):
        eff = script_map.get((i, san), {})
        anim = float(eff.get("anim", base_anim)) * speed
        delay = float(eff.get("delay", base_delay)) * speed
        # GIF starts after the animation completes
        timestamps.append(t + anim)
        t += anim + delay
    return timestamps


def _square_bbox(
    square: int,
    board_offset: Tuple[int, int],
    square_size: int,
    scale: float = 1.0,
) -> Tuple[int, int, int, int]:
    """Return (x, y, w, h) in video pixels for a square's bounding box."""
    file_idx = chess.square_file(square)
    rank_idx = 7 - chess.square_rank(square)
    x0 = board_offset[0] + file_idx * square_size
    y0 = board_offset[1] + rank_idx * square_size
    size = int(square_size * scale)
    pad = (square_size - size) // 2
    return x0 + pad, y0 + pad, size, size


def _build_overlay_filters(
    overlays: List[OverlaySpec],
    board_offset: Tuple[int, int],
    square_size: int,
    gif_scale: float = 1.0,
    base_input_idx: int = 1,
) -> Tuple[List[str], List[str], str, int]:
    """
    Build the ffmpeg -filter_complex fragment for all GIF overlays.

    Returns:
        extra_input_args  — list of '-stream_loop -1 -i <path>' tokens
        filter_parts      — list of filtergraph segments
        last_video_tag    — tag name of the final composited stream
        next_input_idx    — index to use for inputs after these GIFs
    """
    extra_inputs: List[str] = []
    filter_parts: List[str] = []
    last_tag = "[0:v]"
    idx = base_input_idx

    for ov in overlays:
        x, y, w, h = _square_bbox(ov.square, board_offset, square_size, gif_scale)
        w -= w & 1  # ensure even dimensions
        h -= h & 1

        tag_scaled = f"[gif{idx}s]"
        tag_timed = f"[gif{idx}t]"
        tag_out = f"[v{idx}]"

        extra_inputs += ["-stream_loop", "-1", "-i", str(ov.gif_path)]

        # Scale GIF to square size
        filter_parts.append(f"[{idx}:v]scale={w}:{h}:flags=lanczos,format=rgba{tag_scaled}")
        # Trim to desired duration and reset timestamps
        filter_parts.append(
            f"{tag_scaled}trim=duration={ov.duration_sec:.3f},setpts=PTS-STARTPTS{tag_timed}"
        )
        # Overlay: visible only during [start, start+duration]
        enable_expr = f"between(t,{ov.start_sec:.3f},{ov.start_sec + ov.duration_sec:.3f})"
        filter_parts.append(
            f"{last_tag}{tag_timed}overlay={x}:{y}:enable='{enable_expr}'{tag_out}"
        )

        last_tag = tag_out
        idx += 1

    return extra_inputs, filter_parts, last_tag, idx


def apply_meme_overlays(
    input_video: Path,
    overlays: List[OverlaySpec],
    board_offset: Tuple[int, int],
    square_size: int,
    gif_scale: float = 1.0,
    output_path: Optional[Path] = None,
) -> Path:
    """
    Post-process *input_video* to burn in GIF overlays and write *output_path*.

    If there are no overlays, input_video is returned unchanged.
    """
    if not overlays:
        return input_video

    if output_path is None:
        output_path = input_video.with_stem(input_video.stem + "_memes")

    extra_inputs, filter_parts, last_tag, _ = _build_overlay_filters(
        overlays, board_offset, square_size, gif_scale, base_input_idx=1
    )

    cmd = ["ffmpeg", "-y", "-i", str(input_video)]
    cmd += extra_inputs

    if filter_parts:
        cmd += ["-filter_complex", ";".join(filter_parts)]
        cmd += ["-map", last_tag, "-map", "0:a?"]
    else:
        cmd += ["-map", "0:v", "-map", "0:a?"]

    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        str(output_path),
    ]

    subprocess.run(cmd, check=True)
    return output_path
