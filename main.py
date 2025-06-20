import discord
from discord import Embed, app_commands, Interaction, ui, ButtonStyle
from discord.ext import commands, tasks
import asyncio
import requests
import os
import math
import random
from collections import defaultdict, deque
from dotenv import load_dotenv
import certifi
from pymongo import MongoClient, ASCENDING
from pymongo.server_api import ServerApi
from datetime import datetime, timedelta
import pytz
from langdetect import detect, LangDetectException
from enum import Enum
import aiohttp
import json
from dateutil.parser import isoparse
import re
from flask import Flask
import threading
import time
import pyktok as pyk
import instaloader
import tempfile

# Set timezone to Philippines (GMT+8)
PH_TIMEZONE = pytz.timezone("Asia/Manila")
load_dotenv()

# ===========================
# Bot Setup
# ===========================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)

# Rate limiting data
bot.ask_rate_limit = defaultdict(list)
bot.conversations = defaultdict(list)  # In-memory cache for AI conversation
bot.last_message_id = {}  # Store last message IDs for threaded replies

# ===========================
# Flask Web Server to Keep Bot Alive
# ===========================
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

# ===========================
# MongoDB Setup (with SSL Fix)
# ===========================
client = None
db = None
conversations_collection = None
reminders_collection = None
link_collection = None  # New collection for /link command

mongo_uri = os.getenv("MONGO_URI")
if not mongo_uri:
    print("[!] MONGO_URI not found in environment. MongoDB will be disabled.")
else:
    try:
        client = MongoClient(mongo_uri, tlsCAFile=certifi.where())
        db = client.ai_bot
        conversations_collection = db.conversations
        reminders_collection = db.reminders
        link_collection = db.linked_accounts  # Dedicated collection
        
        # Create TTL indexes (only for temporary collections)
        conversations_collection.create_index("timestamp", expireAfterSeconds=604800)  # 7 days
        reminders_collection.create_index("reminder_time", expireAfterSeconds=2592000)  # 30 days
        link_collection.create_index("discord_id", unique=True)  # Ensure one entry per user

        print("✅ Successfully connected to MongoDB")
    except Exception as e:
        print(f"[!] Failed to connect to MongoDB: {e}")

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

if reminders_collection is not None:
    check_reminders.start()

# ===========================
# Owner-only Direct Message Commands
# ===========================
# Define the BOT_OWNER_ID directly in the code
BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID"))

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

# ===========================
# AI Commands
# ===========================

@bot.tree.command(name="ask", description="Chat with an AI assistant using Llama 3")
@app_commands.describe(prompt="What would you like to ask?")
async def ask(interaction: discord.Interaction, prompt: str):
    user_id = interaction.user.id
    channel_id = interaction.channel.id

    # Defer response immediately
    await interaction.response.defer()

    # Rate limiting: 5 requests per minute
    current_time = asyncio.get_event_loop().time()
    timestamps = bot.ask_rate_limit[user_id]
    
    # Clean up old timestamps before appending new one
    bot.ask_rate_limit[user_id] = [t for t in timestamps if current_time - t <= 60]
    bot.ask_rate_limit[user_id].append(current_time)

    if len(bot.ask_rate_limit[user_id]) > 5:
        await interaction.followup.send("⏳ You're being rate-limited. Please wait a minute.")
        return

    async with interaction.channel.typing():
        try:
            # Custom filter for creator questions
            normalized_prompt = prompt.strip().lower()
            if normalized_prompt in ["who made you", "who created you", "who created this bot", "who made this bot"]:
                embed = discord.Embed(description="I was created by **Neroniel**.", color=discord.Color.from_rgb(0, 0, 0))
                embed.set_footer(text="Neroniel AI")
                embed.timestamp = datetime.now(PH_TIMEZONE)
                msg = await interaction.followup.send(embed=embed)
                bot.last_message_id[(user_id, channel_id)] = msg.id
                return

            # Language Detection
            try:
                detected_lang = detect(prompt)
            except LangDetectException:
                detected_lang = "en"  # Default to English

            lang_instruction = {
                "tl": "Please respond in Tagalog.",
                "es": "Por favor responde en español.",
                "fr": "Veuillez répondre en français.",
                "ja": "日本語で答えてください。",
                "ko": "한국어로 답변해 주세요。",
                "zh": "请用中文回答。",
                "ru": "Пожалуйста, отвечайте на русском языке。",
                "ar": "من فضلك أجب بالعربية。",
                "vi": "Vui lòng trả lời bằng tiếng Việt.",
                "th": "กรุณาตอบเป็นภาษาไทย",
                "id": "Silakan jawab dalam bahasa Indonesia"
            }.get(detected_lang, "")

            # Load conversation history from MongoDB (if available)
            history = []
            if conversations_collection:
                if not bot.conversations[user_id]:
                    history_docs = conversations_collection.find({"user_id": user_id}).sort("timestamp", -1).limit(5)
                    for doc in history_docs:
                        bot.conversations[user_id].append({
                            "user": doc["prompt"],
                            "assistant": doc["response"]
                        })
                    bot.conversations[user_id].reverse()  # Maintain order
                history = bot.conversations[user_id][-5:]

            # Build full prompt with language instruction
            system_prompt = f"You are a helpful and friendly AI assistant named Neroniel AI. {lang_instruction}"
            full_prompt = system_prompt
            for msg in history:
                full_prompt += f"User: {msg['user']}\nAssistant: {msg['assistant']}\n"
            full_prompt += f"User: {prompt}\nAssistant:"

            # Call Together AI using async aiohttp instead of requests
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

            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.post(
                    "https://api.together.xyz/v1/completions", 
                    headers=headers,
                    json=payload
                ) as response:
                    if response.status != 200:
                        text = await response.text()
                        await interaction.followup.send(f"❌ API returned error code {response.status}: `{text}`")
                        return
                    data = await response.json()

            if 'error' in data:
                await interaction.followup.send(f"❌ Error from AI API: {data['error']['message']}")
                return

            ai_response = data["choices"][0]["text"].strip()

            # Determine if we should reply to a previous message
            target_message_id = bot.last_message_id.get((user_id, channel_id))

            # Send the AI response
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

            # Update the last message ID for future replies
            bot.last_message_id[(user_id, channel_id)] = reply.id

            # Store in memory and MongoDB
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
            print(f"[EXCEPTION] /ask command error: {e}")

# /clearhistory - Clear stored conversation history
@bot.tree.command(name="clearhistory", description="Clear your AI conversation history")
async def clearhistory(interaction: discord.Interaction):
    user_id = interaction.user.id
    # Clear local memory
    if user_id in bot.conversations:
        bot.conversations[user_id].clear()
    # Clear MongoDB history
    if conversations_collection:
        conversations_collection.delete_many({"user_id": user_id})
    await interaction.response.send_message("✅ Your AI conversation history has been cleared!", ephemeral=True)

# ===========================
# Utility Commands
# ===========================

