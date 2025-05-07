import discord
from discord import app_commands
import math

def setup(bot):
    @bot.tree.command(name="aftertax", description="Calculate how much Robux to send to receive desired amount after 30% tax")
    @app_commands.describe(target="How much Robux do you want to receive *after* tax?")
    async def aftertax(interaction: discord.Interaction, target: int):
        if target <= 0:
            await interaction.response.send_message("❗ Target Robux must be greater than zero.")
            return
        sent = math.ceil(target / 0.7)
        await interaction.response.send_message(f"📬 To receive **{target} Robux**, send **{sent} Robux** (30% tax).")
