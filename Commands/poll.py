import discord
from discord import app_commands
import asyncio

def setup(bot):
    @bot.tree.command(name="poll", description="Create a poll with reactions and result summary")
    @app_commands.describe(
        question="What is the poll question?",
        amount="Duration amount",
        unit="Time unit (seconds, minutes, hours)"
    )
    @app_commands.choices(unit=[
        app_commands.Choice(name="Seconds", value="seconds"),
        app_commands.Choice(name="Minutes", value="minutes"),
        app_commands.Choice(name="Hours", value="hours")
    ])
    async def poll(interaction: discord.Interaction, question: str, amount: int, unit: app_commands.Choice[str]):
        if amount <= 0:
            await interaction.response.send_message("❗ Amount must be greater than zero.", ephemeral=True)
            return
        total_seconds = {"seconds": amount, "minutes": amount * 60, "hours": amount * 3600}.get(unit.value, 0)
        if total_seconds == 0:
            await interaction.response.send_message("❗ Invalid time unit selected.", ephemeral=True)
            return
        if total_seconds > 86400:
            await interaction.response.send_message("❗ Duration cannot exceed 24 hours.", ephemeral=True)
            return
        embed = discord.Embed(title="📊 Poll", description=question, color=discord.Color.orange())
        embed.set_footer(text="Neroniel")
        embed.timestamp = discord.utils.utcnow()
        message = await interaction.channel.send(embed=embed)
        await message.add_reaction("👍")
        await message.add_reaction("👎")
        await interaction.response.send_message("✅ Poll created!", ephemeral=True)
        await asyncio.sleep(total_seconds)
        message = await interaction.channel.fetch_message(message.id)
        reactions = message.reactions
        up_count = next((r.count for r in reactions if str(r.emoji) == "👍"), 0)
        down_count = next((r.count for r in reactions if str(r.emoji) == "👎"), 0)
        if up_count > down_count:
            result = "👍 Upvotes win!"
        elif down_count > up_count:
            result = "👎 Downvotes win!"
        else:
            result = "⚖️ It's a tie!"
        result_embed = discord.Embed(title="📊 Poll Results", description=question, color=discord.Color.green())
        result_embed.add_field(name="👍 Upvotes", value=str(up_count), inline=True)
        result_embed.add_field(name="👎 Downvotes", value=str(down_count), inline=True)
        result_embed.add_field(name="Result", value=result, inline=False)
        result_embed.set_footer(text="Poll has ended")
        result_embed.timestamp = discord.utils.utcnow()
        await message.edit(embed=result_embed)