# /userinfo - Display user information
@bot.tree.command(name="userinfo", description="Display detailed information about a user")
@app_commands.describe(user="The user to get info for (optional, defaults to you)")
async def userinfo(interaction: discord.Interaction, user: discord.User = None):
    if user is None:
        user = interaction.user

    created_at = user.created_at.astimezone(PH_TIMEZONE).strftime("%B %d, %Y • %I:%M %p GMT+8")

    if isinstance(user, discord.Member):
        joined_at = user.joined_at.astimezone(PH_TIMEZONE).strftime("%B %d, %Y • %I:%M %p GMT+8") if user.joined_at else "Unknown"
        roles = [role.mention for role in user.roles if not role.is_default()]
        roles_str = ", ".join(roles) if roles else "No Roles"
        boost_since = user.premium_since.astimezone(PH_TIMEZONE).strftime("%B %d, %Y • %I:%M %p GMT+8") if user.premium_since else "Not Boosting"
        is_bot = user.bot
    else:
        joined_at = "Not in Server"
        roles_str = "N/A"
        boost_since = "Not Boosting"
        is_bot = user.bot

    embed = discord.Embed(color=discord.Color.green())
    embed.add_field(name="Username", value=f"{user.mention}", inline=False)
    embed.add_field(name="Display Name", value=f"`{user.display_name}`", inline=True)
    embed.add_field(name="User ID", value=f"`{user.id}`", inline=True)
    embed.add_field(name="Created Account", value=f"`{created_at}`", inline=False)
    embed.add_field(name="Joined Server", value=f"`{joined_at}`", inline=False)

    if isinstance(user, discord.Member):
        embed.add_field(name="Roles", value=roles_str, inline=False)

    embed.add_field(name="Server Booster Since", value=f"`{boost_since}`", inline=False)

    if is_bot:
        embed.add_field(name="Bot Account", value="✅ Yes", inline=True)

    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)

    await interaction.response.send_message(embed=embed)

# ===========================
# Announcement Command
# ===========================
@bot.tree.command(name="announcement", description="Send an embedded announcement to a specific channel")
@app_commands.describe(message="The message to include in the announcement", channel="The channel to send the announcement to")
async def announcement(interaction: discord.Interaction, message: str, channel: discord.TextChannel):
    BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID"))
    is_owner = interaction.user.id == BOT_OWNER_ID
    is_admin = interaction.user.guild_permissions.administrator
    if not is_owner and not is_admin:
        await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
        return
    embed = discord.Embed(
        title="ANNOUNCEMENT",
        description=f"```\n{message}\n```",
        color=discord.Color.from_rgb(0, 0, 0)
    )
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)
    try:
        await channel.send(embed=embed)
        await interaction.response.send_message(f"✅ Announcement sent to {channel.mention}", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(f"❌ I don't have permission to send messages in {channel.mention}.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ An error occurred: {str(e)}", ephemeral=True)

# ===========================
# Conversion Commands
# ===========================

# Payout Rate
@bot.tree.command(name="payout", description="Convert Robux to PHP based on Payout rate (₱330 for 1000 Robux)")
@app_commands.describe(robux="How much Robux do you want to convert?")
async def payout(interaction: discord.Interaction, robux: int):
    if robux <= 0:
        await interaction.response.send_message("❗ Robux amount must be greater than zero.")
        return
    php = robux * (330 / 1000)
    embed = discord.Embed(color=discord.Color.from_rgb(0, 0, 0))
    embed.add_field(name="Amount:", value=f"{robux} Robux", inline=False)
    embed.add_field(name="Payment:", value=f"₱{php:.2f} PHP", inline=False)
    embed.add_field(name="Note:", value="To be eligible for a payout, you must be a member of the group for at least 14 days. Please ensure this requirement is met before proceeding with any transaction. You can view the Group Link by typing `/group` in the chat.", inline=False)
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="payoutreverse", description="Convert PHP to Robux based on Payout rate (₱330 for 1000 Robux)")
@app_commands.describe(php="How much PHP do you want to convert?")
async def payoutreverse(interaction: discord.Interaction, php: float):
    if php <= 0:
        await interaction.response.send_message("❗ PHP amount must be greater than zero.")
        return
    robux = round((php / 330) * 1000)
    embed = discord.Embed(color=discord.Color.from_rgb(0, 0, 0))
    embed.add_field(name="Payment:", value=f"₱{php:.2f} PHP", inline=False)
    embed.add_field(name="Amount:", value=f"{robux} Robux", inline=False)
    embed.add_field(name="Note:", value="To be eligible for a payout, you must be a member of the group for at least 14 days. Please ensure this requirement is met before proceeding with any transaction. You can view the Group Link by typing `/group` in the chat.", inline=False)
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)
    await interaction.response.send_message(embed=embed)

# Gift Rate
@bot.tree.command(name="gift", description="Convert Robux to PHP based on Gift rate (₱260 for 1000 Robux)")
@app_commands.describe(robux="How much Robux do you want to convert?")
async def gift(interaction: discord.Interaction, robux: int):
    if robux <= 0:
        await interaction.response.send_message("❗ Robux amount must be greater than zero.")
        return
    php = robux * (260 / 1000)
    embed = discord.Embed(color=discord.Color.from_rgb(0, 0, 0))
    embed.add_field(name="Amount:", value=f"{robux} Robux", inline=False)
    embed.add_field(name="Payment:", value=f"₱{php:.2f} PHP", inline=False)
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="giftreverse", description="Convert PHP to Robux based on Gift rate (₱260 for 1000 Robux)")
@app_commands.describe(php="How much PHP do you want to convert?")
async def giftreverse(interaction: discord.Interaction, php: float):
    if php <= 0:
        await interaction.response.send_message("❗ PHP amount must be greater than zero.")
        return
    robux = round((php / 260) * 1000)
    embed = discord.Embed(color=discord.Color.from_rgb(0, 0, 0))
    embed.add_field(name="Payment:", value=f"₱{php:.2f} PHP", inline=False)
    embed.add_field(name="Amount:", value=f"{robux} Robux", inline=False)
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)
    await interaction.response.send_message(embed=embed)
    
# NCT Rate
@bot.tree.command(name="nct", description="Convert Robux to PHP based on NCT rate (₱245/1k)")
@app_commands.describe(robux="How much Robux do you want to convert?")
async def nct(interaction: discord.Interaction, robux: int):
    if robux <= 0:
        await interaction.response.send_message("❗ Invalid input.")
        return
    php = robux * (245 / 1000)
    embed = discord.Embed(color=discord.Color.from_rgb(0, 0, 0))
    embed.add_field(name="Amount:", value=f"{robux} Robux", inline=False)
    embed.add_field(name="Payment:", value=f"₱{php:.2f} PHP", inline=False)
    embed.add_field(name="Note:", value="To proceed with this transaction, you must own the required Gamepass and have Regional Pricing disabled. Please ensure these requirements are met before proceeding with any transaction. You may view the Gamepass details by typing `/gamepass` in the chat and providing your Gamepass ID or Creator Dashboard URL.", inline=False)
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="nctreverse", description="Convert PHP to Robux based on NCT rate (₱245/1k)")
@app_commands.describe(php="How much PHP do you want to convert?")
async def nctreverse(interaction: discord.Interaction, php: float):
    if php <= 0:
        await interaction.response.send_message("❗ PHP amount must be greater than zero.")
        return
    robux = round((php / 245) * 1000)
    embed = discord.Embed(color=discord.Color.from_rgb(0, 0, 0))
    embed.add_field(name="Payment:", value=f"₱{php:.2f} PHP", inline=False)
    embed.add_field(name="Amount:", value=f"{robux} Robux", inline=False)
    embed.add_field(name="Note:", value="To proceed with this transaction, you must own the required Gamepass and have Regional Pricing disabled. Please ensure these requirements are met before proceeding with any transaction. You may view the Gamepass details by typing `/gamepass` in the chat and providing your Gamepass ID or Creator Dashboard URL.", inline=False)
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)
    await interaction.response.send_message(embed=embed)

