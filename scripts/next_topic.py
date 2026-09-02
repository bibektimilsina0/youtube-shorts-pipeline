#!/usr/bin/env python3
"""Pick the next topic from a niche's rotating list.

Prints one topic to stdout so a caller can do:

    TOPIC=$(python scripts/next_topic.py crafted_wild_nepal)

Position is stored in topics/.state/<niche>.pos. Committing that file back is
what makes the rotation advance across GitHub Actions runs — a runner's
filesystem is discarded when the job ends.

Exits 2 (with a message on stderr) when the niche has no list, so a caller can
fall back to --discover:

    TOPIC=$(python scripts/next_topic.py "$NICHE") || TOPIC=""
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOPICS_DIR = ROOT / "topics"
STATE_DIR = TOPICS_DIR / ".state"


def load_topics(niche: str) -> list[str]:
    path = TOPICS_DIR / f"{niche}.txt"
    if not path.exists():
        return []
    topics = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            topics.append(line)
    return topics


def read_pos(niche: str) -> int:
    f = STATE_DIR / f"{niche}.pos"
    try:
        return int(f.read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0


def write_pos(niche: str, pos: int):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"{niche}.pos").write_text(f"{pos}\n")


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: next_topic.py <niche> [--peek]", file=sys.stderr)
        return 2

    niche = sys.argv[1]
    peek = "--peek" in sys.argv[2:]

    topics = load_topics(niche)
    if not topics:
        print(
            f"no topic list at topics/{niche}.txt — caller should fall back to --discover",
            file=sys.stderr,
        )
        return 2

    pos = read_pos(niche) % len(topics)
    print(topics[pos])
    if not peek:
        write_pos(niche, (pos + 1) % len(topics))
    return 0


if __name__ == "__main__":
    sys.exit(main())
