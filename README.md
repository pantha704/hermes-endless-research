<div align="center">

# Hermes Endless Research

**Experimental persistent autonomous research campaigns with checkpointing, evidence tracking and verification gates.**

A "keep digging until it's found" engine for [Hermes Agent](https://hermes-agent.nousresearch.com).
The brain lives on disk in each project's `.research/` directory, so research survives
context limits, model changes, failures, restarts, and exhausted agent turns. No single
session runs forever — **the campaign does.**

`v0.2.0` — experimental. Not production-verified. (Explicit evidence graph, URL
intelligence + scope, and an objective clarifier added in 0.2.0.)

[Install](#-install) · [Quick start](#-quick-start) · [How it works](#-how-it-works) · [Deployment](#-deployment) · [Security](#-security) · [License](#-license)

</div>

---

## What this is

Ask for something and the system **keeps digging** — persisting its entire research state
to disk, resuming the highest-value unfinished clue on every run, checkpointing, and
continuing on a schedule until a **verified, gated SUCCESS** or an honest **EXHAUSTED**.

Built around three layers:

| Layer | Role |
|-------|------|
| **Skill** (`skill/SKILL.md`) | the protocol brain — states, loop, verification rules |
| **CLI** (`skill/scripts/research_project.py`) | the machine — priority sort, state transitions, atomic locking, checkpoints, SUCCESS gate |
| **Cron** (via Hermes) | the heartbeat — durable re-invocation across restarts, no chat needed |

## ⚠️ Important caveats (read first)

- **Experimental (`v0.1.0-beta`).** Expect changes; not yet validated across many campaigns.
- **Model-dependent honesty.** Source interpretation and anti-fabrication are enforced by
  prompt + evidence trail, not by code. **Review important conclusions and verify primary
  sources yourself.**
- **The SUCCESS gate verifies evidence *references resolve* — it cannot prove a source
  asserts a claim.** It is a quality tripwire, not proof.
- **Define `criteria.jsonl` precisely.** Weak criteria can produce misleading success.

---

## 📦 Install

**Prerequisite:** a working [Hermes Agent](https://hermes-agent.nousresearch.com) install
on **Ubuntu / Debian / VPS** (this release documents that single path; Python ≥ 3.9).

```bash
git clone https://github.com/pantha704/hermes-endless-research.git
cd hermes-endless-research
./scripts/install.sh
```

The installer:
1. validates prerequisites (python ≥ 3.9, hermes binary, writable skills dir)
2. copies the skill into `~/.hermes/skills/research/endless-research/`
3. installs an `endless-research` launcher onto `~/.local/bin`
4. runs a **dry self-test** (init + tick, no web research)

Verify:

```bash
./scripts/verify-install.sh
```

Uninstall (removes only the skill + launcher — never your projects or cron jobs):

```bash
./scripts/uninstall.sh --yes
```

---

## 🚀 Quick start

**1. Scaffold a campaign.**

```bash
endless-research init ~/research/my-campaign \
  --objective "Find the original source of X" \
  --success "Original primary document retrieved and verified" \
  --failure "Only secondary/undated references available"
```

**2. Define acceptance criteria** in `~/.research/my-campaign/.research/criteria.jsonl`.
The SUCCESS gate refuses to mark the campaign SUCCESS until these are met and the
evidence trail passes.

**3. Check state / queue.**

```bash
endless-research status ~/research/my-campaign
```

**4. Schedule the two cron jobs** (see the **separate** ready-to-paste prompts,
`templates/research-cron-prompt.md` and `templates/dormant-watcher-prompt.md` — do NOT
combine both into one job). Each cron fire starts a **fresh** Hermes session that loads
the campaign from disk, runs one bounded burst, and checkpoints — the campaign, not the
session, is persistent.

Attach the **worker-lease gate** to the research job (so only one worker runs per
campaign; it also skips the agent entirely when dormant/finished):

```bash
# install the pre-run gate (once)
cp scripts/campaign-lease-gate.py ~/.hermes/scripts/campaign-lease-gate.py

# Job 1 (research) — research-only prompt + lease-gate pre-run script
hermes cron add --every 2h --name "my-campaign research" \
  --workdir "$HOME/research/my-campaign" \
  --script "campaign-lease-gate.py" \
  --skills endless-research --prompt "$(cat templates/research-cron-prompt.md)"

# Job 2 (dormant watcher) — watcher-only prompt
hermes cron add --every 12h --name "my-campaign dormant-watcher" \
  --workdir "$HOME/research/my-campaign" \
  --skills endless-research --prompt "$(cat templates/dormant-watcher-prompt.md)"
```

The lease gate emits `{"wakeAgent": false}` (no model tokens) when another live worker
holds the lease or the campaign is DORMANT/SUCCESS/EXHAUSTED. The research agent RELEASEs
its lease at the end of each tick.

See the **full example project** in `examples/demo-project/`.

---

## 🧠 How it works

### The external brain

```
<project>/
  HERMES.md                   # sticky resume protocol (auto-loaded by cron)
  .research/
    objective.md              # exact finding + success/failure + acceptance criteria
    state.json                # current_state, next_action, blockers, rounds_completed
    frontier.jsonl            # clue queue, priority-scored (0-100)
    criteria.jsonl            # acceptance criteria the SUCCESS gate verifies
    claims.jsonl              # verified claims, each linked to sources
    sources.jsonl             # every source record (url, title, type, accessed)
    search-log.jsonl          # every query + outcome (anti-loop)
    dead-ends.jsonl           # failed branches + reopen conditions
    contradictions.jsonl      # conflicting evidence
    unresolved.md             # open questions, ranked
    checkpoints/              # snapshot per round
    final-report.md           # the SUCCESS deliverable
```

### The six states

**`CONTINUE`** · **`CHECKPOINT`** · **`BLOCKED`** · **`DORMANT`** (resumable; parks the
research job until the watcher re-awakens it) · **`SUCCESS`** (terminal, gated) ·
**`EXHAUSTED`** (terminal, *not* success).

**The honesty rule:** *"I searched a lot" is never SUCCESS.*

### The explicit evidence graph (v0.2.0)

Research is modelled as a **graph, not a linear list**. Sources, claims, clues,
questions, people, dead-ends and contradictions are nodes; `edges.jsonl` holds typed
relationships between them. This tells you *where something came from, what it supports,
what contradicts it, and how things connect* — not just what was found.

```bash
# Add a typed, validated edge (referential integrity is enforced):
endless-research edge ~/research/my-campaign SRC-001 cites SRC-002 --context "listed as original report"
endless-research edge ~/research/my-campaign SRC-001 supports CLM-001
endless-research edge ~/research/my-campaign SRC-002 contradicts CLM-001
endless-research edge ~/research/my-campaign CLUE-001 derived_from SRC-002

# View the graph:
endless-research graph ~/research/my-campaign
```

Relationships: `links_to`, `cites`, `authored_by`, `published_by`, `supports`,
`contradicts`, `answers`, `depends_on`, `derived_from`, `investigates`, `duplicate_of`,
`archived_version_of`, `blocks`. The `edge` command rejects dangling references and
domain violations.

### URL intelligence & scope (no blind crawling)

The engine is a targeted researcher, not a crawler. Per campaign `scope.json` controls
budgets (relevance + page budget, not a strict max-depth).

```bash
# See canonical URL + fingerprint + scope BEFORE fetching:
endless-research inspect ~/research/my-campaign "https://www.site.com/article?utm_source=x"
```

### Objective clarifier (short & smart)

Turns a URL + a vague goal into a measurable research contract without over-interrupting:

```bash
endless-research clarify ~/research/my-campaign "https://site.com/ai-agent" \
  --goal "understand how it works and whether its claims are credible"
# A clear goal compiles the contract immediately.

endless-research clarify ~/research/my-campaign "https://site.com/ai-agent" \
  --goal "learn everything important"
# A vague goal infers sensible defaults, records assumptions, and starts.

endless-research clarify ~/research/my-campaign "https://site.com/ai-agent"
# A materially ambiguous goal asks only the essential questions.
```

### The SUCCESS gate

`resignal <dir> SUCCESS` is automatically **blocked** unless `verify_success` confirms:
1. every criterion in `criteria.jsonl` is `met` or explicitly excepted
2. every evidence source ID resolves to `sources.jsonl`
3. `primary_hard` criteria have a primary source (or exception)
4. `corroboration_required` criteria have ≥2 independent sources (or exception)
5. no critical contradiction is unresolved
6. `final-report.md` is substantive

`--force` bypasses the gate explicitly.

### Concurrency

Two layers stop two ticks from overlapping: the **native Hermes cron in-flight guard**
(cron/scheduler.py) *and* an **atomic flock project lock** (`tick`). A second tick on a
locked project exits code 2 (skip, not fail).

---

## 🐳 Docker (optional)

Layer the skill over the official `hermes-agent` image (built from
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)). Two
persistent volumes: Hermes data and research projects.

```bash
# ensure the base image exists (build from the hermes-agent repo)
docker compose -f docker/docker-compose.yml build
HERMES_UID=$(id -u) HERMES_GID=$(id -g) \
  docker compose -f docker/docker-compose.yml up -d
```

Edit `docker/docker-compose.yml` to match your provider credentials (via env, not
committed values). The official entrypoint (`/init`, s6-overlay) is preserved; a
container-init hook installs the skill into the persistent volume on first start.

---

## 🧪 Testing & CI

```bash
pip install pytest
pytest tests/            # evidence graph, URL intelligence, clarifier,
                         # state machine, locking, checkpoint, success gate
```

CI runs the suite (currently **38 tests**) across Python 3.9/3.11/3.12 and shell-checks
the installer on every push/PR, and builds a tarball prerelease on version tags.

---

## 🔒 Security

- **Public repo stores code only.** Never commit personal research projects, live cron
  databases, `~/.hermes/config.yaml`, .env files, tokens, or campaign logs.
- Each user's `.research/` brain stays **on their machine or private storage**.
- See [SECURITY.md](SECURITY.md). Report issues via the security advisory flow.

**Deployment guidance**

| Deployment | Recommendation |
|---|---|
| Local computer | Good |
| User-owned VPS | **Best** |
| Docker on VPS | Best + easiest |
| GitHub self-hosted runner | Good |
| GitHub-hosted Actions | Optional, less reliable (6h limit; 60-day auto-disable on inactive public repos; disabled on forks by default) |

Real continuous campaigns should run on a machine/VPS/persistent Docker you own — not
inside the public repository or GitHub-hosted Actions.

---

## 📄 License

[MIT](LICENSE). This project extends [Hermes Agent](https://github.com/NousResearch/hermes-agent)
(Nous Research, MIT). This is an **independent community extension** — it is not created
by, affiliated with, or endorsed by Nous Research. Use the Hermes agent name and docs
only for accurate attribution, not to imply endorsement.

---

**Maintainer:** pantha704. An independent community extension built on Hermes Agent.
