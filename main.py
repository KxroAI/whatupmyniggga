import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio
import requests
import os
import threading
import math
import random
from flask import Flask
from collections import defaultdict
from dotenv import load_dotenv
import certifi
from pymongo import MongoClient
from datetime import datetime, timedelta
import pytz
from langdetect import detect, LangDetectException
from enum import Enum

# Set timezone to Philippines (GMT+8)
PH_TIMEZONE = pytz.timezone("Asia/Manila")

# Load environment variables
load_dotenv()

# =============================
# Bot Setup
# =============================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)

# Rate limiting data
bot.ask_rate_limit = defaultdict(list)
bot.conversations = defaultdict(list)  # In-memory cache for AI conversation
bot.last_message_id = {}  # Store last message IDs for threaded replies

# =============================
# Flask Web Server to Keep Bot Alive
# =============================
app = Flask(__name__)
@app.route('/')
def home():
    return "Bot is alive!"

def run_server():
    app.run(host='0.0.0.0', port=5000)

server_thread = threading.Thread(target=run_server)
server_thread.start()

# Optional: Add another threaded task
def check_for_updates():
    while True:
        print("[Background] Checking for updates...")
        time.sleep(300)  # Every 5 minutes

update_thread = threading.Thread(target=check_for_updates)
update_thread.daemon = True
update_thread.start()

# =============================
# MongoDB Setup (with SSL Fix)
# =============================
try:
    client = MongoClient(os.getenv("MONGO_URI"), tlsCAFile=certifi.where())
    db = client.ai_bot
    conversations_collection = db.conversations
    reminders_collection = db.reminders

    # Create TTL indexes
    conversations_collection.create_index("timestamp", expireAfterSeconds=604800)  # 7 days
    reminders_collection.create_index("reminder_time", expireAfterSeconds=2592000)  # 30 days
except Exception as e:
    print(f"[!] Failed to connect to MongoDB: {e}")
    client = None
    conversations_collection = None
    reminders_collection = None

# Background Task: Check Reminders
@tasks.loop(seconds=60)
async def check_reminders():
    if not reminders_collection:
        return
    try:
        now = datetime.now(PH_TIMEZONE)
        expired = reminders_collection.find({"reminder_time": {"$lte": now}})
        for reminder in expired:
            user_id = reminder["user_id"]
            guild_id = reminder["guild_id"]
            channel_id = reminder["channel_id"]
            note = reminder["note"]

            user = bot.get_user(user_id)
            if not user:
                user = await bot.fetch_user(user_id)

            guild = bot.get_guild(guild_id)
            if not guild:
                continue

            channel = guild.get_channel(channel_id)
            if not channel:
                continue

            try:
                await channel.send(f"🔔 {user.mention}, reminder: {note}")
            except discord.Forbidden:
                print(f"[!] Cannot send reminder to {user} in #{channel.name}")

            # Delete reminder after sending
            reminders_collection.delete_one({"_id": reminder["_id"]})
    except Exception as e:
        print(f"[!] Error checking reminders: {e}")

@check_reminders.before_loop
async def before_check_reminders():
    await bot.wait_until_ready()

if reminders_collection:
    check_reminders.start()

# =============================
# Owner-only Direct Message Commands
# =============================
BOT_OWNER_ID = 1163771452403761193  # Replace with your actual Discord ID if different

