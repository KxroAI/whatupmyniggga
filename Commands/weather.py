@bot.tree.command(name="weather", description="Get weather information for a city")
@app_commands.describe(city="City name")
async def weather(interaction: discord.Interaction, city: str):
    api_key = os.getenv("WEATHER_API_KEY")
    url = f"http://api.weatherapi.com/v1/current.json?key={api_key}&q={city}"
    response = requests.get(url)
    data = response.json()
    
    if "error" in data:
        await interaction.response.send_message("❌ City not found.")
        return

    current = data["current"]
    location = data["location"]["name"]

    embed = discord.Embed(title=f"Weather in {location}", color=discord.Color.blue())
    embed.add_field(name="Temperature", value=f"{current['temp_c']}°C", inline=True)
    embed.add_field(name="Feels Like", value=f"{current['feelslike_c']}°C", inline=True)
    embed.add_field(name="Condition", value=current["condition"]["text"], inline=False)
    embed.set_thumbnail(url=f"https:{current['condition']['icon']}")
    await interaction.response.send_message(embed=embed)
