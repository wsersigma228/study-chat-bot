# Architecture

The archive importer normalizes Telegram Desktop JSON into source chats, messages, immutable revisions and attachment metadata. PostgreSQL is the system of record; Alembic manages schema changes.

The optional collector binds one imported chat to a confirmed live Telegram peer and account, then stores new events and reconciliation updates through the same storage path. It does not send messages.

The schedule and homework workers rebuild projections from saved messages. Deterministic rules use a subject catalog, source profiles and explicit text evidence. Unresolved candidates retain review reasons and source references. Homework owner overrides survive reparsing.

The bot reads these projections. Access is limited to the configured owner in private chat and explicitly allowed command groups. The source group is excluded. Channel publication uses a separate destination and persistent status to avoid blindly retrying an uncertain send.

The public default is offline. There is no LLM, OCR or vision service. Attachment availability is represented as metadata, not guessed content.
