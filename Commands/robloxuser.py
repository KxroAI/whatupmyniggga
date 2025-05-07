def setup(bot):
    @bot.tree.command(name="robloxuser", description="Get info about a Roblox user")
    @app_commands.describe(username="The Roblox username to look up")
    async def robloxuser(interaction: discord.Interaction, username: str):
        # Get user data
        user_url = f"https://api.roblox.com/users/get-by-username?username={username}"
        user_response = requests.get(user_url)
        user_data = user_response.json()

        if not user_data.get("success", True):
            await interaction.response.send_message("❌ User not found.")
            return

        user_id = user_data["data"]["id"]
        
        # Get detailed user info including description and badges
        details_url = f"https://users.roblox.com/v1/users/{user_id}"
        details_response = requests.get(details_url)
        details_data = details_response.json()
        
        # Get user badges
        badges_url = f"https://badges.roblox.com/v1/users/{user_id}/badges?limit=100&sortOrder=Asc"
        badges_response = requests.get(badges_url)
        badges_data = badges_response.json()

        # Filter for specific badge names
        relevant_badges = {
            "Veteran Badge": "rbxassetid://123456789",
            "Friendship Badge": "rbxassetid://987654321",
            "Ambassador Badge": "rbxassetid://112233445",
            "Inviter Badge": "rbxassetid://556677889",
            "Homestead Badge": "rbxassetid://223344556",
            "Bricksmith Badge": "rbxassetid://667788990",
            "Official Model Maker Badge": "rbxassetid://334455667",
            "Combat Initiation Badge": "rbxassetid://778899001",
            "Warrior Badge": "rbxassetid://445566778",
            "Bloxxer Badge": "rbxassetid://889900112"
        }

        user_badges = [badge['name'] for badge in badges_data.get("data", [])]
        matched_badges = [badge for badge in user_badges if badge in relevant_badges]

        # Format fields
        username_field = f"[{details_data['name']}](https://www.roblox.com/users/{user_id}/profile)"
        display_name = details_data.get("displayName", "N/A")
        created_at = details_data["created"].split("T")[0]  # Date only
        description = details_data.get("description", "N/A")
        badges_list = "\n".join(matched_badges) if matched_badges else "N/A"

        # Build embed
        embed = discord.Embed(title=f"👤 Roblox User: {username}", color=discord.Color.green())
        embed.add_field(name="Username", value=username_field, inline=False)
        embed.add_field(name="Display Name", value=display_name, inline=True)
        embed.add_field(name="User ID", value=str(user_id), inline=True)
        embed.add_field(name="Account Created", value=created_at, inline=False)
        embed.add_field(name="Description", value=description if description != "N/A" else "N/A", inline=False)
        embed.add_field(name="Badges", value=badges_list, inline=False)
        embed.set_thumbnail(url=f"https://www.roblox.com/headshot-thumbnail/image?userId={user_id}&width=420&height=420&format=png")

        await interaction.response.send_message(embed=embed)
