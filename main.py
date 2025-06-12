import discord
from discord import Embed, app_commands, Interaction, ui, ButtonStyle
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

# Set timezone to Philippines (GMT+8)
PH_TIMEZONE = pytz.timezone("Asia/Manila")
load_dotenv()

# TEST
uri = "mongodb+srv://itskxro:Neroniel@cluster0.5skuybc.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
# Create a new client and connect to the server
client = MongoClient(uri, server_api=ServerApi('1'))
# Send a ping to confirm a successful connection
try:
    client.admin.command('ping')
    print("Pinged your deployment. You successfully connected to MongoDB!")
except Exception as e:
    print(e)

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
try:
    client = MongoClient(os.getenv("MONGO_URI"), tlsCAFile=certifi.where())
    db = client.ai_bot
    conversations_collection = db.conversations
    reminders_collection = db.reminders
    giveaways_collection = db.giveaways

    # Create TTL indexes
    conversations_collection.create_index("timestamp", expireAfterSeconds=604800)  # 7 days
    reminders_collection.create_index("reminder_time", expireAfterSeconds=2592000)  # 30 days
    giveaways_collection.create_index([("message_id", 1)], unique=True)
    giveaways_collection.create_index("ended", expireAfterSeconds=2592000)
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

# ===========================
# Owner-only Direct Message Commands
# ===========================
# Define the BOT_OWNER_ID directly in the code
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

# ===========================
# AI Commands
# ===========================

@bot.tree.command(name="ask", description="Chat with an AI assistant using Llama 3")
@app_commands.describe(prompt="What would you like to ask?")
async def ask(interaction: discord.Interaction, prompt: str):
    user_id = interaction.user.id
    channel_id = interaction.channel.id

    # ❗ RESPOND IMMEDIATELY TO AVOID INTERACTION EXPIRATION ❗
    await interaction.response.defer()

    # Rate limit: 5 messages/user/minute
    current_time = asyncio.get_event_loop().time()
    timestamps = bot.ask_rate_limit[user_id]
    timestamps.append(current_time)
    bot.ask_rate_limit[user_id] = [t for t in timestamps if current_time - t <= 60]
    if len(timestamps) > 5:
        await interaction.followup.send("⏳ You're being rate-limited. Please wait.")
        return

    async with interaction.channel.typing():
        try:
            # Custom filter for creator questions
            normalized_prompt = prompt.strip().lower()
            if normalized_prompt in ["who made you", "who created you", "who created this bot", "who made this bot"]:
                embed = discord.Embed(description="I was created by **Neroniel**.", color=discord.Color.blue())
                embed.set_footer(text="Neroniel AI")
                embed.timestamp = datetime.now(PH_TIMEZONE)
                msg = await interaction.followup.send(embed=embed)
                bot.last_message_id[(user_id, channel_id)] = msg.id
                return

            # Language Detection
            try:
                detected_lang = detect(prompt)
            except LangDetectException:
                detected_lang = "en"  # default to English

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

            async with aiohttp.ClientSession() as session:
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
@app_commands.describe(member="The member to get info for (optional, defaults to you)")
async def userinfo(interaction: discord.Interaction, member: discord.Member = None):
    if member is None:
        member = interaction.user
    # Account creation date
    created_at = member.created_at.astimezone(PH_TIMEZONE).strftime("%B %d, %Y • %I:%M %p GMT+8")
    # Join date
    joined_at = member.joined_at.astimezone(PH_TIMEZONE).strftime("%B %d, %Y • %I:%M %p GMT+8") if member.joined_at else "Unknown"
    # Roles
    roles = [role.mention for role in member.roles if not role.is_default()]
    roles_str = ", ".join(roles) if roles else "No Roles"
    # Boosting status
    boost_since = member.premium_since.astimezone(PH_TIMEZONE).strftime("%B %d, %Y • %I:%M %p GMT+8") if member.premium_since else "Not Boosting"

    embed = discord.Embed(title=f"👤 User Info for {member}", color=discord.Color.green())

    # Basic Info
    embed.add_field(name="Username", value=f"{member.mention}", inline=False)
    embed.add_field(name="Display Name", value=f"`{member.display_name}`", inline=True)
    embed.add_field(name="User ID", value=f"`{member.id}`", inline=True)

    # Dates
    embed.add_field(name="Created Account", value=f"`{created_at}`", inline=False)
    embed.add_field(name="Joined Server", value=f"`{joined_at}`", inline=False)

    # Roles
    embed.add_field(name="Roles", value=roles_str, inline=False)

    # Boosting
    embed.add_field(name="Server Booster Since", value=f"`{boost_since}`", inline=False)

    # Optional: Show if the user is a bot
    if member.bot:
        embed.add_field(name="Bot Account", value="✅ Yes", inline=True)

    # Set thumbnail to user's avatar
    embed.set_thumbnail(url=member.display_avatar.url)

    # Footer and timestamp
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)

    await interaction.response.send_message(embed=embed)