# CT Rate 
@bot.tree.command(name="ct", description="Convert Robux to PHP based on CT rate (₱350/1k)")
@app_commands.describe(robux="How much Robux do you want to convert?")
async def ct(interaction: discord.Interaction, robux: int):
    if robux <= 0:
        await interaction.response.send_message("❗ Invalid input.")
        return
    php = robux * (350 / 1000)
    embed = discord.Embed(color=discord.Color.from_rgb(0, 0, 0))
    embed.add_field(name="Amount:", value=f"{robux} Robux", inline=False)
    embed.add_field(name="Payment:", value=f"₱{php:.2f} PHP", inline=False)
    embed.add_field(name="Note:", value="To proceed with this transaction, you must own the required Gamepass and have Regional Pricing disabled. Please ensure these requirements are met before proceeding with any transaction. You may view the Gamepass details by typing `/gamepass` in the chat and providing your Gamepass ID or Creator Dashboard URL.", inline=False)
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="ctreverse", description="Convert PHP to Robux based on CT rate (₱350/1k)")
@app_commands.describe(php="How much PHP do you want to convert?")
async def ctreverse(interaction: discord.Interaction, php: float):
    if php <= 0:
        await interaction.response.send_message("❗ PHP amount must be greater than zero.")
        return
    robux = round((php / 350) * 1000)
    embed = discord.Embed(color=discord.Color.from_rgb(0, 0, 0))
    embed.add_field(name="Payment:", value=f"₱{php:.2f} PHP", inline=False)
    embed.add_field(name="Amount:", value=f"{robux} Robux", inline=False)
    embed.add_field(name="Note:", value="To proceed with this transaction, you must own the required Gamepass and have Regional Pricing disabled. Please ensure these requirements are met before proceeding with any transaction. You may view the Gamepass details by typing `/gamepass` in the chat and providing your Gamepass ID or Creator Dashboard URL.", inline=False)
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)
    await interaction.response.send_message(embed=embed)

# All Rates Comparison
@bot.tree.command(name="allrates", description="See PHP equivalent across all rates for given Robux")
@app_commands.describe(robux="How much Robux do you want to compare?")
async def allrates(interaction: discord.Interaction, robux: int):
    if robux <= 0:
        await interaction.response.send_message("❗ Robux amount must be greater than zero.")
        return

    rates = {
        "Not Covered Tax (₱245)": 245,
        "Covered Tax (₱350)": 350,
        "Group Payout (₱330)": 330,
        "Gift (₱260)": 260
    }

    embed = discord.Embed(
        title="Robux Conversion Rates",
        color=discord.Color.from_rgb(0, 0, 0)  # Black color
    )

    for label, value in rates.items():
        php_value = (value / 1000) * robux
        embed.add_field(
            name="• " + label,
            value=f"₱{php_value:.2f}",
            inline=False
        )

    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="allratesreverse", description="See Robux equivalent across all rates for given PHP")
@app_commands.describe(php="How much PHP do you want to compare?")
async def allratesreverse(interaction: discord.Interaction, php: float):
    if php <= 0:
        await interaction.response.send_message("❗ PHP amount must be greater than zero.")
        return

    rates = {
        "Not Covered Tax (₱245)": 245,
        "Covered Tax (₱350)": 350,
        "Group Payout (₱330)": 330,
        "Gift (₱260)": 260
    }

    embed = discord.Embed(
        title="PHP to Robux Conversion",
        color=discord.Color.from_rgb(0, 0, 0)  # Black color
    )

    for label, value in rates.items():
        robux_value = round((php / value) * 1000)
        embed.add_field(
            name="• " + label,
            value=f"{robux_value} Robux",
            inline=False
        )

    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)

    await interaction.response.send_message(embed=embed)

# Tax Calculations
@bot.tree.command(name="beforetax", description="Calculate how much Robux you'll receive after 30% tax")
@app_commands.describe(robux="How much Robux is being sent?")
async def beforetax(interaction: discord.Interaction, robux: int):
    if robux <= 0:
        await interaction.response.send_message("❗ Robux amount must be greater than zero.")
        return
    
    received = math.floor(robux * 0.7)
    
    embed = discord.Embed(
        color=discord.Color.from_rgb(0, 0, 0)
    )
    embed.add_field(name="Required to Send:", value=f"{robux} Robux", inline=False)
    embed.add_field(name="Target Receive:", value=f"{received} Robux", inline=False)
    embed.add_field(
        name="Note:",
        value="Roblox applies a 30% fee on transactions within its marketplace, including buying and selling items. This fee is deducted from the total transaction value.",
        inline=False
    )
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="aftertax", description="Calculate how much Robux to send to receive desired amount after 30% tax")
@app_commands.describe(target="How much Robux do you want to receive after tax?")
async def aftertax(interaction: discord.Interaction, target: int):
    if target <= 0:
        await interaction.response.send_message("❗ Target Robux must be greater than zero.")
        return
    
    sent = math.ceil(target / 0.7)
    
    embed = discord.Embed(
        color=discord.Color.from_rgb(0, 0, 0)
    )
    embed.add_field(name="Target Receive:", value=f"{target} Robux", inline=False)
    embed.add_field(name="Required to Send:", value=f"{sent} Robux", inline=False)
    embed.add_field(
        name="Note:",
        value="Roblox applies a 30% fee on transactions within its marketplace. To receive a specific amount, you must account for this deduction by sending more than your target.",
        inline=False
    )
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)
    
    await interaction.response.send_message(embed=embed)

