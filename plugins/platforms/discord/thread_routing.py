"""Optional, vendor-neutral Discord thread selection via lifecycle plugins.

The bounded, process-local index contains only selective threads created for this requester.
No guild-wide enumeration, transcript search, or cross-profile history is used.
"""
import asyncio
import logging
from collections import OrderedDict

from hermes_cli import lifecycle
from hermes_constants import hermes_home_key


def _source(adapter, message, thread=None):
    channel = thread or message.channel
    return adapter.build_source(
        chat_id=str(channel.id), chat_type="thread" if thread else "group",
        user_id=str(message.author.id), guild_id=str(message.guild.id),
        thread_id=str(thread.id) if thread else None,
        parent_chat_id=str(message.channel.id) if thread else None,
        message_id=str(message.id),
    )


def eligible(adapter, message, thread, identity):
    """Public, active same-parent threads only; access and runtime ownership must still match."""
    if (thread.parent_id != message.channel.id or thread.guild.id != message.guild.id
            or thread.archived or thread.locked or thread.is_private()):
        return False
    for member in (message.author, message.guild.me):
        permissions = thread.permissions_for(member)
        if not (permissions.view_channel and permissions.send_messages_in_threads):
            return False
    if adapter._is_sender_authorized(str(message.author.id), "thread", str(thread.id),
                                     thread_id=str(thread.id)) is not True:
        return False
    if identity is not None:
        target_identity = adapter._canonicalize(_source(adapter, message, thread))
        if (target_identity is None or target_identity.runtime_home != identity.runtime_home
                or target_identity.runtime_profile != identity.runtime_profile):
            return False
    return True


async def notify_failed_creation(message, text):
    """Legacy mode must report creation failure even when Discord cannot accept the notice."""
    try:
        await message.channel.send(text)
    except Exception as error:  # health: allow BLE001 -- SDK notification failure is best-effort; omit potentially sensitive exception content
        logging.getLogger(__name__).warning("Thread failure notice failed (%s)", type(error).__name__)


async def select_thread(adapter, message, selective, content):
    """Preserve the legacy path exactly; slash commands bypass only selective decisions."""
    if not selective:
        thread = await adapter._auto_create_thread(message)
        return thread, thread is not None
    if content.startswith("/"):
        return None, False
    return await route_message(adapter, message)


async def route_message(adapter, message):
    """Policy failure never consumes the user's message; dispatch inline instead."""
    if adapter._is_sender_authorized(str(message.author.id), "group", str(message.channel.id)) is not True:
        return None, False
    try:
        source = _source(adapter, message)
        identity = adapter._canonicalize(source)
        runner = getattr(adapter, "gateway_runner", None)
        if runner is None or identity is None:
            return None, False
        async with runner._async_profile_scope_for_source(source):
            return await _choose(adapter, message, identity)
    except Exception as error:  # health: allow BLE001 -- plugin/SDK failure must not consume input; log only its class, never a potentially sensitive exception body or traceback
        logging.getLogger(__name__).warning("Thread routing failed (%s); using inline", type(error).__name__)
        return None, False


async def _choose(adapter, message, identity):
    """Return (target, created) from a closed set revalidated after the plugin await."""
    lanes = getattr(adapter, "_selective_thread_lanes", None)
    if lanes is None:
        lanes = adapter._selective_thread_lanes = OrderedDict()
    lane = (hermes_home_key(), message.guild.id, message.channel.id, message.author.id)
    threads = lanes.setdefault(lane, OrderedDict())
    lanes.move_to_end(lane)
    while len(lanes) > 128:
        lanes.popitem(last=False)
    current = (adapter._client.get_channel(thread.id) for thread in reversed(threads.values()))
    candidates = [thread for thread in current if thread and eligible(adapter, message, thread, identity)][:8]
    choices = {f"candidate_{i}": thread for i, thread in enumerate(candidates)}
    results = await asyncio.wait_for(lifecycle.ainvoke_hook(
        "gateway_thread_route", platform="discord", text=message.content[:4000],
        candidates={key: thread.name[:100] for key, thread in choices.items()},
    ), timeout=5.0)
    choice = results[0].get("choice") if results and isinstance(results[0], dict) else "inline"
    if not isinstance(choice, str):
        return None, False
    if choice == "new_thread":
        thread = await adapter._auto_create_thread(message)
        if thread and eligible(adapter, message, thread, identity):
            threads[thread.id] = thread
            while len(threads) > 8:
                threads.popitem(last=False)
            return thread, True
        return None, False
    chosen = choices.get(choice)
    thread = adapter._client.get_channel(chosen.id) if chosen else None
    if chosen is not None and thread and thread.id == chosen.id and eligible(adapter, message, thread, identity):
        threads.move_to_end(thread.id)
        return thread, False
    return None, False