# ===========================
# Announcement Command
# ===========================
@bot.tree.command(name="announcement", description="Send an embedded announcement to a specific channel")
@app_commands.describe(message="The message to include in the announcement", channel="The channel to send the announcement to")
async def announcement(interaction: discord.Interaction, message: str, channel: discord.TextChannel):
    BOT_OWNER_ID = 1163771452403761193  # Update if needed
    is_owner = interaction.user.id == BOT_OWNER_ID
    is_admin = interaction.user.guild_permissions.administrator
    if not is_owner and not is_admin:
        await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
        return
    embed = discord.Embed(
        title="ANNOUNCEMENT",
        description=f"```\n{message}\n```",
        color=discord.Color.blue()
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
@bot.tree.command(name="payout", description="Convert Robux to PHP based on Payout rate (₱320 for 1000 Robux)")
@app_commands.describe(robux="How much Robux do you want to convert?")
async def payout(interaction: discord.Interaction, robux: int):
    if robux <= 0:
        await interaction.response.send_message("❗ Robux amount must be greater than zero.")
        return
    php = robux * (320 / 1000)
    await interaction.response.send_message(f"💵 {robux} Robux = **₱{php:.2f} PHP**")

@bot.tree.command(name="payoutreverse", description="Convert PHP to Robux based on Payout rate (₱320 for 1000 Robux)")
@app_commands.describe(php="How much PHP do you want to convert?")
async def payoutreverse(interaction: discord.Interaction, php: float):
    if php <= 0:
        await interaction.response.send_message("❗ PHP amount must be greater than zero.")
        return
    robux = round((php / 320) * 1000)
    await interaction.response.send_message(f"💰 ₱{php:.2f} PHP = **{robux} Robux**")

# Gift Rate
@bot.tree.command(name="gift", description="Convert Robux to PHP based on Gift rate (₱250 for 1000 Robux)")
@app_commands.describe(robux="How much Robux do you want to convert?")
async def gift(interaction: discord.Interaction, robux: int):
    if robux <= 0:
        await interaction.response.send_message("❗ Robux amount must be greater than zero.")
        return
    php = robux * (250 / 1000)
    await interaction.response.send_message(f"🎁 {robux} Robux = **₱{php:.2f} PHP**")

@bot.tree.command(name="giftreverse", description="Convert PHP to Robux based on Gift rate (₱250 for 1000 Robux)")
@app_commands.describe(php="How much PHP do you want to convert?")
async def giftreverse(interaction: discord.Interaction, php: float):
    if php <= 0:
        await interaction.response.send_message("❗ PHP amount must be greater than zero.")
        return
    robux = round((php / 250) * 1000)
    await interaction.response.send_message(f"🎉 ₱{php:.2f} PHP = **{robux} Robux**")

# NCT Rate
@bot.tree.command(name="nct", description="Convert Robux to PHP based on NCT rate (₱240/1k)")
@app_commands.describe(robux="How much Robux do you want to convert?")
async def nct(interaction: discord.Interaction, robux: int):
    if robux <= 0:
        await interaction.response.send_message("❗ Invalid input.")
        return
    php = robux * (240 / 1000)
    await interaction.response.send_message(f"💸 {robux} Robux = **₱{php:.2f} PHP**")

@bot.tree.command(name="nctreverse", description="Convert PHP to Robux based on NCT rate (₱240/1k)")
@app_commands.describe(php="How much PHP do you want to convert?")
async def nctreverse(interaction: discord.Interaction, php: float):
    if php <= 0:
        await interaction.response.send_message("❗ PHP amount must be greater than zero.")
        return
    robux = round((php / 240) * 1000)
    await interaction.response.send_message(f"💰 ₱{php:.2f} PHP = **{robux} Robux**")

# CT Rate
@bot.tree.command(name="ct", description="Convert Robux to PHP based on CT rate (₱340/1k)")
@app_commands.describe(robux="How much Robux do you want to convert?")
async def ct(interaction: discord.Interaction, robux: int):
    if robux <= 0:
        await interaction.response.send_message("❗ Invalid input.")
        return
    php = robux * (340 / 1000)
    await interaction.response.send_message(f"💳 {robux} Robux = **₱{php:.2f} PHP**")

@bot.tree.command(name="ctreverse", description="Convert PHP to Robux based on CT rate (₱340/1k)")
@app_commands.describe(php="How much PHP do you want to convert?")
async def ctreverse(interaction: discord.Interaction, php: float):
    if php <= 0:
        await interaction.response.send_message("❗ PHP amount must be greater than zero.")
        return
    robux = round((php / 340) * 1000)
    await interaction.response.send_message(f"💰 ₱{php:.2f} PHP = **{robux} Robux**")

# All Rates Comparison
@bot.tree.command(name="allrates", description="See PHP equivalent across all rates for given Robux")
@app_commands.describe(robux="How much Robux do you want to compare?")
async def allrates(interaction: discord.Interaction, robux: int):
    if robux <= 0:
        await interaction.response.send_message("❗ Robux amount must be greater than zero.")
        return
    rates = {
        "Not Covered Tax (₱240)": 240,
        "Covered Tax (₱340)": 340,
        "Group Payout (₱320)": 320,
        "Gift (₱250)": 250
    }
    result = "\n".join([f"**{label}** → ₱{(value / 1000) * robux:.2f}" for label, value in rates.items()])
    await interaction.response.send_message(f"📊 **{robux} Robux Conversion:**\n{result}")

@bot.tree.command(name="allratesreverse", description="See Robux equivalent across all rates for given PHP")
@app_commands.describe(php="How much PHP do you want to compare?")
async def allratesreverse(interaction: discord.Interaction, php: float):
    if php <= 0:
        await interaction.response.send_message("❗ PHP amount must be greater than zero.")
        return
    rates = {
        "Not Covered Tax (₱240)": 240,
        "Covered Tax (₱340)": 340,
        "Group Payout (₱320)": 320,
        "Gift (₱250)": 250
    }
    result = "\n".join([f"**{label}** → {round((php / value) * 1000)} Robux" for label, value in rates.items()])
    await interaction.response.send_message(f"📊 **₱{php:.2f} PHP Conversion:**\n{result}")

# Tax Calculations
@bot.tree.command(name="beforetax", description="Calculate how much Robux you'll receive after 30% tax")
@app_commands.describe(robux="How much Robux is being sent?")
async def beforetax(interaction: discord.Interaction, robux: int):
    if robux <= 0:
        await interaction.response.send_message("❗ Robux amount must be greater than zero.")
        return
    received = math.floor(robux * 0.7)
    await interaction.response.send_message(f"📤 Sending {robux} → Receive **{received} Robux** after tax.")

@bot.tree.command(name="aftertax", description="Calculate how much Robux to send to receive desired amount after 30% tax")
@app_commands.describe(target="How much Robux do you want to receive after tax?")
async def aftertax(interaction: discord.Interaction, target: int):
    if target <= 0:
        await interaction.response.send_message("❗ Target Robux must be greater than zero.")
        return
    sent = math.ceil(target / 0.7)
    await interaction.response.send_message(f"📬 To receive **{target} Robux**, send **{sent} Robux** (30% tax).")

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
            color=discord.Color.blue()
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
    BOT_OWNER_ID = 1163771452403761193
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
        embed = discord.Embed(color=discord.Color.blue())
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
- `/payout <robux>` - Convert Robux to PHP at Payout rate (₱320/1000)
- `/payoutreverse <php>` - Convert PHP to Robux at Payout rate
- `/gift <robux>` - Convert Robux to PHP at Gift rate (₱250/1000)
- `/giftreverse <php>` - Convert PHP to Robux at Gift rate
- `/nct <robux>` - Convert Robux to PHP at NCT rate (₱240/1k)
- `/nctreverse <php>` - Convert PHP to Robux at NCT rate
- `/ct <robux>` - Convert Robux to PHP at CT rate (₱340/1k)
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
- `/announcement <message> <channel>` - Send an embedded announcement
- `/gamepass <id>` - Show a public Roblox Gamepass Link using an ID or Creator Dashboard URL
- `/avatar [user]` - Display a user's profile picture
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
        color=discord.Color.blue()
    )

    if info["image"]:
        embed.set_image(url=info["image"])

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
        title=f"💰 1cy Group Funds",
        color=discord.Color.blue()
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
        color=discord.Color.blue()
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
            color=discord.Color.blue()
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


