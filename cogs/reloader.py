# cogs/reloader.py

import discord
from discord import app_commands

class Reloader(discord.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="reload", description="Reload a cog")
    @app_commands.describe(cog="Cog name to reload")
    async def reload_cog(self, interaction: discord.Interaction, cog: str):
        try:
            await self.bot.reload_extension(f"cogs.{cog}")
            await interaction.response.send_message(f"🔁 Reloaded cog: `{cog}`")
        except Exception as e:
            await interaction.response.send_message(f"❌ Failed to reload `{cog}`: {e}")


async def setup(bot):
    await bot.add_cog(Reloader(bot))