# ConvertCurrency
@bot.tree.command(name="convertcurrency", description="Convert between two currencies")
@app_commands.describe(amount="Amount to convert", from_currency="Currency to convert from (e.g., USD)", to_currency="Currency to convert to (e.g., PHP)")
async def convertcurrency(interaction: discord.Interaction, amount: float, from_currency: str, to_currency: str):
    api_key = os.getenv("CURRENCY_API_KEY")
    if not api_key:
        await interaction.response.send_message("❌ `CURRENCY_API_KEY` missing.", ephemeral=True)
        return
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()
    url = f"https://api.currencyapi.com/v3/latest?apikey= {api_key}&currencies={to_currency}&base_currency={from_currency}"
    try:
        response = requests.get(url)
        data = response.json()
        if 'error' in data:
            await interaction.response.send_message(f"❌ API Error: {data['error']['message']}")
            print("API Error Response:", data)
            return
        if "data" not in data or to_currency not in data["data"]:
            await interaction.response.send_message("❌ Invalid currency code or no data found.")
            return
        rate = data["data"][to_currency]["value"]
        result = amount * rate
        embed = discord.Embed(title=f"💱 Currency Conversion", color=discord.Color.gold())
        embed.add_field(name="📥 Input", value=f"{amount} {from_currency}", inline=False)
        embed.add_field(name="📉 Rate", value=f"1 {from_currency} = {rate:.4f} {to_currency}", inline=False)
        embed.add_field(name="📤 Result", value=f"≈ **{result:.2f} {to_currency}**", inline=False)
        embed.set_footer(text="Neroniel")
        embed.timestamp = datetime.now(PH_TIMEZONE)
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error during conversion: {str(e)}")
        print("Exception Details:", str(e))

@convertcurrency.autocomplete('from_currency')
@convertcurrency.autocomplete('to_currency')
async def currency_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    # Full list of supported currencies with names
    currencies = [
        "USD - US Dollar", "EUR - Euro", "JPY - Japanese Yen", "GBP - British Pound",
        "AUD - Australian Dollar", "CAD - Canadian Dollar", "CHF - Swiss Franc",
        "CNY - Chinese Yuan", "SEK - Swedish Krona", "NZD - New Zealand Dollar",
        "BRL - Brazilian Real", "INR - Indian Rupee", "RUB - Russian Ruble",
        "ZAR - South African Rand", "SGD - Singapore Dollar", "HKD - Hong Kong Dollar",
        "KRW - South Korean Won", "MXN - Mexican Peso", "TRY - Turkish Lira",
        "EGP - Egyptian Pound", "AED - UAE Dirham", "SAR - Saudi Riyal",
        "ARS - Argentine Peso", "CLP - Chilean Peso", "THB - Thai Baht",
        "MYR - Malaysian Ringgit", "IDR - Indonesian Rupiah", "PHP - Philippine Peso",
        "PLN - Polish Zloty"
    ]
    filtered = [c for c in currencies if current.lower() in c.lower()]
    return [
        app_commands.Choice(name=c, value=c.split(" ")[0])
        for c in filtered[:25]
    ]

