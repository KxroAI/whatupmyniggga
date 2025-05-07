import discord
from discord import app_commands

def setup(bot):
    @bot.tree.command(name="listallcommands", description="List all available slash commands")
    async def listallcommands(interaction: discord.Interaction):
        embed = discord.Embed(
            title="📚 All Available Commands",
            description="A categorized list of all commands for easy navigation.",
            color=discord.Color.blue()
        )
        # 🤖 AI Assistant
        embed.add_field(
            name="🤖 AI Assistant",
            value="""
            `/ask` - Chat with Llama 3 AI (supports threaded conversations)
            `/clearhistory` - Clear your AI conversation history
            """,
            inline=False
        )
        # 💰 Currency Conversion
        embed.add_field(
            name="💰 Currency Conversion",
            value="""
            `/payout <robux>` - Convert Robux to PHP at Payout rate (₱320/1000)
            `/payoutreverse <php>` - Convert PHP to Robux at Payout rate
            `/gift <robux>` - Convert Robux to PHP at Gift rate (₱250/1000)
            `/giftreverse <php>` - Convert PHP to Robux at Gift rate
            `/nct <robux>` - Convert Robux to PHP at NCT rate (₱240/1000)
            `/nctreverse <php>` - Convert PHP to Robux at NCT rate
            `/ct <robux>` - Convert Robux to PHP at CT rate (₱340/1000)
            `/ctreverse <php>` - Convert PHP to Robux at CT rate
            """,
            inline=False
        )
        # 📊 Comparison & Tax
        embed.add_field(
            name="📊 Comparison & Tax",
            value="""
            `/allrates <robux>` - Compare PHP values across all rates
            `/allratesreverse <php>` - Compare Robux needed across all rates
            `/beforetax <robux>` - Calculate how much you'll receive after 30% tax
            `/aftertax <target>` - Calculate how much to send to get X after tax
            """,
            inline=False
        )
        # 🛠️ Utility Tools
        embed.add_field(
            name="🛠️ Utility Tools",
            value="""
            `/userinfo [user]` - View detailed info about a user
            `/purge <amount>` - Delete a number of messages (mod only)
            `/calculator <num1> <op> <num2>` - Perform basic math operations
            `/group` - Show info about the 1cy Roblox Group
            """,
            inline=False
        )
        # 🎉 Fun Commands
        embed.add_field(
            name="🎉 Fun",
            value="""
            `/poll <question> <time> <unit>` - Create a poll with up/down votes
            `/remindme <minutes> <note>` - Set a reminder for yourself
            `/say <message>` - Make the bot say something
            `/donate <user> <amount>` - Donate Robux to someone
            """,
            inline=False
        )
        # Footer
        embed.set_footer(text="Neroniel")
        embed.timestamp = discord.utils.utcnow()
        await interaction.response.send_message(embed=embed)