# ========== Giveaway Command ==========
giveaways_collection.create_index([("message_id", ASCENDING)], unique=True)

class GiveawayDurationType(str, Enum):
    SECONDS = "seconds"
    MINUTES = "minutes"
    HOURS = "hours"
    DAYS = "days"

class EnterGiveawayButton(ui.View):
    def __init__(self, *, giveaway_id: str, timeout=None):
        super().__init__(timeout=timeout)
        self.giveaway_id = giveaway_id
        self.entries = set()

    @ui.button(label="🎉 Enter Giveaway", style=ButtonStyle.blurple)
    async def enter_button(self, interaction: Interaction, button: ui.Button):
        user_id = str(interaction.user.id)

        # Fetch current giveaway data from MongoDB
        giveaway_data = giveaways_collection.find_one({"_id": ObjectId(self.giveaway_id)})
        if not giveaway_data:
            await interaction.response.send_message("❌ This giveaway no longer exists.", ephemeral=True)
            return

        guild = bot.get_guild(giveaway_data["guild_id"])
        if not guild:
            await interaction.response.send_message("❌ Guild not found.", ephemeral=True)
            return

        channel = guild.get_channel(giveaway_data["channel_id"])
        if not channel:
            await interaction.response.send_message("❌ Channel not found.", ephemeral=True)
            return

        entrant_ids = giveaway_data.get("entries", [])

        if user_id in entrant_ids:
            # Remove user from entries
            giveaways_collection.update_one(
                {"_id": ObjectId(self.giveaway_id)},
                {"$pull": {"entries": user_id}}
            )
            self.entries.discard(user_id)
            await interaction.response.send_message("✅ You have successfully left the giveaway!", ephemeral=True)
        else:
            # Add user to entries
            giveaways_collection.update_one(
                {"_id": ObjectId(self.giveaway_id)},
                {"$addToSet": {"entries": user_id}}
            )
            self.entries.add(user_id)
            await interaction.response.send_message("✅ You've successfully entered the giveaway!", ephemeral=True)

        # Update embed participant count
        new_count = len(giveaways_collection.find_one({"_id": ObjectId(self.giveaway_id)}).get("entries", []))
        try:
            message = await channel.fetch_message(int(giveaway_data["message_id"]))
            embed = message.embeds[0]
            for i, field in enumerate(embed.fields):
                if field.name.startswith("🔵 Participants"):
                    embed.set_field_at(i, name=field.name, value=str(new_count))
            await message.edit(embed=embed)
        except Exception as e:
            print(f"[!] Failed to update participant count: {e}")