# ========== Weather Command ==========
PHILIPPINE_CITIES = [
    "Manila", "Quezon City", "Caloocan", "Las PiÃ±as", "Makati",
    "Malabon", "Navotas", "Paranaque", "Pasay", "Muntinlupa",
    "Taguig", "Valenzuela", "Marikina", "Pasig", "San Juan",
    "Cavite", "Cebu", "Davao", "Iloilo", "Baguio", "Zamboanga",
    "Angeles", "Bacolod", "Batangas", "Cagayan de Oro", "Cebu City",
    "Davao City", "General Santos", "Iligan", "Kalibo", "Lapu-Lapu City",
    "Lucena", "Mandaue", "Olongapo", "Ormoc", "Oroquieta", "Ozamiz",
    "Palawan", "Puerto Princesa", "Roxas City", "San Pablo", "Silay"
]
GLOBAL_CAPITAL_CITIES = [
    "Washington D.C.", "London", "Paris", "Berlin", "Rome",
    "Moscow", "Beijing", "Tokyo", "Seoul", "New Delhi", "Islamabad",
    "Canberra", "Ottawa", "Brasilia", "Ottawa", "Cairo", "Nairobi",
    "Pretoria", "Kuala Lumpur", "Jakarta", "Bangkok", "Hanoi", "Athens",
    "Vienna", "Stockholm", "Oslo", "Copenhagen", "Helsinki", "Dublin",
    "Warsaw", "Prague", "Madrid", "Amsterdam", "Brussels", "Bern",
    "Wellington", "Santiago", "Buenos Aires", "Brasilia", "Abu Dhabi",
    "Doha", "Riyadh", "Kuwait City", "Muscat", "Manama", "Doha",
    "Beijing", "Shanghai", "Tokyo", "Seoul", "Sydney", "Melbourne"
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
        await interaction.response.send_message("❌ Weather API key is missing.", ephemeral=True)
        return
    url = f"http://api.weatherapi.com/v1/current.json?key={api_key}&q={city}"
    try:
        response = requests.get(url)
        data = response.json()
        if "error" in data:
            await interaction.response.send_message("❌ City not found or invalid input.", ephemeral=True)
            return
        current = data["current"]
        location = data["location"]["name"]
        region = data["location"]["region"]
        country = data["location"]["country"]
        if unit == "c":
            temperature = current["temp_c"]
            feels_like = current["feelslike_c"]
            unit_label = "°C"
        else:
            temperature = current["temp_f"]
            feels_like = current["feelslike_f"]
            unit_label = "°F"
        humidity = current["humidity"]
        wind_kph = current["wind_kph"]
        condition = current["condition"][0]["text"]
        icon_url = f"https:{current['condition'][0]['icon']}"

        embed = discord.Embed(
            title=f"🌤️ Weather in {location}, {region}, {country}",
            color=discord.Color.from_rgb(0, 0, 0)
        )
        embed.add_field(name="🌡️ Temperature", value=f"{temperature}{unit_label}", inline=True)
        embed.add_field(name="🧯 Feels Like", value=f"{feels_like}{unit_label}", inline=True)
        embed.add_field(name="💧 Humidity", value=f"{humidity}%", inline=True)
        embed.add_field(name="🌬️ Wind Speed", value=f"{wind_kph} km/h", inline=True)
        embed.add_field(name="📝 Condition", value=condition, inline=False)
        embed.set_thumbnail(url=icon_url)
        embed.set_footer(text="Powered by WeatherAPI • Neroniel")
        embed.timestamp = datetime.now(PH_TIMEZONE)
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error fetching weather: {str(e)}", ephemeral=True)

@weather.autocomplete('city')
async def city_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    # Combine Philippine and global capitals
    all_cities = PHILIPPINE_CITIES + GLOBAL_CAPITAL_CITIES
    # Filter based on user input
    filtered = [c for c in all_cities if current.lower() in c.lower()]
    return [
        app_commands.Choice(name=c, value=c)
        for c in filtered[:25]
    ]

# ===========================
# Other Commands
# ===========================

# Purge Command
@bot.tree.command(name="purge", description="Delete a specified number of messages")
@app_commands.describe(amount="How many messages would you like to delete?")
async def purge(interaction: discord.Interaction, amount: int):
    if amount <= 0:
        await interaction.response.send_message("❗ Please specify a positive number of messages.", ephemeral=True)
        return

    BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID"))
    has_permission = interaction.user.guild_permissions.manage_messages or interaction.user.id == BOT_OWNER_ID
    if not has_permission:
        await interaction.response.send_message("❗ You don't have permission to use this command.", ephemeral=True)
        return

    if not interaction.guild.me.guild_permissions.manage_messages:
        await interaction.response.send_message("❗ I don't have permission to delete messages.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"✅ Deleted **{len(deleted)}** messages.", ephemeral=True)

# Group Info Command
@bot.tree.command(name="group", description="Display information about the 1cy Roblox group")
async def groupinfo(interaction: discord.Interaction):
    group_id = 5838002
    try:
        response = requests.get(f"https://groups.roblox.com/v1/groups/{group_id}")
        data = response.json()
        formatted_members = "{:,}".format(data['memberCount'])
        embed = discord.Embed(color=discord.Color.from_rgb(0, 0, 0))
        embed.add_field(name="Group Name", value=f"[{data['name']}](https://www.roblox.com/groups/{group_id})", inline=False)
        embed.add_field(name="Description", value=f"```\n{data.get('description', 'No description')}\n```", inline=False)
        embed.add_field(name="Group ID", value=str(data['id']), inline=True)
        owner = data.get('owner')
        owner_link = f"[{owner['username']}](https://www.roblox.com/users/{owner['userId']}/profile)" if owner else "No Owner"
        embed.add_field(name="Owner", value=owner_link, inline=True)
        embed.add_field(name="Members", value=formatted_members, inline=True)
        embed.set_footer(text="Neroniel")
        embed.timestamp = discord.utils.utcnow()
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
    if amount <= 0:
        await interaction.response.send_message("❗ Amount must be greater than zero.", ephemeral=True)
        return
    total_seconds = {"seconds": amount, "minutes": amount * 60, "hours": amount * 3600}.get(unit.value, 0)
    if total_seconds == 0:
        await interaction.response.send_message("❗ Invalid time unit selected.", ephemeral=True)
        return
    if total_seconds > 86400:
        await interaction.response.send_message("❗ Duration cannot exceed 24 hours.", ephemeral=True)
        return
    embed = discord.Embed(title="📊 Poll", description=question, color=discord.Color.orange())
    embed.set_footer(text="Neroniel")
    embed.timestamp = discord.utils.utcnow()
    message = await interaction.channel.send(embed=embed)
    await message.add_reaction("👍")
    await message.add_reaction("👎")
    await interaction.response.send_message("✅ Poll created!", ephemeral=True)
    await asyncio.sleep(total_seconds)
    message = await interaction.channel.fetch_message(message.id)
    reactions = message.reactions
    up_count = next((r.count for r in reactions if str(r.emoji) == "👍"), 0)
    down_count = next((r.count for r in reactions if str(r.emoji) == "👎"), 0)
    if up_count > down_count:
        result = "👍 Upvotes win!"
    elif down_count > up_count:
        result = "👎 Downvotes win!"
    else:
        result = "⚖️ It's a tie!"
    result_embed = discord.Embed(title="📊 Poll Results", description=question, color=discord.Color.green())
    result_embed.add_field(name="👍 Upvotes", value=str(up_count), inline=True)
    result_embed.add_field(name="👎 Downvotes", value=str(down_count), inline=True)
    result_embed.add_field(name="Result", value=result, inline=False)
    result_embed.set_footer(text="Poll has ended")
    result_embed.timestamp = discord.utils.utcnow()
    await message.edit(embed=result_embed)

# Remind Me Command
@bot.tree.command(name="remindme", description="Set a reminder after X minutes (will ping you in this channel)")
@app_commands.describe(minutes="How many minutes until I remind you?", note="Your reminder message")
async def remindme(interaction: discord.Interaction, minutes: int, note: str):
    if minutes <= 0:
        await interaction.response.send_message("❗ Please enter a positive number of minutes.", ephemeral=True)
        return
    reminder_time = datetime.utcnow() + timedelta(minutes=minutes)
    if reminders_collection:
        reminders_collection.insert_one({
            "user_id": interaction.user.id,
            "guild_id": interaction.guild.id,
            "channel_id": interaction.channel.id,
            "note": note,
            "reminder_time": reminder_time
        })
    await interaction.response.send_message(
        f"⏰ I'll remind you in `{minutes}` minutes: `{note}`",
        ephemeral=True
    )

# Donate Command
@bot.tree.command(name="donate", description="Donate Robux to a Discord user.")
@app_commands.describe(user="The user to donate to.", amount="The amount of Robux to donate.")
async def donate(interaction: discord.Interaction, user: discord.Member, amount: int):
    if amount <= 0:
        await interaction.response.send_message("❗ Robux amount must be greater than zero.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"`{interaction.user.name}` just donated **{amount:,} Robux** to {user.mention}!"
    )

# Say Command
@bot.tree.command(name="say", description="Make the bot say something in chat (no @everyone/@here allowed)")
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
        description="A categorized list of all commands for easy navigation.",
        color=discord.Color.from_rgb(0, 0, 0)  # Black
    )
    
    # 🤖 AI Assistant
    embed.add_field(
        name="🤖 AI Assistant",
        value="""
- `/ask <prompt>` - Chat with Llama 3 AI  
- `/clearhistory` - Clear your AI conversation history
        """,
        inline=False
    )

    # 💰 Currency Conversion
    embed.add_field(
        name="💰 Currency Conversion",
        value="""
- `/payout <robux>` - Convert Robux to PHP at Payout rate (₱330/1000)
- `/payoutreverse <php>` - Convert PHP to Robux at Payout rate
- `/gift <robux>` - Convert Robux to PHP at Gift rate (₱260/1000)
- `/giftreverse <php>` - Convert PHP to Robux at Gift rate
- `/nct <robux>` - Convert Robux to PHP at NCT rate (₱245/1k)
- `/nctreverse <php>` - Convert PHP to Robux at NCT rate
- `/ct <robux>` - Convert Robux to PHP at CT rate (₱350/1k)
- `/ctreverse <php>` - Convert PHP to Robux at CT rate
- `/convertcurrency <amount> <from> <to>` - Convert between currencies
- `/devex [usd/robux] <amount>` - Convert USD ↔ Robux using DevEx rate
        """,
        inline=False
    )

    # 🛠️ Utility Tools
    embed.add_field(
        name="🛠️ Utility Tools",
        value="""
- `/userinfo [user]` - View detailed info about a user  
- `/purge <amount>` - Delete messages (requires mod permissions)    
- `/group` - Show info about the 1cy Roblox Group  
- `/groupfunds` - Show Current Funds of the 1cy Group 
- `/robuxstocks` - Check current Robux Stocks
- `/announcement <message> <channel>` - Send an embedded announcement
- `/gamepass <id>` - Show a public Roblox Gamepass Link using an ID or Creator Dashboard URL
- `/avatar [user]` - Display a user's profile picture
- `/banner [user]` - Display a user's bannner
        """,
        inline=False
    )

    # ⏰ Reminders & Polls
    embed.add_field(
        name="⏰ Reminders & Polls",
        value="""
- `/remindme <minutes> <note>` - Set a personal reminder  
- `/poll <question> <time> <unit>` - Create a timed poll  
        """,
        inline=False
    )

    # 🎁 Fun Commands
    embed.add_field(
        name="🎉 Fun",
        value="""
- `/donate <user> <amount>` - Donate Robux to someone
- `/say <message>` - Make the bot say something
- `/calculator <num1> <operation> <num2>` - Perform math operations
- `/weather <city> [unit]` - Get weather in a city  
- `/tiktok <link>` - Convert an Tiktok Link into a Video
- `/instagram <link>` - Convert an Instagram Link into a Video
        """,
        inline=False
    )

    # 🔧 Developer Tools
    embed.add_field(
        name="🔧 Developer Tools",
        value="""
- `/dm <user> <message>` - Send a direct message to a specific user  
- `/dmall <message>` - Send a direct message to all members in the server
- `/invite` - Get the invite link for the bot  
- `/status` - Show how many servers the bot is in and total user count
- `/payment <method>` - Show payment instructions (Gcash/PayMaya/GoTyme)
        """,
        inline=False
    )

    # Footer
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)
    await interaction.response.send_message(embed=embed)


