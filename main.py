import discord
from discord.ext import commands
import os
import threading
import asyncio
from dotenv import load_dotenv
import importlib.util

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
# Load Commands Dynamically
# ===========================
async def load_commands():
    commands_folder = "Commands"
    for filename in os.listdir(commands_folder):
        if filename.endswith(".py") and not filename.startswith("__"):
            command_name = filename[:-3]
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
# On Ready Event
# ===========================
@bot.event
async def on_ready():
    print(f"Bot is ready! Logged in as {bot.user}")
    await load_commands()
    try:
        await bot.tree.sync()
        print("✅ All commands synced globally!")
    except Exception as e:
        print(f"[!] Command sync failed: {e}")

# Run the bot
bot.run(os.getenv('DISCORD_TOKEN'))
