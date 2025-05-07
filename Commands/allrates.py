import discord
from discord import app_commands

def setup(bot):
    @bot.tree.command(name="allrates", description="See PHP equivalent across all rates for given Robux")
    @app_commands.describe(robux="How much Robux do you want to compare?")
    async def allrates(interaction: discord.Interaction, robux: int):
        if robux <= 0:
            await interaction.response.send_message("❗ Robux amount must be greater than zero.")
            return
        rates = {
            "Not Covered Tax (₱240)": 240,
            "Covered Tax (₱340)": 340,
            "Group Payout (₱320)": 320,
            "Gift (₱250)": 250
        }
        result = "\n".join([f"**{label}** → ₱{(value / 1000) * robux:.2f}" for label, value in rates.items()])
        await interaction.response.send_message(f"📊 **{robux} Robux Conversion:**\n{result}")
