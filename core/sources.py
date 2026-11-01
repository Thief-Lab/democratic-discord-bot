"""Resolve a user query (URL, search text, or local: path) into playable Track objects.

A Track is the in-memory representation of something that *could* be played. It
doesn't hit the DB itself - the player upserts it when it actually plays.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import yt_dlp


LOCAL_AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".webm"}

# Extensions we'll accept on a Discord attachment. Includes video containers
# because FFmpeg will happily strip the video track via -vn.
ATTACHMENT_PLAYABLE_EXTS = {
    ".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".webm",
    ".mp4", ".mov", ".mkv", ".avi", ".m4v", ".m4b", ".aac", ".wma", ".3gp",
}

# --- yt-dlp options -----------------------------------------------------------
_YDL_OPTS_SINGLE = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "default_search": "ytsearch1",
    "source_address": "0.0.0.0",
    "extractor_args": {"youtube": {"skip": ["hls", "dash"]}},
}

_YDL_OPTS_PLAYLIST = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": False,
    "extract_flat": "in_playlist",  # fast: don't resolve every entry up front
    "source_address": "0.0.0.0",
}


@dataclass
class Track:
    source_type: str            # 'youtube' | 'soundcloud' | 'local' | 'discord'
    source_id: str              # canonical id for dedup
    title: str
    duration_s: Optional[int]
    url: Optional[str]          # page URL (for display)
    stream_url: Optional[str]   # direct audio URL for FFmpeg (local: file path)
    requested_by: Optional[int] = None

    @property
    def is_local(self) -> bool:
        return self.source_type == "local"

    @property
    def needs_resolution(self) -> bool:
        """True if stream_url isn't populated yet (e.g. from a flat playlist entry)."""
        return not self.stream_url and not self.is_local


_SPOTIFY_TRACK_RE = re.compile(
    r"(?:open\.spotify\.com/track/|spotify:track:)([A-Za-z0-9]+)"
)
_SPOTIFY_PLAYLIST_RE = re.compile(
    r"(?:open\.spotify\.com/playlist/|spotify:playlist:)([A-Za-z0-9]+)"
)
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "music.youtube.com")
_SOUNDCLOUD_HOSTS = ("soundcloud.com",)

# A "pure" playlist URL we should expand. `watch?v=X&list=Y` is treated as a
# single video (user is requesting the specific video).
_PURE_PLAYLIST_RE = re.compile(
    r"(youtube\.com/playlist\?|youtube\.com/.*[?&]list=|soundcloud\.com/.+/sets/)",
    re.IGNORECASE,
)


def _is_url(s: str) -> bool:
    return bool(_URL_RE.match(s))


def _host_of(url: str) -> str:
    m = re.match(r"^https?://([^/]+)/", url + "/")
    return (m.group(1) if m else "").lower()


def _is_pure_playlist(url: str) -> bool:
    if not _is_url(url):
        return False
    # youtube.com/playlist?list=... - pure playlist page
    if "youtube.com/playlist" in url.lower():
        return True
    # youtube.com/watch?v=X&list=Y - NOT a pure playlist
    if "youtube.com/watch" in url.lower():
        return False
    # soundcloud.com/user/sets/... - pure playlist
    if "/sets/" in url.lower() and "soundcloud.com" in url.lower():
        return True
    return False


