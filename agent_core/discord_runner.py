"""Discord bot runner — private bot with allowlists + confirmation UI.

Public API:
    run_discord(build_opts, *, token=None, allowed_user_ids=None,
                allowed_channel_ids=None, allowed_guild_ids=None,
                session_max_age_seconds=6*3600,
                log_channel="discord")

`build_opts` is `Callable[[*, sender_key, sender_name], ClaudeAgentOptions]`
— the framework calls it once per new Discord user session.

Allow-lists default to the env vars
    DISCORD_BOT_TOKEN, DISCORD_ALLOWED_USER_IDS,
    DISCORD_ALLOWED_CHANNEL_IDS, DISCORD_ALLOWED_GUILD_IDS
"""
import asyncio
import logging
import os
import time
from typing import Callable

import discord

from claude_agent_sdk import (
    ClaudeSDKClient,
    AssistantMessage,
    UserMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    ResultMessage,
    ClaudeAgentOptions,
)

from agent_core import context as agent_context
from agent_core.context import IMAGE_MARKER
from agent_core.tools import cost_log
from agent_core.tools.confirm import confirm_callback as confirm_cb_ctx


log = logging.getLogger("agent-core.discord")


BuildOptsFn = Callable[..., ClaudeAgentOptions]


DISCORD_MSG_LIMIT = 1900


