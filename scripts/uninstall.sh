#!/usr/bin/env bash
# =============================================================================
# hermes-endless-research — uninstaller
#
#   Removes ONLY what install.sh created:
#     - the skill directory  ~/.hermes/skills/research/endless-research
#     - the CLI launcher      ~/.local/bin/endless-research
#
#   It NEVER touches:
#     - your research projects / ~/research/**
#     - ~/.hermes/config.yaml, .env, the cron database, or any other skill
#   Any cron jobs you created for campaigns must be removed by you (they are
#   intentionally left alone).
#
#   USAGE
#     ./scripts/uninstall.sh            # interactive confirmation
#     ./scripts/uninstall.sh --yes      # skip confirmation
# =============================================================================
set -euo pipefail

AUTO_YES="${1:-}"
if [ "$AUTO_YES" != "--yes" ]; then
  echo "This removes the endless-research skill + its CLI launcher."
  echo "It does NOT touch your research projects or cron jobs."
  read -r -p "Proceed? [y/N] " REPLY
  if [ "${REPLY:-N}" != "y" ] && [ "${REPLY:-N}" != "Y" ]; then
    echo "Aborted."
    exit 0
  fi
fi

SKILL_DEST="${HERMES_HOME:-${HOME}/.hermes}/skills/research/endless-research"
BIN_DEST="${PREFIX:-${HOME}/.local}/bin/endless-research"

removed=""
[ -d "$SKILL_DEST" ] && rm -rf "$SKILL_DEST" && removed="$removed skill"
[ -f "$BIN_DEST" ] && rm -f "$BIN_DEST" && removed="$removed launcher"

if [ -n "$removed" ]; then
  echo "Removed:$removed"
else
  echo "Nothing to remove (skill and launcher not found)."
fi
echo "Uninstall complete. Any campaign cron jobs you created remain — remove them with:"
echo "  hermes cron list   # then: hermes cron remove <job-id>"
