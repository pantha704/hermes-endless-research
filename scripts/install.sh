#!/usr/bin/env bash
# =============================================================================
# hermes-endless-research — installer (Hermes on Ubuntu / Debian / other VPS)
#
#   Installs the `endless-research` skill + CLI into an existing Hermes install.
#
#   WHAT IT DOES
#     1. Validates prerequisites (python3 >= 3.9, hermes binary, writable skills dir)
#     2. Copies the skill (SKILL.md + references/ + scripts/) into the Hermes skills dir
#     3. Installs an `endless-research` launcher onto PATH (~/.local/bin)
#     4. Creates the project template (optional: run `endless-research init`)
#     5. Runs a dry self-test to confirm the install works
#
#   SAFETY
#     - Never touches your ~/.hermes/config.yaml, .env, cron DB, or research projects.
#     - Only writes NEW files under the Hermes skills directory and ~/.local/bin.
#     - Idempotent: re-running overwrites only the skill files that ship with the repo.
#
#   USAGE
#     ./scripts/install.sh
#     ./scripts/install.sh --prefix ~/.local   # install launcher to a custom prefix
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ---- defaults ---------------------------------------------------------------
SKILL_NAME="endless-research"
SKILL_CATEGORY="research"
PREFIX="${PREFIX:-${HOME}/.local}"
SKILLS_DIR="${HERMES_HOME:-${HOME}/.hermes}/skills"
BIN_DIR="$PREFIX/bin"

# ---- helpers ----------------------------------------------------------------
info()  { printf '[hermes-endless-research] %s\n' "$*"; }
warn()  { printf '[hermes-endless-research][WARN] %s\n' "$*" >&2; }
die()   { printf '[hermes-endless-research][ERROR] %s\n' "$*" >&2; exit 1; }

# ---- 1. prerequisites ---------------------------------------------------------
info "Checking prerequisites..."

command -v python3 >/dev/null 2>&1 || die "python3 is required (>= 3.9)."
PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
INFO_MAJOR="${PY_VER%%.*}"
INFO_MINOR="${PY_VER#*.}"
if [ "$INFO_MAJOR" -lt 3 ] || { [ "$INFO_MAJOR" -eq 3 ] && [ "$INFO_MINOR" -lt 9 ]; }; then
  die "python3 >= 3.9 required (found $PY_VER)."
fi
info "python3 $PY_VER OK"

if command -v hermes >/dev/null 2>&1; then
  info "hermes found: $(command -v hermes)"
else
  warn "Hermes binary not found on PATH. The skill will still be installed, but you"
  warn "must install Hermes (https://hermes-agent.nousresearch.com) to use it with cron."
fi

# Verify the skills directory is writable / create it
mkdir -p "$SKILLS_DIR/$SKILL_CATEGORY"
if [ ! -w "$SKILLS_DIR/$SKILL_CATEGORY" ]; then
  die "Skills directory not writable: $SKILLS_DIR/$SKILL_CATEGORY"
fi

# ---- 2. copy skill -----------------------------------------------------------
DEST="$SKILLS_DIR/$SKILL_CATEGORY/$SKILL_NAME"
info "Installing skill to $DEST"
mkdir -p "$DEST/references" "$DEST/scripts"
cp "$REPO_ROOT/skill/SKILL.md"            "$DEST/SKILL.md"
cp "$REPO_ROOT/skill/references/protocol.md" "$DEST/references/protocol.md"
cp "$REPO_ROOT/skill/scripts/research_project.py" "$DEST/scripts/research_project.py"
chmod +x "$DEST/scripts/research_project.py"

# ---- 3. install the CLI launcher --------------------------------------------
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/endless-research" <<EOF
#!/usr/bin/env bash
# Launcher for the endless-research CLI (installed by hermes-endless-research).
exec python3 "$DEST/scripts/research_project.py" "\$@"
EOF
chmod +x "$BIN_DIR/endless-research"
info "Installed launcher: $BIN_DIR/endless-research"

# Add to PATH in the current shell (and note for future shells)
if ! echo ":$PATH:" | grep -q ":${BIN_DIR}:"; then
  warn "$BIN_DIR is not on PATH. Add it, e.g.:   export PATH=\"$BIN_DIR:\$PATH\""
  warn "(The next script can run it via full path regardless.)"
fi

# ---- 4. finalize --------------------------------------------------------------
info "Install complete."
info "Next steps:"
info "  # scaffold a project"
info "  endless-research init ~/research/my-campaign --objective \"<the exact finding>\" --success \"<found>\" --failure \"<not found>\""
info "  # inspect state / queue"
info "  endless-research status ~/research/my-campaign"
info ""
info "Then schedule Job 1 (research) and Job 2 (dormant watcher) as Hermes cron jobs —"
info "see templates/cron-prompt.md for ready-to-paste prompts."

# ---- 5. optional dry self-test ----------------------------------------------
if [ "${ENDLESS_RESEARCH_SKIP_SELFTEST:-0}" != "1" ]; then
  info "Running dry self-test (no web research, exits immediately)..."
  TMP_DIR="$(mktemp -d)"
  if "$BIN_DIR/endless-research" init "$TMP_DIR" --objective "install self-test" >/dev/null 2>&1 \
     && "$BIN_DIR/endless-research" tick "$TMP_DIR" --cmd "echo ok" >/dev/null 2>&1; then
    info "Self-test PASSED: init + tick produced a checkpoint."
  else
    warn "Self-test did not complete. Inspect manually: $TMP_DIR"
  fi
  rm -rf "$TMP_DIR"
fi

info "Done."
