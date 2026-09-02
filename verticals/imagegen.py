"""Image generation provider switch.

Three providers, selected with IMAGE_PROVIDER:

  cloudflare    Workers AI FLUX.1-schnell. Free account, no card required,
                10,000 neurons/day — about 100 images at 8 steps, versus the
                4 this pipeline needs per video. Best free quality, but the
                model has no width/height parameters: it returns a square
                image that callers crop to 9:16.
  pollinations  Free, keyless. No signup at all. Serves one model (sana) and
                caps output at roughly 590k pixels (576x1024 for a 9:16
                request), so frames are softer after upscaling.
  gemini        Google AI Studio. Best prompt adherence, but image generation
                is NOT on the free tier — a free-tier key returns 429
                RESOURCE_EXHAUSTED on every image model.

Default is pollinations because it needs no credentials. Set
IMAGE_PROVIDER=cloudflare once CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN
are configured. No provider is guaranteed, so callers keep their own fallback
(a solid-colour frame for b-roll) for when generation fails.
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
CLOUDFLARE_URL = (
    "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}"
)
CLOUDFLARE_MODEL = "@cf/black-forest-labs/flux-1-schnell"
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
    # The service scales any request down to ~590k pixels while keeping the
    # aspect ratio, so asking for 1080x1920 just wastes the budget on an
    # upscale later. Ask for the largest size it will actually return.
    budget = 590000
    ratio = height / width if width else 16 / 9
    req_w = int((budget / ratio) ** 0.5)
    req_h = int(req_w * ratio)

    params = {
        "width": req_w,
        "height": req_h,
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


def _generate_cloudflare(prompt: str, output_path: Path):
    """Generate an image via Cloudflare Workers AI (FLUX.1-schnell).

    The model takes no width/height: it returns a square image, which the
    caller crops to 9:16. `steps` is capped at 8 by the API.
    """
    # Checked before the retrying call: missing credentials are a config
    # error, and retrying them just burns 14 seconds to fail identically.
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    if not account or not token:
        raise RuntimeError(
            "CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN must both be set "
            "for IMAGE_PROVIDER=cloudflare (dash.cloudflare.com -> Workers & "
            "Pages -> Workers AI)"
        )
    _cloudflare_request(prompt, output_path, account, token)


@with_retry(max_retries=3, base_delay=2.0)
def _cloudflare_request(prompt: str, output_path: Path, account: str, token: str):
    """POST to Workers AI and write the returned image. Retried on failure."""
    # flux-1-schnell has no width/height parameters and returns a square, of
    # which the 9:16 crop keeps only the middle ~56% of the width. Asking for
    # a centred vertical composition keeps the subject inside that column.
    if os.environ.get("CLOUDFLARE_VERTICAL_HINT", "1") != "0":
        prompt = (
            f"{prompt}, vertical portrait composition, subject centered "
            "with empty space above and below"
        )

    model = os.environ.get("CLOUDFLARE_IMAGE_MODEL") or CLOUDFLARE_MODEL
    try:
        steps = int(os.environ.get("CLOUDFLARE_STEPS") or 8)
    except ValueError:
        steps = 8
    steps = max(1, min(steps, 8))  # API rejects steps > 8

    r = requests.post(
        CLOUDFLARE_URL.format(account=account, model=model),
        # Only prompt and steps are accepted. Cloudflare's docs also list a
        # `seed` parameter, but the live endpoint rejects it (and width/height)
        # with "Additional or unevaluated properties not allowed", so output is
        # not reproducible and the size is fixed.
        json={
            "prompt": prompt[:2048],  # documented max prompt length
            "steps": steps,
        },
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=180,
    )
    if r.status_code != 200:
        detail = r.text[:200]
        try:
            errs = r.json().get("errors") or []
            if errs:
                detail = "; ".join(str(e.get("message", e)) for e in errs)[:200]
        except Exception:
            pass
        hint = ""
        if r.status_code in (401, 403):
            hint = " — check CLOUDFLARE_API_TOKEN has the Workers AI:Read permission"
        elif r.status_code == 429:
            hint = (
                " — daily free neuron allowance exhausted; retry tomorrow or "
                "set IMAGE_PROVIDER=pollinations"
            )
        raise RuntimeError(f"Cloudflare AI {r.status_code}: {detail}{hint}")

    data = r.json()
    if not data.get("success", True):
        raise RuntimeError(f"Cloudflare AI returned success=false: {str(data)[:200]}")

    # flux-1-schnell answers {"result": {"image": "<base64>"}}; some Workers AI
    # image models stream raw bytes instead, so handle both.
    result = data.get("result") or {}
    b64 = result.get("image")
    if not b64:
        raise RuntimeError(f"No image in Cloudflare response: {str(data)[:200]}")
    output_path.write_bytes(base64.b64decode(b64))


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
    if provider == "cloudflare":
        _generate_cloudflare(prompt, output_path)
    elif provider == "pollinations":
        _generate_pollinations(prompt, output_path, width, height)
    elif provider == "gemini":
        _generate_gemini(prompt, output_path, instruction)
    else:
        raise RuntimeError(
            f"Unknown IMAGE_PROVIDER '{provider}' — expected "
            "'cloudflare', 'pollinations' or 'gemini'"
        )