@bot.tree.command(name="giveaway", description="Start a giveaway event with custom title and button entry")
@app_commands.describe(
    prize="What is the prize?",
    duration_amount="How long will the giveaway last?",
    duration_type="Unit of time (seconds, minutes, hours, days)",
    winners="How many winners should be selected?",
    channel="Which channel to post the giveaway (optional)?"
)
@app_commands.choices(duration_type=[
    app_commands.Choice(name="Seconds", value=GiveawayDurationType.SECONDS),
    app_commands.Choice(name="Minutes", value=GiveawayDurationType.MINUTES),
    app_commands.Choice(name="Hours", value=GiveawayDurationType.HOURS),
    app_commands.Choice(name="Days", value=GiveawayDurationType.DAYS),
])
async def giveaway(interaction: discord.Interaction, 
                   prize: str,
                   duration_amount: int,
                   duration_type: str,
                   winners: int = 1,
                   channel: discord.TextChannel = None):
    
    BOT_OWNER_ID_STR = os.getenv("BOT_OWNER_ID")
    if not BOT_OWNER_ID_STR:
        raise ValueError("BOT_OWNER_ID must be set in the .env file")
    try:
        BOT_OWNER_ID = int(BOT_OWNER_ID_STR)
    except ValueError:
        raise ValueError("BOT_OWNER_ID must be a valid integer")

    if not (interaction.user.guild_permissions.administrator or interaction.user.id == BOT_OWNER_ID):
        await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
        return

    if duration_amount <= 0:
        await interaction.response.send_message("❗ Duration must be greater than zero.", ephemeral=True)
        return
    
    if winners < 1:
        await interaction.response.send_message("❗ There must be at least one winner.", ephemeral=True)
        return

    target_channel = channel or interaction.channel

    unit_seconds = {
        "seconds": 1,
        "minutes": 60,
        "hours": 3600,
        "days": 86400
    }
    duration_seconds = duration_amount * unit_seconds.get(duration_type, 1)
    end_time = datetime.now(PH_TIMEZONE) + timedelta(seconds=duration_seconds)

    guild = bot.get_guild(1177130289672241232)  # Your server ID
    blue_dot = guild.get_emoji_named("blue_dot") if guild else None
    emoji = f"{blue_dot}" if blue_dot else "🔵"

    embed = discord.Embed(title=prize, color=discord.Color.gold())
    embed.add_field(name=f"{emoji} Hosted by", value=interaction.user.mention, inline=False)
    embed.add_field(name=f"{emoji} Ends", value=f"{duration_amount} {duration_type}", inline=False)
    embed.add_field(name=f"{emoji} Winners", value=str(winners), inline=False)
    embed.add_field(name=f"{emoji} Participants", value="0", inline=False)
    embed.set_footer(text="Click the button below to enter!")
    embed.timestamp = end_time

    view = EnterGiveawayButton(giveaway_id="", timeout=duration_seconds)
    await interaction.response.send_message(f"✅ Giveaway started in {target_channel.mention}")
    giveaway_message = await target_channel.send(embed=embed, view=view)

    giveaway_data = {
        "message_id": str(giveaway_message.id),
        "guild_id": interaction.guild.id,
        "channel_id": target_channel.id,
        "prize": prize,
        "host_id": interaction.user.id,
        "end_time": end_time,
        "winners_count": winners,
        "entries": [],
        "ended": False
    }
    result = giveaways_collection.insert_one(giveaway_data)
    giveaway_id = str(result.inserted_id)

    view = EnterGiveawayButton(giveaway_id=giveaway_id, timeout=duration_seconds)
    await giveaway_message.edit(view=view)

    await asyncio.sleep(duration_seconds)

    for item in view.children:
        item.disabled = True
    await giveaway_message.edit(view=view)

    giveaway_data = giveaways_collection.find_one({"_id": ObjectId(giveaway_id)})
    entrants = giveaway_data.get("entries", [])
    if not entrants:
        no_winners_embed = discord.Embed(
            title="🎉 GIVEAWAY ENDED",
            color=discord.Color.red()
        )
        no_winners_embed.add_field(name=f"{emoji} Hosted by", value=interaction.user.mention, inline=False)
        no_winners_embed.add_field(name=f"{emoji} Ends", value=f"{duration_amount} {duration_type}", inline=False)
        no_winners_embed.add_field(name=f"{emoji} Winners", value=str(winners), inline=False)
        no_winners_embed.add_field(name=f"{emoji} Participants", value="0", inline=False)
        no_winners_embed.description = "😢 No valid entries."
        no_winners_embed.set_footer(text="Ended")
        no_winners_embed.timestamp = datetime.now(PH_TIMEZONE)
        await giveaway_message.edit(embed=no_winners_embed)
        return

    if len(entrants) < winners:
        winners = len(entrants)
    selected_users = random.sample(entrants, winners)

    winner_list = "\n".join([f"<@{uid}>" for uid in selected_users])
    result_embed = discord.Embed(
        title="🎉 GIVEAWAY ENDED",
        color=discord.Color.green()
    )
    result_embed.add_field(name=f"{emoji} Hosted by", value=interaction.user.mention, inline=False)
    result_embed.add_field(name=f"{emoji} Ends", value=f"Ended at {end_time.strftime('%B %d, %Y • %I:%M %p GMT+8')}", inline=False)
    result_embed.add_field(name=f"{emoji} Winners", value=winner_list, inline=False)
    result_embed.add_field(name=f"{emoji} Participants", value=str(len(entrants)), inline=False)
    result_embed.set_footer(text="Congratulations!")
    result_embed.timestamp = datetime.now(PH_TIMEZONE)

    await giveaway_message.edit(embed=result_embed, view=None)
    await target_channel.send(f"🎊 Congratulations to the winner(s): {winner_list}\nPrize: **{prize}**")

    giveaways_collection.update_one(
        {"_id": ObjectId(giveaway_id)},
        {
            "$set": {
                "ended": True,
                "winners": selected_users
            }
        }
    )

    for entrant_id in entrants:
        try:
            user = await bot.fetch_user(int(entrant_id))
            if user:
                if entrant_id in selected_users:
                    await user.send(f"🎉 Congratulations! You won the **{prize}** in **{interaction.guild.name}**!")
                else:
                    await user.send(f"😢 Better luck next time! The giveaway for **{prize}** has ended.")
        except discord.Forbidden:
            pass
        except Exception as e:
            print(f"[!] Failed to send DM: {e}")

