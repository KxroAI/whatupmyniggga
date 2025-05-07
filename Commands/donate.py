import discord
from discord import app_commands

def setup(bot):
    @bot.tree.command(name="donate", description="Donate Robux to a Discord user. (Only for fun!)")
    @app_commands.describe(user="The Discord user to donate to.", amount="The amount of Robux to donate.")
    async def donate(interaction: discord.Interaction, user: discord.Member, amount: int):
        if amount <= 0:
            await interaction.response.send_message("❗ Robux amount must be greater than zero.")
            return
        await interaction.response.send_message(
            f"`{interaction.user.name}` just donated **{amount:,} Robux** to {user.mention}!"
        )
