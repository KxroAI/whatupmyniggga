import discord
from discord import app_commands
from datetime import datetime, timedelta

def setup(bot):
    @bot.tree.command(name="remindme", description="Set a reminder after X minutes (will ping you in this channel)")
    @app_commands.describe(minutes="How many minutes until I remind you?", note="Your reminder message")
    async def remindme(interaction: discord.Interaction, minutes: int, note: str):
        if minutes <= 0:
            await interaction.response.send_message("❗ Please enter a positive number of minutes.", ephemeral=True)
            return
        reminder_time = datetime.utcnow() + timedelta(minutes=minutes)
        if hasattr(bot, 'reminders_collection') and bot.reminders_collection:
            bot.reminders_collection.insert_one({
                "user_id": interaction.user.id,
                "guild_id": interaction.guild.id,
                "channel_id": interaction.channel.id,
                "note": note,
                "reminder_time": reminder_time
            })
        await interaction.response.send_message(
            f"⏰ I'll remind you in `{minutes}` minutes: `{note}`",
            ephemeral=True
        )