class _ConfirmView(discord.ui.View):
    def __init__(self, allowed_user_id: int, summary: str, timeout: float = 120):
        super().__init__(timeout=timeout)
        self.allowed_user_id = allowed_user_id
        self.summary = summary
        self.result: bool | None = None
        self._event = asyncio.Event()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.allowed_user_id:
            await interaction.response.send_message("Not your action.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = True
        self._event.set()
        await interaction.response.edit_message(content=f"✅ **Confirmed**\n{self.summary}", view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = False
        self._event.set()
        await interaction.response.edit_message(content=f"❌ **Cancelled**\n{self.summary}", view=None)
        self.stop()

    async def on_timeout(self):
        self.result = False
        self._event.set()


def _make_discord_confirm(channel: discord.abc.Messageable, user_id: int):
    async def confirm(summary: str) -> bool:
        view = _ConfirmView(allowed_user_id=user_id, summary=summary)
        await channel.send(f"🟡 **Proposed action**\n```{summary}```", view=view)
        await view._event.wait()
        return view.result is True
    return confirm


async def _send_chunks(channel: discord.abc.Messageable, text: str):
    text = text.strip()
    if not text:
        return
    while text:
        chunk, text = text[:DISCORD_MSG_LIMIT], text[DISCORD_MSG_LIMIT:]
        await channel.send(chunk)


def _ids_from_env(env_name: str) -> set[int]:
    return {int(x) for x in os.environ.get(env_name, "").split(",") if x.strip()}


def run_discord(
    build_opts: BuildOptsFn,
    *,
    token: str | None = None,
    agent_name: str | None = None,
    allowed_user_ids: set[int] | None = None,
    allowed_channel_ids: set[int] | None = None,
    allowed_guild_ids: set[int] | None = None,
    session_max_age_seconds: int = 6 * 60 * 60,
    log_channel: str = "discord",
) -> None:
    """Run the Discord bot (blocks until killed).

    Args:
        build_opts: Per-session options factory. Called as
            `build_opts(sender_key="discord:<user_id>", sender_name=...)`.
        token: Discord bot token. Falls back to DISCORD_BOT_TOKEN.
        agent_name: Tag set in active_agent ContextVar before each turn so
            memory tools (remember / recall_about_me) scope correctly.
        allowed_user_ids: Set of Discord user IDs allowed to message the bot.
            Falls back to DISCORD_ALLOWED_USER_IDS (comma-separated).
        allowed_channel_ids: Set of channel IDs where the bot will respond
            without being mentioned. Falls back to DISCORD_ALLOWED_CHANNEL_IDS.
        allowed_guild_ids: Set of guild (server) IDs the bot is permitted to
            stay in. Empty = DM-only; bot auto-leaves on join. Falls back to
            DISCORD_ALLOWED_GUILD_IDS.
        session_max_age_seconds: Idle-session TTL. Default 6h.
        log_channel: Tag passed to cost_log.log_turn(). Default "discord".
    """
    if token is None:
        token = os.environ["DISCORD_BOT_TOKEN"]
    if allowed_user_ids is None:
        allowed_user_ids = _ids_from_env("DISCORD_ALLOWED_USER_IDS")
    if allowed_channel_ids is None:
        allowed_channel_ids = _ids_from_env("DISCORD_ALLOWED_CHANNEL_IDS")
    if allowed_guild_ids is None:
        allowed_guild_ids = _ids_from_env("DISCORD_ALLOWED_GUILD_IDS")

    intents = discord.Intents.default()
    intents.message_content = True
    intents.dm_messages = True
    bot = discord.Client(intents=intents)

    sessions: dict[int, ClaudeSDKClient] = {}
    session_meta: dict[int, dict] = {}
    locks: dict[int, asyncio.Lock] = {}

    def _get_lock(user_id: int) -> asyncio.Lock:
        if user_id not in locks:
            locks[user_id] = asyncio.Lock()
        return locks[user_id]

    async def _expire_session(user_id: int):
        if user_id in sessions:
            try:
                await sessions[user_id].disconnect()
            except Exception:
                pass
            sessions.pop(user_id, None)
            session_meta.pop(user_id, None)
            log.info(f"Expired session for user {user_id}")

    async def _get_session(user_id: int, user_name: str | None = None) -> ClaudeSDKClient:
        meta = session_meta.get(user_id, {})
        idle = time.time() - meta.get("last_used", 0)
        if user_id in sessions and idle > session_max_age_seconds:
            await _expire_session(user_id)

        if user_id not in sessions:
            options = build_opts(
                sender_key=f"discord:{user_id}",
                sender_name=user_name,
            )
            client = ClaudeSDKClient(options=options)
            await client.connect()
            sessions[user_id] = client
            session_meta[user_id] = {"last_used": time.time(), "turns": 0}
            log.info(f"Created Claude session for user {user_id}")
        else:
            session_meta[user_id]["last_used"] = time.time()
            session_meta[user_id]["turns"] += 1
        return sessions[user_id]

    async def handle_message(message: discord.Message):
        user_id = message.author.id
        channel = message.channel

        if allowed_user_ids and user_id not in allowed_user_ids:
            log.warning(f"Rejected message from unauthorized user {user_id} ({message.author})")
            return

        content = message.content.strip()
        if not content:
            return

        if content.lower() in ("/reset", "!reset"):
            if user_id in sessions:
                try:
                    await sessions[user_id].disconnect()
                except Exception:
                    pass
                sessions.pop(user_id, None)
            await channel.send("🔄 Conversation reset.")
            return

        lock = _get_lock(user_id)
        if lock.locked():
            await channel.send("⏳ Still working on your previous message — hang on.")
            return

        async with lock:
            async with channel.typing():
                confirm_cb_ctx.set(_make_discord_confirm(channel, user_id))
                if agent_name:
                    agent_context.active_agent.set(agent_name)
                agent_context.active_sender.set(f"discord:{user_id}")
                user_name = getattr(message.author, "display_name", None) or message.author.name
                client = await _get_session(user_id, user_name)

                try:
                    await client.query(content)
                    buffer = ""
                    image_paths: list[str] = []
                    async for msg in client.receive_response():
                        if isinstance(msg, AssistantMessage):
                            for block in msg.content:
                                if isinstance(block, TextBlock):
                                    buffer += block.text
                                elif isinstance(block, ToolUseBlock):
                                    log.info(f"tool: {block.name} {block.input}")
                        elif isinstance(msg, UserMessage):
                            for block in msg.content:
                                if isinstance(block, ToolResultBlock):
                                    for item in (block.content or []):
                                        text = item.get("text") if isinstance(item, dict) else None
                                        if text and IMAGE_MARKER in text:
                                            for line in text.splitlines():
                                                if line.startswith(IMAGE_MARKER):
                                                    image_paths.append(line[len(IMAGE_MARKER):].strip())
                        elif isinstance(msg, ResultMessage):
                            cleaned = "\n".join(
                                l for l in buffer.splitlines()
                                if IMAGE_MARKER not in l
                            ).strip()
                            if cleaned:
                                await _send_chunks(channel, cleaned)
                            for p in image_paths:
                                if os.path.exists(p):
                                    await channel.send(file=discord.File(p))
                            buffer = ""
                            image_paths = []
                            cost = msg.total_cost_usd or 0
                            log.info(f"user={user_id} turns={msg.num_turns} cost=${cost:.4f}")
                            try:
                                cost_log.log_turn(log_channel, user_id, msg.num_turns, cost, getattr(msg, "usage", None))
                            except Exception:
                                log.exception("cost_log failed")
                            break
                except Exception as e:
                    log.exception("Agent error")
                    await channel.send(f"⚠️ Error: `{type(e).__name__}: {e}`")

    async def _maybe_leave_guild(guild: "discord.Guild", context: str) -> None:
        if guild.id in allowed_guild_ids:
            log.info(f"{context}: in allowed guild {guild.name!r} (id={guild.id})")
            return
        log.warning(
            f"{context}: auto-leaving unauthorized guild {guild.name!r} (id={guild.id}, "
            f"owner_id={guild.owner_id}, members~{getattr(guild, 'member_count', '?')})"
        )
        try:
            await guild.leave()
        except Exception:
            log.exception(f"failed to leave guild {guild.id}")

    @bot.event
    async def on_ready():
        log.info(f"Logged in as {bot.user} (id={bot.user.id})")
        log.info(f"User allowlist: {allowed_user_ids or 'EMPTY (no users permitted)'}")
        log.info(
            f"Guild allowlist: {allowed_guild_ids or 'EMPTY (DM-only — will auto-leave any guild)'}"
        )
        for guild in list(bot.guilds):
            await _maybe_leave_guild(guild, "on_ready")

    @bot.event
    async def on_guild_join(guild: discord.Guild):
        await _maybe_leave_guild(guild, "on_guild_join")

    @bot.event
    async def on_message(message: discord.Message):
        if message.author == bot.user:
            return
        is_dm = isinstance(message.channel, discord.DMChannel)
        is_mention = bot.user in message.mentions
        is_allowed_channel = message.channel.id in allowed_channel_ids
        log.info(
            f"seen msg from {message.author} (id={message.author.id}) "
            f"channel={message.channel.id} dm={is_dm} mention={is_mention} "
            f"allowed_channel={is_allowed_channel}"
        )
        if not (is_dm or is_mention or is_allowed_channel):
            return
        await handle_message(message)

    @bot.event
    async def on_disconnect():
        log.warning("Gateway disconnected")

    @bot.event
    async def on_resumed():
        log.info("Gateway resumed")

    if not allowed_user_ids:
        log.warning("DISCORD_ALLOWED_USER_IDS is empty — nobody will be allowed to use the bot.")
    bot.run(token)
