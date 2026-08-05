# Changelog

All notable changes to **hermes-endless-research** are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/), and this project
adheres to [Semantic Versioning](https://semver.org/) (with `v0.x` pre-1.0 semantics).

## [Unreleased]

## [0.1.0-beta] — 2026-08-05

### Added
- **Persistent disk-backed research brain** — a `.research/` directory (objective,
  state, frontier, criteria, claims, sources, search-log, dead-ends, contradictions,
  unresolved, checkpoints) that survives restarts, model changes, and context walls.
- **Deterministic CLI** (`research_project.py`): `init`, `status`, `resignal`, `reset`,
  `checkpoint`, `tick`, `verify_success`.
- **Six-state machine**: `CONTINUE`, `CHECKPOINT`, `BLOCKED`, `SUCCESS`, `EXHAUSTED`,
  and `DORMANT` (resumable; parks the research job until a watcher re-awakens it).
- **Atomic project lock** — `tick` holds an exclusive `flock` on `<proj>/.research/.lock`
  so two ticks can never mutate state simultaneously (exit code 2 = locked/skipped).
- **Deterministic SUCCESS gate** — `verify_success` blocks `resignal SUCCESS` unless
  every acceptance criterion in `criteria.jsonl` is met (or excepted) AND the evidence
  trail checks out (source IDs resolve, primary evidence present, corroboration met,
  no critical unresolved contradiction, substantive final-report).
- **Two-job cron model** — an aggressive research job (fires on
  CONTINUE/CHECKPOINT/BLOCKED, auto-pauses on DORMANT/SUCCESS/EXHAUSTED) plus a cheap
  daily dormant watcher that probes DORMANT campaigns for genuinely new evidence and
  re-awakens them.
- **Installer / uninstaller / verify-install** scripts for Hermes on Ubuntu/VPS.
- **Docker option** layering the skill over the official Hermes image with persistent
  `~/.hermes` and `/research` volumes.
- **Test suite** (pytest): state machine, locking, checkpoint, success gate.
- **CI**: test workflow; release workflow producing a tarball prerelease.
- **Templates**: `HERMES.md`, `objective.md`, `cron-prompt.md`; `.env.example`.

### Notes
- Experimental release. Source interpretation and anti-fabrication remain
  model-dependent; important conclusions need human review.
- Public repo stores code only — never personal research projects or credentials.
