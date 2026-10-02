# Design decisions

- PostgreSQL and Alembic provide persistent revisions and schema evolution.
- Telethon collects through a user session; aiogram handles the separate bot.
- Parsing is deterministic and conservative. No external model is called.
- Source evidence and uncertainty remain visible; owner corrections survive reparsing.
- Private runtime data stays under ignored paths. Shipped examples are invented.
- English is the public interface and fixture language.

These are implementation boundaries, not a record of personal classroom decisions.
