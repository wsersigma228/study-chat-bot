# Validation

Run the available synthetic checks in an isolated Python environment:

```sh
python -m pytest
```

Inspect the test output for skipped checks. Database integration requires a disposable PostgreSQL database through `TEST_DATABASE_URL`; use migrations as required by the checks. Never point integration tests at a production database.

For the offline workflow, apply migrations, import examples/desktop-export.json twice, and compare import statistics. Repeating unchanged data should not create new revisions. Inspect schedule, homework, review states and unavailable attachment metadata using the README commands. The synthetic import does not confirm a live schedule publisher; schedule review candidates are expected, rather than verified schedules.

No live Telegram result follows from offline fixtures or healthy containers. A real collector event, edit, restart reconciliation, bot conversation and channel publication need separate operator-controlled checks. Recorded checks below describe only their stated scope.

## Refactor verification - 2026-10-04

Code commit: `28c260c`. Local Python checks: **56 passed, 4 skipped** (PostgreSQL
checks unavailable locally). The built Python 3.12 image with a separate migrated
PostgreSQL test database: **60 passed, no skips**. This includes legacy and
structured overrides surviving replay/source deletion, approval invalidation,
explicit null deadlines, accept/dismiss/edit writes, repeated/unknown subjects,
extra tasks and saved source callback fingerprints.

Each bot/channel/review/collector/CLI import was checked in a fresh process;
both CLI help entry points and bot startup with a direct `/today` handler call
were checked without a database connection or Telegram requests. Independent
review found no import cycles. Old/new output, source references and fingerprints
matched for four histories containing twelve lesson blocks.

Temporary container verification used only synthetic data and disabled Telegram
publication. Both application and test databases reached existing migration
`0009_bot_responses`; the refactor introduces no new migration. Import replay
reported 8 created, then 8 unchanged; the offline worker produced 4 confirmed
homework tasks and 3 schedule review candidates. The database health check and
worker logs passed; source checksums in the running image matched the checkout.
Project containers were stopped and removed after verification; volumes retained.
No live bot, collector or channel delivery was exercised.

### Compliance follow-up

Code commit `c8d4f91` restores the original schedule timezone initialization
boundary. The previous move to shared settings caused collector/database imports
to load `.env` and fail on an invalid schedule timezone; this was reproduced and
fixed without changing valid configuration behavior. A fresh-process regression
test covers both effects. Local checks: **57 passed, 4 skipped**; rebuilt image
with the separate PostgreSQL test database: **61 passed, no skips**. Independent
startup checks: **11 passed**. Existing migrations remained at `0009_bot_responses`.

The rebuilt offline worker was checked again, including source checksums and
logs. After verification, the task's temporary containers, two disposable volumes
and empty network were removed after checking their ownership. The earlier
volume retention above describes the initial check, before this follow-up.
