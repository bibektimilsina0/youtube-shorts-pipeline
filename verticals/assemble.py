"""ffmpeg video assembly — frames + voiceover + music + captions."""

import random
from pathlib import Path

from .broll import KEN_BURNS_MOVES, animate_frame
from .config import MEDIA_DIR, run_cmd
from .log import log


def _ffmpeg_has_libass() -> bool:
    """Check whether this ffmpeg build ships the `ass` filter (libass).

    Some builds (e.g. minimal/static ones) omit libass; burning captions in
    would fail with `No such filter: 'ass'`, so we skip burn-in instead.
    """
    try:
        r = run_cmd(["ffmpeg", "-hide_banner", "-filters"], capture=True)
        return any(line.split()[1:2] == ["ass"] for line in r.stdout.splitlines())
    except Exception:
        return False


def get_audio_duration(path: Path) -> float:
    """Get duration of an audio file in seconds."""
    r = run_cmd(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture=True,
    )
    return float(r.stdout.strip())



# ─────────────────────────────────────────────────────────────────────
# Shot planning
# ─────────────────────────────────────────────────────────────────────
# Crossfade length between shots. Long enough to read as a dissolve, short
# enough not to blur the subject; each transition also shortens the video by
# this much, which the plan accounts for.
XFADE_SECONDS = 0.45

# The reveal and the closing shot get held longer than the middle of the
# montage — an even split reads as metronomic.
FIRST_SHOT_WEIGHT = 1.35
LAST_SHOT_WEIGHT = 1.25


def _parse_srt_ends(srt_path: Path) -> list[float]:
    """End timestamps (seconds) of each caption group in an SRT.

    These are the natural places to change shot: a cut mid-phrase reads as a
    mistake, a cut as a phrase lands reads as editing. Returns [] if the file
    is missing or unparsable, so callers fall back to an even split.
    """
    if not srt_path or not Path(srt_path).exists():
        return []
    ends = []
    try:
        for line in Path(srt_path).read_text(encoding="utf-8").splitlines():
            if "-->" not in line:
                continue
            tail = line.split("-->")[1].strip().replace(",", ".")
            h, m, rest = tail.split(":")
            ends.append(int(h) * 3600 + int(m) * 60 + float(rest))
    except Exception as e:
        log(f"Could not parse caption timings ({e}) — using even shot lengths")
        return []
    return sorted(set(ends))


def _plan_shots(n: int, total: float, srt_path: Path | None) -> list[float]:
    """Choose a duration for each of `n` shots covering `total` seconds.

    Snaps shot boundaries to caption ends where the timings allow it, and
    weights the first and last shot longer. Always returns exactly n positive
    durations summing to `total`.
    """
    if n <= 0:
        return []
    if n == 1:
        return [total]

    # Target boundaries from the weighted split, before any snapping.
    weights = [1.0] * n
    weights[0] = FIRST_SHOT_WEIGHT
    weights[-1] = LAST_SHOT_WEIGHT
    scale = total / sum(weights)
    targets, acc = [], 0.0
    for w in weights[:-1]:
        acc += w * scale
        targets.append(acc)

    # Snap each boundary to the nearest caption end, keeping boundaries
    # strictly increasing and leaving room for the shots on either side.
    ends = [e for e in _parse_srt_ends(srt_path) if 0 < e < total]
    min_shot = max(1.2, XFADE_SECONDS * 2)
    if ends:
        snapped, prev = [], 0.0
        for i, t in enumerate(targets):
            remaining_shots = n - i - 1
            latest = total - remaining_shots * min_shot
            usable = [e for e in ends if prev + min_shot <= e <= latest]
            if usable:
                best = min(usable, key=lambda e: abs(e - t))
                # Only snap when the caption boundary is genuinely nearby;
                # dragging a shot far off its target hurts pacing more than
                # a mid-phrase cut does.
                snapped.append(best if abs(best - t) <= 1.5 else min(max(t, prev + min_shot), latest))
            else:
                snapped.append(min(max(t, prev + min_shot), latest))
            prev = snapped[-1]
        targets = snapped

    bounds = [0.0] + targets + [total]
    return [max(min_shot * 0.5, bounds[i + 1] - bounds[i]) for i in range(n)]


def _pick_moves(n: int, job_id: str) -> list[str]:
    """A move per shot, varied but deterministic for a given job.

    Seeded by job_id so a re-run of the same job reproduces the same edit,
    while different videos get different motion. Avoids repeating a move
    back-to-back, which is what made the old `i % 3` cycle obvious.
    """
    rng = random.Random(f"kenburns-{job_id}")
    moves, prev = [], None
    for i in range(n):
        # Open on a slow push in and close on a pull out — a conventional,
        # readable shape for a montage.
        if i == 0:
            pick = "zoom_in"
        elif i == n - 1:
            pick = "zoom_out"
        else:
            options = [m for m in KEN_BURNS_MOVES if m != prev]
            pick = rng.choice(options)
        moves.append(pick)
        prev = pick
    return moves


