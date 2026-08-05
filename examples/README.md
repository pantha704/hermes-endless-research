# Examples

This directory holds example/demo material for **hermes-endless-research**.

`demo-project/` is a scaffold placeholder. A real campaign's `.research/` external
brain (objective.md, state.json, frontier.jsonl, criteria.jsonl, checkpoints, ...) is
**intentionally gitignored** — generated research data stays private on your machine /
private storage and is never committed to this public repository.

To build a live demo locally:

```bash
endless-research init examples/demo-project --objective "Your objective here"
endless-research status examples/demo-project
```

See `templates/` for cookie-cutter objective / HERMES.md / cron prompts.
