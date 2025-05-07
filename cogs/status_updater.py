# File: cogs/status_updater.py

import discord
import asyncio
import requests

class StatusUpdater(discord.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.group_id = 5838002
        self.task = self.bot.loop.create_task(self.update_status())

    async def update_status(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                response = requests.get(f"https://groups.roblox.com/v1/groups/{self.group_id}")
                data = response.json()
                member_count = data['memberCount']
                await self.bot.change_presence(
                    status=discord.Status.dnd,
                    activity=discord.Activity(
                        type=discord.ActivityType.watching,
                        name=f"1cy | {member_count} Members"
                    )
                )
            except Exception as e:
                print(f"[!] Error fetching Roblox group info: {e}")
                await self.bot.change_presence(
                    status=discord.Status.dnd,
                    activity=discord.Activity(
                        type=discord.ActivityType.watching,
                        name="1cy"
                    )
                )
            await asyncio.sleep(60)

# Setup function to load this cog
async def setup(bot):
    await bot.add_cog(StatusUpdater(bot))
