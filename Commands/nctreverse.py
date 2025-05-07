import discord
from discord import app_commands
import math

def setup(bot):
    @bot.tree.command(name="nctreverse", description="Convert PHP to Robux based on NCT rate (₱240/1k)")
    @app_commands.describe(php="How much PHP do you want to convert?")
    async def nctreverse(interaction: discord.Interaction, php: float):
        if php <= 0:
            await interaction.response.send_message("❗ PHP amount must be greater than zero.")
            return
        robux = math.ceil((php / 240) * 1000)
        await interaction.response.send_message(f"💰 ₱{php:.2f} PHP = **{robux} Robux**")
