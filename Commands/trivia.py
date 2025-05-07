@bot.tree.command(name="trivia", description="Answer a random trivia question!")
async def trivia(interaction: discord.Interaction):
    response = requests.get("https://opentdb.com/api.php?amount=1&type=multiple")
    data = response.json()["results"][0]
    
    question = data["question"]
    correct = data["correct_answer"]
    options = data["incorrect_answers"] + [correct]
    random.shuffle(options)

    embed = discord.Embed(title="🧠 Trivia", description=question, color=discord.Color.orange())
    for i, opt in enumerate(options):
        embed.add_field(name=f"Option {i+1}", value=opt, inline=False)
    
    await interaction.response.send_message(embed=embed)

    def check(m):
        return m.author == interaction.user and m.content.isdigit() and 1 <= int(m.content) <= len(options)

    try:
        msg = await bot.wait_for("message", timeout=20.0, check=check)
        answer_index = int(msg.content) - 1
        if options[answer_index] == correct:
            await msg.reply("✅ Correct!")
        else:
            await msg.reply(f"❌ Wrong! The correct answer was: {correct}")
    except asyncio.TimeoutError:
        await interaction.followup.send("⏰ Time's up!")
