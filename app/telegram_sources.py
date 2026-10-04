"""Links to Telegram source messages, with a fallback for unbound peers."""

def source_link(peer_id: int | None, message_id: int) -> str:
    if peer_id is not None and str(peer_id).startswith("-100"):
        return f"https://t.me/c/{-peer_id - 1_000_000_000_000}/{message_id}"
    return f"message {message_id}"
