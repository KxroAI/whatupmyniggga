import discord
from discord import app_commands

def setup(bot):
    @bot.tree.command(name="beforetax", description="Calculate how much Robux you'll receive after 30% tax")
    @app_commands.describe(robux="How much Robux is being sent?")
    async def beforetax(interaction: discord.Interaction, robux: int):
        if robux <= 0:
            await interaction.response.send_message("❗ Robux amount must be greater than zero.")
            return
        received = math.floor(robux * 0.7)
        await interaction.response.send_message(f"📤 Sending **{robux} Robux** → You will receive **{received} Robux** after tax.")
