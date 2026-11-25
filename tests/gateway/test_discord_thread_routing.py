"""Selective routing exercises Discord's real message-to-session path, offline."""
from datetime import datetime, UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter
import plugins.platforms.discord.adapter as discord_platform


class Thread:
    def __init__(self, parent, ident=800):
        self.id, self.name = ident, "database migration"
        self.parent, self.parent_id, self.guild = parent, parent.id, parent.guild
        self.archived = self.locked = False

    def is_private(self):
        return False

    def permissions_for(self, member):
        return SimpleNamespace(view_channel=True, send_messages_in_threads=True)


def message(channel, ident=100, text="<@999> build a migration", user=42):
    return SimpleNamespace(
        id=ident, content=text, mentions=[SimpleNamespace(id=999)], attachments=[],
        reference=None, created_at=datetime.now(UTC), channel=channel, guild=channel.guild,
        author=SimpleNamespace(id=user, display_name="Alice", name="alice"), type=None,
    )


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setattr(discord_platform.discord, "Thread", Thread)
    guild = SimpleNamespace(id=1, name="server", me=SimpleNamespace(id=999))
    parent = SimpleNamespace(id=700, name="ops", topic=None, guild=guild)
    thread = Thread(parent)
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake", extra={"thread_routing": "plugin"}))
    adapter._client = SimpleNamespace(user=SimpleNamespace(id=999), get_channel=lambda _: thread)
    adapter._text_batch_delay_seconds = 0
    adapter.set_authorization_check(lambda *args, **kwargs: True)
    adapter._discord_history_backfill = lambda: False
    adapter._auto_create_thread = AsyncMock(return_value=thread)
    adapter.handle_message = AsyncMock()
    hook = AsyncMock(return_value=[{"choice": "inline"}])
    monkeypatch.setattr("hermes_cli.lifecycle.ainvoke_hook", hook)
    return adapter, parent, thread, hook


@pytest.mark.asyncio
async def test_plugin_inline_dispatches_without_creating_thread(rig):
    adapter, parent, _thread, hook = rig
    assert await adapter._handle_message(message(parent, text="<@999> thanks"))
    adapter._auto_create_thread.assert_not_awaited()
    assert adapter.handle_message.await_args.args[0].source.chat_id == "700"
    hook.assert_awaited_once()


@pytest.mark.asyncio
async def test_continuation_reuses_requesters_thread(rig):
    adapter, parent, _thread, hook = rig
    hook.return_value = [{"choice": "new_thread"}]
    assert await adapter._handle_message(message(parent))
    first = adapter.handle_message.await_args.args[0].source
    assert first.chat_id == "800" and first.auto_thread_created
    hook.return_value = [{"choice": "candidate_0"}]
    assert await adapter._handle_message(message(parent, ident=101, text="<@999> continue migration"))
    second = adapter.handle_message.await_args.args[0].source
    assert second.chat_id == "800"
    assert second.auto_thread_created is False
    assert second.auto_thread_initial_name is None
    adapter._auto_create_thread.assert_awaited_once()
    assert hook.await_args.kwargs["candidates"] == {"candidate_0": "database migration"}


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["locked", "archived", "parent", "private", "requester", "bot", "other_user", "other_channel"])
async def test_ineligible_candidates_are_never_disclosed_or_reused(rig, invalid):
    adapter, parent, thread, hook = rig
    hook.return_value = [{"choice": "new_thread"}]
    assert await adapter._handle_message(message(parent))
    next_message = message(parent, ident=101)
    def deny(member_id):
        thread.permissions_for = lambda member: SimpleNamespace(
            view_channel=member.id != member_id, send_messages_in_threads=True)

    mutations = {
        "locked": lambda: setattr(thread, "locked", True),
        "archived": lambda: setattr(thread, "archived", True),
        "parent": lambda: setattr(thread, "parent_id", 701),
        "private": lambda: setattr(thread, "is_private", lambda: True),
        "requester": lambda: deny(42),
        "bot": lambda: deny(999),
        "other_user": lambda: setattr(next_message.author, "id", 43),
        "other_channel": lambda: setattr(next_message, "channel", SimpleNamespace(
            id=701, name="other", topic=None, guild=parent.guild)),
    }
    mutations[invalid]()
    hook.return_value = [{"choice": "candidate_0"}]
    assert await adapter._handle_message(next_message)
    assert hook.await_args.kwargs["candidates"] == {}
    assert adapter.handle_message.await_args.args[0].source.chat_id == str(next_message.channel.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [RuntimeError("network"), TimeoutError(), [], None, [{}], [{"choice": []}], [{"choice": "800"}], [{"choice": "unknown"}]])
async def test_policy_failures_dispatch_inline(rig, response):
    adapter, parent, _thread, hook = rig
    if isinstance(response, Exception):
        hook.side_effect = response
    else:
        hook.return_value = response
    assert await adapter._handle_message(message(parent))
    assert adapter.handle_message.await_args.args[0].source.chat_id == "700"
    adapter._auto_create_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_selective_creation_dispatches_inline(rig):
    adapter, parent, _thread, hook = rig
    hook.return_value = [{"choice": "new_thread"}]
    adapter._auto_create_thread.side_effect = RuntimeError("Discord unavailable")
    assert await adapter._handle_message(message(parent))
    assert adapter.handle_message.await_args.args[0].source.chat_id == "700"


@pytest.mark.asyncio
async def test_slash_command_bypasses_policy_and_thread_creation(rig):
    adapter, parent, _thread, hook = rig
    assert await adapter._handle_message(message(parent, text="<@999> /status"))
    hook.assert_not_awaited()
    adapter._auto_create_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_authorization_never_sends_content_to_policy(rig):
    adapter, parent, _thread, hook = rig
    adapter.set_authorization_check(None)
    assert await adapter._handle_message(message(parent))
    hook.assert_not_awaited()
    assert adapter.handle_message.await_args.args[0].source.chat_id == "700"


