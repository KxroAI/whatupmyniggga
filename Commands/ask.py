import discord
from discord import app_commands
import os
import requests
import asyncio
import pytz
from pymongo import MongoClient
import certifi
from collections import defaultdict

PH_TIMEZONE = pytz.timezone("Asia/Manila")

def setup(bot):
    @bot.tree.command(name="ask", description="Chat with an AI assistant using Llama 3")
    @app_commands.describe(prompt="What would you like to ask?")
    async def ask(interaction: discord.Interaction, prompt: str):
        user_id = interaction.user.id
        channel_id = interaction.channel.id
        await interaction.response.defer()
        current_time = asyncio.get_event_loop().time()
        timestamps = bot.ask_rate_limit[user_id]
        timestamps.append(current_time)
        bot.ask_rate_limit[user_id] = [t for t in timestamps if current_time - t <= 60]
        if len(timestamps) > 5:
            await interaction.followup.send("⏳ You're being rate-limited. Please wait.")
            return
        async with interaction.channel.typing():
            try:
                normalized_prompt = prompt.strip().lower()
                if normalized_prompt in ["who made you", "who created you", "who created this bot", "who made this bot"]:
                    embed = discord.Embed(description="I was created by **Neroniel**.", color=discord.Color.blue())
                    embed.set_footer(text="Neroniel AI")
                    embed.timestamp = datetime.now(PH_TIMEZONE)
                    msg = await interaction.followup.send(embed=embed)
                    bot.last_message_id[(user_id, channel_id)] = msg.id
                    return
                history = []
                if hasattr(bot, 'conversations') and user_id in bot.conversations:
                    history = bot.conversations[user_id][-5:]
                system_prompt = "You are a helpful and friendly AI assistant named Neroniel AI.\n"
                full_prompt = system_prompt
                for msg in history:
                    full_prompt += f"User: {msg['user']}\nAssistant: {msg['assistant']}\n"
                full_prompt += f"User: {prompt}\nAssistant:"
                headers = {
                    "Authorization": f"Bearer {os.getenv('TOGETHER_API_KEY')}",
                    "Content-Type": "application/json"
                }
                payload = {
                    "model": "meta-llama/Llama-3-70b-chat-hf",
                    "prompt": full_prompt,
                    "max_tokens": 2048,
                    "temperature": 0.7
                }
                response = requests.post(
                    "https://api.together.xyz/v1/completions",
                    headers=headers,
                    json=payload
                )
                data = response.json()
                if 'error' in data:
                    await interaction.followup.send(f"❌ Error from AI API: {data['error']['message']}")
                    return
                ai_response = data["choices"][0]["text"].strip()
                target_message_id = bot.last_message_id.get((user_id, channel_id))
                embed = discord.Embed(description=ai_response, color=discord.Color.blue())
                embed.set_footer(text="Neroniel AI")
                embed.timestamp = datetime.now(PH_TIMEZONE)
                if target_message_id:
                    try:
                        msg = await interaction.channel.fetch_message(target_message_id)
                        reply = await msg.reply(embed=embed)
                    except discord.NotFound:
                        msg = await interaction.followup.send(embed=embed)
                        reply = msg
                else:
                    msg = await interaction.followup.send(embed=embed)
                    reply = msg
                bot.last_message_id[(user_id, channel_id)] = reply.id
                bot.conversations[user_id].append({
                    "user": prompt,
                    "assistant": ai_response
                })
                if hasattr(bot, 'conversations_collection'):
                    bot.conversations_collection.insert_one({
                        "user_id": user_id,
                        "prompt": prompt,
                        "response": ai_response,
                        "timestamp": datetime.now(PH_TIMEZONE)
                    })
            except Exception as e:
                await interaction.followup.send(f"❌ Error: {str(e)}")