@bot.tree.command(name="dm", description="Send a direct message to a user (Owner only)")
@app_commands.describe(user="The user you want to message", message="The message to send")
async def dm(interaction: discord.Interaction, user: discord.User, message: str):
    if interaction.user.id != BOT_OWNER_ID:
        await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
        return
    try:
        await user.send(message)
        await interaction.response.send_message(f"✅ Sent DM to {user} ({user.id})", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(f"❌ Unable to send DM to {user}. They might have DMs disabled.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ An error occurred: {str(e)}", ephemeral=True)

@bot.tree.command(name="dmall", description="Send a direct message to all members in the server (Owner only)")
@app_commands.describe(message="The message you want to send to all members")
async def dmall(interaction: discord.Interaction, message: str):
    if interaction.user.id != BOT_OWNER_ID:
        await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
        return
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("❌ This command must be used in a server.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    success_count = 0
    fail_count = 0

    for member in guild.members:
        if member.bot:
            continue  # Skip bots
        try:
            await member.send(message)
            success_count += 1
        except discord.Forbidden:
            fail_count += 1
        except Exception as e:
            print(f"[!] Failed to send DM to {member}: {str(e)}")
            fail_count += 1

    await interaction.followup.send(
        f"✅ Successfully sent DM to **{success_count}** members. ❌ Failed to reach **{fail_count}** members."
    )

# =============================
# AI Commands
# =============================
# /ask - Chat with Llama 3 via Together AI with threaded replies
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
                embed = discord.Embed(description="I was created by **Neroniel**.", color=discord.Color.from_rgb(0, 0, 0))
                embed.set_footer(text="Neroniel AI")
                embed.timestamp = datetime.now(PH_TIMEZONE)
                msg = await interaction.followup.send(embed=embed)
                bot.last_message_id[(user_id, channel_id)] = msg.id
                return

            try:
                detected_lang = detect(prompt)
            except LangDetectException:
                detected_lang = "en"

            lang_instruction = {
                "tl": "Please respond in Tagalog.",
                "es": "Por favor responde en español.",
                "fr": "Veuillez répondre en français.",
                "ja": "日本語で答えてください。",
                "ko": "한국어로 답변해 주세요.",
                "zh": "请用中文回答。",
                "ru": "Пожалуйста, отвечайте на русском языке.",
                "ar": "من فضلك أجب بالعربية.",
                "vi": "Vui lòng trả lời bằng tiếng Việt.",
                "th": "กรุณาตอบเป็นภาษาไทย",
                "id": "Silakan jawab dalam bahasa Indonesia"
            }.get(detected_lang, "")

            history = []
            if conversations_collection:
                if not bot.conversations[user_id]:
                    history_docs = conversations_collection.find({"user_id": user_id}).sort("timestamp", -1).limit(5)
                    for doc in history_docs:
                        bot.conversations[user_id].append({
                            "user": doc["prompt"],
                            "assistant": doc["response"]
                        })
                    bot.conversations[user_id].reverse()
                history = bot.conversations[user_id][-5:]

            system_prompt = f"You are a helpful and friendly AI assistant named Neroniel AI. {lang_instruction}"
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
                "https://api.together.xyz/v1/completions ",
                headers=headers,
                json=payload
            )
            data = response.json()
            if 'error' in data:
                await interaction.followup.send(f"❌ Error from AI API: {data['error']['message']}")
                return
            ai_response = data["choices"][0]["text"].strip()

            target_message_id = bot.last_message_id.get((user_id, channel_id))

            embed = discord.Embed(description=ai_response, color=discord.Color.from_rgb(0, 0, 0))
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

            if conversations_collection:
                conversations_collection.insert_one({
                    "user_id": user_id,
                    "prompt": prompt,
                    "response": ai_response,
                    "timestamp": datetime.now(PH_TIMEZONE)
                })
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {str(e)}")

# /clearhistory - Clear stored conversation history
@bot.tree.command(name="clearhistory", description="Clear your AI conversation history")
async def clearhistory(interaction: discord.Interaction):
    user_id = interaction.user.id
    if user_id in bot.conversations:
        bot.conversations[user_id].clear()
    if conversations_collection:
        conversations_collection.delete_many({"user_id": user_id})
    await interaction.response.send_message("✅ Your AI conversation history has been cleared!", ephemeral=True)

# =============================
# Utility Commands
# =============================
# /userinfo - Display user information
@bot.tree.command(name="userinfo", description="Display detailed information about a user")
@app_commands.describe(member="The member to get info for (optional, defaults to you)")
async def userinfo(interaction: discord.Interaction, member: discord.Member = None):
    if member is None:
        member = interaction.user
    created_at = member.created_at.astimezone(PH_TIMEZONE).strftime("%B %d, %Y • %I:%M %p GMT+8")
    joined_at = member.joined_at.astimezone(PH_TIMEZONE).strftime("%B %d, %Y • %I:%M %p GMT+8") if member.joined_at else "Unknown"
    roles = [role.mention for role in member.roles if not role.is_default()]
    roles_str = ", ".join(roles) if roles else "No Roles"
    boost_since = member.premium_since.astimezone(PH_TIMEZONE).strftime("%B %d, %Y • %I:%M %p GMT+8") if member.premium_since else "Not Boosting"

    embed = discord.Embed(title=f"👤 User Info for {member}", color=discord.Color.from_rgb(0, 0, 0))
    embed.add_field(name="Username", value=f"{member.mention}", inline=False)
    embed.add_field(name="Display Name", value=f"`{member.display_name}`", inline=True)
    embed.add_field(name="User ID", value=f"`{member.id}`", inline=True)
    embed.add_field(name="Created Account", value=f"`{created_at}`", inline=False)
    embed.add_field(name="Joined Server", value=f"`{joined_at}`", inline=False)
    embed.add_field(name="Roles", value=roles_str, inline=False)
    embed.add_field(name="Server Booster Since", value=f"`{boost_since}`", inline=False)
    if member.bot:
        embed.add_field(name="Bot Account", value="✅ Yes", inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)
    await interaction.response.send_message(embed=embed)

# /announcement - Send embedded announcement
@bot.tree.command(name="announcement", description="Send an embedded announcement to a specific channel")
@app_commands.describe(message="The message to include in the announcement", channel="The channel to send the announcement to")
async def announcement(interaction: discord.Interaction, message: str, channel: discord.TextChannel):
    BOT_OWNER_ID = 1163771452403761193
    is_owner = interaction.user.id == BOT_OWNER_ID
    is_admin = interaction.user.guild_permissions.administrator
    if not is_owner and not is_admin:
        await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
        return
    embed = discord.Embed(
        title="📢 ANNOUNCEMENT",
        description=f"```\n{message}\n```",
        color=discord.Color.from_rgb(0, 0, 0)
    )
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)
    try:
        await channel.send(embed=embed)
        await interaction.response.send_message(f"✅ Announcement sent to {channel.mention}", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(f"❌ I can't send messages in {channel.mention}.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ An error occurred: {str(e)}", ephemeral=True)

# =============================
# Conversion Commands
# =============================
@bot.tree.command(name="payout", description="Convert Robux to PHP based on Payout rate (₱320 for 1000 Robux)")
@app_commands.describe(robux="How much Robux do you want to convert?")
async def payout(interaction: discord.Interaction, robux: int):
    if robux <= 0:
        await interaction.response.send_message("❗ Robux amount must be greater than zero.")
        return
    php = robux * (320 / 1000)
    await interaction.response.send_message(f"💵 {robux} Robux = ₱{php:.2f} PHP")

@bot.tree.command(name="gift", description="Convert Robux to PHP based on Gift rate (₱250 for 1000 Robux)")
@app_commands.describe(robux="How much Robux do you want to convert?")
async def gift(interaction: discord.Interaction, robux: int):
    if robux <= 0:
        await interaction.response.send_message("❗ Invalid input.")
        return
    php = robux * (250 / 1000)
    await interaction.response.send_message(f"🎁 {robux} Robux = ₱{php:.2f} PHP")

@bot.tree.command(name="nct", description="Convert Robux to PHP based on NCT rate (₱240/1k)")
@app_commands.describe(robux="How much Robux do you want to convert?")
async def nct(interaction: discord.Interaction, robux: int):
    if robux <= 0:
        await interaction.response.send_message("❗ Invalid input.")
        return
    php = robux * (240 / 1000)
    await interaction.response.send_message(f"💸 {robux} Robux = ₱{php:.2f} PHP")

@bot.tree.command(name="ct", description="Convert Robux to PHP based on CT rate (₱340/1k)")
@app_commands.describe(robux="How much Robux do you want to convert?")
async def ct(interaction: discord.Interaction, robux: int):
    if robux <= 0:
        await interaction.response.send_message("❗ Invalid input.")
        return
    php = robux * (340 / 1000)
    await interaction.response.send_message(f"💳 {robux} Robux = ₱{php:.2f} PHP")

@bot.tree.command(name="beforetax", description="Calculate how much Robux you'll receive after 30% tax")
@app_commands.describe(robux="How much Robux is being sent?")
async def beforetax(interaction: discord.Interaction, robux: int):
    received = math.floor(robux * 0.7)
    await interaction.response.send_message(f"📤 Sending {robux} → Receive **{received} Robux** after tax.")

# ConvertCurrency
@bot.tree.command(name="convertcurrency", description="Convert between two currencies")
@app_commands.describe(amount="Amount to convert", from_currency="Currency to convert from (e.g., USD)", to_currency="Currency to convert to (e.g., PHP)")
async def convertcurrency(interaction: discord.Interaction, amount: float, from_currency: str, to_currency: str):
    api_key = os.getenv("CURRENCY_API_KEY")
    if not api_key:
        await interaction.response.send_message("❌ CURRENCY_API_KEY missing.", ephemeral=True)
        return
    url = f"https://api.currencyapi.com/v3/latest?apikey= {api_key}&currencies={to_currency.upper()}&base_currency={from_currency.upper()}"
    try:
        response = requests.get(url)
        data = response.json()
        if 'error' in data:
            await interaction.response.send_message(f"❌ API Error: {data['error']['message']}")
            return
        if "data" not in data or to_currency.upper() not in data["data"]:
            await interaction.response.send_message("❌ Invalid currency or no data found.")
            return
        rate = data["data"][to_currency.upper()]["value"]
        result = amount * rate
        embed = discord.Embed(title="💱 Currency Conversion", color=discord.Color.from_rgb(0, 0, 0))
        embed.add_field(name="📥 Input", value=f"{amount} {from_currency.upper()}", inline=False)
        embed.add_field(name="📉 Rate", value=f"1 {from_currency.upper()} = {rate:.4f} {to_currency.upper()}", inline=False)
        embed.add_field(name="📤 Result", value=f"≈ **{result:.2f} {to_currency.upper()}**", inline=False)
        embed.set_footer(text="Neroniel")
        embed.timestamp = datetime.now(PH_TIMEZONE)
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {str(e)}")

# ========== Weather Command ==========
PHILIPPINE_CITIES = [
    "Manila", "Quezon City", "Caloocan", "Las Piñas", "Makati",
    "Malabon", "Navotas", "Paranaque", "Pasay", "Muntinlupa",
    "Taguig", "Valenzuela", "Marikina", "Pasig", "San Juan"
]

GLOBAL_CAPITAL_CITIES = [
    "Washington D.C.", "London", "Paris", "Berlin", "Rome",
    "Moscow", "Beijing", "Tokyo", "Seoul", "New Delhi"
]

@bot.tree.command(name="weather", description="Get weather information for a city")
@app_commands.describe(city="City name", unit="Temperature unit (default is Celsius)")
@app_commands.choices(unit=[
    app_commands.Choice(name="Celsius (°C)", value="c"),
    app_commands.Choice(name="Fahrenheit (°F)", value="f")
])
async def weather(interaction: discord.Interaction, city: str, unit: str = "c"):
    api_key = os.getenv("WEATHER_API_KEY")
    if not api_key:
        await interaction.response.send_message("❌ Weather API key missing.", ephemeral=True)
        return
    url = f"http://api.weatherapi.com/v1/current.json?key={api_key}&q={city}"
    try:
        response = requests.get(url)
        data = response.json()
        if "error" in data:
            await interaction.response.send_message("❌ City not found or invalid input.", ephemeral=True)
            return
        current = data["current"][0]
        location = data["location"]["name"]
        temperature = current["temp_c"] if unit == "c" else current["temp_f"]
        feels_like = current["feelslike_c"] if unit == "c" else current["feelslike_f"]
        humidity = current["humidity"]
        wind_kph = current["wind_kph"]
        condition = current["condition"][0]["text"]
        icon_url = f"https:{current['condition'][0]['icon']}"

        embed = discord.Embed(
            title=f"🌤️ Weather in {location}",
            color=discord.Color.from_rgb(0, 0, 0)
        )
        embed.add_field(name="🌡️ Temperature", value=f"{temperature}{unit.upper()}", inline=True)
        embed.add_field(name="🧯 Feels Like", value=f"{feels_like}{unit.upper()}", inline=True)
        embed.add_field(name="💧 Humidity", value=f"{humidity}%", inline=True)
        embed.add_field(name="🌬️ Wind Speed", value=f"{wind_kph} km/h", inline=True)
        embed.add_field(name="📝 Condition", value=condition, inline=False)
        embed.set_thumbnail(url=icon_url)
        embed.set_footer(text="Powered by WeatherAPI • Neroniel")
        embed.timestamp = datetime.now(PH_TIMEZONE)
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error fetching weather: {str(e)}", ephemeral=True)

# =============================
# Other Commands
# =============================
# Purge Command
@bot.tree.command(name="purge", description="Delete a specified number of messages")
@app_commands.describe(amount="How many messages would you like to delete?")
async def purge(interaction: discord.Interaction, amount: int):
    if amount <= 0:
        await interaction.response.send_message("❗ Please specify a positive number.", ephemeral=True)
        return
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message("❗ You don't have permission to use this command.", ephemeral=True)
        return
    await interaction.channel.purge(limit=amount)
    await interaction.response.send_message(f"✅ Deleted **{amount}** messages.", ephemeral=True)

# Group Info Command
@bot.tree.command(name="group", description="Display information about the 1cy Roblox group")
async def groupinfo(interaction: discord.Interaction):
    group_id = 5838002
    try:
        response = requests.get(f"https://groups.roblox.com/v1/groups/ {group_id}")
        data = response.json()
        formatted_members = "{:,}".format(data['memberCount'])
        embed = discord.Embed(color=discord.Color.from_rgb(0, 0, 0))
        embed.add_field(name="Group Name", value=f"[{data['name']}](https://www.roblox.com/groups/ {group_id})", inline=False)
        embed.add_field(name="Description", value=f"```\n{data.get('description', 'No description')}\n```", inline=False)
        embed.add_field(name="Group ID", value=str(data['id']), inline=True)
        owner = data.get('owner')
        owner_link = f"[{owner['username']}](https://www.roblox.com/users/ {owner['userId']}/profile)" if owner else "No owner"
        embed.add_field(name="Owner", value=owner_link, inline=True)
        embed.add_field(name="Members", value=formatted_members, inline=True)
        embed.set_footer(text="Neroniel")
        embed.timestamp = datetime.now(PH_TIMEZONE)
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error fetching group info: {e}", ephemeral=True)

# Poll Command
@bot.tree.command(name="poll", description="Create a poll with reactions and result summary")
@app_commands.describe(question="Poll question", amount="Duration amount", unit="Time unit (seconds, minutes, hours)")
@app_commands.choices(unit=[
    app_commands.Choice(name="Seconds", value="seconds"),
    app_commands.Choice(name="Minutes", value="minutes"),
    app_commands.Choice(name="Hours", value="hours")
])
async def poll(interaction: discord.Interaction, question: str, amount: int, unit: app_commands.Choice[str]):
    total_seconds = {"seconds": amount, "minutes": amount * 60, "hours": amount * 3600}.get(unit.value, 0)
    if total_seconds == 0 or total_seconds > 86400:
        await interaction.response.send_message("❗ Invalid duration.", ephemeral=True)
        return
    embed = discord.Embed(title="📊 Poll", description=question, color=discord.Color.from_rgb(0, 0, 0))
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)
    message = await interaction.channel.send(embed=embed)
    await message.add_reaction("👍")
    await message.add_reaction("👎")
    await interaction.response.send_message("✅ Poll created!", ephemeral=True)
    await asyncio.sleep(total_seconds)
    message = await interaction.channel.fetch_message(message.id)
    reactions = message.reactions
    up = next((r.count for r in reactions if str(r.emoji) == "👍"), 0)
    down = next((r.count for r in reactions if str(r.emoji) == "👎"), 0)
    result = "👍 Upvotes win!" if up > down else ("👎 Downvotes win!" if down > up else "⚖️ It's a tie!")
    result_embed = discord.Embed(title="📊 Poll Results", description=question, color=discord.Color.from_rgb(0, 0, 0))
    result_embed.add_field(name="👍 Upvotes", value=str(up), inline=True)
    result_embed.add_field(name="👎 Downvotes", value=str(down), inline=True)
    result_embed.add_field(name="Result", value=result, inline=False)
    result_embed.set_footer(text="Poll ended")
    result_embed.timestamp = datetime.now(PH_TIMEZONE)
    await message.edit(embed=result_embed)