@pytest.mark.asyncio
async def test_routing_binds_runtime_home_and_separates_profile_candidates(rig, tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override
    adapter, parent, _thread, hook = rig
    active = [tmp_path / "alpha"]

    @asynccontextmanager
    async def scope(source):
        token = set_hermes_home_override(active[0])
        try:
            yield
        finally:
            reset_hermes_home_override(token)

    adapter.gateway_runner = SimpleNamespace(_async_profile_scope_for_source=scope)
    monkeypatch.setattr(adapter, "_canonicalize", lambda source: SimpleNamespace(runtime_home=active[0], runtime_profile=active[0].name))
    observed = []

    async def classify(*args, **kwargs):
        observed.append((get_hermes_home(), kwargs["candidates"]))
        return [{"choice": "new_thread" if len(observed) == 1 else "inline"}]

    hook.side_effect = classify
    assert await adapter._handle_message(message(parent))
    active[0] = tmp_path / "beta"
    assert await adapter._handle_message(message(parent, ident=101))
    assert observed == [(tmp_path / "alpha", {}), (tmp_path / "beta", {})]


@pytest.mark.asyncio
async def test_candidates_bounded_to_eight(rig):
    adapter, parent, _thread, hook = rig
    hook.return_value = [{"choice": "new_thread"}]
    created = [Thread(parent, 800+i) for i in range(10)]
    adapter._auto_create_thread.side_effect = created
    adapter._client.get_channel = {item.id: item for item in created}.get
    for i in range(10):
        assert await adapter._handle_message(message(parent, ident=100+i))
    hook.return_value = [{"choice": "inline"}]
    assert await adapter._handle_message(message(parent, ident=110))
    assert len(hook.await_args.kwargs["candidates"]) == 8


@pytest.mark.asyncio
async def test_candidate_revalidated_after_policy_wait(rig):
    adapter, parent, thread, hook = rig
    hook.return_value = [{"choice": "new_thread"}]
    assert await adapter._handle_message(message(parent))

    async def revoke(*args, **kwargs):
        thread.locked = True
        return [{"choice": "candidate_0"}]

    hook.side_effect = revoke
    assert await adapter._handle_message(message(parent, ident=101))
    assert adapter.handle_message.await_args.args[0].source.chat_id == "700"


@pytest.mark.asyncio
async def test_replaced_cache_entry_cannot_revive_archived_thread(rig):
    adapter, parent, _thread, hook = rig
    hook.return_value = [{"choice": "new_thread"}]
    assert await adapter._handle_message(message(parent))
    replacement = Thread(parent)
    replacement.archived = True
    adapter._client.get_channel = lambda ident: replacement
    hook.return_value = [{"choice": "candidate_0"}]
    assert await adapter._handle_message(message(parent, ident=101))
    assert hook.await_args.kwargs["candidates"] == {}
    assert adapter.handle_message.await_args.args[0].source.chat_id == "700"


@pytest.mark.asyncio
async def test_candidate_routed_to_another_profile_is_not_disclosed(rig, tmp_path, monkeypatch):
    adapter, parent, _thread, hook = rig
    current = SimpleNamespace(runtime_home=tmp_path / "alpha", runtime_profile="alpha")
    monkeypatch.setattr(adapter, "_canonicalize", lambda source: current)
    hook.return_value = [{"choice": "new_thread"}]
    assert await adapter._handle_message(message(parent))
    foreign = SimpleNamespace(runtime_home=tmp_path / "beta", runtime_profile="beta")
    monkeypatch.setattr(adapter, "_canonicalize", lambda source: foreign if source.thread_id else current)
    hook.return_value = [{"choice": "candidate_0"}]
    assert await adapter._handle_message(message(parent, ident=101))
    assert hook.await_args.kwargs["candidates"] == {}
    assert adapter.handle_message.await_args.args[0].source.chat_id == "700"


@pytest.mark.asyncio
@pytest.mark.parametrize("bypass", ["thread", "dm", "reply", "no_thread", "disabled", "legacy"])
async def test_deterministic_bypasses_and_legacy_default(rig, bypass, monkeypatch):
    adapter, parent, thread, hook = rig
    msg = message(parent)
    def dm():
        class DM:
            id, guild = 900, None
        monkeypatch.setattr(discord_platform.discord, "DMChannel", DM)
        msg.channel = DM()
        msg.guild = None

    mutations = {
        "thread": lambda: setattr(msg, "channel", thread),
        "dm": dm,
        "reply": lambda: setattr(msg, "type", discord_platform.discord.MessageType.reply),
        "no_thread": lambda: adapter.config.extra.update(no_thread_channels=["700"]),
        "disabled": lambda: adapter.config.extra.update(auto_thread=False),
        "legacy": lambda: adapter.config.extra.pop("thread_routing"),
    }
    mutations[bypass]()
    assert await adapter._handle_message(msg)
    hook.assert_not_awaited()
    if bypass == "legacy":
        adapter._auto_create_thread.assert_awaited_once()
    else:
        adapter._auto_create_thread.assert_not_awaited()


def test_thread_routing_config_defaults_and_gateway_reader(tmp_path, monkeypatch):
    from hermes_cli.config import load_config
    from gateway.config import load_gateway_config, Platform
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert load_config()["discord"]["thread_routing"] == "legacy"
    (tmp_path / "config.yaml").write_text("discord:\n  thread_routing: plugin\n")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "synthetic-token")
    assert load_gateway_config().platforms[Platform.DISCORD].extra["thread_routing"] == "plugin"
