# Convenience wrappers around `python -m verticals`.
#
# Each target sources .env and uses the project venv, so you don't have to
# repeat `set -a && source .env && set +a` and `.venv/bin/python` every time.
#
#   make run                              # next topic from the rotation
#   make run TOPIC="Snow leopard"         # explicit topic
#   make run NICHE=nepali_satire          # other niche
#   make draft TOPIC="..."                # script only, no video
#   make discover                         # trending topics for the niche
#   make topics                           # peek at the next rotation topic
#   make setup                            # venv + dependencies
#   make oauth                            # YouTube sign-in
#   make clean                            # remove generated media/drafts

PY      := .venv/bin/python
NICHE   ?= crafted_wild_nepal
TOPIC   ?=
VOICE   ?= edge
LLM     ?= gemini

# Every recipe runs in one shell so `source .env` reaches the python call.
.ONESHELL:
SHELL := /bin/bash
# Recipes print their own output; don't echo the shell lines too.
.SILENT:

# Load .env if present. `set -a` exports everything it defines.
define load_env
	set -a
	[ -f .env ] && source .env
	set +a
endef

.PHONY: help run draft discover topics setup oauth clean check

help:
	@grep -E '^#   ' $(MAKEFILE_LIST) | sed 's/^#   /  /'

# Full pipeline: draft -> produce -> upload.
# With no TOPIC, pulls the next entry from topics/$(NICHE).txt; if that niche
# has no list, falls back to trending discovery.
run:
	$(load_env)
	if [ -n "$(TOPIC)" ]; then
	  T="$(TOPIC)"
	elif T=$$($(PY) scripts/next_topic.py "$(NICHE)" 2>/dev/null); then
	  echo "  Topic from rotation: $$T"
	else
	  T=""
	fi
	if [ -n "$$T" ]; then
	  $(PY) -m verticals run --topic "$$T" --niche "$(NICHE)" \
	    --provider $(LLM) --voice $(VOICE) --platform shorts
	else
	  echo "  No topic list for $(NICHE) — using trending discovery"
	  $(PY) -m verticals run --discover --auto-pick --niche "$(NICHE)" \
	    --provider $(LLM) --voice $(VOICE) --platform shorts
	fi

# Script + metadata only. Fast, and spends no image quota.
draft:
	$(load_env)
	if [ -z "$(TOPIC)" ]; then echo "usage: make draft TOPIC=\"your topic\""; exit 2; fi
	$(PY) -m verticals draft --topic "$(TOPIC)" --niche "$(NICHE)" --provider $(LLM) --platform shorts

# What the niche's discovery sources are surfacing right now.
discover:
	$(load_env)
	$(PY) -m verticals topics --niche "$(NICHE)" --limit 15

# Next topic in the rotation, without consuming it.
topics:
	@$(PY) scripts/next_topic.py "$(NICHE)" --peek 2>/dev/null \
	  || echo "  no rotation list for $(NICHE) (uses trending discovery)"

# Print the resolved configuration without running anything.
check:
	$(load_env)
	$(PY) scripts/check_config.py "$(NICHE)"

setup:
	uv venv --python 3.12 2>/dev/null || python3 -m venv .venv
	uv pip install --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.txt \
	  || .venv/bin/pip install --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.txt

oauth:
	$(PY) scripts/setup_youtube_oauth.py

clean:
	rm -rf ~/.verticals/media/* ~/.verticals/drafts/*
	@echo "  cleared generated media and drafts"
