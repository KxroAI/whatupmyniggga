import discord
from discord import app_commands
import math

def setup(bot):
    @bot.tree.command(name="payoutreverse", description="Convert PHP to Robux based on Payout rate (₱320 for 1000 Robux)")
    @app_commands.describe(php="How much PHP do you want to convert?")
    async def payoutreverse(interaction: discord.Interaction, php: float):
        if php <= 0:
            await interaction.response.send_message("❗ PHP amount must be greater than zero.")
            return
        robux = math.ceil((php / 320) * 1000)
        await interaction.response.send_message(f"💰 ₱{php:.2f} PHP = **{robux} Robux**")
