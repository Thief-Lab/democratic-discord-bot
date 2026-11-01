"""Per-guild voice playback and queue management, with rating-gated selection.

The rule for playing a song in a given guild is:

1. Gather user IDs currently in the bot's voice channel.
2. Retrieve their stored ratings for the song (if any).
3. Users who haven't rated the song count as NEUTRAL_DEFAULT (3.0).
4. If veto_enabled and any present user rated the song 1, skip.
5. If min_avg_rating > 0 and average < threshold, skip (auto-cycle only;
   explicit requests bypass the threshold but not the veto).

When the queue empties and auto_cycle_enabled is on, the player pulls random
songs from this guild's catalog, tries them through the rating gate, and
plays the first one that passes.

The currently-playing song is also re-evaluated whenever someone submits a
new rating, so a track can be skipped mid-playback if the room's verdict
drops below the threshold or a vetoer joins in.
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional, Set

import discord

from .database import Database, SongRow
from .sources import Resolver, Track


log = logging.getLogger("democratic.player")

NEUTRAL_DEFAULT = 3.0      # default vote for a present VC member who hasn't rated
REQUESTER_DEFAULT = 4.0    # default vote for the user who queued the song
AUTO_CYCLE_SAMPLE_SIZE = 20  # how many random songs to try when queue is empty

FFMPEG_BEFORE_OPTS = (
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
)
FFMPEG_OPTS = "-vn"


@dataclass
class QueueItem:
    track: Track
    song_id: Optional[int] = None


@dataclass
class PlaybackDecision:
    allowed: bool
    average: float
    reason: Optional[str] = None


@dataclass
class GuildPlayer:
    guild_id: int
    voice: discord.VoiceClient
    text_channel: discord.abc.Messageable
    bot_user_id: int
    loop: asyncio.AbstractEventLoop
    queue: List[QueueItem] = field(default_factory=list)
    current: Optional[QueueItem] = None
    now_playing_message: Optional[discord.Message] = None
    # recent song_ids played via auto-cycle, so we don't repeat immediately
    recent_auto_cycle_ids: List[int] = field(default_factory=list)
    _play_next_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Disconnect-after-idle timer: armed when nothing's playing, cancelled on start.
    _idle_timer: Optional[asyncio.Task] = None


class PlayerManager:
    """One of these per bot process. Tracks a GuildPlayer per active guild."""

    def __init__(
        self,
        db: Database,
        resolver: Resolver,
        ffmpeg_path: Optional[str],
        now_playing_builder: Callable[[GuildPlayer, QueueItem], Awaitable[discord.Message]],
    ):
        self.db = db
        self.resolver = resolver
        self.ffmpeg_path = ffmpeg_path
        self._players: dict[int, GuildPlayer] = {}
        self._now_playing_builder = now_playing_builder

    # ---------- lifecycle ----------

    def get(self, guild_id: int) -> Optional[GuildPlayer]:
        return self._players.get(guild_id)

    def register(self, player: GuildPlayer) -> None:
        self._players[player.guild_id] = player

    def drop(self, guild_id: int) -> None:
        gp = self._players.pop(guild_id, None)
        if gp is not None:
            self._cancel_idle_timer(gp)

    async def shutdown(self) -> None:
        for gp in list(self._players.values()):
            self._cancel_idle_timer(gp)
            try:
                if gp.voice.is_connected():
                    await gp.voice.disconnect(force=True)
            except Exception:
                log.exception("error during shutdown disconnect")
        self._players.clear()

    # ---------- idle timeout ----------

    def _cancel_idle_timer(self, gp: GuildPlayer) -> None:
        t = gp._idle_timer
        if t is not None and not t.done():
            t.cancel()
        gp._idle_timer = None

    def _arm_idle_timer(self, gp: GuildPlayer, seconds: int) -> None:
        self._cancel_idle_timer(gp)
        if seconds <= 0:
            return
        gp._idle_timer = asyncio.create_task(self._idle_disconnect(gp, seconds))

    async def _idle_disconnect(self, gp: GuildPlayer, seconds: int) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        # Re-check: don't leave if something started playing during the sleep,
        # or if the connection's already gone.
        if gp.current is not None or gp.queue:
            return
        if not gp.voice.is_connected():
            self.drop(gp.guild_id)
            return
        try:
            minutes = seconds // 60
            human = (
                f"{minutes} minute{'s' if minutes != 1 else ''}"
                if minutes
                else f"{seconds} seconds"
            )
            await gp.text_channel.send(
                f":zzz: Nothing's been playing for {human} - leaving voice. "
                "Run `/play` to bring me back."
            )
        except Exception:
            pass
        try:
            await gp.voice.disconnect(force=True)
        except Exception:
            log.exception("idle disconnect failed for guild %d", gp.guild_id)
        self.drop(gp.guild_id)

    # ---------- queueing ----------

    async def enqueue(self, guild_id: int, track: Track) -> None:
        gp = self._players.get(guild_id)
        if gp is None:
            raise RuntimeError("Player not joined to voice yet.")
        gp.queue.append(QueueItem(track=track))
        async with gp._play_next_lock:
            should_start = (
                gp.current is None
                and not gp.voice.is_playing()
                and not gp.voice.is_paused()
            )
        if should_start:
            await self.play_next(guild_id)

    async def enqueue_many(self, guild_id: int, tracks: List[Track]) -> None:
        gp = self._players.get(guild_id)
        if gp is None:
            raise RuntimeError("Player not joined to voice yet.")
        for t in tracks:
            gp.queue.append(QueueItem(track=t))
        async with gp._play_next_lock:
            should_start = (
                gp.current is None
                and not gp.voice.is_playing()
                and not gp.voice.is_paused()
            )
        if should_start:
            await self.play_next(guild_id)

    def shuffle_queue(self, guild_id: int) -> int:
        """Shuffle the pending queue (doesn't touch the currently playing song).

        Returns the number of items shuffled.
        """
        gp = self._players.get(guild_id)
        if gp is None:
            return 0
        random.shuffle(gp.queue)
        return len(gp.queue)

    def skip(self, guild_id: int) -> bool:
        gp = self._players.get(guild_id)
        if gp is None:
            return False
        if gp.voice.is_playing() or gp.voice.is_paused():
            gp.voice.stop()  # triggers the after callback -> play_next
            return True
        return False

    async def recheck_current_song(
        self, guild_id: int, song_id: int
    ) -> Optional[PlaybackDecision]:
        """Re-run the rating gate on the currently-playing song.

        Called when someone submits a new rating. If song_id matches what's
        playing AND the new room verdict fails the gate, the song is skipped
        (its title and reason are sent to the bound text channel first).

        Note: this uses is_explicit=False even if the song was originally an
        explicit request. Rationale: the explicit-request bypass exists so
        that requested songs get to start playing without threshold scrutiny,
        but once the room has actually weighed in with ratings, the room's
        verdict applies regardless of how the song got queued.

        Returns the PlaybackDecision if a recheck happened, or None if there
        was no current song or the song_ids don't match.
        """
        gp = self._players.get(guild_id)
        if gp is None or gp.current is None or gp.current.song_id != song_id:
            return None

        decision = await self._decide(
            gp,
            song_id,
            is_explicit=False,
            requested_by=gp.current.track.requested_by,
        )
        if decision.allowed:
            return decision

        try:
            await gp.text_channel.send(
                f":fast_forward: Skipping **{gp.current.track.title}** - {decision.reason}"
            )
        except Exception:
            pass
        # Stop playback; the after-callback fires play_next which moves on.
        if gp.voice.is_playing() or gp.voice.is_paused():
            gp.voice.stop()
        return decision

    # ---------- playback loop ----------

    async def play_next(self, guild_id: int) -> None:
        gp = self._players.get(guild_id)
        if gp is None:
            return
        async with gp._play_next_lock:
            gp.current = None

            # First, try the explicit queue
            played = await self._try_play_from_queue(gp)
            if played:
                return

            # Queue empty: maybe auto-cycle from this guild's catalog
            settings = await self.db.get_settings(gp.guild_id)
            if settings.auto_cycle_enabled:
                played = await self._try_auto_cycle(gp)
                if played:
                    return

            # Nothing to play -- arm the idle-disconnect timer.
            try:
                await gp.text_channel.send(":mailbox_with_no_mail: Queue is empty.")
            except Exception:
                pass
            self._arm_idle_timer(gp, settings.idle_timeout_s)

    async def _try_play_from_queue(self, gp: GuildPlayer) -> bool:
        while gp.queue:
            item = gp.queue.pop(0)

            # Hydrate flat-playlist tracks (fetched with extract_flat, missing stream_url)
            if item.track.needs_resolution:
                hydrated = await self.resolver.hydrate(item.track)
                if hydrated is None:
                    try:
                        await gp.text_channel.send(
                            f":warning: Couldn't resolve **{item.track.title}**, skipping."
                        )
                    except Exception:
                        pass
                    continue
                item.track = hydrated

            song_row = await self.db.upsert_song(
                guild_id=gp.guild_id,
                source_type=item.track.source_type,
                source_id=item.track.source_id,
                title=item.track.title,
                duration_s=item.track.duration_s,
                url=item.track.url,
            )
            item.song_id = song_row.id

            # Explicit requests bypass the average-threshold check -- if you ask
            # for it, you get it. Veto still applies (a "1" rating from anyone
            # in VC still kills it, regardless of how it got queued).
            decision = await self._decide(
                gp,
                item.song_id,
                is_explicit=True,
                requested_by=item.track.requested_by,
            )
            if not decision.allowed:
                try:
                    await gp.text_channel.send(
                        f":fast_forward: Skipping **{item.track.title}** - {decision.reason}"
                    )
                except Exception:
                    pass
                continue

            if await self._start_playback(gp, item):
                return True
        return False

    async def _try_auto_cycle(self, gp: GuildPlayer) -> bool:
        """Pick random songs from this guild's catalog until one passes rating gate."""
        exclude = list(gp.recent_auto_cycle_ids)
        candidates: List[SongRow] = await self.db.random_songs(
            gp.guild_id, AUTO_CYCLE_SAMPLE_SIZE, exclude_ids=exclude
        )
        if not candidates and exclude:
            # If we've excluded everything, let the recent-list drain and try again.
            gp.recent_auto_cycle_ids.clear()
            candidates = await self.db.random_songs(gp.guild_id, AUTO_CYCLE_SAMPLE_SIZE)

        if not candidates:
            return False

        for song in candidates:
            # Auto-cycle picks go through both gates -- the bot is choosing on
            # the room's behalf, so the threshold acts as the "is this popular
            # enough to be worth playing unprompted" filter. No requester here,
            # so no implicit 4/5 anchor; only present VC members count.
            decision = await self._decide(
                gp, song.id, is_explicit=False, requested_by=None
            )
            if not decision.allowed:
                continue
            track = await self.resolver.resolve_song_row(song)
            if track is None:
                continue
            item = QueueItem(track=track, song_id=song.id)
            if await self._start_playback(gp, item):
                # Remember this song so we don't repeat it in the next few cycles.
                gp.recent_auto_cycle_ids.append(song.id)
                # Keep the recent list bounded.
                max_recent = max(10, AUTO_CYCLE_SAMPLE_SIZE // 2)
                if len(gp.recent_auto_cycle_ids) > max_recent:
                    gp.recent_auto_cycle_ids.pop(0)
                return True
        return False

    async def _start_playback(self, gp: GuildPlayer, item: QueueItem) -> bool:
        gp.current = item
        # Something's about to play; cancel any pending idle disconnect.
        self._cancel_idle_timer(gp)
        try:
            self._start_ffmpeg(gp, item)
        except Exception:
            log.exception("failed to start ffmpeg for %s", item.track.title)
            gp.current = None
            return False
        try:
            gp.now_playing_message = await self._now_playing_builder(gp, item)
        except Exception:
            log.exception("failed to send Now Playing message")
        return True

    def _start_ffmpeg(self, gp: GuildPlayer, item: QueueItem) -> None:
        if not item.track.stream_url:
            raise RuntimeError(f"Track has no stream_url: {item.track.title}")

        kwargs: dict = {
            "before_options": FFMPEG_BEFORE_OPTS if not item.track.is_local else "",
            "options": FFMPEG_OPTS,
        }
        if self.ffmpeg_path:
            kwargs["executable"] = self.ffmpeg_path

        source = discord.FFmpegPCMAudio(item.track.stream_url, **kwargs)
        source = discord.PCMVolumeTransformer(source, volume=1.0)

        loop = gp.loop
        guild_id = gp.guild_id

        def _after(error: Optional[Exception]) -> None:
            if error:
                log.error("ffmpeg playback error: %s", error)
            try:
                asyncio.run_coroutine_threadsafe(self.play_next(guild_id), loop)
            except Exception:
                log.exception("failed to schedule play_next")

        gp.voice.play(source, after=_after)

    # ---------- rating-gated decision ----------

    async def _decide(
        self,
        gp: GuildPlayer,
        song_id: int,
        *,
        is_explicit: bool = False,
        requested_by: Optional[int] = None,
    ) -> PlaybackDecision:
        """Decide whether to play song_id given the room's ratings.

        Voting math
        -----------
        - The requester's vote always counts (the act of queueing is itself a
          vote of confidence). If they haven't rated explicitly, they default
          to REQUESTER_DEFAULT (4.0). If they have rated, that explicit rating
          is used. They count even if they're no longer in the voice channel.
        - Everyone else currently in VC also counts. Implicit default is
          NEUTRAL_DEFAULT (3.0); their explicit rating overrides if present.
        - Auto-cycle picks have no requester (the bot picked) -- pass
          requested_by=None and only present VC members are counted.

        Gate
        ----
        - is_explicit=True (user typed /play) bypasses the threshold check
          entirely; you asked for it, you get to start playing it. Veto
          still applies.
        - Veto fires only on votes from users currently in VC -- the
          requester's stored 1 from a different session shouldn't kill a
          track if they're not even here.
        """
        settings = await self.db.get_settings(gp.guild_id)

        present_user_ids = self._present_user_ids(gp)
        # Union of present users + requester. The requester is added even if
        # not in VC, so their implicit 4/5 anchors the average against a
        # single low vote.
        relevant_user_ids: Set[int] = set(present_user_ids)
        if requested_by is not None:
            relevant_user_ids.add(requested_by)

        ratings_by_user = await self.db.ratings_for_song_by_users(
            song_id, relevant_user_ids
        )

        scores: List[float] = []
        for uid in relevant_user_ids:
            if uid in ratings_by_user:
                scores.append(float(ratings_by_user[uid]))
            elif uid == requested_by:
                scores.append(REQUESTER_DEFAULT)
            else:
                scores.append(NEUTRAL_DEFAULT)
        average = sum(scores) / len(scores) if scores else NEUTRAL_DEFAULT

        # Veto check: only ratings from users currently in VC can veto. A
        # requester who rated 1 in a previous session and isn't here now
        # doesn't veto.
        if settings.veto_enabled:
            present_one_voters = [
                uid for uid in present_user_ids
                if ratings_by_user.get(uid) == 1
            ]
            if present_one_voters:
                return PlaybackDecision(
                    allowed=False,
                    average=average,
                    reason="a present rater gave it a 1 (veto mode)",
                )

        if (
            not is_explicit
            and settings.min_avg_rating > 0
            and average < settings.min_avg_rating
        ):
            return PlaybackDecision(
                allowed=False,
                average=average,
                reason=f"average {average:.2f} below threshold {settings.min_avg_rating:.2f}",
            )

        return PlaybackDecision(allowed=True, average=average)

    def _present_user_ids(self, gp: GuildPlayer) -> Set[int]:
        vc = gp.voice.channel
        if vc is None:
            return set()
        return {
            m.id for m in vc.members
            if not m.bot and m.id != gp.bot_user_id
        }
