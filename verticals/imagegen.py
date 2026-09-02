"""Image generation provider switch.

Two providers, selected with IMAGE_PROVIDER:

  pollinations  Free, keyless HTTP endpoint (Flux). No signup, no quota.
                Ignores the requested width/height and returns 576x1024;
                callers rescale, so only sharpness suffers.
  gemini        Google AI Studio. Better quality and prompt adherence, but
                image generation is NOT on the free tier — a free-tier key
                returns 429 RESOURCE_EXHAUSTED on every image model.

Default is pollinations: it works with no billing set up. Neither provider is
guaranteed, so callers keep their own fallback (a solid-colour frame for
b-roll) for when generation fails.
"""

import base64
import os
import urllib.parse
from pathlib import Path

import requests

from .config import get_gemini_key
from .log import log
from .retry import with_retry

DEFAULT_PROVIDER = "pollinations"

POLLINATIONS_URL = "https://image.pollinations.ai/prompt/"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def active_provider() -> str:
    """Resolve the configured provider name (lowercased, default applied)."""
    return (os.environ.get("IMAGE_PROVIDER") or DEFAULT_PROVIDER).strip().lower()


@with_retry(max_retries=3, base_delay=2.0)
def _generate_pollinations(prompt: str, output_path: Path, width: int, height: int):
    """Generate an image via Pollinations. No API key required."""
    # Prompt goes in the path, so it must be fully percent-encoded — an
    # unescaped "/" or "?" would otherwise truncate or corrupt the request.
    url = POLLINATIONS_URL + urllib.parse.quote(prompt, safe="")
    params = {
        "width": width,
        "height": height,
        "nologo": "true",
        # Vary the seed so three prompts in one run never collide on a cached
        # image; the service keys its cache on prompt+seed.
        "seed": int.from_bytes(os.urandom(2), "big"),
    }
    model = os.environ.get("POLLINATIONS_MODEL")
    if model:
        params["model"] = model

    r = requests.get(url, params=params, timeout=180)
    if r.status_code != 200:
        raise RuntimeError(f"Pollinations HTTP {r.status_code}: {r.text[:200]}")
    if not r.content:
        raise RuntimeError("Pollinations returned an empty body")
    # The endpoint answers 200 with an HTML error page when overloaded, so
    # check the payload is actually an image before writing it.
    ctype = r.headers.get("Content-Type", "")
    if not ctype.startswith("image/"):
        raise RuntimeError(f"Pollinations returned {ctype or 'unknown type'}, not an image")
    output_path.write_bytes(r.content)


@with_retry(max_retries=3, base_delay=2.0)
def _generate_gemini(prompt: str, output_path: Path, instruction: str):
    """Generate an image via Gemini. Requires a billing-enabled key."""
    api_key = get_gemini_key()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")

    model = os.environ.get("GEMINI_IMAGE_MODEL") or "gemini-3.1-flash-image"
    body = {
        "contents": [{"parts": [{"text": f"{instruction}: {prompt}"}]}],
        "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]},
    }
    r = requests.post(
        GEMINI_URL.format(model=model), json=body, timeout=90,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
    )
    if r.status_code != 200:
        try:
            detail = r.json().get("error", {}).get("message", r.text[:200])
        except Exception:
            detail = r.text[:200]
        hint = ""
        if r.status_code == 403:
            hint = (
                " — check that GEMINI_API_KEY is set in this environment and is "
                "an AI Studio key (https://aistudio.google.com/apikey), not a "
                "Vertex AI / service-account credential"
            )
        elif r.status_code == 429:
            hint = (
                " — image generation is not included in the Gemini free tier. "
                "Enable billing, or set IMAGE_PROVIDER=pollinations to use the "
                "free keyless provider."
            )
        raise RuntimeError(f"Gemini API {r.status_code}: {detail}{hint}")

    data = r.json()
    for part in data.get("candidates", [{}])[0].get("content", {}).get("parts", []):
        if "inlineData" in part:
            output_path.write_bytes(base64.b64decode(part["inlineData"]["data"]))
            return
    raise RuntimeError("No image in Gemini response")


def generate_image(
    prompt: str,
    output_path: Path,
    width: int,
    height: int,
    instruction: str = "Generate an image",
):
    """Generate one image with the configured provider.

    Raises on failure — callers decide whether to fall back.
    """
    provider = active_provider()
    if provider == "pollinations":
        _generate_pollinations(prompt, output_path, width, height)
    elif provider == "gemini":
        _generate_gemini(prompt, output_path, instruction)
    else:
        raise RuntimeError(
            f"Unknown IMAGE_PROVIDER '{provider}' — expected 'pollinations' or 'gemini'"
        )
