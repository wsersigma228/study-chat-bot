# Study Chat Bot

A Python backend for importing a Telegram Desktop JSON export, building text schedules and homework records, and optionally serving them through a Telegram bot.

This public project ships only invented English examples. All example names, IDs, dates, rooms and messages are synthetic. No original chat exports, attachments, Telegram sessions, credentials or deployment history are included.

## What it does

- Imports messages, edits, reply references and attachment metadata into PostgreSQL. Repeating the same import does not create duplicate revisions.
- Parses supported English schedule and homework formats deterministically, preserving source references and visible uncertainty.
- Keeps missing attachments explicit; it does not extract text from images or documents.
- Provides owner review of uncertain homework while preserving owner edits during reparsing.
- Includes an optional read-only Telethon collector, an aiogram bot with restricted access, and channel publication with persistent state.

No LLM or OCR integration is implemented. The parser handles a small set of documented formats, not arbitrary natural language. A synthetic offline demonstration does not establish live Telegram behavior.

## Offline demonstration

Use Python 3.12 or newer and PostgreSQL. Create a virtual environment and install the pinned dependencies:

```sh
python -m venv .venv
# Activate .venv using your shell, then:
python -m pip install -r requirements.lock
cp .env.example .env
```

Set `DATABASE_URL` in your private `.env` to a PostgreSQL database you control. Keep the Telegram variables empty for the offline workflow. Containers should run on a dedicated remote host; no local containers are needed to edit the code.

Apply migrations, import the invented fixture, and inspect the projections:

```sh
alembic upgrade head
python -m app.cli import --export examples/desktop-export.json
python -m app.cli summary
python -m app.cli schedule-sync
python -m app.cli schedule --date 2030-04-06
python -m app.cli homework-sync
python -m app.cli homework
python -m app.cli schedule-reviews
python -m app.cli homework-reviews
```

The demo does not confirm a live publisher. Schedule candidates therefore remain in review and the date command may report no verified schedule; inspect `schedule-reviews` for the explicit reasons. Homework with an explicit subject can still be parsed from the invented text.

`python -m app.cli show --id 1` displays one saved source message locally. Do not share that output when using a private export.

## Optional live operation

See [OWNER_SETUP.md](OWNER_SETUP.md). Live operation needs your own credentials, confirmed group and publisher identities, and a private export matching that group. Synthetic IDs must never be used as actual Telegram configuration.

The source group, groups allowed to issue bot commands, and destination channel are separate settings. The collector never posts to the source group. Configuring a destination channel enables external publication by the worker.

## Project map

- `app/importer.py`: archive normalization and revision storage.
- `app/collector.py`, `app/telegram_cli.py`: optional Telegram collection and setup.
- `app/schedule.py`, `app/homework.py`: deterministic projections.
- `app/bot.py`, `app/homework_review.py`: bot responses and owner review.
- `app/channel.py`: publication state and reconciliation.
- `migrations/`: Alembic database migrations.
- `config/`, `examples/`: invented catalog, source profiles and fixture data.

Read [ARCHITECTURE.md](ARCHITECTURE.md), [DOMAIN_RULES.md](DOMAIN_RULES.md) and [VALIDATION.md](VALIDATION.md) for implementation boundaries.