# Register persistent views on startup
@bot.event
async def on_ready():
    if not bot.tree.synced:
        await bot.tree.sync()
        print("Commands synced!")

    active_giveaways = giveaways_collection.find({"ended": False})
    for g in active_giveaways:
        view = EnterGiveawayButton(giveaway_id=str(g["_id"]), timeout=1)
        bot.add_view(view)
    print("Persistent views registered.")

# /reroll - Pick new winner from same giveaway
@bot.tree.command(name="reroll", description="Reroll a new winner for an ended giveaway")
@app_commands.describe(message_id="The message ID of the giveaway post")
async def reroll(interaction: discord.Interaction, message_id: str):
    BOT_OWNER_ID_STR = os.getenv("BOT_OWNER_ID")
    if not BOT_OWNER_ID_STR:
        raise ValueError("BOT_OWNER_ID must be set in the .env file")
    try:
        BOT_OWNER_ID = int(BOT_OWNER_ID_STR)
    except ValueError:
        raise ValueError("BOT_OWNER_ID must be a valid integer")

    if not (interaction.user.guild_permissions.administrator or interaction.user.id == BOT_OWNER_ID):
        await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
        return

    giveaway = giveaways_collection.find_one({"message_id": message_id, "ended": True})
    if not giveaway:
        await interaction.response.send_message("❌ Could not find an ended giveaway with that message ID.", ephemeral=True)
        return

    guild = bot.get_guild(giveaway["guild_id"])
    if not guild:
        await interaction.response.send_message("❌ Guild not found.", ephemeral=True)
        return

    channel = guild.get_channel(giveaway["channel_id"])
    if not channel:
        await interaction.response.send_message("❌ Channel not found.", ephemeral=True)
        return

    try:
        message = await channel.fetch_message(int(message_id))
    except discord.NotFound:
        await interaction.response.send_message("❌ Message not found.", ephemeral=True)
        return

    entrants = giveaway.get("entries", [])
    if not entrants:
        await interaction.response.send_message("❌ No participants to reroll from.", ephemeral=True)
        return

    winner = random.choice(entrants)
    winner_user = guild.get_member(int(winner)) or await guild.fetch_member(int(winner))

    embed = discord.Embed(
        title="🎉 GIVEAWAY REROLLED",
        color=discord.Color.gold()
    )
    embed.add_field(name="🎁 Prize", value=giveaway["prize"], inline=False)
    embed.add_field(name="🏆 New Winner", value=winner_user.mention, inline=False)
    embed.set_footer(text="Congratulations!")
    embed.timestamp = datetime.now(PH_TIMEZONE)

    await interaction.response.send_message(f"🎊 New winner: {winner_user.mention}", embed=embed)

