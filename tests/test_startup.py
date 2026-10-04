"""Offline startup checks: no database connection or live Telegram traffic."""

import asyncio
import subprocess
import sys
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("module", ["app.bot", "app.channel", "app.homework_review", "app.collector",
                                    "app.cli", "app.telegram_cli"])
def test_independent_import(module):
    subprocess.run([sys.executable, "-c", f"import {module}"], check=True, capture_output=True, timeout=30)


@pytest.mark.parametrize("module", ["app.cli", "app.telegram_cli"])
def test_cli_help(module):
    result = subprocess.run([sys.executable, "-m", module, "--help"],
                            check=True, capture_output=True, text=True, timeout=30)
    assert "usage:" in result.stdout


def test_bot_startup_and_today_handler(monkeypatch):
    from sqlalchemy.ext.asyncio import create_async_engine
    import app.bot as module

    settings = module.BotSettings("123456789:" + "a" * 35, 101, frozenset(), -1000000000303)
    monkeypatch.setattr(module.BotSettings, "read", lambda: settings)
    monkeypatch.setattr(module, "engine", lambda: create_async_engine(
        "postgresql+asyncpg://study_bot:fictional@localhost/study_bot_test"))
    calls = []
    async def saved(message, config, factory, target, kind="schedule"):
        assert config is settings
        calls.append(target)
    monkeypatch.setattr(module, "send_saved_response", saved)
    async def idle(*args):
        return None
    monkeypatch.setattr("app.homework_review.review_loop", idle)
    monkeypatch.setattr("app.channel.refresh_publications", idle)
    async def poll(bot, dispatcher):
        assert bot.id == 123456789
        router = dispatcher.sub_routers[0]
        handler = next(entry.callback for entry in router.message.handlers
                       if entry.callback.__name__ == "today")
        await handler(SimpleNamespace(chat=SimpleNamespace(id=101, type="private"),
                                      from_user=SimpleNamespace(id=101), message_id=1))
    monkeypatch.setattr(module, "poll", poll)
    asyncio.run(module.run())
    assert len(calls) == 1


def test_shared_source_link():
    from app.telegram_sources import source_link
    assert source_link(-1000000000101, 7) == "https://t.me/c/101/7"
    assert source_link(None, 7) == source_link(-303, 7) == "message 7"
    assert source_link(None, 7, fallback_label="source") == "source 7"
    assert source_link(-1000000000101, 7, fallback_label="source") == "https://t.me/c/101/7"


def test_collector_and_database_imports_do_not_load_schedule_settings(tmp_path):
    import os
    from pathlib import Path

    (tmp_path / ".env").write_text("STUDY_REFACTOR_SMOKE=loaded\n", encoding="utf-8")
    runtime = os.environ.copy()
    runtime["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    runtime["STUDY_TIMEZONE"] = "Invalid/RefactorSmoke"
    runtime.pop("STUDY_REFACTOR_SMOKE", None)
    result = subprocess.run([sys.executable, "-c",
        "import os; import app.telegram_settings, app.db, app.collector; "
        "assert 'STUDY_REFACTOR_SMOKE' not in os.environ"],
        cwd=tmp_path, env=runtime, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