# Remind Me Command
@bot.tree.command(name="remindme", description="Set a reminder after X minutes (will ping you in this channel)")
@app_commands.describe(minutes="How many minutes until I remind you?", note="Your reminder message")
async def remindme(interaction: discord.Interaction, minutes: int, note: str):
    if minutes <= 0:
        await interaction.response.send_message("❗ Enter a positive number.", ephemeral=True)
        return
    reminder_time = datetime.now(PH_TIMEZONE) + timedelta(minutes=minutes)
    if reminders_collection:
        reminders_collection.insert_one({
            "user_id": interaction.user.id,
            "guild_id": interaction.guild.id,
            "channel_id": interaction.channel.id,
            "note": note,
            "reminder_time": reminder_time
        })
    await interaction.response.send_message(f"⏰ Reminder set in `{minutes}` minutes: `{note}`", ephemeral=True)

# Donate Command
@bot.tree.command(name="donate", description="Donate Robux to a Discord user.")
@app_commands.describe(user="The user to donate to.", amount="Robux amount")
async def donate(interaction: discord.Interaction, user: discord.Member, amount: int):
    if amount <= 0:
        await interaction.response.send_message("❗ Amount must be greater than zero.", ephemeral=True)
        return
    await interaction.response.send_message(f"`{interaction.user}` donated **{amount:,} Robux** to {user.mention}!")

