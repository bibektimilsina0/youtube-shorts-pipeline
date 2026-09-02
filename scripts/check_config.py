#!/usr/bin/env python3
"""Print the resolved pipeline configuration without running anything."""

import os
import sys
from pathlib import Path

niche = sys.argv[1] if len(sys.argv) > 1 else "(default)"
provider = os.environ.get("IMAGE_PROVIDER") or "pollinations (default)"

print(f"  niche           {niche}")
print(f"  image provider  {provider}")
print(f"  text model      {os.environ.get('GEMINI_TEXT_MODEL') or 'gemini-3.6-flash (default)'}")
print(f"  yt privacy      {os.environ.get('YT_PRIVACY') or 'private (default)'}")

keys = ["GEMINI_API_KEY"]
if provider.startswith("cloudflare"):
    keys += ["CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"]
for k in keys:
    print(f"  {k:15} {'set' if os.environ.get(k) else '- MISSING'}")

token = Path.home() / ".verticals" / "youtube_token.json"
print(f"  youtube token   {'present' if token.exists() else '- MISSING (run: make oauth)'}")

rot = Path(__file__).resolve().parent.parent / "topics" / f"{niche}.txt"
print(f"  topic rotation  {'yes' if rot.exists() else 'no (uses trending discovery)'}")
