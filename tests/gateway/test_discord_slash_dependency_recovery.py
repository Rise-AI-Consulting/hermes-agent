"""Slash authorization recovers after discord.py becomes available (regression for #137083).

Only dependency installation is simulated; imports and configured authorization gates are real.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from gateway.config import PlatformConfig


@pytest.fixture(params=["package", "directory-plugin"])
def late_adapter(request, monkeypatch):
    from hermes_cli.plugins import PluginManager, PluginManifest

    for name in (
        "DISCORD_ALLOWED_USERS", "DISCORD_ALLOWED_ROLES", "DISCORD_ALLOWED_CHANNELS",
        "DISCORD_IGNORED_CHANNELS", "DISCORD_ALLOW_ALL_USERS", "GATEWAY_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(name, raising=False)
    # A None cache entry makes actual imports fail, even if discord.py is installed.
    monkeypatch.setitem(sys.modules, "discord", None)
    canonical = importlib.import_module("plugins.platforms.discord.adapter")
    if request.param == "directory-plugin":
        plugin_dir = Path(__file__).resolve().parents[2] / "plugins/platforms/discord"
        plugin = PluginManager()._load_directory_module(
            PluginManifest(name="discord-platform", path=str(plugin_dir), kind="platform"),
            module_name="hermes_plugins.slash_dependency_recovery",
        )
        module = sys.modules[plugin.__name__ + ".adapter"]
    else:
        module = canonical
    slash_module = importlib.import_module(module.__package__ + ".adapter_slash_auth")
    monkeypatch.setattr(slash_module, "discord", None, raising=False)
    monkeypatch.setattr(canonical, "discord", None)
    adapter = module.DiscordAdapter(PlatformConfig(
        enabled=True, token="fake", extra={"allowed_users": "42"},
    ))
    adapter._derive_connect_gates()
    yield adapter, module
    for name in list(sys.modules):
        if name.startswith("hermes_plugins.slash_dependency_recovery"):
            sys.modules.pop(name, None)


def _install_dependency(module, monkeypatch):
    dependency = ModuleType("discord")
    dependency.DMChannel = type("DMChannel", (), {})
    dependency.Thread = type("Thread", (), {})
    monkeypatch.setitem(sys.modules, "discord", dependency)
    # Only the running adapter adopted the newly available dependency; its mixin did not.
    monkeypatch.setattr(module, "discord", dependency)
    return dependency


def _interaction(user_id=42, channel_id=700):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, name="Alice"),
        channel=SimpleNamespace(id=channel_id, name="ops"),
        channel_id=channel_id,
        guild=SimpleNamespace(id=1, get_member=lambda uid: None),
    )


def test_stale_slash_binding_recovers_for_configured_user(late_adapter, monkeypatch):
    adapter, module = late_adapter
    _install_dependency(module, monkeypatch)

    assert adapter._evaluate_slash_authorization(_interaction()) == (True, None)


@pytest.mark.parametrize("channel_present", [True, False])
def test_missing_dependency_fails_closed(late_adapter, monkeypatch, channel_present):
    adapter, _ = late_adapter
    # Even an identified allowlisted actor / explicit open mode cannot bypass dependency failure.
    monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")
    interaction = _interaction()
    if not channel_present:
        interaction.channel = None
        interaction.channel_id = None

    assert adapter._evaluate_slash_authorization(interaction) == (
        False, "discord dependency unavailable",
    )


@pytest.mark.parametrize("policy,user_id,channel_id,reason", [
    ({}, 99, 700, "user not in DISCORD_ALLOWED_USERS / DISCORD_ALLOWED_ROLES"),
    ({"allowed_channels": "700"}, 42, 701, "channel not in DISCORD_ALLOWED_CHANNELS"),
    ({"allowed_channels": "700", "ignored_channels": "700"}, 42, 700,
     "channel in DISCORD_IGNORED_CHANNELS"),
])
def test_recovered_dependency_preserves_authorization(
    late_adapter, monkeypatch, policy, user_id, channel_id, reason,
):
    adapter, module = late_adapter
    _install_dependency(module, monkeypatch)
    adapter.config.extra.update(policy)
    adapter._derive_connect_gates()

    assert adapter._evaluate_slash_authorization(_interaction(user_id, channel_id)) == (
        False, reason,
    )


def test_dependency_failure_does_not_poison_later_invocations(late_adapter, monkeypatch):
    adapter, module = late_adapter
    assert adapter._evaluate_slash_authorization(_interaction()) == (
        False, "discord dependency unavailable",
    )

    _install_dependency(module, monkeypatch)

    assert adapter._evaluate_slash_authorization(_interaction()) == (True, None)
