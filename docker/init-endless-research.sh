#!/bin/sh
# =============================================================================
# s6-overlay container-init hook for hermes-endless-research.
# Runs once at container start. Idempotent: copies the skill into the persistent
# /opt/data/skills tree only if it is not already present, so a mounted volume
# wins. Runs as root before services drop to the `hermes` user.
# =============================================================================
set -eu

SRC=/usr/local/share/hermes-endless-research/skill
DEST=/opt/data/skills/research/endless-research

# Already installed (e.g. a mounted /opt/data/skills from the host) -> nothing to do.
if [ -e "$DEST/SKILL.md" ]; then
  exit 0
fi

mkdir -p "$(dirname "$DEST")"
cp -r "$SRC" "$DEST"

# Make sure the `hermes` user (the runtime user) can read/run the skill.
if [ -d /opt/data/skills ] && command -v chown >/dev/null 2>&1; then
  chown -R hermes:hermes /opt/data/skills/research/endless-research 2>/dev/null || true
fi

exit 0
