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

# Rate limiting and conversation cache
bot.ask_rate_limit = {}
bot.conversations = {}
bot.last_message_id = {}

# ===========================
# Background Tasks
# ===========================
@bot.tree.command(name="listallcommands", description="List all available slash commands")
async def listallcommands(interaction: discord.Interaction):
    embed = discord.Embed(title="📚 All Available Commands", description="A categorized list of all commands.", color=discord.Color.blue())
    embed.add_field(name="🤖 AI Assistant", value="/ask, /clearhistory", inline=False)
    embed.add_field(name="💰 Currency Conversion", value="/payout, /payoutreverse, /gift, /giftreverse, /nct, /nctreverse, /ct, /ctreverse", inline=False)
    embed.add_field(name="📊 Comparison & Tax", value="/allrates, /allratesreverse, /beforetax, /aftertax", inline=False)
    embed.add_field(name="🛠️ Utility Tools", value="/userinfo, /purge, /calculator, /group", inline=False)
    embed.add_field(name="🎉 Fun", value="/poll, /remindme, /say, /donate", inline=False)
    await interaction.response.send_message(embed=embed)

# Background Task: Check Reminders
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

# Update presence with Roblox group member count
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
    print("All commands synced!")
    # Start background tasks
    bot.loop.create_task(check_reminders())
    bot.loop.create_task(update_presence())

# Run the bot
bot.run(os.getenv('DISCORD_TOKEN'))