# Say Command
@bot.tree.command(name="say", description="Make the bot say something in chat")
@app_commands.describe(message="Message for the bot to say")
async def say(interaction: discord.Interaction, message: str):
    if "@everyone" in message or "@here" in message:
        await interaction.response.send_message("❌ No @everyone/@here allowed.", ephemeral=True)
        return
    await interaction.channel.send(message)
    await interaction.response.send_message("✅ Message sent!", ephemeral=True)

# Calculator Command
@bot.tree.command(name="calculator", description="Perform basic math operations")
@app_commands.describe(num1="First number", operation="Operation", num2="Second number")
@app_commands.choices(operation=[
    app_commands.Choice(name="Addition (+)", value="add"),
    app_commands.Choice(name="Subtraction (-)", value="subtract"),
    app_commands.Choice(name="Multiplication (*)", value="multiply"),
    app_commands.Choice(name="Division (/)", value="divide")
])
async def calculator(interaction: discord.Interaction, num1: float, operation: app_commands.Choice[str], num2: float):
    if operation.value == "divide" and num2 == 0:
        await interaction.response.send_message("❌ Cannot divide by zero.", ephemeral=True)
        return
    try:
        if operation.value == "add":
            result = num1 + num2
            symbol = "+"
        elif operation.value == "subtract":
            result = num1 - num2
            symbol = "-"
        elif operation.value == "multiply":
            result = num1 * num2
            symbol = "*"
        elif operation.value == "divide":
            result = num1 / num2
            symbol = "/"
        await interaction.response.send_message(f"🔢 `{num1} {symbol} {num2} = {result}`")
    except Exception as e:
        await interaction.response.send_message(f"⚠️ An error occurred: {str(e)}")