# ===========================
# Payment Command
# ===========================
class PaymentMethod(str, Enum):
    GCASH = "Gcash"
    PAYMAYA = "PayMaya"
    GOTYME = "GoTyme"
@bot.tree.command(name="payment", description="Show payment instructions for Gcash, PayMaya, or GoTyme")
@app_commands.describe(method="Choose a payment method to display instructions")
@app_commands.choices(method=[
    app_commands.Choice(name=PaymentMethod.GCASH, value=PaymentMethod.GCASH),
    app_commands.Choice(name=PaymentMethod.PAYMAYA, value=PaymentMethod.PAYMAYA),
    app_commands.Choice(name=PaymentMethod.GOTYME, value=PaymentMethod.GOTYME),
])
async def payment(interaction: discord.Interaction, method: PaymentMethod):
    payment_info = {
        PaymentMethod.GCASH: {
            "title": "Gcash Payment",
            "description": "Account Initials: M R G.\nAccount Number: `09550333612`",
            "image": "https://raw.githubusercontent.com/KxroAI/whatupmyniggga/c52d0cb1f626fd55d24a6181fd3821c9dd9f1455/IMG_2868.jpeg"
        },
        PaymentMethod.PAYMAYA: {
            "title": "PayMaya Payment",
            "description": "Account Initials: N G.\nAccount Number: `09550333612`",
            "image": "https://raw.githubusercontent.com/KxroAI/whatupmyniggga/refs/heads/main/IMG_2869.jpeg"
        },
        PaymentMethod.GOTYME: {
            "title": "GoTyme Payment",
            "description": "Account Initials: N G.\nAccount Number: HIDDEN",
            "image": "https://raw.githubusercontent.com/KxroAI/whatupmyniggga/refs/heads/main/IMG_2870.jpeg"
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

# ========== Avatar Command ==========
@bot.tree.command(name="avatar", description="Display a user's profile picture")
@app_commands.describe(user="The user whose avatar you want to see")
async def avatar(interaction: discord.Interaction, user: discord.Member = None):
    if user is None:
        user = interaction.user  

    embed = discord.Embed(
        title=f"{user}'s Avatar",
        color=discord.Color.from_rgb(0, 0, 0)
    )
    embed.set_image(url=user.display_avatar.url)
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)

    await interaction.response.send_message(embed=embed)

# ========== Banner Command ==========
@bot.tree.command(name="banner", description="Display a user's banner")
@app_commands.describe(user="The user whose banner you want to see")
async def banner(interaction: discord.Interaction, user: discord.User = None):
    if user is None:
        user = interaction.user

    try:
        fetched_user = await bot.fetch_user(user.id)
    except discord.NotFound:
        await interaction.response.send_message("❌ User not found.", ephemeral=True)
        return

    banner_url = fetched_user.banner.url if fetched_user.banner else None
    server_banner_url = None

    if interaction.guild:
        try:
            member = await interaction.guild.fetch_member(user.id)
            if member.guild_avatar:
                server_banner_url = member.guild_avatar.url
        except discord.NotFound:
            pass

    embed = discord.Embed(
        color=discord.Color.from_rgb(0, 0, 0)
    )

    if banner_url:
        embed.set_image(url=banner_url)
    elif server_banner_url:
        embed.set_image(url=server_banner_url)
    else:
        embed.description = f"**{user.mention} has no banner or server banner.**"

    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)

    await interaction.response.send_message(embed=embed)

# ========== Invite Command ==========
@bot.tree.command(name="invite", description="Get the invite link for the bot")
async def invite(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🔗 Invite N Bot",
        description="Click [here](https://discord.com/oauth2/authorize?client_id=1358242947790803084&permissions=8&integration_type=0&scope=bot%20applications.commands ) to invite the bot to your server!",
        color=discord.Color.from_rgb(0, 0, 0)  # Black using RGB
    )
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)
    await interaction.response.send_message(embed=embed)

# ========== Status Command ==========
@bot.tree.command(name="status", description="Show how many servers the bot is in and total user count")
async def status(interaction: discord.Interaction):
    guilds = interaction.client.guilds
    total_servers = len(guilds)
    total_users = sum(guild.member_count for guild in guilds)

    description = f"**Total Servers:** {total_servers}\n"
    description += f"**Total Users:** {total_users}\n"

    embed = discord.Embed(
        title="📊 Bot Status",
        description=description,
        color=discord.Color.from_rgb(0, 0, 0)  # Black using RGB
    )
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)

    await interaction.response.send_message(embed=embed)

