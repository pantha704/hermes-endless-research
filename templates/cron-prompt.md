# Cron campaign prompts — hermes-endless-research

Two cron jobs run a campaign: an aggressive research digger and a cheap dormant
watcher. Each prompt is self-contained (cron sessions have no chat context and
`skip_memory=True`), so all state comes from the on-disk `.research/` directory.

Replace placeholders:
- `<PROJECT>` — absolute path to the research project, e.g. `~/research/my-campaign`
- `<RESEARCH_JOB_ID>` — the cron job id created for the research job (Job 1)
- `<OBJECTIVE>` — a short objective summary for the delivery

---

## Job 1 — Research job (aggressive digger)

Cadence: `every 2h` (from-finish). Fires only during CONTINUE / CHECKPOINT / BLOCKED;
auto-pauses on DORMANT / SUCCESS / EXHAUSTED (the `--cron <RESEARCH_JOB_ID>` flag on
`resignal` does this deterministically).

```
Run ONE research tick on the project at <PROJECT> per the HERMES.md protocol and the
endless-research skill. Your cron job id is <RESEARCH_JOB_ID>. The objective lives in
.research/objective.md.

0. CONCURRENCY: do your whole tick through the atomic project lock so two runs can never
   mutate state. Run:
     python3 ~/.hermes/skills/research/endless-research/scripts/research_project.py tick . [--note "<what this tick checks>"]
   If it exits code 2, the project is locked by a prior run — stop cleanly and report
   "skipped: locked".

1. Read .research/state.json FIRST. STOP if current_state is SUCCESS, EXHAUSTED, or
   DORMANT — do not dig; just report the verdict.
   - SUCCESS/EXHAUSTED are TERMINAL. Ensure the research cron is paused via
     `research_project.py resignal . <STATE> --cron <RESEARCH_JOB_ID>`.
   - DORMANT means clues are spent but the topic may reopen later; stay armed.

2. Resume the highest-priority PENDING frontier clue (`research_project.py status .`
   for the deterministic sort — do not hand-pick).

3. Dig with web search and extraction. Record real sources in .research/sources.jsonl
   and claim->source links in .research/claims.jsonl. Log queries in
   .research/search-log.jsonl to prevent repeating dead searches.

4. If you find the answer and it satisfies the objective's SUCCESS criteria with
   traceable, independently-verifiable evidence and no material unresolved
   contradiction:
   - define criteria in .research/criteria.jsonl,
   - run `research_project.py verify_success .` — it must report UNBLOCKED,
   - then `research_project.py resignal . SUCCESS --note "<summary>" --cron <RESEARCH_JOB_ID>`
     (this pauses the cron job),
   - write the full .research/final-report.md,
   - report success in your delivery.

5. Otherwise: quality-gate new clues into .research/frontier.jsonl, log dead branches,
   leave state as CONTINUE (or BLOCKED/DORMANT/EXHAUSTED only if genuinely appropriate)
   via `research_project.py resignal . <STATE> --note "..." --cron <RESEARCH_JOB_ID>`.

6. Deliver a SHORT progress note: current state, what was checked this tick, the top
   pending frontier item(s), and the next clue to investigate.

HARD RULES: Never fabricate a citation, URL, quote, or search result. Never claim a
source supports more than it does. Multiple pages citing the same original are NOT
independent confirmation — verify the original. Never declare SUCCESS just because you
searched a lot. This is a durable campaign; each tick only needs to make genuine
progress, not finish everything. Do not schedule, edit, or remove any cron job except
resignal's own auto-pause via --cron.
```

---

## Job 2 — Dormant watcher (cheap, daily/weekly)

Cadence: `0 12 * * *` (daily) or weekly. Scope: ONLY detect genuinely new evidence
for a DORMANT campaign; NEVER run the full campaign.

```
You are the DORMANT WATCHER for the research campaign at <PROJECT> (this working
directory). Your single job: detect whether genuinely NEW evidence exists that justifies
re-awakening a DORMANT campaign. You NEVER run the full research campaign.

STEP 1 — Read .research/state.json. If current_state is NOT "DORMANT", do nothing and
output ONLY:
    [SILENT]
In all non-DORMANT states this is the whole action.

STEP 2 — If current_state IS "DORMANT", do ONE BOUNDED new-evidence probe (not the full
campaign):
  - Read .research/objective.md to recall the exact question, key names, and the open gap.
  - Perform at most 3 targeted web searches using NEW terminology and sources that were
    unavailable or untried when the campaign went dormant.
  - Look only for materially new primary sources, newly released documents/filings/papers,
    recently opened archives, or a new authoritative page that could change the earlier
    conclusion or close the previously-unsolvable gap. Do NOT recursively chase citations.

STEP 3 — Decide:
  - If you found material NEW evidence that meaningfully advances the objective: re-awaken
    the campaign by running, in this project workdir:
      python3 ~/.hermes/skills/research/endless-research/scripts/research_project.py resignal . CONTINUE --note "new evidence appeared; <what and where>" --cron <RESEARCH_JOB_ID>
    Then deliver a short report: what you found, its URL, and why the campaign should resume.
  - If nothing material: stay DORMANT, do NOT change state, and output ONLY:
    [SILENT]

RULES: Never fabricate a citation, URL, search result, or page. Never run the full research
campaign, never recursively dig, never delegate, never modify any other cron job. The only
cron action allowed is the single resignal call above via its --cron flag, and only when you
genuinely re-awaken the campaign.
```
