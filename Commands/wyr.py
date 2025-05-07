def setup(bot):
    @bot.tree.command(name="wyr", description="Would You Rather...")
    async def wyr(interaction: discord.Interaction):
        response = requests.get("https://api.truthordarebot.xyz/v1/wyr")
        data = response.json()

        embed = discord.Embed(title="❓ Would You Rather...", description=data["question"], color=discord.Color.dark_blue())
        await interaction.response.send_message(embed=embed)
