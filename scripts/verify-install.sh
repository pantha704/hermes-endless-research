#!/usr/bin/env bash
# =============================================================================
# hermes-endless-research — verify-install
#
#   Confirms the installation is sound:
#     1. skill directory present with SKILL.md / references / scripts
#     2. script is syntactically valid python
#     3. CLI responds (version / help)
#     4. a HEADLESS self-test: init -> tick -> checkpoint -> state.json sanity
#
#   USAGE
#     ./scripts/verify-install.sh [--check-vs-skill <path>]
#     # --check-vs-skill: also warn if local repo skill differs from installed
#
#   Exit codes: 0 = all checks passed; 1 = a check failed; 2 = prereq missing
# =============================================================================
set -euo pipefail

SKILL_DEST="${HERMES_HOME:-${HOME}/.hermes}/skills/research/endless-research"
BIN="${PREFIX:-${HOME}/.local}/bin/endless-research"
SCRIPT="$SKILL_DEST/scripts/research_project.py"

fail=0

command -v python3 >/dev/null 2>&1 || { echo "[FAIL] python3 not found"; exit 2; }
[ -d "$SKILL_DEST" ]            || { echo "[FAIL] skill dir missing: $SKILL_DEST";            fail=1; }
[ -f "$SKILL_DEST/SKILL.md" ]   || { echo "[FAIL] SKILL.md missing";                          fail=1; }
[ -f "$SKILL_DEST/references/protocol.md" ] || { echo "[FAIL] references/protocol.md missing"; fail=1; }
[ -f "$SCRIPT" ]                || { echo "[FAIL] research_project.py missing";               fail=1; }

# syntax check
python3 -c "import ast,sys; ast.parse(open('$SCRIPT',encoding='utf-8').read())" \
  || { echo "[FAIL] research_project.py is not valid python"; fail=1; }

# CLI responds
if [ "$fail" -eq 0 ]; then
  if ! python3 "$SCRIPT" --help >/dev/null 2>&1; then
    echo "[FAIL] CLI --help failed"; fail=1
  else
    echo "[OK] CLI responds."
  fi
fi

# full headless self-test
if [ "$fail" -eq 0 ]; then
  TMP_DIR="$(mktemp -d)"
  if python3 "$SCRIPT" init "$TMP_DIR" --objective "verify-install self-test" >/dev/null 2>&1 \
     && python3 "$SCRIPT" tick "$TMP_DIR" --cmd "echo noop" >/dev/null 2>&1; then
    state_file="$TMP_DIR/.research/state.json"
    if [ -f "$state_file" ] \
       && grep -q '"rounds_completed": 1' "$state_file" \
       && [ -d "$TMP_DIR/.research/checkpoints" ] \
       && [ "$(ls -1 "$TMP_DIR/.research/checkpoints" 2>/dev/null | wc -l)" -ge 1 ]; then
      echo "[OK] headless self-test: init + tick + checkpoint + state.json all good."
    else
      echo "[FAIL] self-test: state.json or checkpoints not as expected"; fail=1
    fi
  else
    echo "[FAIL] self-test: init/tick failed"; fail=1
  fi
  rm -rf "$TMP_DIR"
fi

# optional diff vs repo copy
if [ "${1:-}" = "--check-vs-skill" ] && [ "$fail" -eq 0 ]; then
  REPO="$2"
  if [ -f "$REPO/skill/scripts/research_project.py" ] \
     && ! diff -q "$SCRIPT" "$REPO/skill/scripts/research_project.py" >/dev/null 2>&1; then
    echo "[WARN] installed research_project.py differs from the repo copy." \
         "Re-run install.sh to sync."; fail=1
  fi
fi

if [ "$fail" -eq 0 ]; then
  echo ""
  echo "All checks passed. endless-research is installed and operational."
  echo "Usage: $(basename "$BIN") init|status|tick|verify_success ...  (or via $SCRIPT)"
  exit 0
else
  echo ""
  echo "One or more checks FAILED. Re-run ./scripts/install.sh"
  exit 1
fi
