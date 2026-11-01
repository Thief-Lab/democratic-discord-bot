"""Democratic Discord Bot - entry point."""
from __future__ import annotations

import asyncio
import logging
import sys

import discord
from discord.ext import commands

from core.config import load_config
from core.database import Database
from core.player import PlayerManager
from core.sources import Resolver
from cogs.music import MusicCog, setup_now_playing
from cogs.settings import SettingsCog, StatsCog


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-24s  %(levelname)-7s  %(message)s",
)
log = logging.getLogger("democratic.bot")


async def main() -> None:
    config = load_config()

    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    intents.voice_states = True

    bot = commands.Bot(command_prefix="!", intents=intents)

    db = Database(config.db_path)
    await db.connect()

    resolver = Resolver(
        spotify_client_id=config.spotify_client_id,
        spotify_client_secret=config.spotify_client_secret,
        local_music_dir=config.local_music_dir,
    )

    # PlayerManager needs a callback that posts Now Playing messages when a new
    # song starts. setup_now_playing returns that callback, closing over `db`.
    now_playing_builder = await setup_now_playing(db)

    player_manager = PlayerManager(
        db=db,
        resolver=resolver,
        ffmpeg_path=config.ffmpeg_path,
        now_playing_builder=now_playing_builder,
    )

    await bot.add_cog(MusicCog(bot, db, resolver, player_manager))
    await bot.add_cog(SettingsCog(bot, db))
    await bot.add_cog(StatsCog(bot, db))

    @bot.event
    async def on_ready():
        log.info("Logged in as %s (id=%s)", bot.user, bot.user.id if bot.user else "?")

        # Per-guild sync is INSTANT (vs. global sync which can take up to an
        # hour to propagate). For each guild the bot is currently in, copy
        # the global command tree into the guild's namespace and sync it.
        # New commands show up the next time someone opens the slash menu.
        for guild in bot.guilds:
            try:
                bot.tree.copy_global_to(guild=guild)
                synced = await bot.tree.sync(guild=guild)
                log.info(
                    "Synced %d commands to guild %s (%d)",
                    len(synced), guild.name, guild.id,
                )
            except Exception:
                log.exception("per-guild sync failed for %s (%d)", guild.name, guild.id)

        # Also sync globally, so any guild the bot joins later eventually
        # picks the commands up (without us having to be online when it joins).
        try:
            synced = await bot.tree.sync()
            log.info("Synced %d global slash commands", len(synced))
        except Exception:
            log.exception("global slash command sync failed")

    try:
        await bot.start(config.discord_token)
    finally:
        await player_manager.shutdown()
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down.")
        sys.exit(0)
