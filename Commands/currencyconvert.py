@bot.tree.command(name="currencyconvert", description="Convert between currencies (e.g., USD to PHP)")
@app_commands.describe(amount="Amount to convert", from_currency="From currency (e.g., USD)", to_currency="To currency (e.g., PHP)")
async def convert(interaction: discord.Interaction, amount: float, from_currency: str, to_currency: str):
    api_key = os.getenv("CURRENCY_API_KEY")
    url = f"https://api.currencyapi.com/v3/latest?apikey={api_key}&currencies={to_currency}&base_currency={from_currency}"
    response = requests.get(url)
    data = response.json()
    
    if "data" not in data:
        await interaction.response.send_message("❌ Invalid currency code.")
        return

    rate = data["data"][to_currency]["value"]
    result = amount * rate

    embed = discord.Embed(title="💱 Currency Conversion", color=discord.Color.gold())
    embed.add_field(name="Input", value=f"{amount} {from_currency}", inline=True)
    embed.add_field(name="Rate", value=f"1 {from_currency} = {rate:.4f} {to_currency}", inline=True)
    embed.add_field(name="Result", value=f"≈ {result:.2f} {to_currency}", inline=False)
    await interaction.response.send_message(embed=embed)
