import discord
from discord import app_commands

def setup(bot):
    @bot.tree.command(name="purge", description="Delete a specified number of messages")
    @app_commands.describe(amount="How many messages would you like to delete?")
    async def purge(interaction: discord.Interaction, amount: int):
        if amount <= 0:
            await interaction.response.send_message("❗ Please specify a positive number of messages.", ephemeral=True)
            return
        BOT_OWNER_ID = 1163771452403761193
        has_permission = interaction.user.guild_permissions.manage_messages or interaction.user.id == BOT_OWNER_ID
        if not has_permission:
            await interaction.response.send_message("❗ You don't have permission to use this command.", ephemeral=True)
            return
        if not interaction.guild.me.guild_permissions.manage_messages:
            await interaction.response.send_message("❗ I don't have permission to delete messages.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(limit=amount)
        await interaction.followup.send(f"✅ Deleted **{len(deleted)}** messages.", ephemeral=True)
