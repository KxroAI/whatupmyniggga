import discord
from discord import app_commands

def setup(bot):
    @bot.tree.command(name="nct", description="Convert Robux to PHP based on NCT rate (₱240/1k)")
    @app_commands.describe(robux="How much Robux do you want to convert?")
    async def nct(interaction: discord.Interaction, robux: int):
        if robux <= 0:
            await interaction.response.send_message("❗ Invalid input.")
            return
        php = robux * (240 / 1000)
        await interaction.response.send_message(f"💵 {robux} Robux = **₱{php:.2f} PHP**")
