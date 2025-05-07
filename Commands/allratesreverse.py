import discord
from discord import app_commands
import math

def setup(bot):
    @bot.tree.command(name="allratesreverse", description="See Robux equivalent across all rates for given PHP")
    @app_commands.describe(php="How much PHP do you want to compare?")
    async def allratesreverse(interaction: discord.Interaction, php: float):
        if php <= 0:
            await interaction.response.send_message("❗ PHP amount must be greater than zero.")
            return
        rates = {
            "Not Covered Tax (₱240)": 240,
            "Covered Tax (₱340)": 340,
            "Group Payout (₱320)": 320,
            "Gift (₱250)": 250
        }
        result = "\n".join([f"**{label}** → {math.ceil((php / value) * 1000)} Robux" for label, value in rates.items()])
        await interaction.response.send_message(f"📊 **₱{php:.2f} PHP Conversion:**\n{result}")
