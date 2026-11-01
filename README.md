# Democratic Discord Bot

A Discord music bot that plays songs the room actually likes. Users rate songs 1–5 with buttons under the "Now Playing" message; the bot uses those ratings to decide what to play next for the people currently in voice chat.

## Features

- **Play from YouTube, SoundCloud, Spotify links, or a local folder.** Spotify tracks are resolved to YouTube for playback.
- **1–5 rating buttons** attached to every Now Playing message. Ratings are stored per-user per-song in SQLite and are updateable (a newer rating overwrites an older one). The Now Playing message also has a **Skip** button and a **Remove** button (the Remove button deletes the song from this server's master playlist — its ratings cascade with it).
- **Sticky Now Playing** — when chat buries the Now Playing message, the bot automatically deletes the old one and reposts it at the bottom so the rating buttons are always within reach. Debounced so it doesn't spam on rapid chat.
- **Democratic play rules** (configurable per server):
  - **Minimum average rating threshold** — only play songs whose average rating among users currently in the voice channel is at or above the threshold. Unrated songs count as a neutral 3.0 so new music still gets a chance.
  - **Veto mode** — if anyone currently in the voice channel rated the song a 1, it gets skipped automatically.
  - **Auto-cycle (master playlist mode)** — when the queue empties, the bot keeps playing by picking random songs from this server's catalog (everything anyone in *this* server has played), filtered through this server's rating rules.

Songs and ratings are scoped **per server**. Playing a song in one server doesn't add it to another server's catalog, and a 5-star rating in one server doesn't carry over to another — each server has its own taste.
- **Shuffle support** — shuffle a playlist as it's enqueued, shuffle the pending queue any time, or play a random shuffle of your local music folder.
- **Auto-disconnect** — the bot leaves voice on its own after a configurable idle timeout (default 5 minutes of nothing playing) or after a 60-second grace when it's the only one left in the channel. Either condition keeps the bot from sitting in voice forever.
- **Slash commands** for play/skip/queue and for adjusting the server's rating settings.

## Requirements

- **Python 3.10+**
- **FFmpeg** on your system PATH (or set `FFMPEG_PATH` in `.env`).
  - Windows: `winget install Gyan.FFmpeg` or grab a build from https://www.gyan.dev/ffmpeg/builds/
  - macOS: `brew install ffmpeg`
  - Linux: `sudo apt install ffmpeg`
- A **Discord bot application** and token from https://discord.com/developers/applications
  - Enable the `Server Members` and `Message Content` intents on the Bot page.
  - Invite the bot to your server with at least these scopes/permissions: `bot`, `applications.commands`, `Connect`, `Speak`, `Send Messages`, `Embed Links`, `Use Slash Commands`.
- (Optional) **Spotify API credentials** if you want Spotify link support. https://developer.spotify.com/dashboard
- (Optional) A local folder of audio files if you want to play music directly off your machine. Set `LOCAL_MUSIC_DIR` in `.env`.

## Setup

```bash
# 1. Clone / open this folder, then:
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt

# 2. Create your .env from the template and fill in DISCORD_TOKEN
copy .env.example .env        # Windows
# cp .env.example .env        # macOS/Linux

# 3. Run it
python bot.py
```

First run creates `./data/bot.db` automatically.

## Commands

| Command | What it does |
|---|---|
| `/play <query> [shuffle]` | Play/queue a YouTube search, URL, SoundCloud link, or Spotify track/playlist link. If `shuffle:true` and the query resolves to multiple tracks, the playlist is shuffled before it's enqueued. |
| `/playfile <file>` | Attach an audio or video file from your computer (mp3, m4a, mp4, wav, flac, ogg, webm, mov, mkv, …) and play it. The bot streams it from Discord's CDN — no download to disk. |
| `/playlocal [query]` | Play from your local music folder on the *bot host* (set `LOCAL_MUSIC_DIR` in `.env`). Provide a filename/substring to pick a specific track; leave blank to shuffle-queue every audio file in the folder. |
| `/skip` | Skip the current song. |
| `/shuffle` | Shuffle the pending queue (the currently playing song stays). |
| `/cycle` | Start the master playlist immediately — turns auto-cycle on (if it isn't already) and begins picking random tracks from this server's catalog. |
| `/queue` | Show what's queued. |
| `/playlist [page]` | Show this server's master playlist, paginated (15 songs per page), sorted by average rating. |
| `/nowplaying` | Re-send the Now Playing embed (with rating buttons). |
| `/leave` | Disconnect from voice. |
| `/settings threshold <0.0-5.0>` | Set the minimum average rating required to play a song. `0` disables. |
| `/settings veto <on/off>` | Toggle: skip any song that *anyone in the VC* rated a 1. |
| `/settings autocycle <on/off>` | When the queue empties, keep going by pulling random songs from this server's catalog (everything anyone in this server has played). |
| `/settings idle <minutes>` | Minutes of nothing-playing before the bot leaves voice. `0` disables; max 120. Default is **5**. |
| `/settings show` | Show this server's current threshold, veto, auto-cycle, idle timeout, and how many songs are in this server's catalog. |
| `/stats` | Show rating stats for the current song. |

## How the rating math works

When the bot considers whether to play a song:

1. Build the set of users whose votes count: everyone currently in the bot's voice channel, **plus the user who queued the song** (even if they're no longer in VC — the act of queueing is itself a vote).
2. Look up each of their stored ratings for the song.
3. Apply implicit defaults to anyone who hasn't rated:
    - The **requester defaults to 4.0** — choosing to play a song is treated as a soft +1 from them. (If they've rated explicitly, that rating is used instead.)
    - Everyone else defaults to **3.0 (neutral)** — a song nobody in the room has rated will play.
4. If **veto mode is on** and any user *currently in VC* rated the song a **1**, the song is skipped regardless of the average. Veto applies to both explicit `/play` requests and auto-cycle picks. (A requester who isn't in VC and rated 1 in a previous session doesn't veto — only present-rater 1s veto.)
5. If the average is **below the threshold**, the song is skipped — but **only when the bot picked the song itself** (auto-cycle). Songs you explicitly `/play` bypass the threshold check, on the principle that an explicit request is itself a vote of confidence.

The requester-implicit-4 keeps a single low rating from killing the song against thin attendance. With threshold 2.5, two people in VC (one of whom is the requester), and a non-requester rating 2: the average is `(2 + 4) / 2 = 3.0`, comfortably above threshold. Without the requester anchor, that single 2 would have averaged 2.0 (with the requester counted as a neutral 3, just `(2 + 3) / 2 = 2.5` — which is right at the threshold).

This gate also fires **live, mid-playback**: every time someone clicks a 1–5 button, the bot recomputes the room's verdict on the currently-playing song. If the new rating drops the average below the threshold (or triggers a veto), the song is skipped immediately rather than waiting for it to finish. Note that this re-evaluation uses the auto-cycle interpretation — i.e., the explicit-request bypass applies only at queue time. Once a song is playing and the room weighs in with actual ratings, the room's verdict applies regardless of how the song was queued.

This means:

- A song one person loves (5) and nobody else has rated still averages 5 — the room will hear it.
- A song one person has rated 1 and a thousand people haven't rated averages 2.6 — below a default 3.0 threshold, it won't play. (And with veto mode on, the single 1 alone kills it.)
- As more people rate, the average gets more representative.

## How auto-cycle works

Turn it on with `/settings autocycle on`. When the explicit queue empties, the bot pulls a small batch of random songs from **this server's** catalog (the `songs` table, scoped to this guild — populated by everything anyone has ever played in this server via this bot) and tries them one by one through the rating gate. The first one that passes plays. Recently auto-cycled songs are held back for a few cycles so you don't hear the same track twice in quick succession.

If a song in the master playlist has a stale stream URL (YouTube URLs expire), the bot re-resolves it fresh before playing.

## Auto-disconnect

The bot leaves voice on its own under two conditions:

- **Idle timeout.** If nothing has played for the configured number of minutes (default **5**, change with `/settings idle <minutes>`, set to `0` to disable), the bot disconnects. The timer is armed every time the queue empties (and auto-cycle has nothing to play either) and is cancelled the moment a new song starts.
- **Empty voice channel.** If everyone leaves the bot's voice channel, the bot waits **60 seconds** to absorb momentary disconnects, then leaves on its own. Not configurable — there's no use case for the bot babysitting an empty channel.

Either condition triggers a brief "leaving voice" message in the bound text channel before the disconnect, so it's never silent about why it left. Run `/play` to bring it back.

## Sticky Now Playing

The rating buttons are only useful if people can see them. The bot watches the text channel it posted the Now Playing embed in, and if chat has buried it by more than a handful of messages, it deletes the old embed and reposts a fresh one at the bottom — same song, same buttons, no duplication. This is debounced (a few seconds of cooldown) so a busy chat doesn't cause the embed to flap.

## Playing files from your computer

Two different "local" workflows:

**`/playfile` — attach a file to the slash command (most people want this).**
Click the slash command, attach an audio or video file from your machine, send. The bot plays it through Discord's CDN. No setup required — works for everyone in the server. Songs played this way *can* be rated like any other song, but they don't auto-cycle (Discord's CDN URLs expire after a few hours).

**`/playlocal` — bot-host music folder.**
For when *you* (the bot operator) want to expose a music library on the bot's machine. Set `LOCAL_MUSIC_DIR` in `.env` to a folder of audio files. Subfolders are scanned recursively. Then:

- `/playlocal query:<name>` to search for a specific file (substring match on the filename).
- `/playlocal` with no query to queue every file, shuffled.
- `/play local:<filename>` also works as a single-track shortcut.

## Project layout

```
Democratic Discord Bot/
├── bot.py                 # entry point — loads env, starts the bot
├── requirements.txt
├── .env.example
├── .gitignore
├── README.md
├── core/
│   ├── __init__.py
│   ├── config.py          # loads .env into a Config dataclass
│   ├── database.py        # async SQLite wrapper + schema + migrations
│   ├── sources.py         # YouTube / SoundCloud / Spotify / local resolvers
│   ├── player.py          # per-guild queue + rating-gated playback + auto-cycle
│   └── views.py           # Discord button UI for Now Playing
└── cogs/
    ├── __init__.py
    ├── music.py           # /play /playlocal /skip /shuffle /queue /nowplaying /leave + sticky
    └── settings.py        # /settings threshold|veto|autocycle|show + /stats
```

## Troubleshooting

- **"Ffmpeg not found"** — install FFmpeg (see Requirements) or set `FFMPEG_PATH` in `.env`.
- **"Missing Permissions"** — the bot needs `Connect` and `Speak` in the voice channel and `Send Messages` / `Embed Links` / `Manage Messages` in the text channel. (`Manage Messages` is what lets the sticky behavior delete its old Now Playing embeds; without it, you'll just get a growing pile of embeds instead of one at the bottom.)
- **"This interaction failed"** on buttons — usually means the bot was restarted and the old Now Playing message's view is no longer attached. Use `/nowplaying` to regenerate.
- **Spotify links don't work** — fill in `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` in `.env`.
- **`/playlocal` says "nothing found"** — check `LOCAL_MUSIC_DIR` in `.env` and that the folder actually contains audio files (`.mp3`, `.flac`, `.m4a`, `.opus`, `.ogg`, `.wav`).
- **Auto-cycle never picks anything** — either this server's catalog is empty (play a few songs *in this server* first to populate it — songs played in other servers don't count), or your threshold is so high that nothing passes. Lower it with `/settings threshold` or check `/settings show` to see how many songs are in this server's catalog.