# /cancelgiveaway - Cancel ongoing giveaway
@bot.tree.command(name="cancelgiveaway", description="Cancel an active giveaway before it ends")
@app_commands.describe(message_id="The message ID of the giveaway post")
async def cancelgiveaway(interaction: discord.Interaction, message_id: str):
    BOT_OWNER_ID_STR = os.getenv("BOT_OWNER_ID")
    if not BOT_OWNER_ID_STR:
        raise ValueError("BOT_OWNER_ID must be set in the .env file")
    try:
        BOT_OWNER_ID = int(BOT_OWNER_ID_STR)
    except ValueError:
        raise ValueError("BOT_OWNER_ID must be a valid integer")

    if not (interaction.user.guild_permissions.administrator or interaction.user.id == BOT_OWNER_ID):
        await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
        return

    giveaway = giveaways_collection.find_one({"message_id": message_id, "ended": False})
    if not giveaway:
        await interaction.response.send_message("❌ Could not find an active giveaway with that message ID.", ephemeral=True)
        return

    guild = bot.get_guild(giveaway["guild_id"])
    if not guild:
        await interaction.response.send_message("❌ Guild not found.", ephemeral=True)
        return

    channel = guild.get_channel(giveaway["channel_id"])
    if not channel:
        await interaction.response.send_message("❌ Channel not found.", ephemeral=True)
        return

    try:
        message = await channel.fetch_message(int(message_id))
    except discord.NotFound:
        await interaction.response.send_message("❌ Message not found.", ephemeral=True)
        return

    embed = message.embeds[0]
    embed.title = "🚫 GIVEAWAY CANCELED"
    embed.color = discord.Color.red()
    embed.description = "This giveaway was canceled early."

    for item in message.components[0].children:
        item.disabled = True

    view = discord.ui.View.from_message(message)
    for item in view.children:
        item.disabled = True

    await message.edit(embed=embed, view=view)

    giveaways_collection.update_one(
        {"_id": giveaway["_id"]},
        {"$set": {"ended": True}}
    )

    await interaction.response.send_message(f"✅ Giveaway `{message_id}` has been canceled.")

