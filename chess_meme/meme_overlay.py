"""
GIF overlay pipeline.

Provides two things:
  1. compute_move_timestamps() — converts renderer speed params into wall-clock
     seconds so we know when each move's delay phase starts.
  2. apply_meme_overlays()    — post-processes the rendered .mp4 to burn in
     the selected GIFs via ffmpeg overlay filters, optionally mixing in
     per-meme sound effects.
"""

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import chess


@dataclass
class OverlaySpec:
    gif_path: Path
    square: int        # chess square index (0-63)
    start_sec: float   # when to start showing the GIF
    duration_sec: float
    sound_path: Optional[Path] = field(default=None)  # .mp3 to play at start_sec


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


def _build_audio_filters(
    overlays: List[OverlaySpec],
    first_sound_input_idx: int,
    has_video_audio: bool,
) -> Tuple[List[str], List[str], str]:
    """
    Build ffmpeg input args and filter_complex fragments for sound overlays.

    Returns:
        extra_input_args  — ['-i', path, ...] for each sound file
        filter_parts      — filtergraph segments for adelay / amix
        audio_map         — '-map' target for the final mixed audio stream,
                            or "0:a?" if there are no sounds
    """
    sound_overlays = [ov for ov in overlays if ov.sound_path and ov.sound_path.exists()]
    if not sound_overlays:
        return [], [], "0:a?"

    extra_inputs: List[str] = []
    filter_parts: List[str] = []
    sound_tags: List[str] = []

    for i, ov in enumerate(sound_overlays):
        input_idx = first_sound_input_idx + i
        extra_inputs += ["-i", str(ov.sound_path)]

        delay_ms = int(ov.start_sec * 1000)
        tag = f"[sa{i}]"

        # Delay audio to start_sec, trim to gif duration, normalise timestamps
        filter_parts.append(
            f"[{input_idx}:a]"
            f"adelay={delay_ms}|{delay_ms},"
            f"atrim=duration={ov.start_sec + ov.duration_sec:.3f},"
            f"asetpts=PTS-STARTPTS"
            f"{tag}"
        )
        sound_tags.append(tag)

    # Mix original audio (if present) with all sound effects
    if has_video_audio:
        all_in = "[0:a]" + "".join(sound_tags)
        n = 1 + len(sound_tags)
    else:
        all_in = "".join(sound_tags)
        n = len(sound_tags)

    filter_parts.append(
        f"{all_in}amix=inputs={n}:normalize=0:dropout_transition=0[aout]"
    )

    return extra_inputs, filter_parts, "[aout]"


def apply_meme_overlays(
    input_video: Path,
    overlays: List[OverlaySpec],
    board_offset: Tuple[int, int],
    square_size: int,
    gif_scale: float = 1.0,
    output_path: Optional[Path] = None,
    has_audio: bool = True,
) -> Path:
    """
    Post-process *input_video* to burn in GIF overlays and write *output_path*.

    Sound effects (.mp3 in OverlaySpec.sound_path) are mixed into the output
    using ffmpeg adelay + amix.  If has_audio=False, the source has no audio
    track and only sound effects are placed (if any).

    If there are no overlays, input_video is returned unchanged.
    """
    if not overlays:
        return input_video

    if output_path is None:
        output_path = input_video.with_stem(input_video.stem + "_memes")

    video_extra, video_filters, last_video_tag, next_idx = _build_overlay_filters(
        overlays, board_offset, square_size, gif_scale, base_input_idx=1
    )

    audio_extra, audio_filters, audio_map = _build_audio_filters(
        overlays,
        first_sound_input_idx=next_idx,
        has_video_audio=has_audio,
    )

    all_filter_parts = video_filters + audio_filters

    cmd = ["ffmpeg", "-y", "-i", str(input_video)]
    cmd += video_extra
    cmd += audio_extra

    if all_filter_parts:
        cmd += ["-filter_complex", ";".join(all_filter_parts)]
        video_map = last_video_tag if video_filters else "0:v"
        cmd += ["-map", video_map]
        if audio_filters:
            cmd += ["-map", audio_map]
        elif has_audio:
            cmd += ["-map", "0:a?"]
    else:
        cmd += ["-map", "0:v"]
        if has_audio:
            cmd += ["-map", "0:a?"]

    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        str(output_path),
    ]

    subprocess.run(cmd, check=True)
    return output_path
