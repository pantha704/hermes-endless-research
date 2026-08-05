# Security Policy

## Scope

This repository ships a **skills/CLI extension** for [Hermes Agent](https://hermes-agent.nousresearch.com).
It persists research state in project `.research/` directories. It does **not** itself
store secrets, but the workflows it drives (web research, model calls, cron) use the
credentials of the **host Hermes installation**.

## What this project stores and where

| Data | Location | Public? |
|------|----------|---------|
| Skill code | `~/.hermes/skills/research/endless-research/` (or `/opt/data/skills/...` in Docker) | This repo is the source; installing copies it locally |
| Research state / brain | `<project>/.research/` | **Local only — never commit to this repo** |
| API keys / tokens | host Hermes `.env` / config | **Never here** |

## Security posture

- **Never commit real configuration.** API keys, provider credentials, Telegram/Discord
  tokens, `~/.hermes/config.yaml`, live cron databases, personal research projects,
  campaign logs, and user-specific delivery destinations **must not** be committed.
- **This is an experimental package.** The source-interpretation and anti-fabrication
  guarantees are model-dependent. Review important conclusions and verify the primary
  sources yourself before acting on a research verdict.
- **SUCCESS is gated** (`verify_success`) to reduce premature/overconfident success —
  but the gate verifies that *recorded evidence references resolve*, not that a source
  actually asserts a claim. Treat the gate as a quality tripwire, not proof.
- **Cron jobs** you create run with the permissions of the host. Give scrapers/web tools
  only the access they need; consider running research in an isolated container/user.

## Reporting a vulnerability

For security issues in **this repository**, open a [private advisory](https://github.com/pantha704/hermes-endless-research/security/advisories)
or email the maintainers via the GitHub account. Do **not** open a public issue for
credential leaks or config exposure.

For vulnerabilities in **Hermes Agent itself**, follow the guidance in the
[Hermes Agent security policy](https://github.com/NousResearch/hermes-agent/security).

## Supported versions

Only the latest tagged release is supported. This is `v0.1.0-beta` — expect breaking
changes as it matures.
