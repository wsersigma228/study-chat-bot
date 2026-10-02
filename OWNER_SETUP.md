# Optional live setup

The shipped example is offline. Do not use its invented IDs to connect a real group. The collector reads a user account's group; the bot serves separate command groups or a separate channel.

## Private preparation

1. Obtain your own Telegram API ID and hash and bot token. Save them only in .env. Set DATABASE_URL and STUDY_TIMEZONE for your deployment.
2. Put a Telegram Desktop JSON export of your intended source group at private/export/result.json. Review its messages and publisher identity privately. Import it into your own database after applying migrations.
3. Run `python scripts/prepare_local_config.py --export private/export/result.json` to create a private fingerprint and local references. This is local preparation, not live identity confirmation. Review any generated source associations before approving them.
4. Set EXPORT_FINGERPRINT_PATH to the generated private fingerprint path. Set SCHEDULE_PUBLISHER_MESSAGE_IDS to at least three distinct positive IDs of actual schedule messages from that same export, and verify that they belong to the intended publisher.
5. Run `python -m app.telegram_cli login`, then save the reported TELEGRAM_ACCOUNT_USER_ID in your private configuration.
6. Run `python -m app.telegram_cli select-group` and explicitly confirm the intended peer. Historical message evidence must match the reviewed export. Save SOURCE_CHAT_ID.
7. Run `python -m app.telegram_cli confirm-publisher`, confirm the actual sender and save SCHEDULE_PUBLISHER_USER_ID. Use `add-publisher --message-id <id>` only for a separately checked additional publisher.

Never copy a Desktop export ID into a live peer setting. Keep private exports, sessions, fingerprints and source associations out of Git.

## Bot and worker

Set BOT_TOKEN, BOT_OWNER_USER_ID and BOT_ALLOWED_CHAT_IDS for the groups that may request bot responses. The source group is excluded. Run `python -m app.bot` for polling and `python -m app.cli schedule-watch` to rebuild projections. The owner's private chat supports review; other private users are not granted access.

Leave BOT_CHANNEL_ID empty to disable channel publication. If enabled, use a destination distinct from source and command groups. The worker can publish and update destination messages. `channel-post --date YYYY-MM-DD` is an explicit publication command. An uncertain send requires inspecting the destination before using `--replace`.

## Local containers during active work

Run containers locally only while actively working on this project. Keep .env and volumes private. Compose disables automatic restart; do not install an autostart service or deploy this project remotely. Set POSTGRES_PASSWORD before using Compose; use a URL-safe password or percent-encode it in URLs. Inspect Git status and preserve local changes before updating code.

```sh
docker compose build
docker compose up -d db
docker compose run --rm migrate
docker compose up -d worker
# Only after private live setup:
docker compose --profile live up -d bot collector
```

The collector uses a named session volume in Compose. Authorize it within that same volume before starting collection. The collector's fingerprint path in Compose points inside the private export mount; the preparation script writes the fingerprint to that location. Do not substitute the offline example for your source group.

When finished, run `docker compose --profile live stop` and verify that no project containers are running. Volumes are retained.

Check the running Git commit, database migrations, container state and logs. Use `python -m app.telegram_cli status` with the same private configuration to inspect collection state. A healthy container does not prove successful collection or a real bot conversation. Preserve database, media and session volumes; never reset them during routine upgrades.
