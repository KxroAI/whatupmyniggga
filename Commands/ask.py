# File: cogs/ask.py

import discord
from discord import app_commands
from datetime import datetime
import os
import requests
import asyncio
import pytz
from pymongo import MongoClient
import certifi
from collections import defaultdict
from langdetect import detect

PH_TIMEZONE = pytz.timezone("Asia/Manila")

class AskCommand(discord.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="ask", description="Chat with an AI assistant using Llama 3")
    @app_commands.describe(prompt="What would you like to ask?")
    async def ask(self, interaction: discord.Interaction, prompt: str):
        user_id = interaction.user.id
        channel_id = interaction.channel.id
        await interaction.response.defer()

        # Initialize memory storage if not exists
        if user_id not in self.bot.conversations:
            self.bot.conversations[user_id] = []
        if (user_id, channel_id) not in self.bot.last_message_id:
            self.bot.last_message_id[(user_id, channel_id)] = None

        # Rate limit: 5 messages/user/minute
        current_time = asyncio.get_event_loop().time()
        if user_id not in self.bot.ask_rate_limit:
            self.bot.ask_rate_limit[user_id] = []
        timestamps = self.bot.ask_rate_limit[user_id]
        timestamps.append(current_time)
        self.bot.ask_rate_limit[user_id] = [t for t in timestamps if current_time - t <= 60]

        if len(timestamps) > 5:
            await interaction.followup.send("⏳ You're being rate-limited. Please wait.")
            return

        try:
            # Language detection
            try:
                lang = detect(prompt)
            except:
                lang = "en"  # Default to English if undetectable

            # Build system prompt based on detected language
            system_prompts = {
                "en": "You are a helpful and friendly AI assistant named Neroniel AI.",
                "es": "Eres un asistente de IA útil y amigable llamado Neroniel AI.",
                "fr": "Vous êtes un assistant IA utile et sympathique nommé Neroniel AI.",
                "ja": "あなたは役立ち、親しみやすいAIアシスタントであるNeroniel AIです。",
                "ko": "당신은 유용하고 친절한 AI 어시스턴트인 Neroniel AI입니다。",
                "zh-cn": "你是一个有用且友好的人工智能助手，名叫Neroniel AI。",
                "ru": "Вы полезный и дружелюбный ИИ-ассистент по имени Нерониэль АI。",
                "tl": "Ikaw ay isang kapaki-pakinabang at mapagmahal na AI assistant na nagngangalang Neroniel AI."
            }
            system_prompt = system_prompts.get(lang[:2], system_prompts["en"])
            system_prompt += "\nRespond in the same language as the user."

            # Custom filter for creator questions
            normalized_prompt = prompt.strip().lower()
            if normalized_prompt in [
                "who made you", "who created you",
                "who created this bot", "who made this bot"
            ]:
                embed = discord.Embed(
                    description="I was created by **Neroniel**.",
                    color=discord.Color.blue()
                )
                embed.set_footer(text="Neroniel AI")
                embed.timestamp = datetime.now(PH_TIMEZONE)
                msg = await interaction.followup.send(embed=embed)
                self.bot.last_message_id[(user_id, channel_id)] = msg.id
                return

            # Load conversation history from MongoDB (if available)
            history = []
            if hasattr(self.bot, 'conversations_collection') and self.bot.conversations_collection:
                if not self.bot.conversations[user_id]:
                    history_docs = self.bot.conversations_collection.find({"user_id": user_id}).sort("timestamp", -1).limit(5)
                    for doc in history_docs:
                        self.bot.conversations[user_id].append({
                            "user": doc["prompt"],
                            "assistant": doc["response"]
                        })
                    self.bot.conversations[user_id].reverse()  # Maintain order
                history = self.bot.conversations[user_id][-5:]

            # Build full prompt
            full_prompt = system_prompt + "\n"
            for msg in history:
                full_prompt += f"User: {msg['user']}\nAssistant: {msg['assistant']}\n"
            full_prompt += f"User: {prompt}\nAssistant:"

            # Call Together AI
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

            # Determine if we should reply to a previous message
            target_message_id = self.bot.last_message_id.get((user_id, channel_id))

            # Send the AI response
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

            # Update the last message ID for future replies
            self.bot.last_message_id[(user_id, channel_id)] = reply.id

            # Store in memory and MongoDB
            self.bot.conversations[user_id].append({
                "user": prompt,
                "assistant": ai_response
            })

            if hasattr(self.bot, 'conversations_collection') and self.bot.conversations_collection:
                try:
                    self.bot.conversations_collection.insert_one({
                        "user_id": user_id,
                        "prompt": prompt,
                        "response": ai_response,
                        "timestamp": datetime.now(PH_TIMEZONE)
                    })
                except Exception as e:
                    print(f"[!] Failed to save conversation: {e}")

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {str(e)}")


async def setup(bot):
    await bot.add_cog(AskCommand(bot))