# List All Commands
@bot.tree.command(name="listallcommands", description="List all available slash commands")
async def listallcommands(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📚 All Available Commands",
        description="A categorized list of all commands.",
        color=discord.Color.from_rgb(0, 0, 0)
    )
    embed.add_field(name="🤖 AI Assistant", value="""
        `/ask <prompt>` - Chat with Llama 3  
        `/clearhistory` - Clear AI conversation
    """, inline=False)
    embed.add_field(name="💰 Currency Conversion", value="""
        `/payout <robux>` - Payout rate  
        `/gift <robux>` - Gift rate  
        `/nct <robux>` - NCT rate  
        `/ct <robux>` - CT rate  
        `/convertcurrency <amount> <from> <to>` - Convert currencies
    """, inline=False)
    embed.add_field(name="🛠️ Utility Tools", value="""
        `/userinfo [user]` - View user details  
        `/purge <amount>` - Delete messages  
        `/calculator <num1> <op> <num2>` - Math operations  
        `/group` - Show 1cy Roblox group  
        `/weather <city>` - Get weather  
        `/payment <method>` - Payment Methods
    """, inline=False)
    embed.add_field(name="⏰ Reminders & Polls", value="""
        `/remindme <minutes> <note>` - Set a reminder  
        `/poll <question> <time> <unit>` - Create a poll
    """, inline=False)
    embed.add_field(name="🎉 Fun", value="""
        `/donate <user> <amount>` - Donate Robux  
        `/say <message>` - Make bot speak
    """, inline=False)
    embed.add_field(name="🔧 Developer Tools", value="""
        `/dm <user> <message>` - Send DM  
        `/dmall <message>` - DM all users
    """, inline=False)
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)
    await interaction.response.send_message(embed=embed)

