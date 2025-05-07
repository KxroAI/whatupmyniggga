import discord
from discord import app_commands

def setup(bot):
    @bot.tree.command(name="payout", description="Convert Robux to PHP based on Payout rate (₱320 for 1000 Robux)")
    @app_commands.describe(robux="How much Robux do you want to convert?")
    async def payout(interaction: discord.Interaction, robux: int):
        if robux <= 0:
            await interaction.response.send_message("❗ Robux amount must be greater than zero.")
            return
        php = robux * (320 / 1000)
        await interaction.response.send_message(f"💵 {robux} Robux = **₱{php:.2f} PHP**")
