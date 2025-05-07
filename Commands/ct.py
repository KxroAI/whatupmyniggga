import discord
from discord import app_commands

def setup(bot):
    @bot.tree.command(name="ct", description="Convert Robux to PHP based on CT rate (₱340/1k)")
    @app_commands.describe(robux="How much Robux do you want to convert?")
    async def ct(interaction: discord.Interaction, robux: int):
        if robux <= 0:
            await interaction.response.send_message("❗ Invalid input.")
            return
        php = robux * (340 / 1000)
        await interaction.response.send_message(f"💵 {robux} Robux = **₱{php:.2f} PHP**")
