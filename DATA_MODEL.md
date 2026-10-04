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

## Owner corrections and schedule JSON

New owner actions store `homework.owner_override` as
`{"fields": {...}, "meta": {"approved_sources": [...]}}`. The centralized helpers in
`homework.py` also read legacy flat overrides, moving `_approved_sources` into
metadata on the next owner action. No schema or data migration is required;
reparsing leaves stored overrides unchanged. Explicit null field values clear a
deadline. When approved source revisions change, the projection is shown until the
owner reviews it again. Legacy corrections without an approval snapshot retain
their existing precedence. Updates assign a fresh JSON object for SQLAlchemy.
The legacy reader can be removed only after a verified conversion of all stored
flat overrides; retaining it now avoids an unnecessary eager data migration.

`ScheduleDay.payload` remains JSONB, produced by `schedule.project()`: `state`,
`date_basis`, `slots`, `source_ids`, `context_ids`, `inferences`, `source_revisions`
and `warnings`. Slot fields include `number`, `raw_subject`, `subject_key` and
`room`. An estimated view copies this payload and adds `estimate_source_date`;
it does not persist an estimated row. Separate slot tables are unnecessary while
consumers read complete days without SQL filters over individual lessons.
