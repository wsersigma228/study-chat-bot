# Architecture

The archive importer normalizes Telegram Desktop JSON into source chats, messages, immutable revisions and attachment metadata. PostgreSQL is the system of record; Alembic manages schema changes.

The optional collector binds one imported chat to a confirmed live Telegram peer and account, then stores new events and reconciliation updates through the same storage path. It does not send messages.

The schedule and homework workers rebuild projections from saved messages. Deterministic rules use a subject catalog, source profiles and explicit text evidence. Unresolved candidates retain review reasons and source references. Homework owner overrides survive reparsing.

The bot reads these projections. Access is limited to the configured owner in private chat and explicitly allowed command groups. The source group is excluded. Channel publication uses a separate destination and persistent status to avoid blindly retrying an uncertain send.

The public default is offline. There is no LLM, OCR or vision service. Attachment availability is represented as metadata, not guessed content.

## Shared application code

`bot.py` owns access checks, handlers and delivery. `channel.py` owns persisted
publication and refreshes; neither channel publication nor owner review imports
the handler module. Both use `study_view.py` for text, keyboards, source references,
attachment checks and response views. `homework_selection.py` reads history and
selects tasks into an `Assignment` dataclass with named fields. Telegram source
URLs have one implementation in `telegram_sources.py`, also used by the CLI.

Override access and updates live in `homework.py`; the review module uses these
helpers rather than inspecting the stored format. Models and projection SQL stay
unchanged. The importer/collector still feed stored revisions into schedule and
homework projections, which the CLI, views and publication code read.

The existing timezone initialization in `schedule.py` still loads `.env` on
import. Startup-only initialization is deferred: changing it safely requires
coordinating timezone consumers and preserving host CLI `.env` behavior. Moving
it into the shared settings module would unnecessarily make collector/database
imports validate schedule timezone settings. `MEDIA_ROOT` lookup in attachment
checks and collector setup environment reads also remain future cleanup candidates.