# ========== Group Funds Command ==========
@bot.tree.command(name="groupfunds", description="Get current Funds of the 1cy Roblox Group")
async def group_funds(interaction: discord.Interaction):
    BOT_OWNER_ID = os.getenv("BOT_OWNER_ID")

    # Check if user is either an Admin or the Bot Owner
    if not interaction.user.guild_permissions.administrator and str(interaction.user.id) != BOT_OWNER_ID:
        await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
        return

    await interaction.response.defer()

    group_id = 5838002  # ← Replace with your group ID if needed
    ROBLOX_COOKIE = os.getenv("ROBLOX_COOKIE")

    if not ROBLOX_COOKIE:
        await interaction.followup.send("❌ Missing `.ROBLOSECURITY` cookie in environment.")
        return

    headers = {
        "Cookie": ROBLOX_COOKIE,
        "User-Agent": "Mozilla/5.0"
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        currency_url = f"https://economy.roblox.com/v1/groups/{group_id}/currency"      
        async with session.get(currency_url) as resp:
            if resp.status != 200:
                try:
                    error_data = await resp.json()
                    error_msg = error_data.get("errors", [{"message": "Unknown"}])[0]["message"]
                except Exception:
                    error_msg = "Unknown error"

                if resp.status == 401:
                    await interaction.followup.send("❌ Unauthorized: Invalid or expired `.ROBLOSECURITY` cookie.")
                elif resp.status == 403:
                    await interaction.followup.send("❌ Forbidden: Account does not have permission to view group funds.")
                else:
                    await interaction.followup.send(f"❌ Failed to fetch group funds: `{error_msg}`")
                return

            currency_data = await resp.json()
            robux = currency_data.get("robux", 0)

    # Format Embed
    embed = discord.Embed(
        color=discord.Color.from_rgb(0, 0, 0)
    )
    embed.add_field(name="Current Balance", value=f"{robux:,} R$", inline=False)
    embed.set_footer(text="Fetched via Roblox API | Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)

    await interaction.followup.send(embed=embed)

# ========== Robux Stocks Command ==========
@bot.tree.command(name="robuxstocks", description="Check the current Robux Stocks")
async def stocks(interaction: discord.Interaction):
    BOT_OWNER_ID = os.getenv("BOT_OWNER_ID")
    # Check if user is either an Admin or the Bot Owner
    if not interaction.user.guild_permissions.administrator and str(interaction.user.id) != BOT_OWNER_ID:
        await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
        return
    await interaction.response.defer()

    # Load environment variables
    roblox_user_id = int(os.getenv("ROBLOX_STOCKS_ID"))  # Target user ID from .env
    ROBLOX_STOCKS = os.getenv("ROBLOX_STOCKS")  # Cookie for authentication

    if not roblox_user_id or not ROBLOX_STOCKS:
        await interaction.followup.send("❌ Missing required environment variables.")
        return

    headers = {
        "Cookie": ROBLOX_STOCKS,
        "User-Agent": "Mozilla/5.0"
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        currency_url = f"https://economy.roblox.com/v1/users/{roblox_user_id}/currency"       
        async with session.get(currency_url) as resp:
            if resp.status != 200:
                try:
                    error_data = await resp.json()
                    error_msg = error_data.get("errors", [{"message": "Unknown"}])[0]["message"]
                except Exception:
                    error_msg = "Unknown error"
                if resp.status == 401:
                    await interaction.followup.send("❌ Unauthorized: Invalid or expired `.ROBLOSECURITY` cookie.")
                elif resp.status == 403:
                    await interaction.followup.send("❌ Forbidden: Account does not have permission to view currency.")
                else:
                    await interaction.followup.send(f"❌ Failed to fetch Robux balance: `{error_msg}`")
                return
            currency_data = await resp.json()
            robux = currency_data.get("robux", 0)

    embed = discord.Embed(
        color=discord.Color.from_rgb(0, 0, 0)
    )
    embed.add_field(name="Current Balance", value=f"{robux:,} R$", inline=False)
    embed.set_footer(text="Fetched via Roblox API | Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)
    await interaction.followup.send(embed=embed)

# ========== Gamepass Command ==========
@bot.tree.command(name="gamepass", description="Show a public Roblox Gamepass Link using an ID or Creator Dashboard URL")
@app_commands.describe(id="The Roblox Gamepass ID", link="Roblox Creator Dashboard URL to convert")
async def gamepass(interaction: discord.Interaction, id: int = None, link: str = None):
    if id is not None and link is not None:
        await interaction.response.send_message("❌ Please provide either an ID or a Link, not both.", ephemeral=True)
        return

    pass_id = None

    # If ID is provided, use that directly
    if id is not None:
        pass_id = id
    elif link is not None:
        # Use regex to extract the Gamepass ID from the Dashboard URL
        match = re.search(r'/passes/(\d+)/', link)
        if match:
            pass_id = match.group(1)
        else:
            await interaction.response.send_message("❌ Invalid Roblox Dashboard Gamepass Link.", ephemeral=True)
            return
    else:
        await interaction.response.send_message("❌ Please provide either a Gamepass ID or a Dashboard Link.", ephemeral=True)
        return

    base_url = f"https://www.roblox.com/game-pass/{pass_id}" 

    embed = discord.Embed(
        color=discord.Color.from_rgb(0, 0, 0)
    )
    embed.add_field(
        name="🔗 Link",
        value=f"`{base_url}`\n\n[View Gamepass]({base_url})",
        inline=False
    )
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)

    await interaction.response.send_message(embed=embed)

# ========== Devex Command ==========
@bot.tree.command(name="devex", description="Convert between Robux and USD using the current DevEx rate")
@app_commands.describe(
    conversion_type="Choose the type of value you're entering",
    amount="The amount of Robux or USD to convert"
)
@app_commands.choices(conversion_type=[
    app_commands.Choice(name="Robux to USD", value="robux"),
    app_commands.Choice(name="USD to Robux", value="usd")
])
async def devex(interaction: discord.Interaction, conversion_type: app_commands.Choice[str], amount: float):
    if amount <= 0:
        await interaction.response.send_message("❗ Please enter a positive amount.", ephemeral=True)
        return

    devex_rate = 0.0035  # $0.0035 per Robux

    if conversion_type.value == "robux":
        robux = amount
        usd = robux * devex_rate
        embed = discord.Embed(
            title="💎 DevEx Conversion: Robux → USD",
            description=f"Converting **{robux} Robux** at the rate of **$0.0035/Robux**:",
            color=discord.Color.green()
        )
        embed.add_field(name="Total USD Value", value=f"**${usd:.4f} USD**", inline=False)
    else:
        usd = amount
        robux = usd / devex_rate
        embed = discord.Embed(
            title="💎 DevEx Conversion: USD → Robux",
            description=f"Converting **${usd:.4f} USD** at the rate of **$0.0035/Robux**:",
            color=discord.Color.from_rgb(0, 0, 0)
        )
        embed.add_field(name="Total Robux Value", value=f"**{int(robux)} Robux**", inline=False)

    embed.add_field(
        name="Note",
        value="This is an estimate based on the current DevEx rate. Actual payout may vary.",
        inline=False
    )
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)

    await interaction.response.send_message(embed=embed)

# ========== Tiktok Command ==========
@bot.tree.command(name="tiktok", description="Convert a TikTok Link into a Video")
@app_commands.describe(link="The TikTok Video URL to Convert", spoiler="Should the video be sent as a spoiler?")
async def tiktok(interaction: discord.Interaction, link: str, spoiler: bool = False):
    await interaction.response.defer(ephemeral=False)  # Changed to False

    try:
        # Create a temporary directory to store the downloaded video
        with tempfile.TemporaryDirectory() as tmpdir:
            original_dir = os.getcwd()
            os.chdir(tmpdir)

            # Download TikTok video using pyktok
            pyk.save_tiktok(link, save_video=True)

            # Find the downloaded MP4 file
            video_files = [f for f in os.listdir(tmpdir) if f.endswith(".mp4")]
            if not video_files:
                await interaction.followup.send("❌ Failed to download TikTok video.")
                return

            video_path = os.path.join(tmpdir, video_files[0])

            # Prepare filename with spoiler prefix if needed
            filename = os.path.basename(video_path)
            if spoiler:
                filename = f"SPOILER_{filename}"

            await interaction.followup.send(
                file=discord.File(fp=video_path, filename=filename),
                ephemeral=False  # Ensures everyone can see the message
            )

            os.chdir(original_dir)

    except Exception as e:
        await interaction.followup.send(f"❌ An error occurred: {str(e)}")

# ========== Instagram Command ==========
@bot.tree.command(name="instagram", description="Convert an Instagram Link into a Video/Image")
@app_commands.describe(link="The Instagram Post URL to Convert", spoiler="Should the media be sent as a spoiler?")
async def instagram(interaction: discord.Interaction, link: str, spoiler: bool = False):
    await interaction.response.defer(ephemeral=False)  # Now visible to everyone

    try:
        # Create a temporary directory to store the downloaded media
        with tempfile.TemporaryDirectory() as tmpdir:
            original_dir = os.getcwd()
            os.chdir(tmpdir)

            loader = instaloader.Instaloader(
                download_pictures=True,
                download_videos=True,
                dirname_pattern=tmpdir,
                save_metadata=False,
                quiet=True
            )

            # Extract shortcode from URL
            shortcode = instaloader.Post.shortcode_from_url(link)
            post = instaloader.Post.from_shortcode(loader.context, shortcode)

            # Download the post
            loader.download_post(post, target="ig_post")

            # Find the downloaded media file (.jpg or .mp4)
            media_files = [f for f in os.listdir(tmpdir) if f.endswith(".jpg") or f.endswith(".mp4")]
            if not media_files:
                await interaction.followup.send("❌ Failed to download Instagram media.")
                return

            media_path = os.path.join(tmpdir, media_files[0])

            # Prepare filename with spoiler prefix if needed
            filename = os.path.basename(media_path)
            if spoiler:
                filename = f"SPOILER_{filename}"

            await interaction.followup.send(
                file=discord.File(fp=media_path, filename=filename),
                ephemeral=False  # Ensures message is visible to everyone
            )

            os.chdir(original_dir)

    except Exception as e:
        await interaction.followup.send(f"❌ An error occurred: {str(e)}")

# ========== Eligible Command ==========
@bot.tree.command(name="eligible", description="Check if you are eligible for group payouts")
async def eligible(interaction: discord.Interaction):
    user_id = interaction.user.id
    group_id = 5838002  # Your Roblox Group ID
    api_key = os.getenv("ROBLOX_API_KEY")
    
    if not api_key:
        await interaction.response.send_message("❌ ROBLOX_API_KEY not found in environment.", ephemeral=True)
        return

    if link_collection is None:
        await interaction.response.send_message("❌ Database is currently unavailable. Please try again later.", ephemeral=True)
        return

    # Fetch linked Roblox info
    user_data = link_collection.find_one({"discord_id": user_id})
    if not user_data or "roblox_id" not in user_data:
        embed = discord.Embed(
            title="⚠️ Not Linked",
            description="Please link your Roblox account using `/link <username>` first.",
            color=discord.Color.orange()
        )
        embed.set_footer(text="Neroniel")
        embed.timestamp = datetime.now(PH_TIMEZONE)
        await interaction.response.send_message(embed=embed)
        return

    roblox_id = user_data["roblox_id"]

    # Check group membership
    headers = {
        "accept": "application/json",
        "x-api-key": api_key
    }
    url = f"https://groups.roblox.com/v1/groups/{group_id}/members/{roblox_id}"  
    response = requests.get(url, headers=headers)
    
    if response.status_code != 200:
        await interaction.response.send_message("❌ Error checking group membership.", ephemeral=True)
        return

    data = response.json()
    role = data["role"]["name"]
    join_date = isoparse(data["joined"])
    now = datetime.now(PH_TIMEZONE)
    time_in_group = now - join_date
    days_in_group = time_in_group.days
    eligible = days_in_group >= 14

    if not eligible:
        embed = discord.Embed(
            title="⏳ Not Eligible Yet",
            description=f"You joined {days_in_group} day(s) ago. You must be in the group for at least 14 days to be eligible.",
            color=discord.Color.gold()
        )
    else:
        embed = discord.Embed(
            title="✅ Eligible",
            description=f"You are eligible for group payouts.\n"
                        f"**Role:** {role}\n"
                        f"**Days in Group:** {days_in_group}",
            color=discord.Color.green()
        )
    
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)
    await interaction.response.send_message(embed=embed)


# ========== Link Command ==========
@bot.tree.command(name="link", description="Link your Roblox account to your Discord profile")
@app_commands.describe(robloxusername="Your Roblox username")
async def link(interaction: discord.Interaction, robloxusername: str):
    user_id = interaction.user.id
    
    # Get Roblox ID from username
    url = f"https://users.roblox.com/v1/users/search?keyword={robloxusername}"
    response = requests.get(url)
    data = response.json()
    
    roblox_id = None
    for user in data.get("data", []):
        if user["name"].lower() == robloxusername.lower():
            roblox_id = user["id"]
            break

    if not roblox_id:
        await interaction.response.send_message("❌ Could not find that Roblox username.", ephemeral=True)
        return

    # Save to link_collection 
    if link_collection:
        link_collection.update_one(
            {"discord_id": user_id},
            {
                "$set": {
                    "roblox_id": roblox_id,
                    "roblox_username": robloxusername
                }
            },
            upsert=True
        )

    embed = discord.Embed(
        title="✅ Account Linked",
        description=f"Successfully linked `{robloxusername}` to your Discord account.",
        color=discord.Color.green()
    )
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)
    await interaction.response.send_message(embed=embed)