# =============================
# Payment Command
# =============================
class PaymentMethod(str, Enum):
    GCASH = "Gcash"
    PAYMAYA = "PayMaya"
    GOTYME = "GoTyme"

@bot.tree.command(name="payment", description="Show payment instructions for Gcash, PayMaya, or GoTyme")
@app_commands.describe(method="Choose a payment method to display instructions")
@app_commands.choices(method=[
    app_commands.Choice(name="Gcash", value="Gcash"),
    app_commands.Choice(name="PayMaya", value="PayMaya"),
    app_commands.Choice(name="GoTyme", value="GoTyme"),
])
async def payment(interaction: discord.Interaction, method: PaymentMethod):
    payment_info = {
        PaymentMethod.GCASH: {
            "title": "Gcash Payment",
            "description": "Account Initials: M R G.\nAccount Number: 09550333612",
            "image": "https://imgur.com/gallery/gcash-14t2mY7 "
        },
        PaymentMethod.PAYMAYA: {
            "title": "PayMaya Payment",
            "description": "Account Initials: N G.\nAccount Number: 09550333612",
            "image": "https://imgur.com/gallery/paymaya-rkrd7l0 "
        },
        PaymentMethod.GOTYME: {
            "title": "GoTyme Payment",
            "description": "Account Initials: N G.\nAccount Number: HIDDEN",
            "image": "https://imgur.com/gallery/gotyme-4LNfYsO "
        }
    }

    info = payment_info[method]
    embed = discord.Embed(
        title=info["title"],
        description=info["description"],
        color=discord.Color.from_rgb(0, 0, 0)
    )
    if info["image"]:
        embed.set_image(url=info["image"])
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)
    await interaction.response.send_message(embed=embed)

