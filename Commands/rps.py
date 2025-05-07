@bot.tree.command(name="rps", description="Play Rock Paper Scissors against the bot")
@app_commands.describe(choice="Your choice: rock, paper, or scissors")
async def rps(interaction: discord.Interaction, choice: str):
    choices = ["rock", "paper", "scissors"]
    bot_choice = random.choice(choices)
    win_cases = {"rock": "scissors", "paper": "rock", "scissors": "paper"}

    if choice.lower() not in choices:
        await interaction.response.send_message("❗ Choose either rock, paper, or scissors.")
        return

    if win_cases[choice] == bot_choice:
        result = "🎉 You won!"
    elif choice == bot_choice:
        result = "⚖️ It's a tie!"
    else:
        result = "😢 You lost!"

    embed = discord.Embed(title="🎮 Rock Paper Scissors", color=discord.Color.purple())
    embed.add_field(name="You chose", value=choice.capitalize(), inline=True)
    embed.add_field(name="Bot chose", value=bot_choice.capitalize(), inline=True)
    embed.add_field(name="Result", value=result, inline=False)
    await interaction.response.send_message(embed=embed)
