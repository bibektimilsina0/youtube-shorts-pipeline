"""B-roll image generation (see imagegen.py for providers) + Ken Burns animation."""

from pathlib import Path

from PIL import Image

from .config import VIDEO_WIDTH, VIDEO_HEIGHT, broll_frames, run_cmd
from .imagegen import active_provider, generate_image
from .log import log


def _fallback_frame(i: int, out_dir: Path) -> Path:
    """Solid colour fallback frame if image generation fails."""
    colors = [(20, 20, 60), (40, 10, 40), (10, 30, 50)]
    img = Image.new("RGB", (VIDEO_WIDTH, VIDEO_HEIGHT), colors[i % len(colors)])
    path = out_dir / f"broll_{i}.png"
    img.save(path)
    return path


def generate_broll(prompts: list, out_dir: Path) -> list[Path]:
    """Generate 3 b-roll frames with the configured image provider.

    Falls back to a solid-colour frame per prompt that fails, so a provider
    outage degrades the video instead of failing the run.
    """
    provider = active_provider()
    n = broll_frames()
    frames = []

    for i, prompt in enumerate(prompts[:n]):
        out_path = out_dir / f"broll_{i}.png"
        log(f"Generating b-roll frame {i+1}/{min(n, len(prompts))} via {provider}...")

        try:
            generate_image(prompt, out_path, VIDEO_WIDTH, VIDEO_HEIGHT)

            # Resize/crop to 9:16 portrait
            img = Image.open(out_path).convert("RGB")
            target_w, target_h = VIDEO_WIDTH, VIDEO_HEIGHT
            orig_w, orig_h = img.size
            scale = max(target_w / orig_w, target_h / orig_h)
            new_w, new_h = int(orig_w * scale), int(orig_h * scale)
            img = img.resize((new_w, new_h), Image.LANCZOS)
            left = (new_w - target_w) // 2
            top = (new_h - target_h) // 2
            img = img.crop((left, top, left + target_w, top + target_h))
            img.save(out_path)
            frames.append(out_path)

        except Exception as e:
            log(f"Frame {i+1} failed: {e} — using fallback")
            frames.append(_fallback_frame(i, out_dir))

    return frames


# Ken Burns move vocabulary. The original three (zoom_in, pan_right,
# zoom_out) cycled with `i % 3`, which reads as mechanical once a video has
# more than a handful of frames. Diagonals and the remaining pan directions
# give a 9-move set, and ASSEMBLE picks from it pseudo-randomly.
KEN_BURNS_MOVES = (
    "zoom_in",
    "zoom_out",
    "pan_right",
    "pan_left",
    "pan_up",
    "pan_down",
    "zoom_in_pan_right",
    "zoom_in_pan_left",
    "zoom_out_pan_up",
)

# How far the frame travels. The original 1.12 (12%) is barely perceptible
# over a 4s hold; 22% reads as deliberate camera movement without turning
# into a lurch. Overshoot must exceed the zoom so a pan has room to travel.
KB_ZOOM = 1.22
KB_OVERSHOOT = 1.28


def animate_frame(img_path: Path, out_path: Path, duration: float, effect: str = "zoom_in"):
    """Ken Burns animation on a single frame.

    `effect` is one of KEN_BURNS_MOVES; anything unrecognised falls back to a
    centred zoom_in so a bad value degrades instead of raising.
    """
    fps = 30
    frames = max(1, int(duration * fps))
    w, h = VIDEO_WIDTH, VIDEO_HEIGHT

    # Scale up first so there is material to pan across, then zoompan crops
    # back to the target size.
    sw, sh = int(w * KB_OVERSHOOT), int(h * KB_OVERSHOOT)

    # Centre expressions, and the travel range available at a given zoom.
    cx = "iw/2-(iw/zoom/2)"
    cy = "ih/2-(ih/zoom/2)"
    z_in = f"{KB_ZOOM}-{KB_ZOOM - 1:.4f}*on/{frames}"   # start wide, close in
    z_out = f"1.0+{KB_ZOOM - 1:.4f}*on/{frames}"        # start tight, pull out
    z_fix = f"{KB_ZOOM}"

    # Pan travel: at a fixed zoom the visible window is iw/zoom wide, so the
    # maximum offset is iw-iw/zoom. Using that exactly would hit the frame
    # edge, so travel 90% of it.
    span_x = f"(iw-iw/zoom)*0.9"
    span_y = f"(ih-ih/zoom)*0.9"
    prog = f"on/{frames}"

    if effect == "zoom_out":
        z, x, y = z_out, cx, cy
    elif effect == "pan_right":
        z, x, y = z_fix, f"{span_x}*{prog}", cy
    elif effect == "pan_left":
        z, x, y = z_fix, f"{span_x}*(1-{prog})", cy
    elif effect == "pan_up":
        z, x, y = z_fix, cx, f"{span_y}*(1-{prog})"
    elif effect == "pan_down":
        z, x, y = z_fix, cx, f"{span_y}*{prog}"
    elif effect == "zoom_in_pan_right":
        z, x, y = z_in, f"{span_x}*{prog}", cy
    elif effect == "zoom_in_pan_left":
        z, x, y = z_in, f"{span_x}*(1-{prog})", cy
    elif effect == "zoom_out_pan_up":
        z, x, y = z_out, cx, f"{span_y}*(1-{prog})"
    else:  # zoom_in and any unknown value
        z, x, y = z_in, cx, cy

    vf = (
        f"scale={sw}:{sh},"
        f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={w}x{h}:fps={fps}"
    )

    run_cmd([
        "ffmpeg", "-loop", "1", "-i", str(img_path),
        "-vf", vf, "-t", str(duration), "-r", str(fps),
        "-pix_fmt", "yuv420p", str(out_path), "-y", "-loglevel", "quiet",
    ])
