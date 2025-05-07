@bot.tree.command(name="meme", description="Send a random meme")
async def meme(interaction: discord.Interaction):
    response = requests.get("https://meme-api.com/gimme")
    data = response.json()
    
    embed = discord.Embed(title=data["title"], color=discord.Color.random())
    embed.set_image(url=data["url"])
    await interaction.response.send_message(embed=embed)
