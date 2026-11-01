"""Slash commands for playing music and managing the queue.

This cog also implements the "sticky" Now Playing behavior: whenever a user sends
a message in the channel the bot is bound to, the Now Playing embed + rating
buttons get re-posted at the bottom so they're always visible without scrolling.
It also leaves voice when nobody's left in the bot's voice channel, and re-runs
the rating gate when a user submits a new rating on the playing song.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core.database import Database
from core.player import GuildPlayer, PlayerManager, QueueItem
from core.sources import Resolver, Track
from core.views import RatingView, build_now_playing_embed


log = logging.getLogger("democratic.cogs.music")

# Minimum seconds between sticky reposts per guild.
STICKY_COOLDOWN_S = 4.0

# When the bot is alone in voice (no non-bot members), wait this long for someone
# to come back before disconnecting. Grace period is fixed (not configurable).
ALONE_GRACE_S = 60.0


class MusicCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        db: Database,
        resolver: Resolver,
        player_manager: PlayerManager,
    ):
        self.bot = bot
        self.db = db
        self.resolver = resolver
        self.players = player_manager
        self._last_bump_at: dict[int, float] = {}
        self._bump_tasks: dict[int, asyncio.Task] = {}
        self._bump_locks: dict[int, asyncio.Lock] = {}
        # Per-guild "leave when alone" timers.
        self._alone_tasks: dict[int, asyncio.Task] = {}

    async def _ensure_voice(
        self,
        interaction: discord.Interaction,
        *,
        deferred: bool,
    ) -> Optional[GuildPlayer]:
        send = (
            interaction.followup.send
            if deferred
            else interaction.response.send_message
        )

        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await send("This command only works inside a server.", ephemeral=True)
            return None

        member = interaction.user
        if member.voice is None or member.voice.channel is None:
            await send("You need to be in a voice channel first.", ephemeral=True)
            return None

        target_channel = member.voice.channel
        existing = self.players.get(interaction.guild.id)
        if existing and existing.voice.is_connected():
            if existing.voice.channel and existing.voice.channel.id == target_channel.id:
                return existing
            try:
                await existing.voice.move_to(target_channel)
            except Exception as e:
                log.exception("voice move_to failed")
                await send(f":x: Couldn't move to your voice channel: `{e!s}`", ephemeral=True)
                return None
            if isinstance(interaction.channel, discord.abc.Messageable):
                existing.text_channel = interaction.channel
            return existing

        try:
            voice = await target_channel.connect(self_deaf=True, timeout=20.0)
        except Exception as e:
            log.exception("voice connect failed")
            await send(
                f":x: Couldn't connect to voice: `{e!s}`\n"
                "Common causes: missing davey/PyNaCl, missing Connect/Speak permission, "
                "or network blocking Discord voice ports.",
                ephemeral=True,
            )
            return None
        bot_user = self.bot.user
        if bot_user is None:
            await send("Bot isn't fully ready yet - try again in a moment.", ephemeral=True)
            return None
        text_ch = interaction.channel
        if not isinstance(text_ch, discord.abc.Messageable):
            await send("This channel doesn't accept messages.", ephemeral=True)
            return None

        gp = GuildPlayer(
            guild_id=interaction.guild.id,
            voice=voice,
            text_channel=text_ch,
            bot_user_id=bot_user.id,
            loop=asyncio.get_running_loop(),
        )
        self.players.register(gp)
        return gp

    @app_commands.command(
        name="play",
        description="Queue a song or playlist. Works with YouTube, SoundCloud, Spotify, or local:<name>.",
    )
    @app_commands.describe(
        query="URL, search text, or local:filename",
        shuffle="If the query is a playlist, shuffle the tracks before queueing.",
    )
    async def play(
        self,
        interaction: discord.Interaction,
        query: str,
        shuffle: bool = False,
    ):
        await interaction.response.defer(thinking=True)
        gp = await self._ensure_voice(interaction, deferred=True)
        if gp is None:
            return

        try:
            tracks = await asyncio.wait_for(
                self.resolver.resolve(query, requested_by=interaction.user.id),
                timeout=60.0,
            )
        except asyncio.TimeoutError:
            await interaction.followup.send(
                f":x: Resolving `{query}` took too long. YouTube/Spotify might be slow, "
                "or yt-dlp may be out of date. Try again, or run `pip install -U yt-dlp`."
            )
            return
        except Exception as e:
            log.exception("resolver.resolve failed")
            await interaction.followup.send(f":x: Resolver error: `{e!s}`")
            return
        if not tracks:
            await interaction.followup.send(f":x: Couldn't find anything for `{query}`.")
            return

        if shuffle and len(tracks) > 1:
            random.shuffle(tracks)

        await self.players.enqueue_many(interaction.guild_id, tracks)  # type: ignore[arg-type]

        if len(tracks) == 1:
            await interaction.followup.send(f":musical_note: Queued **{tracks[0].title}**")
        else:
            shuf = " (shuffled)" if shuffle else ""
            await interaction.followup.send(
                f":musical_note: Queued {len(tracks)} tracks{shuf}."
            )

    @app_commands.command(
        name="playlocal",
        description="Play a local file. Omit query to shuffle ALL local files.",
    )
    @app_commands.describe(query="Optional: file name to search for. Leave blank to shuffle all.")
    async def playlocal(self, interaction: discord.Interaction, query: Optional[str] = None):
        await interaction.response.defer(thinking=True)
        if not self.resolver.local_enabled:
            await interaction.followup.send(
                ":x: Local playback isn't configured. Set `LOCAL_MUSIC_DIR` in `.env`."
            )
            return

        gp = await self._ensure_voice(interaction, deferred=True)
        if gp is None:
            return

        if query:
            tracks = await self.resolver.resolve(
                f"local:{query}", requested_by=interaction.user.id
            )
            if not tracks:
                await interaction.followup.send(f":x: No local file matches `{query}`.")
                return
            await self.players.enqueue_many(interaction.guild_id, tracks)  # type: ignore[arg-type]
            await interaction.followup.send(
                f":musical_note: Queued local file **{tracks[0].title}**."
            )
            return

        files = self.resolver.list_local_files()
        if not files:
            await interaction.followup.send(":x: No audio files found in your local music folder.")
            return

        random.shuffle(files)
        tracks = []
        for path in files:
            rel = str(path.relative_to(self.resolver.local_dir))  # type: ignore[arg-type]
            tracks.append(Track(
                source_type="local",
                source_id=rel,
                title=path.stem,
                duration_s=None,
                url=None,
                stream_url=str(path),
                requested_by=interaction.user.id,
            ))
        await self.players.enqueue_many(interaction.guild_id, tracks)  # type: ignore[arg-type]
        await interaction.followup.send(
            f":musical_note: Shuffled and queued **{len(tracks)}** local files."
        )

    @app_commands.command(
        name="playfile",
        description="Attach an audio or video file from your computer and play it.",
    )
    @app_commands.describe(file="Audio or video file (mp3, m4a, mp4, wav, flac, ogg, webm, mov, mkv, ...)")
    async def playfile(
        self,
        interaction: discord.Interaction,
        file: discord.Attachment,
    ):
        await interaction.response.defer(thinking=True)
        gp = await self._ensure_voice(interaction, deferred=True)
        if gp is None:
            return

        track = self.resolver.resolve_attachment(file, requested_by=interaction.user.id)
        if track is None:
            await interaction.followup.send(
                f":x: `{file.filename}` doesn't look like a playable audio/video file."
            )
            return

        await self.players.enqueue_many(interaction.guild_id, [track])  # type: ignore[arg-type]
        await interaction.followup.send(
            f":musical_note: Queued attached file **{track.title}**."
        )

    @app_commands.command(name="shuffle", description="Shuffle the current queue (not the playing song).")
    async def shuffle_cmd(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        n = self.players.shuffle_queue(interaction.guild.id)
        if n == 0:
            await interaction.response.send_message("Nothing in the queue to shuffle.", ephemeral=True)
        else:
            await interaction.response.send_message(
                f":twisted_rightwards_arrows: Shuffled **{n}** queued tracks."
            )

    @app_commands.command(name="skip", description="Skip the current song")
    async def skip(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        skipped = self.players.skip(interaction.guild.id)
        if skipped:
            await interaction.response.send_message(":fast_forward: Skipped.")
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)

    @app_commands.command(name="queue", description="Show the upcoming queue")
    async def queue(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        gp = self.players.get(interaction.guild.id)
        if gp is None or (gp.current is None and not gp.queue):
            await interaction.response.send_message("Queue is empty.", ephemeral=True)
            return
        lines = []
        if gp.current:
            lines.append(f"**Now:** {gp.current.track.title}")
        for i, item in enumerate(gp.queue[:15], start=1):
            lines.append(f"`{i}.` {item.track.title}")
        if len(gp.queue) > 15:
            lines.append(f"...and {len(gp.queue) - 15} more.")
        await interaction.response.send_message("\n".join(lines))

    @app_commands.command(name="nowplaying", description="Re-send the Now Playing message")
    async def nowplaying(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        gp = self.players.get(interaction.guild.id)
        if gp is None or gp.current is None:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self._repost_now_playing(gp)
        await interaction.followup.send("Re-sent.", ephemeral=True)

    @app_commands.command(
        name="playlist",
        description="Show this server's master playlist (paginated, sorted by avg rating).",
    )
    @app_commands.describe(page="Page number (default 1, 15 songs per page)")
    async def playlist(self, interaction: discord.Interaction, page: int = 1):
        if interaction.guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        PAGE_SIZE = 15
        total = await self.db.count_songs(interaction.guild.id)
        if total == 0:
            await interaction.response.send_message(
                "This server's master playlist is empty - play a few songs to populate it.",
                ephemeral=True,
            )
            return

        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(1, min(page, pages))
        offset = (page - 1) * PAGE_SIZE

        rows = await self.db.list_songs(
            interaction.guild.id, offset=offset, limit=PAGE_SIZE
        )

        lines = []
        for i, r in enumerate(rows, start=offset + 1):
            rating = f"{r.avg_rating:.2f}★" if r.avg_rating is not None else "  - "
            votes = f"({r.vote_count})"
            title = r.title if len(r.title) <= 60 else r.title[:57] + "..."
            if r.url:
                line = f"`{i:>3}.` {rating} {votes} [{title}]({r.url})"
            else:
                line = f"`{i:>3}.` {rating} {votes} {title}"
            lines.append(line)

        embed = discord.Embed(
            title=f"Master Playlist - page {page}/{pages}",
            description="\n".join(lines) if lines else "_(empty page)_",
            color=discord.Color.blurple(),
        )
        embed.set_footer(
            text=(
                f"{total} song{'s' if total != 1 else ''} total - "
                "use /playlist page:N to navigate"
            )
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="cycle",
        description="Start playing random songs from this server's catalog (turns auto-cycle on).",
    )
    async def cycle(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        gp = await self._ensure_voice(interaction, deferred=True)
        if gp is None:
            return

        total = await self.db.count_songs(interaction.guild.id)
        if total == 0:
            await interaction.followup.send(
                ":x: This server's catalog is empty. Play a few songs first to populate it, "
                "then `/cycle` will have something to pick from."
            )
            return

        # Make sure auto-cycle is enabled -- otherwise play_next would just stop
        # at "queue is empty" again.
        settings = await self.db.get_settings(interaction.guild.id)
        if not settings.auto_cycle_enabled:
            await self.db.set_auto_cycle(interaction.guild.id, True)
            await interaction.followup.send(
                f":repeat: Auto-cycle was off - turning it on. "
                f"Picking from this server's catalog ({total} songs)..."
            )
        else:
            await interaction.followup.send(
                f":repeat: Picking from this server's catalog ({total} songs)..."
            )

        # If something's already playing, leave it alone -- auto-cycle picks up
        # automatically when this song ends. Otherwise, kick off play_next now.
        if gp.current is None and not gp.voice.is_playing():
            await self.players.play_next(interaction.guild.id)

    @app_commands.command(name="leave", description="Disconnect the bot from voice")
    async def leave(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        gp = self.players.get(interaction.guild.id)
        if gp is None:
            await interaction.response.send_message("Not connected.", ephemeral=True)
            return
        try:
            await gp.voice.disconnect(force=True)
        finally:
            self._cancel_alone_timer(interaction.guild.id)
            self.players.drop(interaction.guild.id)
        await interaction.response.send_message(":wave: Left voice.")

    @commands.Cog.listener()
    async def on_rating_skip_pressed(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        self.players.skip(interaction.guild.id)

    @commands.Cog.listener()
    async def on_rating_changed(
        self, interaction: discord.Interaction, song_id: int
    ):
        """Re-run the gate live when someone rates the playing song down."""
        if interaction.guild is None:
            return
        try:
            await self.players.recheck_current_song(interaction.guild.id, song_id)
        except Exception:
            log.exception("rating recheck failed")

    @commands.Cog.listener()
    async def on_rating_remove_pressed(
        self, interaction: discord.Interaction, song_id: int
    ):
        """Remove the song from this guild's master playlist."""
        if interaction.guild is None:
            return

        # Grab the title before deleting (for the public confirmation message).
        try:
            song = await self.db.get_song(song_id)
        except Exception:
            log.exception("get_song failed during remove")
            song = None
        title = song.title if song else "(unknown)"

        # Refuse if the song belongs to another guild (shouldn't happen via UI,
        # but defense in depth — delete_song scopes by guild_id anyway).
        if song is not None and song.guild_id != interaction.guild.id:
            return

        try:
            deleted = await self.db.delete_song(interaction.guild.id, song_id)
        except Exception:
            log.exception("delete_song failed")
            return
        if not deleted:
            return  # already gone — silent

        # Public confirmation in the bound channel (or the channel the button
        # was clicked in, as a fallback).
        gp = self.players.get(interaction.guild.id)
        target = gp.text_channel if gp is not None else interaction.channel
        if target is not None:
            try:
                await target.send(
                    f":wastebasket: **{title}** was removed from the master playlist "
                    f"by {interaction.user.display_name}."
                )
            except Exception:
                log.exception("remove confirmation send failed")

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        """Leave voice if the bot is alone for ALONE_GRACE_S seconds."""
        if member.guild is None:
            return
        gp = self.players.get(member.guild.id)
        if gp is None or not gp.voice.is_connected():
            return
        bot_channel = gp.voice.channel
        if bot_channel is None:
            return

        # Did this state change touch our voice channel? If not, ignore.
        old_ch = before.channel
        new_ch = after.channel
        touches_ours = (
            (old_ch is not None and old_ch.id == bot_channel.id)
            or (new_ch is not None and new_ch.id == bot_channel.id)
        )
        if not touches_ours:
            return

        # Did the *bot itself* get disconnected externally? Clean up.
        if member.id == gp.bot_user_id and new_ch is None:
            self._cancel_alone_timer(gp.guild_id)
            self.players.drop(gp.guild_id)
            return

        # Count humans currently in the bot's voice channel.
        humans = sum(1 for m in bot_channel.members if not m.bot)
        if humans == 0:
            self._schedule_alone_leave(gp)
        else:
            self._cancel_alone_timer(gp.guild_id)

    def _schedule_alone_leave(self, gp: GuildPlayer) -> None:
        existing = self._alone_tasks.get(gp.guild_id)
        if existing and not existing.done():
            return  # already counting down
        self._alone_tasks[gp.guild_id] = asyncio.create_task(
            self._leave_after_grace(gp)
        )

    def _cancel_alone_timer(self, guild_id: int) -> None:
        t = self._alone_tasks.pop(guild_id, None)
        if t is not None and not t.done():
            t.cancel()

    async def _leave_after_grace(self, gp: GuildPlayer) -> None:
        try:
            await asyncio.sleep(ALONE_GRACE_S)
        except asyncio.CancelledError:
            return
        # Recheck before pulling the trigger -- someone might have rejoined
        # right at the last moment and the cancel raced us.
        if not gp.voice.is_connected():
            self.players.drop(gp.guild_id)
            return
        bot_channel = gp.voice.channel
        if bot_channel is not None:
            humans = sum(1 for m in bot_channel.members if not m.bot)
            if humans > 0:
                return
        try:
            await gp.text_channel.send(
                ":wave: Voice channel is empty - leaving. Run `/play` to bring me back."
            )
        except Exception:
            pass
        try:
            await gp.voice.disconnect(force=True)
        except Exception:
            log.exception("alone-leave disconnect failed for guild %d", gp.guild_id)
        self.players.drop(gp.guild_id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        gp = self.players.get(message.guild.id)
        if gp is None or gp.current is None:
            return
        if gp.now_playing_message is None:
            return
        np_channel_id = getattr(gp.text_channel, "id", None)
        if np_channel_id is None or message.channel.id != np_channel_id:
            return
        self._schedule_sticky_bump(gp)

    def _schedule_sticky_bump(self, gp: GuildPlayer) -> None:
        existing = self._bump_tasks.get(gp.guild_id)
        if existing and not existing.done():
            return
        self._bump_tasks[gp.guild_id] = asyncio.create_task(self._bump_after_delay(gp))

    async def _bump_after_delay(self, gp: GuildPlayer) -> None:
        now = time.monotonic()
        last = self._last_bump_at.get(gp.guild_id, 0.0)
        wait = max(0.5, STICKY_COOLDOWN_S - (now - last))
        try:
            await asyncio.sleep(wait)
            await self._repost_now_playing(gp)
            self._last_bump_at[gp.guild_id] = time.monotonic()
        except Exception:
            log.exception("sticky bump failed")
        finally:
            self._bump_tasks.pop(gp.guild_id, None)

    async def _repost_now_playing(self, gp: GuildPlayer) -> None:
        lock = self._bump_locks.setdefault(gp.guild_id, asyncio.Lock())
        async with lock:
            if gp.current is None or gp.current.song_id is None:
                return
            item = gp.current
            summary = await self.db.rating_summary(item.song_id)

            mention = None
            if item.track.requested_by and gp.voice and gp.voice.guild:
                member = gp.voice.guild.get_member(item.track.requested_by)
                if member is not None:
                    mention = member.display_name

            embed = build_now_playing_embed(
                title=item.track.title,
                url=item.track.url,
                duration_s=item.track.duration_s,
                requested_by_mention=mention,
                average=summary.average if summary.average is not None else 0.0,
                rating_count=summary.count,
            )
            view = RatingView(self.db, item.song_id)

            old = gp.now_playing_message
            try:
                new_msg = await gp.text_channel.send(embed=embed, view=view)
            except Exception:
                log.exception("failed to send new sticky Now Playing message")
                return
            gp.now_playing_message = new_msg
            if old is not None and old.id != new_msg.id:
                try:
                    await old.delete()
                except Exception:
                    pass


async def setup_now_playing(db: Database):
    """Returns a builder function the PlayerManager uses to post Now Playing messages."""

    async def builder(gp: GuildPlayer, item: QueueItem) -> discord.Message:
        assert item.song_id is not None
        summary = await db.rating_summary(item.song_id)
        mention = None
        if item.track.requested_by:
            guild = gp.voice.guild if gp.voice else None
            if guild is not None:
                member = guild.get_member(item.track.requested_by)
                if member is not None:
                    mention = member.display_name

        embed = build_now_playing_embed(
            title=item.track.title,
            url=item.track.url,
            duration_s=item.track.duration_s,
            requested_by_mention=mention,
            average=summary.average if summary.average is not None else 0.0,
            rating_count=summary.count,
        )
        view = RatingView(db, item.song_id)

        if gp.now_playing_message is not None:
            try:
                await gp.now_playing_message.delete()
            except Exception:
                pass
            gp.now_playing_message = None

        msg = await gp.text_channel.send(embed=embed, view=view)
        return msg

    return builder
