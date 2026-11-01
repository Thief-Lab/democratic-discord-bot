"""Per-guild settings: rating threshold, veto mode, auto-cycle, idle timeout, and stats."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from core.database import Database


class SettingsCog(commands.GroupCog, group_name="settings"):
    """Slash command group: /settings threshold | veto | autocycle | idle | show"""

    def __init__(self, bot: commands.Bot, db: Database):
        self.bot = bot
        self.db = db
        super().__init__()

    @app_commands.command(
        name="threshold",
        description="Minimum average rating required to play a song (0.0 disables, max 5.0).",
    )
    @app_commands.describe(value="Number between 0.0 and 5.0. 0 = no threshold.")
    async def threshold(self, interaction: discord.Interaction, value: float):
        if interaction.guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        if not 0.0 <= value <= 5.0:
            await interaction.response.send_message(
                "Value must be between 0.0 and 5.0.", ephemeral=True
            )
            return
        await self.db.set_threshold(interaction.guild.id, value)
        if value == 0:
            await interaction.response.send_message(
                ":ok_hand: Threshold disabled - any song will play."
            )
        else:
            await interaction.response.send_message(
                f":ok_hand: New threshold: **{value:.2f}**. Songs averaging below that "
                "(among people in the voice channel) will be skipped."
            )

    @app_commands.command(
        name="veto",
        description="When on, any song rated 1 by a present user is skipped.",
    )
    @app_commands.describe(enabled="on or off")
    @app_commands.choices(
        enabled=[
            app_commands.Choice(name="on", value="on"),
            app_commands.Choice(name="off", value="off"),
        ]
    )
    async def veto(
        self,
        interaction: discord.Interaction,
        enabled: app_commands.Choice[str],
    ):
        if interaction.guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        is_on = enabled.value == "on"
        await self.db.set_veto(interaction.guild.id, is_on)
        await interaction.response.send_message(
            f":shield: Veto mode: **{'on' if is_on else 'off'}**."
        )

    @app_commands.command(
        name="autocycle",
        description="When queue empties, keep playing random songs from this server's catalog.",
    )
    @app_commands.describe(enabled="on or off")
    @app_commands.choices(
        enabled=[
            app_commands.Choice(name="on", value="on"),
            app_commands.Choice(name="off", value="off"),
        ]
    )
    async def autocycle(
        self,
        interaction: discord.Interaction,
        enabled: app_commands.Choice[str],
    ):
        if interaction.guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        is_on = enabled.value == "on"
        await self.db.set_auto_cycle(interaction.guild.id, is_on)
        total = await self.db.count_songs(interaction.guild.id)
        if is_on:
            await interaction.response.send_message(
                f":repeat: Auto-cycle: **on**. When the queue empties, I'll play random songs "
                f"from this server's catalog ({total} known) that pass this server's rating rules."
            )
        else:
            await interaction.response.send_message(
                ":repeat: Auto-cycle: **off**. The bot will stop when the queue empties."
            )

    @app_commands.command(
        name="idle",
        description="Minutes of nothing-playing before the bot leaves voice. 0 disables.",
    )
    @app_commands.describe(minutes="0 disables auto-leave. Max 120 (2 hours).")
    async def idle(self, interaction: discord.Interaction, minutes: int):
        if interaction.guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        if not 0 <= minutes <= 120:
            await interaction.response.send_message(
                "Minutes must be between 0 and 120.", ephemeral=True
            )
            return
        await self.db.set_idle_timeout(interaction.guild.id, minutes * 60)
        if minutes == 0:
            await interaction.response.send_message(
                ":zzz: Idle auto-leave disabled. (The bot will still leave when nobody's "
                "in the voice channel with it.)"
            )
        else:
            await interaction.response.send_message(
                f":zzz: Idle timeout: **{minutes} minute"
                f"{'s' if minutes != 1 else ''}**. If nothing's playing for that long, "
                "the bot leaves voice."
            )

    @app_commands.command(name="show", description="Show this server's current settings.")
    async def show(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        s = await self.db.get_settings(interaction.guild.id)
        total = await self.db.count_songs(interaction.guild.id)
        thresh = f"{s.min_avg_rating:.2f}" if s.min_avg_rating > 0 else "disabled"
        veto = "on" if s.veto_enabled else "off"
        auto = "on" if s.auto_cycle_enabled else "off"
        if s.idle_timeout_s <= 0:
            idle_disp = "disabled"
        else:
            mins = s.idle_timeout_s // 60
            idle_disp = (
                f"{mins} min" if mins and s.idle_timeout_s % 60 == 0
                else f"{s.idle_timeout_s} sec"
            )
        await interaction.response.send_message(
            f"**Threshold:** {thresh}\n"
            f"**Veto mode:** {veto}\n"
            f"**Auto-cycle:** {auto}\n"
            f"**Idle timeout:** {idle_disp}\n"
            f"**Songs in this server's catalog:** {total}"
        )


class StatsCog(commands.Cog):
    """Top-level /stats command (separate from the settings group)."""

    def __init__(self, bot: commands.Bot, db: Database):
        self.bot = bot
        self.db = db

    @app_commands.command(name="stats", description="Show rating stats for the current song.")
    async def stats(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        music_cog = self.bot.get_cog("MusicCog")
        if music_cog is None:
            await interaction.response.send_message("Music isn't loaded.", ephemeral=True)
            return
        gp = music_cog.players.get(interaction.guild.id)  # type: ignore[attr-defined]
        if gp is None or gp.current is None or gp.current.song_id is None:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        summary = await self.db.rating_summary(gp.current.song_id)
        title = gp.current.track.title
        if summary.count == 0:
            await interaction.response.send_message(f"**{title}** - no votes yet.")
            return
        await interaction.response.send_message(
            f"**{title}**\n"
            f"Votes: {summary.count}\n"
            f"Average: {summary.average:.2f} stars\n"
            f"Range: {summary.lowest} - {summary.highest}"
        )
