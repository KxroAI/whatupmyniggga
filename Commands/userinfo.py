import discord
from discord import app_commands
import pytz

PH_TIMEZONE = pytz.timezone("Asia/Manila")

def setup(bot):
    @bot.tree.command(name="userinfo", description="Display detailed information about a user")
    @app_commands.describe(member="The member to get info for (optional, defaults to you)")
    async def userinfo(interaction: discord.Interaction, member: discord.Member = None):
        if member is None:
            member = interaction.user
        # Account creation date
        created_at = member.created_at.astimezone(PH_TIMEZONE).strftime("%B %d, %Y • %I:%M %p GMT+8")
        # Join date
        joined_at = member.joined_at.astimezone(PH_TIMEZONE).strftime("%B %d, %Y • %I:%M %p GMT+8") if member.joined_at else "Unknown"
        # Roles
        roles = [role.mention for role in member.roles if not role.is_default()]
        roles_str = ", ".join(roles) if roles else "No Roles"
        # Boosting status
        boost_since = member.premium_since.astimezone(PH_TIMEZONE).strftime("%B %d, %Y • %I:%M %p GMT+8") if member.premium_since else "Not Boosting"
        embed = discord.Embed(title=f"👤 User Info for {member}", color=discord.Color.green())
        # Basic Info
        embed.add_field(name="Username", value=f"{member.mention}", inline=False)
        embed.add_field(name="Display Name", value=f"`{member.display_name}`", inline=True)
        embed.add_field(name="User ID", value=f"`{member.id}`", inline=True)
        # Dates
        embed.add_field(name="Created Account", value=f"`{created_at}`", inline=False)
        embed.add_field(name="Joined Server", value=f"`{joined_at}`", inline=False)
        # Roles
        embed.add_field(name="Roles", value=roles_str, inline=False)
        # Boosting
        embed.add_field(name="Server Booster Since", value=f"`{boost_since}`", inline=False)
        # Optional: Show if the user is a bot
        if member.bot:
            embed.add_field(name="Bot Account", value="✅ Yes", inline=True)
        # Set thumbnail to user's avatar
        embed.set_thumbnail(url=member.display_avatar.url)
        # Footer and timestamp
        embed.set_footer(text="Neroniel")
        embed.timestamp = datetime.now(PH_TIMEZONE)
        await interaction.response.send_message(embed=embed)
