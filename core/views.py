"""Discord UI components — Now Playing embed + 1-5 rating buttons + Skip."""
from __future__ import annotations

from typing import Optional

import discord

from .database import Database


def build_now_playing_embed(
    title: str,
    url: Optional[str],
    duration_s: Optional[int],
    requested_by_mention: Optional[str],
    average: float,
    rating_count: int,
) -> discord.Embed:
    embed = discord.Embed(
        title="Now Playing",
        description=f"[{title}]({url})" if url else f"**{title}**",
        color=discord.Color.blurple(),
    )
    if duration_s:
        mins, secs = divmod(int(duration_s), 60)
        embed.add_field(name="Length", value=f"{mins}:{secs:02d}", inline=True)
    if rating_count > 0:
        embed.add_field(
            name="Rating",
            value=f"{average:.2f} ★  ({rating_count} vote{'s' if rating_count != 1 else ''})",
            inline=True,
        )
    else:
        embed.add_field(name="Rating", value="no votes yet", inline=True)
    if requested_by_mention:
        embed.set_footer(text=f"Requested by {requested_by_mention}")
    embed.add_field(
        name="\u200b",
        value="Rate this song 1 – 5 below. Your rating is remembered for next time.",
        inline=False,
    )
    return embed


class RatingView(discord.ui.View):
    """View with 1..5 rating buttons and a Skip button for a specific song.

    Note: this view is NOT persisted across bot restarts. After a restart, the old
    Now Playing message's buttons will no longer respond — use `/nowplaying` to
    regenerate the view on the current track.
    """

    def __init__(self, db: Database, song_id: int):
        super().__init__(timeout=None)
        self.db = db
        self.song_id = song_id
        for n in range(1, 6):
            self.add_item(_RatingButton(db=db, song_id=song_id, value=n))
        self.add_item(_SkipButton(song_id=song_id))
        self.add_item(_RemoveButton(song_id=song_id))


def _update_embed_rating_field(
    embed: discord.Embed, average: Optional[float], count: int
) -> discord.Embed:
    rating_value = (
        f"{average:.2f} ★  ({count} vote{'s' if count != 1 else ''})"
        if average is not None
        else "no votes yet"
    )
    for i, field in enumerate(embed.fields):
        if field.name == "Rating":
            embed.set_field_at(i, name="Rating", value=rating_value, inline=True)
            break
    return embed


class _RatingButton(discord.ui.Button):
    def __init__(self, db: Database, song_id: int, value: int):
        style = {
            1: discord.ButtonStyle.danger,
            2: discord.ButtonStyle.danger,
            3: discord.ButtonStyle.secondary,
            4: discord.ButtonStyle.success,
            5: discord.ButtonStyle.success,
        }[value]
        super().__init__(
            label=str(value),
            style=style,
            custom_id=f"rate:{song_id}:{value}",
            row=0,
        )
        self._db = db
        self._song_id = song_id
        self._value = value

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._db.set_rating(interaction.user.id, self._song_id, self._value)
        summary = await self._db.rating_summary(self._song_id)

        msg = interaction.message
        if msg and msg.embeds:
            embed = msg.embeds[0]
            _update_embed_rating_field(embed, summary.average, summary.count)
            # Atomic: acknowledges the interaction AND updates the message in one call.
            await interaction.response.edit_message(embed=embed, view=self.view)
        else:
            await interaction.response.defer()

        await interaction.followup.send(
            f"Thanks - your rating: **{self._value}/5**.", ephemeral=True
        )

        # Notify the music cog so it can re-check the gate. If this rating just
        # dropped the song below threshold (or vetoed it), the cog will skip it.
        interaction.client.dispatch("rating_changed", interaction, self._song_id)


class _SkipButton(discord.ui.Button):
    """Signal that someone pressed Skip. The music cog listens for the event."""

    def __init__(self, song_id: int):
        super().__init__(
            label="Skip",
            style=discord.ButtonStyle.primary,
            custom_id=f"skip:{song_id}",
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        interaction.client.dispatch("rating_skip_pressed", interaction)
        await interaction.response.send_message("Skip requested.", ephemeral=True)


class _RemoveButton(discord.ui.Button):
    """Removes the song from this guild's master playlist (cascades to ratings).

    Cosmetic UX: the rating buttons keep working on the existing Now Playing
    message after a remove, but any new rating attempt will fail (the song row
    is gone, FK constraint). Acceptable edge case -- a fresh `/nowplaying` or
    the next sticky bump will create a new view bound to the next track.
    """

    def __init__(self, song_id: int):
        super().__init__(
            label="Remove",
            style=discord.ButtonStyle.danger,
            custom_id=f"remove:{song_id}",
            row=1,
        )
        self._song_id = song_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            ":wastebasket: Removing from this server's master playlist...",
            ephemeral=True,
        )
        interaction.client.dispatch(
            "rating_remove_pressed", interaction, self._song_id
        )
