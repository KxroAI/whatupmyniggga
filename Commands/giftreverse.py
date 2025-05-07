import discord
from discord import app_commands
import math

def setup(bot):
    @bot.tree.command(name="giftreverse", description="Convert PHP to Robux based on Gift rate (₱250 for 1000 Robux)")
    @app_commands.describe(php="How much PHP do you want to convert?")
    async def giftreverse(interaction: discord.Interaction, php: float):
        if php <= 0:
            await interaction.response.send_message("❗ PHP amount must be greater than zero.")
            return
        robux = math.ceil((php / 250) * 1000)
        await interaction.response.send_message(f"🎉 ₱{php:.2f} PHP = **{robux} Robux**")
