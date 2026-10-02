# Data model

- `source_chats`: Desktop export identity, optional confirmed live peer/account, publisher bindings and collection cursors.
- `messages`: stable Telegram message identity, current revision, text, sender and reply metadata.
- `message_revisions`: stored snapshots with revision numbers.
- `attachments`: source and local availability, storage identity, size and checksum when available.
- `schedule_days`, `schedule_reviews`: daily schedule projections and unresolved source candidates.
- `homework`: projected tasks, evidence, due information, status and owner overrides.
- `homework_review_notices`: persistent owner review notifications.
- `channel_states`, `channel_posts`: destination publication coordination and message state.
- `bot_responses`: persisted bot response references for refreshes.

See app/models.py and migrations/ for the authoritative columns and constraints. Desktop export IDs, live Telegram peers and internal database row IDs are different identifiers.
