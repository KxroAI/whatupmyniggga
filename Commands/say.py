import discord
from discord import app_commands

def setup(bot):
    @bot.tree.command(name="say", description="Make the bot say something in chat (no @everyone/@here allowed)")
    @app_commands.describe(message="Message for the bot to say")
    async def say(interaction: discord.Interaction, message: str):
        if "@everyone" in message or "@here" in message:
            await interaction.response.send_message(
                "❌ You cannot use `@everyone` or `@here` in the message.",
                ephemeral=True
            )
            return
        await interaction.response.send_message(message)