# /editgiveaway - Edit active giveaway
@bot.tree.command(name="editgiveaway", description="Edit an active giveaway before it ends")
@app_commands.describe(
    message_id="The message ID of the giveaway post",
    prize="New prize name",
    duration_amount="New duration amount",
    duration_type="New unit of time",
    winners="New number of winners"
)
@app_commands.choices(duration_type=[
    app_commands.Choice(name="Seconds", value="seconds"),
    app_commands.Choice(name="Minutes", value="minutes"),
    app_commands.Choice(name="Hours", value="hours"),
    app_commands.Choice(name="Days", value="days"),
])
async def editgiveaway(
    interaction: discord.Interaction,
    message_id: str,
    prize: str = None,
    duration_amount: int = None,
    duration_type: str = None,
    winners: int = None
):
    BOT_OWNER_ID_STR = os.getenv("BOT_OWNER_ID")
    if not BOT_OWNER_ID_STR:
        raise ValueError("BOT_OWNER_ID must be set in the .env file")
    try:
        BOT_OWNER_ID = int(BOT_OWNER_ID_STR)
    except ValueError:
        raise ValueError("BOT_OWNER_ID must be a valid integer")

    if not (interaction.user.guild_permissions.administrator or interaction.user.id == BOT_OWNER_ID):
        await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
        return

    giveaway = giveaways_collection.find_one({"message_id": message_id, "ended": False})
    if not giveaway:
        await interaction.response.send_message("❌ Could not find an active giveaway with that message ID.", ephemeral=True)
        return

    guild = bot.get_guild(giveaway["guild_id"])
    if not guild:
        await interaction.response.send_message("❌ Guild not found.", ephemeral=True)
        return

    channel = guild.get_channel(giveaway["channel_id"])
    if not channel:
        await interaction.response.send_message("❌ Channel not found.", ephemeral=True)
        return

    try:
        message = await channel.fetch_message(int(message_id))
    except discord.NotFound:
        await interaction.response.send_message("❌ Message not found.", ephemeral=True)
        return

    update_data = {}
    if prize:
        update_data["prize"] = prize

    if duration_amount and duration_type:
        unit_seconds = {
            "seconds": 1,
            "minutes": 60,
            "hours": 3600,
            "days": 86400
        }
        duration_seconds = duration_amount * unit_seconds.get(duration_type, 1)
        end_time = datetime.now(PH_TIMEZONE) + timedelta(seconds=duration_seconds)
        update_data["end_time"] = end_time

    if winners:
        update_data["winners_count"] = winners

    if not update_data:
        await interaction.response.send_message("⚠️ No changes provided.", ephemeral=True)
        return

    giveaways_collection.update_one(
        {"_id": giveaway["_id"]},
        {"$set": update_data}
    )

    embed = message.embeds[0]
    if "prize" in update_data:
        embed.title = prize
    if "end_time" in update_data:
        for i, field in enumerate(embed.fields):
            if field.name.startswith("🔵 Ends"):
                embed.set_field_at(i, name=field.name, value=f"{duration_amount} {duration_type}")
    if "winners_count" in update_data:
        for i, field in enumerate(embed.fields):
            if field.name.startswith("🔵 Winners"):
                embed.set_field_at(i, name=field.name, value=str(winners))

    await message.edit(embed=embed)
    await interaction.response.send_message(f"✅ Giveaway `{message_id}` has been updated.")

