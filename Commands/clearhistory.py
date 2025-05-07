import discord
from discord import app_commands

def setup(bot):
    @bot.tree.command(name="clearhistory", description="Clear your AI conversation history")
    async def clearhistory(interaction: discord.Interaction):
        user_id = interaction.user.id
        # Clear local memory
        if user_id in bot.conversations:
            bot.conversations[user_id].clear()
        # Clear MongoDB history
        if hasattr(bot, 'conversations_collection') and bot.conversations_collection:
            bot.conversations_collection.delete_many({"user_id": user_id})
        await interaction.response.send_message("✅ Your AI conversation history has been cleared!", ephemeral=True)
