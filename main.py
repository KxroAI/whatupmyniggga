import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import asyncio
import requests
import math
import threading
import random
from flask import Flask
from collections import defaultdict
from dotenv import load_dotenv
import certifi
from pymongo import MongoClient
from datetime import datetime, timedelta
import pytz

# Load environment variables
load_dotenv()

PH_TIMEZONE = pytz.timezone("Asia/Manila")

# ===========================
# Bot Setup
# ===========================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)

# Rate limit & conversation cache
bot.ask_rate_limit = defaultdict(list)
bot.conversations = defaultdict(list)
bot.last_message_id = {}

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

# ===========================
# MongoDB Setup
# ===========================
try:
    client = MongoClient(os.getenv("MONGO_URI"), tlsCAFile=certifi.where())
    db = client.ai_bot
    conversations_collection = db.conversations
    reminders_collection = db.reminders
    conversations_collection.create_index("timestamp", expireAfterSeconds=604800)
    reminders_collection.create_index("reminder_time", expireAfterSeconds=2592000)
except Exception as e:
    print(f"[!] Failed to connect to MongoDB: {e}")
    client = None
    conversations_collection = None
    reminders_collection = None

# ===========================
# Background Tasks
# ===========================

# Reminder checker
@tasks.loop(seconds=60)
async def check_reminders():
    if not reminders_collection:
        return
    now = datetime.now(PH_TIMEZONE)
    expired = reminders_collection.find({"reminder_time": {"$lte": now}})
    for reminder in expired:
        user = bot.get_user(reminder["user_id"])
        channel = bot.get_channel(reminder["channel_id"])
        if user and channel:
            try:
                await channel.send(f"🔔 {user.mention}, reminder: {reminder['note']}")
                reminders_collection.delete_one({"_id": reminder["_id"]})
            except:
                pass

@check_reminders.before_loop
async def before_check_reminders():
    await bot.wait_until_ready()

if reminders_collection:
    check_reminders.start()

# Presence updater
@tasks.loop(seconds=60)
async def update_presence():
    group_id = 5838002
    try:
        response = requests.get(f"https://groups.roblox.com/v1/groups/{group_id}")
        data = response.json()
        member_count = "{:,}".format(data['memberCount'])
        await bot.change_presence(
            status=discord.Status.dnd,
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=f"1cy | {member_count} Members"
            )
        )
    except Exception as e:
        await bot.change_presence(
            status=discord.Status.dnd,
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="1cy"
            )
        )

update_presence.start()

# ===========================
# Event: On Ready
# ===========================
@bot.event
async def on_ready():
    print(f"Bot is ready! Logged in as {bot.user}")
    try:
        await bot.tree.sync()
        print("✅ All commands synced successfully.")
    except Exception as e:
        print(f"❌ Error syncing commands: {e}")

# ===========================
# Event: Message Handling
# ===========================
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
            "hi tapos ano? magiging friends tayo? lagi tayong mag-uusap mula umaga hanggang madaling araw? "
            "tas magiging close tayo? sa sobrang close natin nahuhulog na tayo sa isa't isa, tapos ano? "
            "lalaki makikita kang iba. magsasawa ka na, iiwan mo ako. tapos ano? magmamakaawa ako sayo "
            "kasi mahal kita pero ano? wala kang gagawin, hahayaan mo lang akong umiiyak habang hinahabol kita. "
            "kaya wag na lang. thanks nalang sa hi mo"
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

# ===========================
# Run the Bot
# ===========================
bot.run(os.getenv('DISCORD_TOKEN'))