def assemble_video(
    frames: list[Path],
    voiceover: Path,
    out_dir: Path,
    job_id: str,
    lang: str = "en",
    ass_path: str | None = None,
    music_path: str | None = None,
    duck_filter: str | None = None,
) -> Path:
    """Assemble final video from frames, voiceover, captions, and music."""
    log("Assembling video...")
    duration = get_audio_duration(voiceover)

    # Plan the edit: how long each shot runs, and how the camera moves in it.
    # Shots are padded by the crossfade length because each xfade consumes
    # XFADE_SECONDS of overlap — without the padding the montage would finish
    # short of the voiceover and the last shot would be cut off.
    srt_for_timing = Path(str(ass_path).replace(".ass", ".srt")) if ass_path else None
    n = len(frames)
    n_fades = max(0, n - 1)
    montage_target = duration + n_fades * XFADE_SECONDS
    shot_lengths = _plan_shots(n, montage_target, srt_for_timing)
    moves = _pick_moves(n, job_id)

    log(
        f"Edit plan: {n} shots over {duration:.1f}s, "
        f"{n_fades} crossfade{'s' if n_fades != 1 else ''} "
        f"({min(shot_lengths):.1f}-{max(shot_lengths):.1f}s per shot)"
    )

    # Animate each frame with its planned move and length.
    animated = []
    for i, frame in enumerate(frames):
        anim = out_dir / f"anim_{i}.mp4"
        animate_frame(frame, anim, shot_lengths[i], moves[i])
        animated.append(anim)

    merged_video = out_dir / "merged_video.mp4"
    if n_fades == 0:
        merged_video = animated[0]
    else:
        # Crossfade the shots together in one filter_complex. The concat
        # demuxer this replaces produced hard cuts between every shot.
        cmd = ["ffmpeg"]
        for a in animated:
            cmd += ["-i", str(a)]

        # xfade takes two inputs at a time, so chain them: each step fades
        # the running result into the next shot. `offset` is measured on the
        # running result's timeline, which shortens by XFADE_SECONDS at every
        # step — hence subtracting the fades already applied.
        parts, prev, elapsed = [], "0:v", 0.0
        for i in range(1, n):
            elapsed += shot_lengths[i - 1]
            offset = elapsed - XFADE_SECONDS * i
            label = f"v{i}"
            parts.append(
                f"[{prev}][{i}:v]xfade=transition=fade:"
                f"duration={XFADE_SECONDS}:offset={max(0.0, offset):.3f}[{label}]"
            )
            prev = label

        run_cmd(cmd + [
            "-filter_complex", ";".join(parts),
            "-map", f"[{prev}]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-pix_fmt", "yuv420p",
            str(merged_video), "-y", "-loglevel", "error",
        ])

    # Build the final ffmpeg command with optional captions + music
    out_path = MEDIA_DIR / f"verticals_{job_id}_{lang}.mp4"

    # Determine video filter (captions via ASS)
    vf_parts = []
    if ass_path and Path(ass_path).exists():
        if _ffmpeg_has_libass():
            # Escape special chars in path for ffmpeg filter
            escaped_ass = str(ass_path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
            vf_parts.append(f"ass={escaped_ass}")
        else:
            log(
                "WARNING: this ffmpeg build has no libass — captions will NOT "
                "be burned in. The SRT is still uploaded to YouTube. Install "
                "an ffmpeg with libass (brew/apt builds include it) for "
                "burned-in captions."
            )
    vf = ",".join(vf_parts) if vf_parts else None

    if music_path and Path(music_path).exists():
        # Three inputs: video, voiceover, music
        cmd = ["ffmpeg", "-i", str(merged_video), "-i", str(voiceover)]

        # Loop music to match video duration, apply ducking
        music_filter = f"[2:a]aloop=loop=-1:size=2e+09,atrim=0:{duration}"
        if duck_filter:
            music_filter += f",{duck_filter}"
        music_filter += "[music]"

        # Mix voiceover + ducked music
        audio_filter = f"{music_filter};[1:a][music]amix=inputs=2:duration=first:dropout_transition=2[aout]"

        cmd += [
            "-stream_loop", "-1", "-i", str(music_path),
            "-filter_complex", audio_filter,
        ]

        if vf:
            cmd += ["-vf", vf]

        cmd += [
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest",
            str(out_path), "-y", "-loglevel", "error",
        ]
    else:
        # Two inputs: video + voiceover (no music)
        cmd = ["ffmpeg", "-i", str(merged_video), "-i", str(voiceover)]

        if vf:
            cmd += ["-vf", vf]

        cmd += [
            # Stream-copy when there is no caption burn-in: the xfade pass
            # already encoded at crf 20, and a second encode would only lose
            # quality. With captions the re-encode is unavoidable.
            "-c:v", "libx264" if vf else "copy",
        ]
        if vf:
            cmd += ["-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p"]
        cmd += [
            "-c:a", "aac", "-shortest",
            str(out_path), "-y", "-loglevel", "error",
        ]

    run_cmd(cmd)
    log(f"Video assembled: {out_path}")
    return out_path
