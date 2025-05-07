import discord
from discord.ext import commands
import os
import threading
import asyncio
from dotenv import load_dotenv
import importlib.util

# Load environment variables
load_dotenv()

# ===========================
# Bot Setup
# ===========================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Rate limiting and conversation cache
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
    print(f"[!] Failed to start Flask server: {e}")

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
    bot.conversations_collection.create_index("timestamp", expireAfterSeconds=604800)  # 7 days
    bot.reminders_collection.create_index("reminder_time", expireAfterSeconds=2592000)  # 30 days
except Exception as e:
    print(f"[!] Failed to connect to MongoDB: {e}")
    bot.conversations_collection = None
    bot.reminders_collection = None

# ===========================
# Background Tasks
# ===========================

# Reminder Checker
async def check_reminders():
    while True:
        if not bot.reminders_collection:
            await asyncio.sleep(60)
            continue
        now = datetime.utcnow()
        try:
            expired = bot.reminders_collection.find({"reminder_time": {"$lte": now}})
            for reminder in expired:
                user = bot.get_user(reminder["user_id"])
                guild = bot.get_guild(reminder["guild_id"])
                channel = guild.get_channel(reminder["channel_id"]) if guild else None
                if user and channel:
                    await channel.send(f"🔔 {user.mention}, reminder: {reminder['note']}")
                bot.reminders_collection.delete_one({"_id": reminder["_id"]})
        except Exception as e:
            print(f"[!] Error checking reminders: {e}")
        await asyncio.sleep(60)

# Update Presence with Roblox Group Members
async def update_presence():
    while True:
        try:
            response = requests.get("https://groups.roblox.com/v1/groups/5838002")
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
            print(f"[!] Error fetching group info: {e}")
            await bot.change_presence(
                status=discord.Status.dnd,
                activity=discord.Activity(
                    type=discord.ActivityType.watching,
                    name="1cy"
                )
            )
        await asyncio.sleep(60)

# ===========================
# Load Commands from Commands Folder
# ===========================
async def load_commands():
    commands_folder = "Commands"
    for filename in os.listdir(commands_folder):
        if filename.endswith(".py"):
            command_name = filename[:-3]  # Remove .py extension
            module_path = f"{commands_folder}.{command_name}"
            spec = importlib.util.find_spec(module_path)
            if spec:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                if hasattr(module, 'setup'):
                    module.setup(bot)
                    print(f"[+] Loaded command: /{command_name}")
            else:
                print(f"[!] Failed to load command: /{command_name}")

# ===========================
# Bot Events
# ===========================
@bot.event
async def on_ready():
    print(f"Bot is ready! Logged in as {bot.user}")
    await bot.tree.sync()
    print("✅ All commands synced!")
    # Start background tasks
    bot.loop.create_task(check_reminders())
    bot.loop.create_task(update_presence())

# Auto-react and custom messages
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

# Run the bot
bot.run(os.getenv('DISCORD_TOKEN'))
