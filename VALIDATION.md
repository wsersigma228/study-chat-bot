# Validation

Run the available synthetic checks in an isolated Python environment:

```sh
python -m pytest
```

Inspect the test output for skipped checks. Database integration requires a disposable PostgreSQL database through `TEST_DATABASE_URL`; use migrations as required by the checks. Never point integration tests at a production database.

For the offline workflow, apply migrations, import examples/desktop-export.json twice, and compare import statistics. Repeating unchanged data should not create new revisions. Inspect schedule, homework, review states and unavailable attachment metadata using the README commands. The synthetic import does not confirm a live schedule publisher; schedule review candidates are expected, rather than verified schedules.

No live Telegram result follows from offline fixtures or healthy containers. A real collector event, edit, restart reconciliation, bot conversation and channel publication need separate operator-controlled checks. This document supplies commands, not fabricated pass counts.