# /pickwinner - Manually select winners
@bot.tree.command(name="pickwinner", description="Manually pick winner(s) for an active giveaway")
@app_commands.describe(
    message_id="The message ID of the giveaway post",
    number_of_winners="How many winners to pick (optional, defaults to original)"
)
async def pickwinner(interaction: discord.Interaction, message_id: str, number_of_winners: int = None):
    BOT_OWNER_ID_STR = os.getenv("BOT_OWNER_ID")
    if not BOT_OWNER_ID_STR:
        raise ValueError("BOT_OWNER_ID must be set in the .env file")
    try:
        BOT_OWNER_ID = int(BOT_OWNER_ID_STR)
    except ValueError:
        raise ValueError("BOT_OWNER_ID must be a valid integer")

    if not (interaction.user.guild_permissions.administrator or interaction.user.id == BOT_OWNER_ID):
        await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
        return

    giveaway = giveaways_collection.find_one({"message_id": message_id, "ended": False})
    if not giveaway:
        await interaction.response.send_message("❌ Could not find an active giveaway with that message ID.", ephemeral=True)
        return

    guild = bot.get_guild(giveaway["guild_id"])
    if not guild:
        await interaction.response.send_message("❌ Guild not found.", ephemeral=True)
        return

    channel = guild.get_channel(giveaway["channel_id"])
    if not channel:
        await interaction.response.send_message("❌ Channel not found.", ephemeral=True)
        return

    try:
        message = await channel.fetch_message(int(message_id))
    except discord.NotFound:
        await interaction.response.send_message("❌ Message not found.", ephemeral=True)
        return

    entrant_ids = giveaway.get("entries", [])
    if not entrant_ids:
        await interaction.response.send_message("❌ No participants to choose from.", ephemeral=True)
        return

    if number_of_winners is None:
        number_of_winners = giveaway.get("winners_count", 1)

    if number_of_winners < 1:
        await interaction.response.send_message("❗ Must pick at least one winner.", ephemeral=True)
        return

    if len(entrant_ids) < number_of_winners:
        number_of_winners = len(entrant_ids)

    selected_users = random.sample(entrant_ids, number_of_winners)

    # Announce
    winner_mentions = "\n".join([f"<@{uid}>" for uid in selected_users])
    embed = discord.Embed(title="🎉 GIVEAWAY ENDED", color=discord.Color.green())
    embed.add_field(name="🎁 Prize", value=giveaway["prize"], inline=False)
    embed.add_field(name="🏆 Winner(s)", value=winner_mentions, inline=False)
    embed.set_footer(text="Congratulations!")
    embed.timestamp = datetime.now(PH_TIMEZONE)

    await message.edit(embed=embed, view=None)
    await channel.send(f"🎊 Congratulations to the winner(s): {winner_mentions}\nPrize: **{giveaway['prize']}**")

    giveaways_collection.update_one(
        {"_id": giveaway["_id"]},
        {
            "$set": {
                "ended": True,
                "winners": selected_users
            }
        }
    )

    # Notify users
    for entrant_id in entrant_ids:
        try:
            user = await bot.fetch_user(int(entrant_id))
            if user:
                if entrant_id in selected_users:
                    await user.send(f"🎉 Congratulations! You won the **{giveaway['prize']}** in **{guild.name}**!")
                else:
                    await user.send(f"😢 Better luck next time! The giveaway for **{giveaway['prize']}** has ended.")
        except Exception as e:
            print(f"[!] Failed to send DM: {e}")

    await interaction.response.send_message("✅ Winners have been manually picked and notified!", ephemeral=True)


# ===========================
# Bot Events
# ===========================

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