class Resolver:
    """Stateful resolver - keeps Spotify auth and local folder config."""

    def __init__(
        self,
        spotify_client_id: Optional[str],
        spotify_client_secret: Optional[str],
        local_music_dir: Optional[Path],
    ):
        self._local_dir = local_music_dir
        self._spotify = None
        if spotify_client_id and spotify_client_secret:
            try:
                import spotipy
                from spotipy.oauth2 import SpotifyClientCredentials

                self._spotify = spotipy.Spotify(
                    auth_manager=SpotifyClientCredentials(
                        client_id=spotify_client_id,
                        client_secret=spotify_client_secret,
                    )
                )
            except Exception:
                self._spotify = None

    @property
    def local_dir(self) -> Optional[Path]:
        return self._local_dir

    @property
    def local_enabled(self) -> bool:
        return self._local_dir is not None and self._local_dir.is_dir()

    # ---------- public API ----------

    async def resolve(self, query: str, requested_by: Optional[int] = None) -> List[Track]:
        """Turn a query into one or more Tracks. May hit the network."""
        q = query.strip()
        if not q:
            return []

        # local:<filename>
        if q.lower().startswith("local:"):
            return self._resolve_local(q[len("local:"):].strip(), requested_by)

        # Spotify track URL -> single YouTube search
        m = _SPOTIFY_TRACK_RE.search(q)
        if m:
            yt_query = await self._spotify_track_to_query(m.group(1))
            if yt_query is None:
                return []
            return await self._ytdlp_resolve(yt_query, requested_by, playlist=False)

        # Spotify playlist URL -> multiple YouTube searches
        m = _SPOTIFY_PLAYLIST_RE.search(q)
        if m:
            return await self._spotify_playlist_to_tracks(m.group(1), requested_by)

        # Pure playlist URL -> expand all entries
        if _is_url(q) and _is_pure_playlist(q):
            return await self._ytdlp_resolve(q, requested_by, playlist=True)

        # Any other URL, or free-text search
        return await self._ytdlp_resolve(q, requested_by, playlist=False)

    def resolve_attachment(
        self,
        attachment,  # discord.Attachment
        requested_by: Optional[int] = None,
    ) -> Optional[Track]:
        """Build a Track from a Discord message attachment.

        The CDN URL (`attachment.url`) is fed straight to FFmpeg - no download,
        no disk write. Discord-attachment Tracks are NOT re-resolvable: their
        signed CDN URLs expire after a few hours, so they can't participate in
        auto-cycle. They get a song_id and ratings as normal, but
        `resolve_song_row` will return None for them.
        """
        filename = getattr(attachment, "filename", "") or "audio"
        ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext and ext not in ATTACHMENT_PLAYABLE_EXTS:
            return None

        att_id = getattr(attachment, "id", None)
        url = getattr(attachment, "url", None)
        if att_id is None or not url:
            return None

        # Strip extension from the title for nicer display.
        title = filename.rsplit(".", 1)[0] if "." in filename else filename

        return Track(
            source_type="discord",
            source_id=str(att_id),
            title=title or "Attached audio",
            duration_s=None,
            url=url,             # the CDN URL is also the "page" here
            stream_url=url,      # FFmpeg can pull this directly over HTTPS
            requested_by=requested_by,
        )

    async def resolve_song_row(self, song, requested_by: Optional[int] = None) -> Optional[Track]:
        """Re-resolve a DB SongRow into a playable Track with a fresh stream URL.

        Used by the auto-cycle feature: stored stream URLs expire, so we have to
        re-hit yt-dlp for each play. For local files, we just reconstruct the path.
        Discord-attachment songs can't be re-resolved (CDN URLs expire), so they
        never auto-cycle.
        """
        if song.source_type == "discord":
            return None
        if song.source_type == "local":
            if not self.local_enabled:
                return None
            path = (self._local_dir / song.source_id).resolve()  # type: ignore[union-attr]
            if not path.is_file() or not self._is_under_local_dir(path):
                return None
            return Track(
                source_type="local",
                source_id=song.source_id,
                title=song.title,
                duration_s=song.duration_s,
                url=None,
                stream_url=str(path),
                requested_by=requested_by,
            )
        # youtube / soundcloud: prefer canonical URL, fall back to id-based URL.
        query = song.url
        if not query:
            if song.source_type == "youtube":
                query = f"https://www.youtube.com/watch?v={song.source_id}"
            elif song.source_type == "soundcloud":
                # We don't have the URL path, so best-effort: skip.
                return None
        tracks = await self._ytdlp_resolve(query, requested_by, playlist=False)
        return tracks[0] if tracks else None

    async def hydrate(self, track: Track) -> Optional[Track]:
        """Fill in stream_url for a flat-playlist Track before playback."""
        if not track.needs_resolution or not track.url:
            return track
        tracks = await self._ytdlp_resolve(track.url, track.requested_by, playlist=False)
        if not tracks:
            return None
        hydrated = tracks[0]
        # Preserve requested_by from the original
        hydrated.requested_by = track.requested_by
        return hydrated

    def list_local_files(self) -> List[Path]:
        """Return all audio files under LOCAL_MUSIC_DIR, recursively."""
        if not self.local_enabled:
            return []
        out: List[Path] = []
        for path in self._local_dir.rglob("*"):  # type: ignore[union-attr]
            if path.is_file() and path.suffix.lower() in LOCAL_AUDIO_EXTS:
                out.append(path)
        return out

    # ---------- local files ----------

    def _resolve_local(self, name: str, requested_by: Optional[int]) -> List[Track]:
        if not self.local_enabled:
            return []
        if not name:
            return []

        candidate = (self._local_dir / name).resolve()  # type: ignore[union-attr]
        if candidate.is_file() and self._is_under_local_dir(candidate):
            return [self._track_from_local_file(candidate, requested_by)]

        name_l = name.lower()
        best: Optional[Path] = None
        for path in self._local_dir.rglob("*"):  # type: ignore[union-attr]
            if not path.is_file() or path.suffix.lower() not in LOCAL_AUDIO_EXTS:
                continue
            stem = path.stem.lower()
            if name_l == stem or name_l in stem:
                best = path
                if name_l == stem:
                    break
        if best is not None:
            return [self._track_from_local_file(best, requested_by)]
        return []

    def _is_under_local_dir(self, path: Path) -> bool:
        if self._local_dir is None:
            return False
        try:
            path.relative_to(self._local_dir)
            return True
        except Exception:
            return False

    def _track_from_local_file(self, path: Path, requested_by: Optional[int]) -> Track:
        rel = str(path.relative_to(self._local_dir))  # type: ignore[arg-type]
        return Track(
            source_type="local",
            source_id=rel,
            title=path.stem,
            duration_s=None,
            url=None,
            stream_url=str(path),
            requested_by=requested_by,
        )

    # ---------- Spotify ----------

    async def _spotify_track_to_query(self, spotify_id: str) -> Optional[str]:
        if self._spotify is None:
            return None
        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(
                None, lambda: self._spotify.track(spotify_id)  # type: ignore[union-attr]
            )
        except Exception:
            return None
        return _spotify_track_to_search_string(data)

    async def _spotify_playlist_to_tracks(
        self, playlist_id: str, requested_by: Optional[int]
    ) -> List[Track]:
        if self._spotify is None:
            return []
        loop = asyncio.get_running_loop()
        queries: List[str] = []
        try:
            results = await loop.run_in_executor(
                None, lambda: self._spotify.playlist_items(playlist_id, limit=100)  # type: ignore[union-attr]
            )
        except Exception:
            return []
        while results:
            for item in results.get("items", []):
                track = item.get("track")
                q = _spotify_track_to_search_string(track)
                if q:
                    queries.append(q)
            if results.get("next"):
                try:
                    results = await loop.run_in_executor(
                        None, lambda: self._spotify.next(results)  # type: ignore[union-attr]
                    )
                except Exception:
                    break
            else:
                break

        # Each query becomes its own ytsearch1 - done sequentially so we don't
        # hammer YouTube all at once.
        tracks: List[Track] = []
        for q in queries:
            got = await self._ytdlp_resolve(q, requested_by, playlist=False)
            if got:
                tracks.append(got[0])
        return tracks

    # ---------- yt-dlp ----------

    async def _ytdlp_resolve(
        self, query: str, requested_by: Optional[int], *, playlist: bool
    ) -> List[Track]:
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, self._ytdlp_extract, query, playlist)
        if info is None:
            return []

        entries = info.get("entries")
        if entries:
            tracks: List[Track] = []
            for item in entries:
                if not item:
                    continue
                t = self._track_from_ytdlp(item, requested_by, flat=playlist)
                if t is not None:
                    tracks.append(t)
            return tracks

        t = self._track_from_ytdlp(info, requested_by, flat=False)
        return [t] if t is not None else []

    @staticmethod
    def _ytdlp_extract(query: str, playlist: bool) -> Optional[dict]:
        opts = _YDL_OPTS_PLAYLIST if playlist else _YDL_OPTS_SINGLE
        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                if _is_url(query):
                    return ydl.extract_info(query, download=False)
                return ydl.extract_info(f"ytsearch1:{query}", download=False)
            except Exception:
                return None

    @staticmethod
    def _track_from_ytdlp(
        info: dict, requested_by: Optional[int], *, flat: bool
    ) -> Optional[Track]:
        extractor = (info.get("extractor") or info.get("ie_key") or "").lower()
        webpage = info.get("webpage_url") or info.get("original_url") or info.get("url")
        host = _host_of(webpage) if webpage and _is_url(webpage) else ""

        if "soundcloud" in extractor or any(h in host for h in _SOUNDCLOUD_HOSTS):
            source_type = "soundcloud"
        elif "youtube" in extractor or any(h in host for h in _YOUTUBE_HOSTS):
            source_type = "youtube"
        else:
            source_type = "youtube"

        source_id = info.get("id")
        if not source_id:
            return None

        title = info.get("title") or "Unknown"
        duration = info.get("duration")
        try:
            duration_s = int(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration_s = None

        # When flat=True, we only have metadata - no stream_url. The player
        # will call resolver.hydrate() before playback.
        stream_url = None
        if not flat:
            stream_url = info.get("url")
            if not stream_url:
                formats = info.get("formats") or []
                audio_only = [
                    f for f in formats
                    if f.get("acodec") and f.get("acodec") != "none"
                    and (f.get("vcodec") == "none" or not f.get("vcodec"))
                ]
                if audio_only:
                    audio_only.sort(key=lambda f: f.get("abr") or 0, reverse=True)
                    stream_url = audio_only[0].get("url")

        # For flat playlist entries, webpage might just be an id; normalize to
        # a full watch URL so hydrate() can re-fetch.
        if flat and source_type == "youtube" and (not webpage or not _is_url(webpage)):
            webpage = f"https://www.youtube.com/watch?v={source_id}"

        return Track(
            source_type=source_type,
            source_id=str(source_id),
            title=title,
            duration_s=duration_s,
            url=webpage,
            stream_url=stream_url,
            requested_by=requested_by,
        )


def _spotify_track_to_search_string(data: Optional[dict]) -> Optional[str]:
    if not data:
        return None
    title = data.get("name", "")
    artists = ", ".join(a.get("name", "") for a in data.get("artists", []) if a)
    q = f"{artists} {title}".strip()
    return q or None
