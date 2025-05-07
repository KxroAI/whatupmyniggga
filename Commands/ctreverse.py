import discord
from discord import app_commands
import math

def setup(bot):
    @bot.tree.command(name="ctreverse", description="Convert PHP to Robux based on CT rate (₱340/1k)")
    @app_commands.describe(php="How much PHP do you want to convert?")
    async def ctreverse(interaction: discord.Interaction, php: float):
        if php <= 0:
            await interaction.response.send_message("❗ PHP amount must be greater than zero.")
            return
        robux = math.ceil((php / 340) * 1000)
        await interaction.response.send_message(f"💰 ₱{php:.2f} PHP = **{robux} Robux**")