# ========== Unlink Command ==========
@bot.tree.command(name="unlink", description="Unlink your Roblox account from your Discord profile")
async def unlink(interaction: discord.Interaction):
    user_id = interaction.user.id
    
    if link_collection is None:
        await interaction.response.send_message("❌ Database is currently unavailable. Please try again later.", ephemeral=True)
        return

    result = link_collection.delete_one({"discord_id": user_id})

    if result.deleted_count > 0:
        embed = discord.Embed(
            title="✅ Account Unlinked",
            description="Your Roblox account has been successfully unlinked.",
            color=discord.Color.green()
        )
    else:
        embed = discord.Embed(
            title="⚠️ Not Linked",
            description="You don't have a Roblox account linked to your Discord profile.",
            color=discord.Color.orange()
        )

    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)
    await interaction.response.send_message(embed=embed)


# ===========================
# Bot Events
# ===========================

@bot.event
async def on_ready():
    print(f"Bot is ready! Logged in as {bot.user}")
    await bot.tree.sync()
    print("All commands synced!")

    # Start background tasks after bot is ready
    if reminders_collection is not None:
        if not check_reminders.is_running():
            check_reminders.start()

    group_id = 5838002
    while True:
        try:
            response = requests.get(f"https://groups.roblox.com/v1/groups/{group_id}") 
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
            "hi tapos ano? magiging friends tayo? lagi tayong mag-uusap mula umaga hanggang madaling araw? tas magiging close tayo? sa sobrang close natin nahuhulog na tayo sa isa't isa, tapos ano? liligawan mo ko? sasagutin naman kita. paplanuhin natin yung pangarap natin sa isa't isa tapos ano? may makikita kang iba. magsasawa ka na, iiwan mo na ako. tapos magmamakaawa ako sayo kasi mahal kita pero ano? wala kang gagawin, hahayaan mo lang akong umiiyak while begging you to stay. kaya wag na lang. thanks nalang sa hi mo")
        await message.channel.send(reply)
    elif content == "hello":
        await message.channel.send("hello, baby")
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
