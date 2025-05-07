import discord
from discord.ext import commands
import os
import threading
import asyncio
import requests  # Required for auto-replies or other future uses
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Bot Setup
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Global Variables
bot.ask_rate_limit = {}
bot.conversations = {}
bot.last_message_id = {}

# ===========================
# Flask Web Server to Keep Bot Alive
# ===========================
try:
    from flask import Flask
    app = Flask(__name__)
    @app.route('/')
    def home():
        return "Bot is alive!"
    def run_server():
        app.run(host='0.0.0.0', port=5000)
    server_thread = threading.Thread(target=run_server)
    server_thread.start()
except Exception as e:
    print(f"[!] Flask server failed: {e}")

# ===========================
# MongoDB Setup
# ===========================
try:
    from pymongo import MongoClient
    import certifi
    client = MongoClient(os.getenv("MONGO_URI"), tlsCAFile=certifi.where())
    db = client.ai_bot
    bot.conversations_collection = db.conversations
    bot.reminders_collection = db.reminders
except Exception as e:
    print(f"[!] Failed to connect to MongoDB: {e}")
    bot.conversations_collection = None
    bot.reminders_collection = None

# ===========================
# Load Cogs Dynamically
# ===========================
async def load_cogs():
    for filename in os.listdir("cogs"):
        if filename.endswith(".py") and not filename.startswith("__"):
            cog_name = filename[:-3]
            try:
                await bot.load_extension(f"cogs.{cog_name}")
                print(f"[+] Loaded cog: {cog_name}")
            except Exception as e:
                print(f"[!] Failed to load cog {cog_name}: {e}")

# ===========================
# Custom Message Responses
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

    # Auto-react channels
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

    # Always process commands at the end
    await bot.process_commands(message)

# ===========================
# On Ready Event
# ===========================
@bot.event
async def on_ready():
    print(f"Bot is ready! Logged in as {bot.user}")
    await load_cogs()
    
    # Sync slash commands
    try:
        await bot.tree.sync()
        print("✅ All commands synced globally!")
    except Exception as e:
        print(f"[!] Command sync failed: {e}")

# Run the bot
bot.run(os.getenv('DISCORD_TOKEN'))