# =============================
# Bot Events
# =============================
@bot.event
async def on_ready():
    print(f"Bot is ready! Logged in as {bot.user}")
    await bot.tree.sync()
    print("All commands synced!")

    group_id = 5838002
    while True:
        try:
            response = requests.get(f"https://groups.roblox.com/v1/groups/ {group_id}")
            data = response.json()
            member_count = data['memberCount']
            await bot.change_presence(status=discord.Status.dnd,
                                   activity=discord.Activity(
                                       type=discord.ActivityType.watching,
                                       name=f"1cy | {member_count} Members"))
        except Exception as e:
            print(f"Error fetching group info: {str(e)}")
            await bot.change_presence(status=discord.Status.dnd,
                                   activity=discord.Activity(
                                       type=discord.ActivityType.watching,
                                       name="1cy"))
        await asyncio.sleep(60)

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    content = message.content.lower()
    if content == "hobie":
        await message.channel.send("mapanghe")
    elif content == "neroniel":
        await message.channel.send("masarap")
    elif content == "hi":
        reply = (
            "hi tapos ano? magiging friends tayo? lagi tayong mag-uusap mula umaga hanggang madaling araw? tas magiging close tayo? sa sobrang close natin nahuhulog na tayo sa isa't isa, tapos ano? liligawan mo ko? sasagutin naman kita. paplanuhin natin yung pangarap natin sa isa't isa tapos ano? may makikita kang iba. magsasawa ka na, iiwan mo na ako. tapos magmamakaawa ako sayo kasi mahal kita pero ano? wala kang gagawin, hahayaan mo lang akong umiiyak while begging you to stay. kaya wag na lang. thanks nalang sa hi mo"
        )
        await message.channel.send(reply)
    auto_react_channels = [
        1225294057371074760,
        1107600826664501258,
        1107591404877791242,
        1368123462077513738
    ]
    if message.channel.id in auto_react_channels:
        await message.add_reaction("🎀")
    if message.channel.id == 1107281584337461321:
        await message.add_reaction("<:1cy_heart:1258694384346468362>")

# Run the bot
bot.run(os.getenv('DISCORD_TOKEN'))
