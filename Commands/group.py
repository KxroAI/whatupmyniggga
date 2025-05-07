import discord
from discord import app_commands
import requests

def setup(bot):
    @bot.tree.command(name="group", description="Display information about the 1cy Roblox group")
    async def groupinfo(interaction: discord.Interaction):
        group_id = 5838002
        try:
            response = requests.get(f"https://groups.roblox.com/v1/groups/{group_id}")
            data = response.json()
            formatted_members = "{:,}".format(data['memberCount'])
            embed = discord.Embed(color=discord.Color.blue())
            embed.add_field(name="Group Name", value=f"[{data['name']}](https://www.roblox.com/groups/{group_id})", inline=False)
            embed.add_field(name="Description", value=f"```\n{data['description'] or 'No description'}\n```", inline=False)
            embed.add_field(name="Group ID", value=str(data['id']), inline=True)
            owner = data['owner']
            owner_link = f"[{owner['username']}](https://www.roblox.com/users/{owner['userId']}/profile)" if owner else "No owner"
            embed.add_field(name="Owner", value=owner_link, inline=True)
            embed.add_field(name="Members", value=formatted_members, inline=True)
            embed.set_footer(text="Neroniel")
            embed.timestamp = discord.utils.utcnow()
            await interaction.response.send_message(embed=embed)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error fetching group info: {e}", ephemeral=True)
