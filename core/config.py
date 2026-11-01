"""Configuration loaded from environment / .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    discord_token: str
    spotify_client_id: Optional[str]
    spotify_client_secret: Optional[str]
    local_music_dir: Optional[Path]
    db_path: Path
    ffmpeg_path: Optional[str]

    @property
    def spotify_enabled(self) -> bool:
        return bool(self.spotify_client_id and self.spotify_client_secret)

    @property
    def local_enabled(self) -> bool:
        return self.local_music_dir is not None and self.local_music_dir.is_dir()


def load_config() -> Config:
    load_dotenv()

    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in."
        )

    local_dir_raw = os.getenv("LOCAL_MUSIC_DIR", "").strip()
    local_dir = Path(local_dir_raw).expanduser() if local_dir_raw else None

    db_path_raw = os.getenv("DB_PATH", "./data/bot.db").strip()
    db_path = Path(db_path_raw).expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    ffmpeg = os.getenv("FFMPEG_PATH", "").strip() or None

    return Config(
        discord_token=token,
        spotify_client_id=os.getenv("SPOTIFY_CLIENT_ID", "").strip() or None,
        spotify_client_secret=os.getenv("SPOTIFY_CLIENT_SECRET", "").strip() or None,
        local_music_dir=local_dir,
        db_path=db_path,
        ffmpeg_path=ffmpeg,
    )
