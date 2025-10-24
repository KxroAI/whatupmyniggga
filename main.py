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
from instaloader import Instaloader, Post, TwoFactorAuthRequiredException
import tempfile
from urllib.parse import urlencode, urlparse, parse_qs

# Set timezone to Philippines (GMT+8)
PH_TIMEZONE = pytz.timezone("Asia/Manila")
load_dotenv()

# ===========================
# Bot Setup
# ===========================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
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
rates_collection = None

mongo_uri = os.getenv("MONGO_URI")
if not mongo_uri:
    print("[!] MONGO_URI not found in environment. MongoDB will be disabled.")
else:
    try:
        client = MongoClient(mongo_uri, tlsCAFile=certifi.where())
        db = client.ai_bot

        # Initialize collections
        conversations_collection = db.conversations
        reminders_collection = db.reminders
        rates_collection = db.rates  # ← New collection for rates

        # Create TTL indexes
        conversations_collection.create_index(
            "timestamp", expireAfterSeconds=604800)  # 7 days
        reminders_collection.create_index(
            "reminder_time", expireAfterSeconds=2592000)  # 30 days

        # Create index for guild_id in rates collection
        rates_collection.create_index([("guild_id", ASCENDING)], unique=True)

        print("✅ Successfully connected to MongoDB")
    except Exception as e:
        print(f"[!] Failed to connect to MongoDB: {e}")
        client = None
        conversations_collection = None
        reminders_collection = None
        rates_collection = None


# Background Task: Check Reminders
@tasks.loop(seconds=60)
async def check_reminders():
    if reminders_collection is None:
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


# Rates DB
def get_current_rates(guild_id: str):
    # Check if MongoDB is disabled
    if rates_collection is None:
        return {"payout": 330.0, "gift": 260.0, "nct": 245.0, "ct": 350.0}

    guild_id = str(guild_id)
    result = rates_collection.find_one({"guild_id": guild_id})

    return {
        "payout": result.get("payout_rate", 330.0) if result else 330.0,
        "gift": result.get("gift_rate", 260.0) if result else 260.0,
        "nct": result.get("nct_rate", 245.0) if result else 245.0,
        "ct": result.get("ct_rate", 350.0) if result else 350.0
    }


DEFAULT_RATES = {
    "payout_rate": 330.0,
    "gift_rate": 260.0,
    "nct_rate": 245.0,
    "ct_rate": 350.0
}

# ===========================
# Owner-only Direct Message Commands
# ===========================
# Define the BOT_OWNER_ID directly in the code
BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID"))


@bot.tree.command(name="dm",
                  description="Send a direct message to a user (Owner only)")
@app_commands.describe(user="The user you want to message",
                       message="The message to send")
async def dm(interaction: discord.Interaction, user: discord.User,
             message: str):
    if interaction.user.id != BOT_OWNER_ID:
        await interaction.response.send_message(
            "❌ You don't have permission to use this command.", ephemeral=True)
        return
    try:
        await user.send(message)
        await interaction.response.send_message(
            f"✅ Sent DM to {user} ({user.id})", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(
            f"❌ Unable to send DM to {user}. They might have DMs disabled.",
            ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(
            f"❌ An error occurred: {str(e)}", ephemeral=True)


@bot.tree.command(
    name="dmall",
    description=
    "Send a direct message to all members in the server (Owner only)")
@app_commands.describe(message="The message you want to send to all members")
async def dmall(interaction: discord.Interaction, message: str):
    if interaction.user.id != BOT_OWNER_ID:
        await interaction.response.send_message(
            "❌ You don't have permission to use this command.", ephemeral=True)
        return

    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "❌ This command must be used in a server.", ephemeral=True)
        return

    # Defer response (since fetching members may take time)
    await interaction.response.defer(ephemeral=True)

    # Fetch all members if not already chunked
    if not guild.chunked:
        try:
            await guild.chunk()  # This loads all members
        except Exception as e:
            await interaction.followup.send(f"❌ Failed to fetch members: {e}",
                                            ephemeral=True)
            return

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
            print(f"[!] Failed to send DM to {member} ({member.id}): {str(e)}")
            fail_count += 1

    await interaction.followup.send(
        f"✅ Successfully sent DM to **{success_count}** members. "
        f"❌ Failed to reach **{fail_count}** members.")


# ===========================
# AI Commands
# ===========================


@bot.tree.command(name="ask",
                  description="Chat with an AI assistant using Llama 3")
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
    bot.ask_rate_limit[user_id] = [
        t for t in timestamps if current_time - t <= 60
    ]
    bot.ask_rate_limit[user_id].append(current_time)

    if len(bot.ask_rate_limit[user_id]) > 5:
        await interaction.followup.send(
            "⏳ You're being rate-limited. Please wait a minute.")
        return

    async with interaction.channel.typing():
        try:
            # Custom filter for creator questions
            normalized_prompt = prompt.strip().lower()
            if normalized_prompt in [
                    "who made you", "who created you", "who created this bot",
                    "who made this bot"
            ]:
                embed = discord.Embed(
                    description="I was created by **Neroniel**.",
                    color=discord.Color.from_rgb(0, 0, 0))
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
                    history_docs = conversations_collection.find({
                        "user_id":
                        user_id
                    }).sort("timestamp", -1).limit(5)
                    for doc in history_docs:
                        bot.conversations[user_id].append({
                            "user":
                            doc["prompt"],
                            "assistant":
                            doc["response"]
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

            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(
                    total=10)) as session:
                async with session.post(
                        "https://api.together.xyz/v1/completions",
                        headers=headers,
                        json=payload) as response:
                    if response.status != 200:
                        text = await response.text()
                        await interaction.followup.send(
                            f"❌ API returned error code {response.status}: `{text}`"
                        )
                        return
                    data = await response.json()

            if 'error' in data:
                await interaction.followup.send(
                    f"❌ Error from AI API: {data['error']['message']}")
                return

            ai_response = data["choices"][0]["text"].strip()

            # Determine if we should reply to a previous message
            target_message_id = bot.last_message_id.get((user_id, channel_id))

            # Send the AI response
            embed = discord.Embed(description=ai_response,
                                  color=discord.Color.from_rgb(0, 0, 0))
            embed.set_footer(text="Neroniel AI")
            embed.timestamp = datetime.now(PH_TIMEZONE)

            if target_message_id:
                try:
                    msg = await interaction.channel.fetch_message(
                        target_message_id)
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
                    "user_id":
                    user_id,
                    "prompt":
                    prompt,
                    "response":
                    ai_response,
                    "timestamp":
                    datetime.now(PH_TIMEZONE)
                })

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {str(e)}")
            print(f"[EXCEPTION] /ask command error: {e}")


# /clearhistory - Clear stored conversation history
@bot.tree.command(name="clearhistory",
                  description="Clear your AI conversation history")
async def clearhistory(interaction: discord.Interaction):
    user_id = interaction.user.id
    # Clear local memory
    if user_id in bot.conversations:
        bot.conversations[user_id].clear()
    # Clear MongoDB history
    if conversations_collection:
        conversations_collection.delete_many({"user_id": user_id})
    await interaction.response.send_message(
        "✅ Your AI conversation history has been cleared!", ephemeral=True)


# ===========================
# Utility Commands
# ===========================


# /userinfo - Display user information
@bot.tree.command(name="userinfo",
                  description="Display detailed information about a user")
@app_commands.describe(
    user="The user to get info for (optional, defaults to you)")
async def userinfo(interaction: discord.Interaction,
                   user: discord.User = None):
    if user is None:
        user = interaction.user

    created_at = user.created_at.astimezone(PH_TIMEZONE).strftime(
        "%B %d, %Y • %I:%M %p GMT+8")

    if isinstance(user, discord.Member):
        joined_at = user.joined_at.astimezone(PH_TIMEZONE).strftime(
            "%B %d, %Y • %I:%M %p GMT+8") if user.joined_at else "Unknown"
        roles = [role.mention for role in user.roles if not role.is_default()]
        roles_str = ", ".join(roles) if roles else "No Roles"
        boost_since = user.premium_since.astimezone(
            PH_TIMEZONE).strftime("%B %d, %Y • %I:%M %p GMT+8"
                                  ) if user.premium_since else "Not Boosting"
        is_bot = user.bot
    else:
        joined_at = "Not in Server"
        roles_str = "N/A"
        boost_since = "Not Boosting"
        is_bot = user.bot

    embed = discord.Embed(color=discord.Color.green())
    embed.add_field(name="Username", value=f"{user.mention}", inline=False)
    embed.add_field(name="Display Name",
                    value=f"`{user.display_name}`",
                    inline=True)
    embed.add_field(name="User ID", value=f"`{user.id}`", inline=True)
    embed.add_field(name="Created Account",
                    value=f"`{created_at}`",
                    inline=False)
    embed.add_field(name="Joined Server", value=f"`{joined_at}`", inline=False)

    if isinstance(user, discord.Member):
        embed.add_field(name="Roles", value=roles_str, inline=False)

    embed.add_field(name="Server Booster Since",
                    value=f"`{boost_since}`",
                    inline=False)

    if is_bot:
        embed.add_field(name="Bot Account", value="✅ Yes", inline=True)

    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)

    await interaction.response.send_message(embed=embed)


# ===========================
# Announcement Command
# ===========================
class AnnouncementModal(ui.Modal, title="Create Announcement"):
    def __init__(self):
        super().__init__()
        self.title_input = ui.TextInput(
            label="Title (optional)",
            default="ANNOUNCEMENT",
            required=False,
            max_length=256
        )
        self.message_input = ui.TextInput(
            label="Message (required)",
            placeholder="Paste your message here (supports line breaks)",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=4000
        )
        self.use_codeblock_input = ui.TextInput(
            label="Use Code Block? (Yes/No)",
            default="No",
            required=True,
            placeholder="Type 'Yes' or 'No'"
        )
        self.add_item(self.title_input)
        self.add_item(self.message_input)
        self.add_item(self.use_codeblock_input)

    async def on_submit(self, interaction: Interaction):
        title = self.title_input.value.strip() or "ANNOUNCEMENT"
        message = self.message_input.value.strip()
        use_codeblock = self.use_codeblock_input.value.strip().lower() in ("yes", "y", "true", "1")
        embed = discord.Embed(
            title="📎 Media/File",
            description="Please upload **an image** (PNG, JPG, GIF, etc.), or type `skip` to continue without media.",
            color=discord.Color.from_rgb(0, 0, 0)
        )
        embed.set_footer(text="Neroniel")
        embed.timestamp = datetime.now(PH_TIMEZONE)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        self.interaction = interaction
        self.title = title
        self.message = message
        self.use_codeblock = use_codeblock
        self.media_files = []
        await self.wait_for_media_or_skip()

    async def wait_for_media_or_skip(self):
        def check(m):
            return (
                m.author == self.interaction.user and
                m.channel == self.interaction.channel and
                (m.attachments or m.content.strip().lower() in ("skip", "end"))
            )
        try:
            msg = await bot.wait_for("message", timeout=300.0, check=check)

            # Delete only text commands like "skip" or "end"
            if msg.content.strip().lower() in ("skip", "end"):
                await msg.delete()
                await self.show_confirmation()
                return

            # Filter only image attachments
            valid_images = []
            for att in msg.attachments:
                if att.content_type and att.content_type.startswith('image'):
                    valid_images.append(att)

            if not valid_images:
                # Delete non-image message and prompt again
                await msg.delete()
                embed = discord.Embed(
                    title="📎 Media/File",
                    description="❌ Only **image files** are allowed.\nPlease upload an image or type `skip`.",
                    color=discord.Color.from_rgb(0, 0, 0)
                )
                embed.set_footer(text="Neroniel")
                embed.timestamp = datetime.now(PH_TIMEZONE)
                await self.interaction.edit_original_response(embed=embed)
                await self.wait_for_media_or_skip()
                return

            # ✅ DO NOT delete image message — keep it so URL stays valid
            self.media_files.extend(valid_images)
            count = len(self.media_files)
            embed = discord.Embed(
                title="📎 Media/File",
                description=f"You have added {count} image(s). Type `end` to continue, or upload more images.",
                color=discord.Color.from_rgb(0, 0, 0)
            )
            embed.set_footer(text="Neroniel")
            embed.timestamp = datetime.now(PH_TIMEZONE)
            await self.interaction.edit_original_response(embed=embed)
            await self.wait_for_media_or_skip()

        except asyncio.TimeoutError:
            embed = discord.Embed(
                title="⏰ Time out",
                description="Please run `/announcement` again.",
                color=discord.Color.from_rgb(0, 0, 0)
            )
            embed.set_footer(text="Neroniel")
            embed.timestamp = datetime.now(PH_TIMEZONE)
            await self.interaction.edit_original_response(embed=embed, view=None)

    async def show_confirmation(self):
        description = f"```\n{self.message}\n```" if self.use_codeblock else self.message
        embed = discord.Embed(
            title=self.title,
            description=description,
            color=discord.Color.from_rgb(0, 0, 0)
        )
        if self.media_files:
            embed.set_image(url=self.media_files[0].url)
        embed.set_footer(text="Neroniel • Preview")
        embed.timestamp = datetime.now(PH_TIMEZONE)
        view = AnnouncementConfirmationView(
            author=self.interaction.user,
            title=self.title,
            message=self.message,
            use_codeblock=self.use_codeblock,
            media_files=self.media_files
        )
        await self.interaction.edit_original_response(embed=embed, view=view)


class AnnouncementConfirmationView(ui.View):
    def __init__(self, author: discord.User, title: str, message: str, use_codeblock: bool, media_files: list):
        super().__init__(timeout=180)
        self.author = author
        self.title = title
        self.message = message
        self.use_codeblock = use_codeblock
        self.media_files = media_files

    async def interaction_check(self, interaction: Interaction) -> bool:
        return interaction.user == self.author

    @ui.button(label="Send", style=ButtonStyle.green)
    async def send_announcement(self, interaction: Interaction, button: ui.Button):
        await interaction.response.send_message(
            "Please select a channel to send the announcement to:",
            view=ChannelSelectForSendView(
                author=self.author,
                title=self.title,
                message=self.message,
                use_codeblock=self.use_codeblock,
                media_files=self.media_files
            ),
            ephemeral=True
        )

    @ui.button(label="Edit", style=ButtonStyle.gray)
    async def edit_announcement(self, interaction: Interaction, button: ui.Button):
        modal = AnnouncementModal()
        modal.title_input.default = self.title
        modal.message_input.default = self.message
        modal.use_codeblock_input.default = "Yes" if self.use_codeblock else "No"
        await interaction.response.send_modal(modal)

    @ui.button(label="Cancel", style=ButtonStyle.red)
    async def cancel_announcement(self, interaction: Interaction, button: ui.Button):
        cancel_embed = discord.Embed(
            title="❌ Announcement cancelled.",
            color=discord.Color.from_rgb(0, 0, 0)
        )
        cancel_embed.set_footer(text="Neroniel")
        cancel_embed.timestamp = datetime.now(PH_TIMEZONE)
        await interaction.response.edit_message(embed=cancel_embed, view=None)


class ChannelSelectForSendView(ui.View):
    def __init__(self, author: discord.User, title: str, message: str, use_codeblock: bool, media_files: list):
        super().__init__(timeout=180)
        self.author = author
        self.title = title
        self.message = message
        self.use_codeblock = use_codeblock
        self.media_files = media_files

    @ui.select(cls=ui.ChannelSelect, channel_types=[discord.ChannelType.text], placeholder="Select a channel...")
    async def select_channel(self, interaction: Interaction, select: ui.ChannelSelect):
        if interaction.user != self.author:
            await interaction.response.send_message("❌ Not your menu.", ephemeral=True)
            return
        selected_channel = select.values[0]

        # ✅ FIX: Fetch real channel to avoid AppCommandChannel error
        try:
            real_channel = await interaction.guild.fetch_channel(selected_channel.id)
        except discord.NotFound:
            await interaction.response.send_message("❌ Channel not found.", ephemeral=True)
            return
        except discord.Forbidden:
            await interaction.response.send_message("❌ I can't access that channel.", ephemeral=True)
            return

        description = f"```\n{self.message}\n```" if self.use_codeblock else self.message
        embed = discord.Embed(
            title=self.title,
            description=description,
            color=discord.Color.from_rgb(0, 0, 0)
        )
        if self.media_files:
            embed.set_image(url=self.media_files[0].url)
        embed.set_footer(text="Neroniel")
        embed.timestamp = datetime.now(PH_TIMEZONE)

        try:
            await real_channel.send(embed=embed)
            # Optional: send extra images as separate messages
            # for img in self.media_files[1:]:
            #     await real_channel.send(file=await img.to_file())
            success_embed = discord.Embed(
                title="✅ Announcement sent!",
                color=discord.Color.from_rgb(0, 0, 0)
            )
            success_embed.set_footer(text="Neroniel")
            success_embed.timestamp = datetime.now(PH_TIMEZONE)
            await interaction.response.edit_message(embed=success_embed, view=None)
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I don't have permission to send messages in that channel.", ephemeral=True
            )
        except Exception as e:
            error_embed = discord.Embed(
                title="❌ Failed to send",
                description=str(e),
                color=discord.Color.from_rgb(0, 0, 0)
            )
            error_embed.set_footer(text="Neroniel")
            error_embed.timestamp = datetime.now(PH_TIMEZONE)
            await interaction.response.send_message(embed=error_embed, ephemeral=True)


@bot.tree.command(name="announcement", description="Create an announcement with a guided form")
async def announcement(interaction: discord.Interaction):
    BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID"))
    is_owner = interaction.user.id == BOT_OWNER_ID
    is_admin = interaction.user.guild_permissions.administrator
    if not is_owner and not is_admin:
        await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
        return
    await interaction.response.send_modal(AnnouncementModal())


# ===========================
# Conversion Commands
# ===========================


# Set Rate
@bot.tree.command(
    name="setrate",
    description=
    "Set custom conversion rates for this server (minimum allowed rates enforced)"
)
@app_commands.describe(payout_rate="PHP per 1000 Robux for Payout",
                       gift_rate="PHP per 1000 Robux for Gift",
                       nct_rate="PHP per 1000 Robux for NCT",
                       ct_rate="PHP per 1000 Robux for CT")
async def setrate(interaction: discord.Interaction,
                  payout_rate: float = None,
                  gift_rate: float = None,
                  nct_rate: float = None,
                  ct_rate: float = None):
    await interaction.response.defer(ephemeral=True)

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send(
            "❌ You must be an administrator to use this command.",
            ephemeral=True)
        return

    guild_id = str(interaction.guild.id)
    current_rates = get_current_rates(guild_id)

    # Prepare new values, preserving existing ones if not provided
    new_rates = {
        "payout_rate":
        payout_rate if payout_rate is not None else current_rates["payout"],
        "gift_rate":
        gift_rate if gift_rate is not None else current_rates["gift"],
        "nct_rate":
        nct_rate if nct_rate is not None else current_rates["nct"],
        "ct_rate":
        ct_rate if ct_rate is not None else current_rates["ct"]
    }

    # Enforce minimum rate limits
    errors = []
    if payout_rate is not None and payout_rate < DEFAULT_RATES["payout_rate"]:
        errors.append(
            f"Payout Rate (min: ₱{DEFAULT_RATES['payout_rate']}/1000 Robux)")
    if gift_rate is not None and gift_rate < DEFAULT_RATES["gift_rate"]:
        errors.append(
            f"Gift Rate (min: ₱{DEFAULT_RATES['gift_rate']}/1000 Robux)")
    if nct_rate is not None and nct_rate < DEFAULT_RATES["nct_rate"]:
        errors.append(
            f"NCT Rate (min: ₱{DEFAULT_RATES['nct_rate']}/1000 Robux)")
    if ct_rate is not None and ct_rate < DEFAULT_RATES["ct_rate"]:
        errors.append(f"CT Rate (min: ₱{DEFAULT_RATES['ct_rate']}/1000 Robux)")

    if errors:
        error_msg = "❗ You cannot set rates below the minimum:\n" + "\n".join(
            errors)
        await interaction.followup.send(error_msg, ephemeral=True)
        return

    update_data = {
        "guild_id": guild_id,
        "payout_rate": new_rates["payout_rate"],
        "gift_rate": new_rates["gift_rate"],
        "nct_rate": new_rates["nct_rate"],
        "ct_rate": new_rates["ct_rate"],
        "updated_at": datetime.now(PH_TIMEZONE)
    }

    try:
        if rates_collection is not None:
            rates_collection.update_one({"guild_id": guild_id},
                                        {"$set": update_data},
                                        upsert=True)

            embed = discord.Embed(title="✅ Rates Updated",
                                  color=discord.Color.green())

            updated_fields = []
            if payout_rate is not None:
                updated_fields.append(
                    ("• Payout Rate",
                     f"₱{new_rates['payout_rate']:.2f} / 1000 Robux"))
            if gift_rate is not None:
                updated_fields.append(
                    ("• Gift Rate",
                     f"₱{new_rates['gift_rate']:.2f} / 1000 Robux"))
            if nct_rate is not None:
                updated_fields.append(
                    ("• NCT Rate",
                     f"₱{new_rates['nct_rate']:.2f} / 1000 Robux"))
            if ct_rate is not None:
                updated_fields.append(
                    ("• CT Rate", f"₱{new_rates['ct_rate']:.2f} / 1000 Robux"))

            for label, value in updated_fields:
                embed.add_field(name=label, value=value, inline=False)

            embed.set_footer(text="Neroniel")
            embed.timestamp = datetime.now(PH_TIMEZONE)

            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send("❌ Database not connected.",
                                            ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Error updating rates: {str(e)}",
                                        ephemeral=True)


# Reset Rate
@bot.tree.command(
    name="resetrate",
    description=
    "Reset specific conversion rates back to default (e.g., payout, gift)")
@app_commands.describe(payout="Reset Payout rate",
                       gift="Reset Gift rate",
                       nct="Reset NCT rate",
                       ct="Reset CT rate")
async def resetrate(interaction: discord.Interaction,
                    payout: bool = False,
                    gift: bool = False,
                    nct: bool = False,
                    ct: bool = False):
    await interaction.response.defer(ephemeral=True)

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send(
            "❌ You must be an administrator to use this command.",
            ephemeral=True)
        return

    guild_id = str(interaction.guild.id)

    # Check if any option was selected
    if not any([payout, gift, nct, ct]):
        await interaction.followup.send(
            "❗ Please select at least one rate to reset.", ephemeral=True)
        return

    update_data = {}
    reset_fields = []

    if payout:
        update_data["payout_rate"] = DEFAULT_RATES["payout_rate"]
        reset_fields.append("Payout")
    if gift:
        update_data["gift_rate"] = DEFAULT_RATES["gift_rate"]
        reset_fields.append("Gift")
    if nct:
        update_data["nct_rate"] = DEFAULT_RATES["nct_rate"]
        reset_fields.append("NCT")
    if ct:
        update_data["ct_rate"] = DEFAULT_RATES["ct_rate"]
        reset_fields.append("CT")

    try:
        if rates_collection is not None:
            result = rates_collection.update_one({"guild_id": guild_id},
                                                 {"$set": update_data})

            if result.modified_count > 0 or result.upserted_id is not None:
                embed = discord.Embed(
                    title="✅ Rates Reset",
                    description=
                    "Selected rates have been successfully reset to default values.",
                    color=discord.Color.green())
                embed.add_field(name="Reset Fields",
                                value=", ".join(reset_fields),
                                inline=False)
            else:
                embed = discord.Embed(
                    title="⚠️ No Changes Made",
                    description=
                    "No matching server found or no actual changes were needed.",
                    color=discord.Color.orange())
        else:
            embed = discord.Embed(title="❌ Database Error",
                                  description="Database not connected.",
                                  color=discord.Color.red())

        await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"❌ Error resetting rates: {str(e)}",
                                        ephemeral=True)


# Payout Rate
@bot.tree.command(
    name="payout",
    description="Convert Robux to PHP based on current Payout rate")
@app_commands.describe(robux="How much Robux do you want to convert?")
async def payout(interaction: discord.Interaction, robux: int):
    if robux <= 0:
        await interaction.response.send_message(
            "❗ Robux amount must be greater than zero.")
        return

    guild_id = interaction.guild.id
    rates = get_current_rates(guild_id)
    php = robux * (rates["payout"] / 1000)

    embed = discord.Embed(color=discord.Color.from_rgb(0, 0, 0))
    embed.add_field(name="Amount:", value=f"{robux} Robux", inline=False)
    embed.add_field(name="Payment:", value=f"₱{php:.2f} PHP", inline=False)
    embed.add_field(
        name="Note:",
        value=
        "To be eligible for a payout, you must be a member of the group for at least 14 days. Please ensure this requirement is met before proceeding with any transaction. You can view the Group Link by typing `/group` in the chat.",
        inline=False)
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="payoutreverse",
    description="Convert PHP to Robux based on current Payout rate")
@app_commands.describe(php="How much PHP do you want to convert?")
async def payoutreverse(interaction: discord.Interaction, php: float):
    if php <= 0:
        await interaction.response.send_message(
            "❗ PHP amount must be greater than zero.")
        return

    guild_id = interaction.guild.id
    rates = get_current_rates(guild_id)
    robux = round((php / rates["payout"]) * 1000)

    embed = discord.Embed(color=discord.Color.from_rgb(0, 0, 0))
    embed.add_field(name="Payment:", value=f"₱{php:.2f} PHP", inline=False)
    embed.add_field(name="Amount:", value=f"{robux} Robux", inline=False)
    embed.add_field(
        name="Note:",
        value=
        "To be eligible for a payout, you must be a member of the group for at least 14 days. Please ensure this requirement is met before proceeding with any transaction. You can view the Group Link by typing `/group` in the chat.",
        inline=False)
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)
    await interaction.response.send_message(embed=embed)


# Gift Rate
@bot.tree.command(
    name="gift", description="Convert Robux to PHP based on current Gift rate")
@app_commands.describe(robux="How much Robux do you want to convert?")
async def gift(interaction: discord.Interaction, robux: int):
    if robux <= 0:
        await interaction.response.send_message(
            "❗ Robux amount must be greater than zero.")
        return

    guild_id = interaction.guild.id
    rates = get_current_rates(guild_id)
    php = robux * (rates["gift"] / 1000)

    embed = discord.Embed(color=discord.Color.from_rgb(0, 0, 0))
    embed.add_field(name="Amount:", value=f"{robux} Robux", inline=False)
    embed.add_field(name="Payment:", value=f"₱{php:.2f} PHP", inline=False)
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="giftreverse",
    description="Convert PHP to Robux based on current Gift rate")
@app_commands.describe(php="How much PHP do you want to convert?")
async def giftreverse(interaction: discord.Interaction, php: float):
    if php <= 0:
        await interaction.response.send_message(
            "❗ PHP amount must be greater than zero.")
        return

    guild_id = interaction.guild.id
    rates = get_current_rates(guild_id)
    robux = round((php / rates["gift"]) * 1000)

    embed = discord.Embed(color=discord.Color.from_rgb(0, 0, 0))
    embed.add_field(name="Payment:", value=f"₱{php:.2f} PHP", inline=False)
    embed.add_field(name="Amount:", value=f"{robux} Robux", inline=False)
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)
    await interaction.response.send_message(embed=embed)


# NCT Rate
@bot.tree.command(name="nct",
                  description="Convert Robux to PHP based on current NCT rate")
@app_commands.describe(robux="How much Robux do you want to convert?")
async def nct(interaction: discord.Interaction, robux: int):
    if robux <= 0:
        await interaction.response.send_message("❗ Invalid input.")
        return

    guild_id = interaction.guild.id
    rates = get_current_rates(guild_id)
    php = robux * (rates["nct"] / 1000)

    embed = discord.Embed(color=discord.Color.from_rgb(0, 0, 0))
    embed.add_field(name="Amount:", value=f"{robux} Robux", inline=False)
    embed.add_field(name="Payment:", value=f"₱{php:.2f} PHP", inline=False)
    embed.add_field(
        name="Note:",
        value=
        "To proceed with this transaction, you must own the required Gamepass and have Regional Pricing disabled. Please ensure these requirements are met before proceeding with any transaction. You may view the Gamepass details by typing `/gamepass` in the chat and providing your Gamepass ID or Creator Dashboard URL.",
        inline=False)
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="nctreverse",
                  description="Convert PHP to Robux based on current NCT rate")
@app_commands.describe(php="How much PHP do you want to convert?")
async def nctreverse(interaction: discord.Interaction, php: float):
    if php <= 0:
        await interaction.response.send_message(
            "❗ PHP amount must be greater than zero.")
        return

    guild_id = interaction.guild.id
    rates = get_current_rates(guild_id)
    robux = round((php / rates["nct"]) * 1000)

    embed = discord.Embed(color=discord.Color.from_rgb(0, 0, 0))
    embed.add_field(name="Payment:", value=f"₱{php:.2f} PHP", inline=False)
    embed.add_field(name="Amount:", value=f"{robux} Robux", inline=False)
    embed.add_field(
        name="Note:",
        value=
        "To proceed with this transaction, you must own the required Gamepass and have Regional Pricing disabled. Please ensure these requirements are met before proceeding with any transaction. You may view the Gamepass details by typing `/gamepass` in the chat and providing your Gamepass ID or Creator Dashboard URL.",
        inline=False)
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)
    await interaction.response.send_message(embed=embed)


# CT Rate
@bot.tree.command(name="ct",
                  description="Convert Robux to PHP based on current CT rate")
@app_commands.describe(robux="How much Robux do you want to convert?")
async def ct(interaction: discord.Interaction, robux: int):
    if robux <= 0:
        await interaction.response.send_message("❗ Invalid input.")
        return

    guild_id = interaction.guild.id
    rates = get_current_rates(guild_id)
    php = robux * (rates["ct"] / 1000)

    embed = discord.Embed(color=discord.Color.from_rgb(0, 0, 0))
    embed.add_field(name="Amount:", value=f"{robux} Robux", inline=False)
    embed.add_field(name="Payment:", value=f"₱{php:.2f} PHP", inline=False)
    embed.add_field(
        name="Note:",
        value=
        "To proceed with this transaction, you must own the required Gamepass and have Regional Pricing disabled. Please ensure these requirements are met before proceeding with any transaction. You may view the Gamepass details by typing `/gamepass` in the chat and providing your Gamepass ID or Creator Dashboard URL.",
        inline=False)
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="ctreverse",
                  description="Convert PHP to Robux based on current CT rate")
@app_commands.describe(php="How much PHP do you want to convert?")
async def ctreverse(interaction: discord.Interaction, php: float):
    if php <= 0:
        await interaction.response.send_message(
            "❗ PHP amount must be greater than zero.")
        return

    guild_id = interaction.guild.id
    rates = get_current_rates(guild_id)
    robux = round((php / rates["ct"]) * 1000)

    embed = discord.Embed(color=discord.Color.from_rgb(0, 0, 0))
    embed.add_field(name="Payment:", value=f"₱{php:.2f} PHP", inline=False)
    embed.add_field(name="Amount:", value=f"{robux} Robux", inline=False)
    embed.add_field(
        name="Note:",
        value=
        "To proceed with this transaction, you must own the required Gamepass and have Regional Pricing disabled. Please ensure these requirements are met before proceeding with any transaction. You may view the Gamepass details by typing `/gamepass` in the chat and providing your Gamepass ID or Creator Dashboard URL.",
        inline=False)
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)
    await interaction.response.send_message(embed=embed)


# All Rates Comparison
@bot.tree.command(
    name="allrates",
    description="See PHP equivalent across all rates for given Robux")
@app_commands.describe(robux="How much Robux do you want to compare?")
async def allrates(interaction: discord.Interaction, robux: int):
    if robux <= 0:
        await interaction.response.send_message(
            "❗ Robux amount must be greater than zero.", ephemeral=True)
        return

    guild_id = str(interaction.guild.id)
    rates = get_current_rates(guild_id)

    embed = discord.Embed(title=f"Robux Conversion Rates ({robux} Robux)",
                          color=discord.Color.from_rgb(0, 0, 0))

    conversion_data = {
        "Payout Rate": rates["payout"],
        "Gift Rate": rates["gift"],
        "NCT Rate": rates["nct"],
        "CT Rate": rates["ct"]
    }

    for label, rate in conversion_data.items():
        php_value = (rate / 1000) * robux
        embed.add_field(name=f"• {label}",
                        value=f"₱{php_value:.2f}",
                        inline=False)

    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="allratesreverse",
    description="See Robux equivalent across all rates for given PHP")
@app_commands.describe(php="How much PHP do you want to compare?")
async def allratesreverse(interaction: discord.Interaction, php: float):
    if php <= 0:
        await interaction.response.send_message(
            "❗ PHP amount must be greater than zero.", ephemeral=True)
        return

    guild_id = str(interaction.guild.id)
    rates = get_current_rates(guild_id)

    embed = discord.Embed(title="PHP to Robux Conversion",
                          color=discord.Color.from_rgb(0, 0, 0))

    conversion_data = {
        "Payout Rate": rates["payout"],
        "Gift Rate": rates["gift"],
        "NCT Rate": rates["nct"],
        "CT Rate": rates["ct"]
    }

    for label, rate in conversion_data.items():
        robux_value = round((php / rate) * 1000)
        embed.add_field(name=f"• {label}",
                        value=f"{robux_value} Robux",
                        inline=False)

    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)

    await interaction.response.send_message(embed=embed)


# Tax Calculations
@bot.tree.command(
    name="beforetax",
    description="Calculate how much Robux you'll receive after 30% tax")
@app_commands.describe(robux="How much Robux is being sent?")
async def beforetax(interaction: discord.Interaction, robux: int):
    if robux <= 0:
        await interaction.response.send_message(
            "❗ Robux amount must be greater than zero.")
        return

    received = math.floor(robux * 0.7)

    embed = discord.Embed(color=discord.Color.from_rgb(0, 0, 0))
    embed.add_field(name="Required to Send:",
                    value=f"{robux} Robux",
                    inline=False)
    embed.add_field(name="Target Receive:",
                    value=f"{received} Robux",
                    inline=False)
    embed.add_field(
        name="Note:",
        value=
        "Roblox applies a 30% fee on transactions within its marketplace, including buying and selling items. This fee is deducted from the total transaction value.",
        inline=False)
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="aftertax",
    description=
    "Calculate how much Robux to send to receive desired amount after 30% tax")
@app_commands.describe(
    target="How much Robux do you want to receive after tax?")
async def aftertax(interaction: discord.Interaction, target: int):
    if target <= 0:
        await interaction.response.send_message(
            "❗ Target Robux must be greater than zero.")
        return

    sent = math.ceil(target / 0.7)

    embed = discord.Embed(color=discord.Color.from_rgb(0, 0, 0))
    embed.add_field(name="Target Receive:",
                    value=f"{target} Robux",
                    inline=False)
    embed.add_field(name="Required to Send:",
                    value=f"{sent} Robux",
                    inline=False)
    embed.add_field(
        name="Note:",
        value=
        "Roblox applies a 30% fee on transactions within its marketplace. To receive a specific amount, you must account for this deduction by sending more than your target.",
        inline=False)
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)

    await interaction.response.send_message(embed=embed)


# ConvertCurrency
@bot.tree.command(name="convertcurrency",
                  description="Convert between two currencies")
@app_commands.describe(amount="Amount to convert",
                       from_currency="Currency to convert from (e.g., USD)",
                       to_currency="Currency to convert to (e.g., PHP)")
async def convertcurrency(interaction: discord.Interaction, amount: float,
                          from_currency: str, to_currency: str):
    api_key = os.getenv("CURRENCY_API_KEY")
    if not api_key:
        await interaction.response.send_message(
            "❌ `CURRENCY_API_KEY` missing.", ephemeral=True)
        return
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()
    url = f"https://api.currencyapi.com/v3/latest?apikey= {api_key}&currencies={to_currency}&base_currency={from_currency}"
    try:
        response = requests.get(url)
        data = response.json()
        if 'error' in data:
            await interaction.response.send_message(
                f"❌ API Error: {data['error']['message']}")
            print("API Error Response:", data)
            return
        if "data" not in data or to_currency not in data["data"]:
            await interaction.response.send_message(
                "❌ Invalid currency code or no data found.")
            return
        rate = data["data"][to_currency]["value"]
        result = amount * rate
        embed = discord.Embed(title=f"💱 Currency Conversion",
                              color=discord.Color.gold())
        embed.add_field(name="📥 Input",
                        value=f"{amount} {from_currency}",
                        inline=False)
        embed.add_field(name="📉 Rate",
                        value=f"1 {from_currency} = {rate:.4f} {to_currency}",
                        inline=False)
        embed.add_field(name="📤 Result",
                        value=f"≈ **{result:.2f} {to_currency}**",
                        inline=False)
        embed.set_footer(text="Neroniel")
        embed.timestamp = datetime.now(PH_TIMEZONE)
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(
            f"❌ Error during conversion: {str(e)}")
        print("Exception Details:", str(e))


@convertcurrency.autocomplete('from_currency')
@convertcurrency.autocomplete('to_currency')
async def currency_autocomplete(
        interaction: discord.Interaction,
        current: str) -> list[app_commands.Choice[str]]:
    # Full list of supported currencies with names
    currencies = [
        "USD - US Dollar", "EUR - Euro", "JPY - Japanese Yen",
        "GBP - British Pound", "AUD - Australian Dollar",
        "CAD - Canadian Dollar", "CHF - Swiss Franc", "CNY - Chinese Yuan",
        "SEK - Swedish Krona", "NZD - New Zealand Dollar",
        "BRL - Brazilian Real", "INR - Indian Rupee", "RUB - Russian Ruble",
        "ZAR - South African Rand", "SGD - Singapore Dollar",
        "HKD - Hong Kong Dollar", "KRW - South Korean Won",
        "MXN - Mexican Peso", "TRY - Turkish Lira", "EGP - Egyptian Pound",
        "AED - UAE Dirham", "SAR - Saudi Riyal", "ARS - Argentine Peso",
        "CLP - Chilean Peso", "THB - Thai Baht", "MYR - Malaysian Ringgit",
        "IDR - Indonesian Rupiah", "PHP - Philippine Peso",
        "PLN - Polish Zloty"
    ]
    filtered = [c for c in currencies if current.lower() in c.lower()]
    return [
        app_commands.Choice(name=c, value=c.split(" ")[0])
        for c in filtered[:25]
    ]


# ========== Weather Command ==========
PHILIPPINE_CITIES = [
    "Manila", "Quezon City", "Caloocan", "Las PiÃ±as", "Makati", "Malabon",
    "Navotas", "Paranaque", "Pasay", "Muntinlupa", "Taguig", "Valenzuela",
    "Marikina", "Pasig", "San Juan", "Cavite", "Cebu", "Davao", "Iloilo",
    "Baguio", "Zamboanga", "Angeles", "Bacolod", "Batangas", "Cagayan de Oro",
    "Cebu City", "Davao City", "General Santos", "Iligan", "Kalibo",
    "Lapu-Lapu City", "Lucena", "Mandaue", "Olongapo", "Ormoc", "Oroquieta",
    "Ozamiz", "Palawan", "Puerto Princesa", "Roxas City", "San Pablo", "Silay"
]
GLOBAL_CAPITAL_CITIES = [
    "Washington D.C.", "London", "Paris", "Berlin", "Rome", "Moscow",
    "Beijing", "Tokyo", "Seoul", "New Delhi", "Islamabad", "Canberra",
    "Ottawa", "Brasilia", "Ottawa", "Cairo", "Nairobi", "Pretoria",
    "Kuala Lumpur", "Jakarta", "Bangkok", "Hanoi", "Athens", "Vienna",
    "Stockholm", "Oslo", "Copenhagen", "Helsinki", "Dublin", "Warsaw",
    "Prague", "Madrid", "Amsterdam", "Brussels", "Bern", "Wellington",
    "Santiago", "Buenos Aires", "Brasilia", "Abu Dhabi", "Doha", "Riyadh",
    "Kuwait City", "Muscat", "Manama", "Doha", "Beijing", "Shanghai", "Tokyo",
    "Seoul", "Sydney", "Melbourne"
]


@bot.tree.command(name="weather",
                  description="Get weather information for a city")
@app_commands.describe(city="City name",
                       unit="Temperature unit (default is Celsius)")
@app_commands.choices(unit=[
    app_commands.Choice(name="Celsius (°C)", value="c"),
    app_commands.Choice(name="Fahrenheit (°F)", value="f")
])
async def weather(interaction: discord.Interaction,
                  city: str,
                  unit: str = "c"):
    api_key = os.getenv("WEATHER_API_KEY")
    if not api_key:
        await interaction.response.send_message(
            "❌ Weather API key is missing.", ephemeral=True)
        return
    url = f"http://api.weatherapi.com/v1/current.json?key={api_key}&q={city}"
    try:
        response = requests.get(url)
        data = response.json()
        if "error" in data:
            await interaction.response.send_message(
                "❌ City not found or invalid input.", ephemeral=True)
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
            color=discord.Color.from_rgb(0, 0, 0))
        embed.add_field(name="🌡️ Temperature",
                        value=f"{temperature}{unit_label}",
                        inline=True)
        embed.add_field(name="🧯 Feels Like",
                        value=f"{feels_like}{unit_label}",
                        inline=True)
        embed.add_field(name="💧 Humidity", value=f"{humidity}%", inline=True)
        embed.add_field(name="🌬️ Wind Speed",
                        value=f"{wind_kph} km/h",
                        inline=True)
        embed.add_field(name="📝 Condition", value=condition, inline=False)
        embed.set_thumbnail(url=icon_url)
        embed.set_footer(text="Powered by WeatherAPI • Neroniel")
        embed.timestamp = datetime.now(PH_TIMEZONE)
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(
            f"❌ Error fetching weather: {str(e)}", ephemeral=True)


@weather.autocomplete('city')
async def city_autocomplete(interaction: discord.Interaction,
                            current: str) -> list[app_commands.Choice[str]]:
    # Combine Philippine and global capitals
    all_cities = PHILIPPINE_CITIES + GLOBAL_CAPITAL_CITIES
    # Filter based on user input
    filtered = [c for c in all_cities if current.lower() in c.lower()]
    return [app_commands.Choice(name=c, value=c) for c in filtered[:25]]


# ===========================
# Other Commands
# ===========================


# Purge Command
@bot.tree.command(name="purge",
                  description="Delete a specified number of messages")
@app_commands.describe(amount="How many messages would you like to delete?")
async def purge(interaction: discord.Interaction, amount: int):
    if amount <= 0:
        await interaction.response.send_message(
            "❗ Please specify a positive number of messages.", ephemeral=True)
        return

    BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID"))
    has_permission = interaction.user.guild_permissions.manage_messages or interaction.user.id == BOT_OWNER_ID
    if not has_permission:
        await interaction.response.send_message(
            "❗ You don't have permission to use this command.", ephemeral=True)
        return

    if not interaction.guild.me.guild_permissions.manage_messages:
        await interaction.response.send_message(
            "❗ I don't have permission to delete messages.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"✅ Deleted **{len(deleted)}** messages.",
                                    ephemeral=True)


# Group Info Command
@bot.tree.command(name="group",
                  description="Display information about the 1cy Roblox group")
async def groupinfo(interaction: discord.Interaction):
    GROUP_ID = int(os.getenv("GROUP_ID"))
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                    f"https://groups.roblox.com/v1/groups/{GROUP_ID}"
            ) as response:
                if response.status != 200:
                    raise Exception(f"API Error: {response.status}")
                data = await response.json()
        formatted_members = "{:,}".format(data['memberCount'])
        embed = discord.Embed(color=discord.Color.from_rgb(0, 0, 0))
        embed.add_field(
            name="Group Name",
            value=f"[{data['name']}](https://www.roblox.com/groups/{GROUP_ID})",
            inline=False)
        embed.add_field(name="Description",
                        value=f"""
{data.get('description', 'No description')}
""",
                        inline=False)
        embed.add_field(name="Group ID", value=str(data['id']), inline=True)
        owner = data.get('owner')
        owner_link = f"[{owner['username']}](https://www.roblox.com/users/{owner['userId']}/profile)" if owner else "No Owner"
        embed.add_field(name="Owner", value=owner_link, inline=True)
        embed.add_field(name="Members", value=formatted_members, inline=True)
        embed.set_footer(text="Neroniel")
        embed.timestamp = discord.utils.utcnow()
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(
            f"❌ Error fetching group info: {e}", ephemeral=True)


# Poll Command
@bot.tree.command(
    name="poll", description="Create a poll with reactions and result summary")
@app_commands.describe(question="Poll question",
                       amount="Duration amount",
                       unit="Time unit (seconds, minutes, hours)")
@app_commands.choices(unit=[
    app_commands.Choice(name="Seconds", value="seconds"),
    app_commands.Choice(name="Minutes", value="minutes"),
    app_commands.Choice(name="Hours", value="hours")
])
async def poll(interaction: discord.Interaction, question: str, amount: int,
               unit: app_commands.Choice[str]):
    if amount <= 0:
        await interaction.response.send_message(
            "❗ Amount must be greater than zero.", ephemeral=True)
        return
    total_seconds = {
        "seconds": amount,
        "minutes": amount * 60,
        "hours": amount * 3600
    }.get(unit.value, 0)
    if total_seconds == 0:
        await interaction.response.send_message(
            "❗ Invalid time unit selected.", ephemeral=True)
        return
    if total_seconds > 86400:
        await interaction.response.send_message(
            "❗ Duration cannot exceed 24 hours.", ephemeral=True)
        return
    embed = discord.Embed(title="📊 Poll",
                          description=question,
                          color=discord.Color.orange())
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
    result_embed = discord.Embed(title="📊 Poll Results",
                                 description=question,
                                 color=discord.Color.green())
    result_embed.add_field(name="👍 Upvotes", value=str(up_count), inline=True)
    result_embed.add_field(name="👎 Downvotes",
                           value=str(down_count),
                           inline=True)
    result_embed.add_field(name="Result", value=result, inline=False)
    result_embed.set_footer(text="Poll has ended")
    result_embed.timestamp = discord.utils.utcnow()
    await message.edit(embed=result_embed)


# Remind Me Command
@bot.tree.command(
    name="remindme",
    description="Set a reminder after X minutes (will ping you in this channel)"
)
@app_commands.describe(minutes="How many minutes until I remind you?",
                       note="Your reminder message")
async def remindme(interaction: discord.Interaction, minutes: int, note: str):
    if minutes <= 0:
        await interaction.response.send_message(
            "❗ Please enter a positive number of minutes.", ephemeral=True)
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
        f"⏰ I'll remind you in `{minutes}` minutes: `{note}`", ephemeral=True)


# Donate Command
@bot.tree.command(name="donate", description="Donate Robux to a Discord user.")
@app_commands.describe(user="The user to donate to.",
                       amount="The amount of Robux to donate.")
async def donate(interaction: discord.Interaction, user: discord.Member,
                 amount: int):
    if amount <= 0:
        await interaction.response.send_message(
            "❗ Robux amount must be greater than zero.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"`{interaction.user.name}` just donated **{amount:,} Robux** to {user.mention}!"
    )


# Say Command
@bot.tree.command(
    name="say",
    description=
    "Make the bot say something in chat (no @everyone/@here allowed)")
@app_commands.describe(message="Message for the bot to say")
async def say(interaction: discord.Interaction, message: str):
    if "@everyone" in message or "@here" in message:
        await interaction.response.send_message(
            "❌ No @everyone/@here allowed.", ephemeral=True)
        return
    await interaction.channel.send(message)
    await interaction.response.send_message("✅ Message sent!", ephemeral=True)


# Calculator Command
@bot.tree.command(name="calculator",
                  description="Perform basic math operations")
@app_commands.describe(num1="First number",
                       operation="Operation",
                       num2="Second number")
@app_commands.choices(operation=[
    app_commands.Choice(name="Addition (+)", value="add"),
    app_commands.Choice(name="Subtraction (-)", value="subtract"),
    app_commands.Choice(name="Multiplication (*)", value="multiply"),
    app_commands.Choice(name="Division (/)", value="divide")
])
async def calculator(interaction: discord.Interaction, num1: float,
                     operation: app_commands.Choice[str], num2: float):
    if operation.value == "divide" and num2 == 0:
        await interaction.response.send_message("❌ Cannot divide by zero.",
                                                ephemeral=True)
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
        await interaction.response.send_message(
            f"🔢 `{num1} {symbol} {num2} = {result}`")
    except Exception as e:
        await interaction.response.send_message(
            f"⚠️ An error occurred: {str(e)}")


# List All Commands
class CommandPaginator(ui.View):

    def __init__(self, embeds: list[discord.Embed], timeout: int = 180):
        super().__init__(timeout=timeout)
        self.embeds = embeds
        self.current_page = 0
        self.update_buttons()

    def update_buttons(self):
        self.children[0].disabled = self.current_page == 0  # Previous
        self.children[1].disabled = self.current_page == len(
            self.embeds) - 1  # Next

    @ui.button(label="◀️ Previous", style=ButtonStyle.gray)
    async def previous_page(self, interaction: Interaction, button: ui.Button):
        self.current_page -= 1
        self.update_buttons()
        await interaction.response.edit_message(
            embed=self.embeds[self.current_page], view=self)

    @ui.button(label="Next ▶️", style=ButtonStyle.gray)
    async def next_page(self, interaction: Interaction, button: ui.Button):
        self.current_page += 1
        self.update_buttons()
        await interaction.response.edit_message(
            embed=self.embeds[self.current_page], view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        # Edit original message to disable buttons
        try:
            await self.message.edit(view=self)
        except:
            pass


@bot.tree.command(
    name="listallcommands",
    description="List all available slash commands with pagination.")
async def listallcommands(interaction: discord.Interaction):
    # Define all commands by category (verified against your code)
    categories = {
        "🤖 AI Assistant": [
            "`/ask <prompt>` – Chat with Llama 3 AI",
            "`/clearhistory` – Clear your AI conversation history"
        ],
        "💰 Currency & Rates": [
            "`/payout <robux>` – Convert Robux to PHP (Payout rate)",
            "`/payoutreverse <php>` – Convert PHP to Robux (Payout rate)",
            "`/gift <robux>` – Convert using Gift rate",
            "`/giftreverse <php>` – Reverse Gift conversion",
            "`/nct <robux>` – Convert using NCT rate",
            "`/nctreverse <php>` – Reverse NCT conversion",
            "`/ct <robux>` – Convert using CT rate",
            "`/ctreverse <php>` – Reverse CT conversion",
            "`/allrates <robux>` – Compare all rates for given Robux",
            "`/allratesreverse <php>` – Compare all rates for given PHP",
            "`/setrate` – Set custom conversion rates (admin)",
            "`/resetrate` – Reset rates to default (admin)",
            "`/devex` – Convert Robux ↔ USD using DevEx rate"
        ],
        "💱 Currency & Tax Tools": [
            "`/convertcurrency <amount> <from> <to>` – Convert between world currencies",
            "`/beforetax <robux>` – Calculate received Robux after 30% tax",
            "`/aftertax <target>` – Calculate how much to send to receive target after tax"
        ],
        "🛠️ Utility Tools": [
            "`/userinfo [user]` – View user info",
            "`/avatar [user]` – Show user’s avatar",
            "`/banner [user]` – Show user’s banner",
            "`/group` – Show 1cy Roblox group info",
            "`/gamepass <id|link>` – Get public Gamepass link",
            "`/roblox <username|id>` – Get Roblox user profile",
            "`/stocks` – Show group funds & Robux stocks",
            "`/weather <city>` – Get weather info",
            "`/calculator <num1> <op> <num2>` – Basic math",
            "`/purge <amount>` – Delete messages (mod only)"
        ],
        "📢 Announcements & Messaging": [
            "`/announcement <message> <channel> [title] [use_codeblock] [image]` – Send embed announcement",
            "`/say <message>` – Make bot say something",
            "`/donate <user> <amount>` – Donate Robux (fun command)",
            "`/dm <user> <message>` – DM a user (owner only)",
            "`/dmall <message>` – DM all server members (owner only)"
        ],
        "⏰ Reminders & Polls": [
            "`/remindme <minutes> <note>` – Set a reminder",
            "`/poll <question> <time> <unit>` – Create a timed poll"
        ],
        "📱 Social Media Tools": [
            "`/tiktok <link>` – Download TikTok video",
            "`/instagram <link>` – Convert Instagram post to EmbedEZ link"
        ],
        "💳 Payments & Verification": [
            "`/payment <method>` – Show Gcash/PayMaya/GoTyme info",
            "`/checkpayout <username>` – Check Roblox payout eligibility",
            "`/check [cookie|creds]` – Check Roblox account details"
        ],
        "🔧 Developer & Info": [
            "`/invite` – Get bot invite link",
            "`/status` – Show bot server/user stats",
            "`/snipe` – Show last deleted message in channel"
        ]
    }

    # Build embeds (1 per category)
    embeds = []
    for name, cmds in categories.items():
        embed = discord.Embed(title=name,
                              description="\n".join(cmds),
                              color=discord.Color.from_rgb(0, 0, 0))
        embed.set_footer(text="Neroniel • Use buttons to navigate")
        embed.timestamp = datetime.now(PH_TIMEZONE)
        embeds.append(embed)

    if not embeds:
        await interaction.response.send_message("❌ No commands found.",
                                                ephemeral=True)
        return

    view = CommandPaginator(embeds)
    await interaction.response.send_message(embed=embeds[0], view=view)
    # Store message ref for timeout cleanup
    view.message = await interaction.original_response()


# ===========================
# Payment Command
# ===========================
class PaymentMethod(str, Enum):
    GCASH = "Gcash"
    PAYMAYA = "PayMaya"
    GOTYME = "GoTyme"


@bot.tree.command(
    name="payment",
    description="Show payment instructions for Gcash, PayMaya, or GoTyme")
@app_commands.describe(
    method="Choose a payment method to display instructions")
@app_commands.choices(method=[
    app_commands.Choice(name=PaymentMethod.GCASH, value=PaymentMethod.GCASH),
    app_commands.Choice(name=PaymentMethod.PAYMAYA,
                        value=PaymentMethod.PAYMAYA),
    app_commands.Choice(name=PaymentMethod.GOTYME, value=PaymentMethod.GOTYME),
])
async def payment(interaction: discord.Interaction, method: PaymentMethod):
    payment_info = {
        PaymentMethod.GCASH: {
            "title":
            "Gcash Payment",
            "description":
            "Account Initials: M R G.\nAccount Number: `09550333612`",
            "image":
            "https://raw.githubusercontent.com/KxroAI/whatupmyniggga/c52d0cb1f626fd55d24a6181fd3821c9dd9f1455/IMG_2868.jpeg"
        },
        PaymentMethod.PAYMAYA: {
            "title":
            "PayMaya Payment",
            "description":
            "Account Initials: N G.\nAccount Number: `09550333612`",
            "image":
            "https://raw.githubusercontent.com/KxroAI/whatupmyniggga/refs/heads/main/IMG_2869.jpeg"
        },
        PaymentMethod.GOTYME: {
            "title":
            "GoTyme Payment",
            "description":
            "Account Initials: N G.\nAccount Number: HIDDEN",
            "image":
            "https://raw.githubusercontent.com/KxroAI/whatupmyniggga/refs/heads/main/IMG_2870.jpeg"
        }
    }

    info = payment_info[method]

    embed = discord.Embed(title=info["title"],
                          description=info["description"],
                          color=discord.Color.from_rgb(0, 0, 0))

    if info["image"]:
        embed.set_image(url=info["image"])

    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)

    await interaction.response.send_message(embed=embed)


# ========== Avatar Command ==========
@bot.tree.command(name="avatar",
                  description="Display a user's profile picture")
@app_commands.describe(user="The user whose avatar you want to see")
async def avatar(interaction: discord.Interaction,
                 user: discord.Member = None):
    if user is None:
        user = interaction.user

    embed = discord.Embed(title=f"{user}'s Avatar",
                          color=discord.Color.from_rgb(0, 0, 0))
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
        await interaction.response.send_message("❌ User not found.",
                                                ephemeral=True)
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

    embed = discord.Embed(color=discord.Color.from_rgb(0, 0, 0))

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
        description=
        "Click [here](https://discord.com/oauth2/authorize?client_id=1358242947790803084&permissions=8&integration_type=0&scope=bot%20applications.commands ) to invite the bot to your server!",
        color=discord.Color.from_rgb(0, 0, 0)  # Black using RGB
    )
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)
    await interaction.response.send_message(embed=embed)


# ========== Status Command ==========
@bot.tree.command(
    name="status",
    description="Show how many servers the bot is in and total user count")
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


# ========== Stocks Command ==========
@bot.tree.command(name="stocks",
                  description="Show Roblox Group Funds and Robux Stocks")
async def stocks(interaction: discord.Interaction):
    await interaction.response.defer()
    headers = {"User-Agent": "Mozilla/5.0"}
    GROUP_ID_1CY = 5838002
    GROUP_ID_MC = 1081179215
    ROBLOX_COOKIE_1CY = os.getenv("ROBLOX_COOKIE")
    ROBLOX_COOKIE_MC = os.getenv("ROBLOX_COOKIE2")
    ROBLOX_STOCKS = os.getenv("ROBLOX_STOCKS")
    roblox_user_id = int(os.getenv("ROBLOX_STOCKS_ID")) if os.getenv(
        "ROBLOX_STOCKS_ID") else None

    # Check required variables
    missing_vars = []
    if not ROBLOX_COOKIE_1CY: missing_vars.append("ROBLOX_COOKIE")
    if not ROBLOX_COOKIE_MC: missing_vars.append("ROBLOX_COOKIE2")
    if not ROBLOX_STOCKS: missing_vars.append("ROBLOX_STOCKS")
    if not roblox_user_id: missing_vars.append("ROBLOX_STOCKS_ID")
    if missing_vars:
        await interaction.followup.send(
            f"❌ Missing required environment variables: {', '.join(missing_vars)}"
        )
        return

    # Initialize data containers
    data = {
        '1cy_group_funds': "||HIDDEN||",
        'mc_group_funds': "||HIDDEN||",
        'account_balance': "||HIDDEN||",
        '1cy_pending': "||HIDDEN||",
        'mc_pending': "||HIDDEN||",
        '1cy_daily_sales': "||HIDDEN||",
        'mc_daily_sales': "||HIDDEN||"
    }

    async with aiohttp.ClientSession() as session:
        # === 1cy Group Funds ===
        try:
            url = f"https://economy.roblox.com/v1/groups/{GROUP_ID_1CY}/currency"
            headers["Cookie"] = ROBLOX_COOKIE_1CY
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    res = await resp.json()
                    data['1cy_group_funds'] = f"{res.get('robux', 0):,} R$"
        except Exception as e:
            print(f"[ERROR] 1cy Group Funds: {str(e)}")

        # === Modded Corporations Group Funds ===
        try:
            url = f"https://economy.roblox.com/v1/groups/{GROUP_ID_MC}/currency"
            headers["Cookie"] = ROBLOX_COOKIE_MC
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    res = await resp.json()
                    data['mc_group_funds'] = f"{res.get('robux', 0):,} R$"
        except Exception as e:
            print(f"[ERROR] MC Group Funds: {str(e)}")

        # === Account Balance (from ROBLOX_STOCKS cookie) ===
        try:
            url = f"https://economy.roblox.com/v1/users/{roblox_user_id}/currency"
            headers["Cookie"] = ROBLOX_STOCKS
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    res = await resp.json()
                    data['account_balance'] = f"{res.get('robux', 0):,} R$"
        except Exception as e:
            print(f"[ERROR] Account Balance: {str(e)}")

        # === 1cy Revenue ===
        try:
            url = f"https://economy.roblox.com/v1/groups/{GROUP_ID_1CY}/revenue/summary/daily"
            headers["Cookie"] = ROBLOX_COOKIE_1CY
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    res = await resp.json()
                    data['1cy_pending'] = f"{res.get('pendingRobux', 0):,} R$"
                    data[
                        '1cy_daily_sales'] = f"{res.get('itemSaleRobux', 0):,} R$"
        except Exception as e:
            print(f"[ERROR] 1cy Revenue: {str(e)}")

        # === Modded Corporations Revenue ===
        try:
            url = f"https://economy.roblox.com/v1/groups/{GROUP_ID_MC}/revenue/summary/daily"
            headers["Cookie"] = ROBLOX_COOKIE_MC
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    res = await resp.json()
                    data['mc_pending'] = f"{res.get('pendingRobux', 0):,} R$"
                    data[
                        'mc_daily_sales'] = f"{res.get('itemSaleRobux', 0):,} R$"
        except Exception as e:
            print(f"[ERROR] MC Revenue: {str(e)}")

    # Build Embed
    embed = discord.Embed(color=discord.Color.from_rgb(0, 0, 0),
                          timestamp=datetime.now(PH_TIMEZONE))
    embed.add_field(name="1cy Group Funds",
                    value=data['1cy_group_funds'],
                    inline=False)
    embed.add_field(name="Modded Corporations Group Funds",
                    value=data['mc_group_funds'],
                    inline=False)
    embed.add_field(name="Account Balance",
                    value=data['account_balance'],
                    inline=False)
    embed.add_field(name="1cy Group Pending",
                    value=data['1cy_pending'],
                    inline=False)
    embed.add_field(name="Modded Corporations Group Pending",
                    value=data['mc_pending'],
                    inline=False)
    embed.add_field(name="1cy Daily Sales",
                    value=data['1cy_daily_sales'],
                    inline=False)
    embed.add_field(name="Modded Corporations Daily Sales",
                    value=data['mc_daily_sales'],
                    inline=False)
    embed.set_footer(text="Fetched via Roblox API | Neroniel")
    await interaction.followup.send(embed=embed)


# ========== Gamepass Command ==========
@bot.tree.command(
    name="gamepass",
    description=
    "Show a public Roblox Gamepass Link using an ID or Creator Dashboard URL")
@app_commands.describe(id="The Roblox Gamepass ID",
                       link="Roblox Creator Dashboard URL to convert")
async def gamepass(interaction: discord.Interaction,
                   id: int = None,
                   link: str = None):
    if id is not None and link is not None:
        await interaction.response.send_message(
            "❌ Please provide either an ID or a Link, not both.",
            ephemeral=True)
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
            await interaction.response.send_message(
                "❌ Invalid Roblox Dashboard Gamepass Link.", ephemeral=True)
            return
    else:
        await interaction.response.send_message(
            "❌ Please provide either a Gamepass ID or a Dashboard Link.",
            ephemeral=True)
        return

    base_url = f"https://www.roblox.com/game-pass/{pass_id}"

    embed = discord.Embed(color=discord.Color.from_rgb(0, 0, 0))
    embed.add_field(name="🔗 Link",
                    value=f"`{base_url}`\n\n[View Gamepass]({base_url})",
                    inline=False)
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)

    await interaction.response.send_message(embed=embed)


# ========== Devex Command ==========
@bot.tree.command(
    name="devex",
    description="Convert between Robux and USD using the current DevEx rate")
@app_commands.describe(
    conversion_type="Choose the type of value you're entering",
    amount="The amount of Robux or USD to convert")
@app_commands.choices(conversion_type=[
    app_commands.Choice(name="Robux to USD", value="robux"),
    app_commands.Choice(name="USD to Robux", value="usd")
])
async def devex(interaction: discord.Interaction,
                conversion_type: app_commands.Choice[str], amount: float):
    if amount <= 0:
        await interaction.response.send_message(
            "❗ Please enter a positive amount.", ephemeral=True)
        return

    devex_rate = 0.0035  # $0.0035 per Robux

    if conversion_type.value == "robux":
        robux = amount
        usd = robux * devex_rate
        embed = discord.Embed(
            title="💎 DevEx Conversion: Robux → USD",
            description=
            f"Converting **{robux} Robux** at the rate of **$0.0035/Robux**:",
            color=discord.Color.green())
        embed.add_field(name="Total USD Value",
                        value=f"**${usd:.4f} USD**",
                        inline=False)
    else:
        usd = amount
        robux = usd / devex_rate
        embed = discord.Embed(
            title="💎 DevEx Conversion: USD → Robux",
            description=
            f"Converting **${usd:.4f} USD** at the rate of **$0.0035/Robux**:",
            color=discord.Color.from_rgb(0, 0, 0))
        embed.add_field(name="Total Robux Value",
                        value=f"**{int(robux)} Robux**",
                        inline=False)

    embed.add_field(
        name="Note",
        value=
        "This is an estimate based on the current DevEx rate. Actual payout may vary.",
        inline=False)
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)

    await interaction.response.send_message(embed=embed)


# ========== Tiktok Command ==========
@bot.tree.command(name="tiktok",
                  description="Convert a TikTok Link into a Video")
@app_commands.describe(link="The TikTok Video URL to Convert",
                       spoiler="Should the video be sent as a spoiler?")
async def tiktok(interaction: discord.Interaction,
                 link: str,
                 spoiler: bool = False):
    await interaction.response.defer(ephemeral=False)

    original_dir = os.getcwd()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            print(f"[DEBUG] temp dir: {tmpdir}")

            os.chdir(tmpdir)
            print("[DEBUG] Changed cwd to temp dir")

            # Attempt to download TikTok video
            # If pyktok doesn't report failure, this should drop an .mp4 somewhere under tmpdir
            pyk.save_tiktok(link, save_video=True)

            # Debug: list everything in the temp directory after download
            for root, dirs, files in os.walk(tmpdir):
                rel_root = os.path.relpath(root, tmpdir)
                print(f"[DEBUG] Inspecting {rel_root or './'}: {files}")

            # Recursively search for the .mp4 video file
            video_files = [
                os.path.join(root, f) for root, _, files in os.walk(tmpdir)
                for f in files if f.lower().endswith(".mp4")
            ]

            if not video_files:
                await interaction.followup.send(
                    "❌ Failed to find TikTok video after download.")
                return

            video_path = video_files[0]
            filename = os.path.basename(video_path)
            if spoiler:
                filename = f"SPOILER_{filename}"

            await interaction.followup.send(file=discord.File(
                fp=video_path, filename=filename),
                                            ephemeral=False)
    except Exception as e:
        await interaction.followup.send(
            f"❌ An error occurred while processing the video: {e}")
        print(f"[ERROR] {e}")
    finally:
        os.chdir(original_dir)
        print(f"[DEBUG] Restored cwd to {original_dir}")


# ========== Instagram Command ==========
@bot.tree.command(name="instagram",
                  description="Convert Instagram Link into a Media/Video")
@app_commands.describe(link="Instagram post or reel URL",
                       spoiler="Should the video be sent as a spoiler?")
async def instagram_embedez(interaction: discord.Interaction,
                            link: str,
                            spoiler: bool = False):
    match = re.search(r"instagram\.com/(p|reel)/([^/]+)/", link)
    if not match:
        await interaction.response.send_message(
            "❌ Invalid Instagram post or reel link.", ephemeral=False)
        return

    short_code = match.group(2)
    instagramez_link = f"https://instagramez.com/p/{short_code}"

    message = f"[EmbedEZ]({instagramez_link})"
    await interaction.response.send_message(message, ephemeral=False)


# ========== Check Payout Command =======
@bot.tree.command(
    name="checkpayout",
    description="Check if a Roblox User is Eligible for Group Payout")
@app_commands.describe(username="Roblox Username",
                       group="Which group to check (default: 1cy)")
@app_commands.choices(group=[
    app_commands.Choice(name="1cy", value="1cy"),
    app_commands.Choice(name="Modded Corporations", value="mc")
])
async def check_payout(interaction: discord.Interaction,
                       username: str,
                       group: app_commands.Choice[str] = None):
    # Determine group settings
    if group and group.value == "mc":
        GROUP_ID = "1081179215"
        ROBLOX_COOKIE = os.getenv("ROBLOX_COOKIE2")
        group_name = "Modded Corporations"
    else:
        GROUP_ID = "5838002"
        ROBLOX_COOKIE = os.getenv("ROBLOX_COOKIE")
        group_name = "1cy"

    if not ROBLOX_COOKIE:
        await interaction.response.send_message(
            f"❌ `ROBLOX_COOKIE{'2' if group and group.value == 'mc' else ''}` is not set in environment variables.",
            ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)
    embed = discord.Embed(color=0x00bfff)
    embed.title = group_name
    embed.set_footer(text="/group | Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)

    # Step 1: Resolve username to user_id and display_name
    try:
        async with aiohttp.ClientSession() as session:
            url = 'https://users.roblox.com/v1/usernames/users'
            headers = {'Content-Type': 'application/json'}
            data = {'usernames': [username], 'excludeBannedUsers': True}
            async with session.post(url, headers=headers, json=data) as resp:
                if resp.status == 200:
                    json_data = await resp.json()
                    if json_data['data']:
                        user_id = json_data['data'][0]['id']
                        display_name = json_data['data'][0]['displayName']
                    else:
                        embed.description = "❌ User not found with that username."
                        embed.color = discord.Color.red()
                        await interaction.followup.send(embed=embed)
                        return
                else:
                    embed.description = f"❌ Error resolving username. Status code: {resp.status}"
                    embed.color = discord.Color.red()
                    await interaction.followup.send(embed=embed)
                    return
    except Exception as e:
        embed.description = f"❌ An error occurred during username lookup: `{str(e)}`"
        embed.color = discord.Color.red()
        await interaction.followup.send(embed=embed)
        return

    # Step 2: Check if user is in the group
    try:
        async with aiohttp.ClientSession() as session:
            membership_url = f'https://groups.roblox.com/v1/users/{user_id}/groups/roles'
            async with session.get(membership_url) as membership_resp:
                if membership_resp.status == 200:
                    groups = await membership_resp.json()
                    in_group = any(group['group']['id'] == int(GROUP_ID)
                                   for group in groups['data'])
                else:
                    embed.description = f"`{username}` ({display_name}) is ❌ not a member of the Group."
                    embed.color = discord.Color.red()
                    await interaction.followup.send(embed=embed)
                    return
    except Exception as e:
        embed.description = f"❌ An error occurred during Group Membership check: `{str(e)}`"
        embed.color = discord.Color.red()
        await interaction.followup.send(embed=embed)
        return

    # Step 3: Fetch the user's group role
    role_name = "Unknown"
    try:
        async with aiohttp.ClientSession() as session:
            roles_url = f'https://groups.roblox.com/v2/users/{user_id}/groups/roles'
            async with session.get(roles_url) as roles_resp:
                if roles_resp.status == 200:
                    roles_data = await roles_resp.json()
                    for group_entry in roles_data.get('data', []):
                        if str(group_entry['group']['id']) == GROUP_ID:
                            role_name = group_entry['role']['name']
                            break
    except Exception as e:
        print(f"[ERROR] Failed to fetch group role: {e}")
        role_name = "Error fetching role"

    # Step 3: Check payout eligibility
    try:
        async with aiohttp.ClientSession() as session:
            url = f'https://economy.roblox.com/v1/groups/{GROUP_ID}/users-payout-eligibility?userIds={user_id}'
            headers = {
                'Cookie': ROBLOX_COOKIE,
                'Accept': 'application/json',
                'Content-Type': 'application/json'
            }
            async with session.get(url, headers=headers) as response:
                text = await response.text()
                if response.status == 200:
                    try:
                        data = json.loads(text)
                        if "usersGroupPayoutEligibility" in data:
                            eligibility_status = data[
                                "usersGroupPayoutEligibility"].get(
                                    str(user_id))
                            if eligibility_status is None:
                                embed.description = f"`{username}` ({display_name}) was not found in the Payout Eligibility list."
                                embed.color = discord.Color.orange()
                            else:
                                is_eligible = eligibility_status if isinstance(
                                    eligibility_status,
                                    bool) else str(eligibility_status).lower(
                                    ) in ['true', 'eligible']
                                status_text = "✅ Eligible" if is_eligible else "❌ Not Currently Eligible"
                                embed.description = f"`{username}` ({display_name}) is **{status_text}**\n**Group Role:** {role_name}"
                                embed.color = discord.Color.green(
                                ) if is_eligible else discord.Color.red()
                        else:
                            embed.description = "❌ Invalid response format from Roblox API."
                            embed.color = discord.Color.red()
                    except json.JSONDecodeError:
                        embed.description = f"❌ Error parsing JSON response: {text}"
                        embed.color = discord.Color.red()
                    except Exception as e:
                        embed.description = f"❌ Error processing response: {str(e)}"
                        embed.color = discord.Color.red()
                else:
                    embed.description = f"❌ API Error: Status {response.status}\nResponse: {text}"
                    embed.color = discord.Color.red()
    except Exception as e:
        embed.description = f"❌ An error occurred during payout check: `{str(e)}`"
        embed.color = discord.Color.red()

    await interaction.followup.send(embed=embed)


# ========== Check Command ==========
async def get_csrf_token(session):
    async with session.post("https://auth.roblox.com/v2/logout") as resp:
        return resp.headers.get("x-csrf-token")


async def get_cookie_from_login(username,
                                password,
                                interaction,
                                captcha_data=None):
    url = "https://auth.roblox.com/v2/login"
    payload = {"ctype": "Username", "cvalue": username, "password": password}
    headers = {}
    async with aiohttp.ClientSession() as session:
        bot.xcsrf_token = await get_csrf_token(session)
        if bot.xcsrf_token:
            headers["x-csrf-token"] = bot.xcsrf_token

        if captcha_data:
            payload.update({
                "captchaToken": captcha_data["token"],
                "captchaId": captcha_data["id"]
            })

        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status == 200:
                data = await resp.json()
                cookie = None
                set_cookies = resp.headers.getall("Set-Cookie")
                for c in set_cookies:
                    if ".ROBLOSECURITY" in c:
                        cookie = c.split(".ROBLOSECURITY=")[1].split(";")[0]
                return {
                    "cookie": cookie,
                    "xcsrf": resp.headers.get("x-csrf-token")
                }
            elif resp.status == 403:
                data = await resp.json()
                error = data.get("errors", [{}])[0]
                if error.get("code") == 2:
                    field_data = json.loads(error.get("fieldData", "{}"))
                    captcha_id = field_data.get("unifiedCaptchaId")
                    return {"captcha": True, "captcha_id": captcha_id}
                else:
                    raise Exception(f"Login failed: {error}")
            else:
                text = await resp.text()
                raise Exception(f"Unexpected response: {resp.status} - {text}")


async def fetch_roblox_info(cookie):
    async with aiohttp.ClientSession(
            headers={"Cookie": f".ROBLOSECURITY={cookie}"}) as session:
        # Step 1: Get authenticated user ID
        async with session.get(
                "https://users.roblox.com/v1/users/authenticated") as r:
            if r.status != 200:
                raise Exception("Invalid or expired .ROBLOSECURITY cookie.")
            auth_data = await r.json()
            user_id = auth_data["id"]

        # Step 2: Get user info
        async with session.get(
                f"https://users.roblox.com/v1/users/{user_id}") as r:
            user_data = await r.json()
            username = user_data["name"]
            display_name = user_data.get("displayName", username)
            description = user_data.get("description", "None")

        # Step 3: Robux Balance
        async with session.get(
                f"https://economy.roblox.com/v1/users/{user_id}/currency"
        ) as r:
            economy_data = await r.json()
            robux = economy_data.get("robux", 0)

        # Step 4: Premium Status
        async with session.get(
                f"https://premiumfeatures.roblox.com/v1/users/{user_id}/validate-membership"
        ) as r:
            premium = await r.json()

        # Step 5: Credit Balance
        async with session.get("https://billing.roblox.com/v1/credit") as r:
            credit_info = await r.json()
            credit = credit_info.get("balance", 0)

        # Step 6: Email Verified?
        async with session.get(
                "https://accountinformation.roblox.com/v1/email") as r:
            email_info = await r.json()
            email_verified = email_info.get("verified", False)

        # Step 7: Phone Verified?
        async with session.get(
                "https://accountsettings.roblox.com/v1/privacy") as r:
            phone_info = await r.json()
            phone_verified = phone_info.get("phoneDiscovery", "") == "AllUsers"

        # Step 8: Can View Inventory
        async with session.get(
                f"https://inventory.roblox.com/v1/users/{user_id}/can-view-inventory"
        ) as r:
            inv_info = await r.json()
            inv_public = inv_info.get("canView", False)

        # Step 9: Primary Group
        async with session.get(
                f"https://groups.roblox.com/v1/users/{user_id}/groups/primary/role"
        ) as r:
            group_info = await r.json()
            group = group_info.get("group", None)

        # Step 10: Total RAP
        rap = await get_total_rap(user_id, session)

        return {
            "username": username,
            "userid": user_id,
            "display_name": display_name,
            "description": description,
            "robux": robux,
            "premium": premium,
            "credit": credit,
            "email_verified": email_verified,
            "phone_verified": phone_verified,
            "inv_public": inv_public,
            "group": group,
            "rap": rap
        }


async def get_total_rap(user_id, session):
    total_rap = 0
    cursor = ""
    while True:
        url = f"https://inventory.roblox.com/v1/users/{user_id}/assets/collectibles?sortOrder=Asc&limit=100"
        if cursor:
            url += f"&cursor={cursor}"
        async with session.get(url) as r:
            data = await r.json()
            for item in data.get("data", []):
                total_rap += item.get("recentAveragePrice", 0)
            cursor = data.get("nextPageCursor")
            if not cursor:
                break
    return total_rap


@bot.tree.command(
    name="check",
    description="Check details of a Roblox account using cookie or credentials."
)
@app_commands.describe(cookie="Provide .ROBLOSECURITY cookie",
                       username="Your Roblox username",
                       password="Your Roblox password")
async def check(interaction: Interaction,
                cookie: str = None,
                username: str = None,
                password: str = None):
    if cookie and (username or password):
        await interaction.response.send_message(
            "❌ Please provide either a cookie OR username + password.",
            ephemeral=True)
        return
    if not cookie and not (username and password):
        await interaction.response.send_message(
            "❌ Please provide either a cookie OR username and password.",
            ephemeral=True)
        return

    loading_embed = Embed(title="🔍 Loading Account Info...",
                          description="Please wait...",
                          color=discord.Color.orange())
    init_msg = await interaction.channel.send(embed=loading_embed)

    try:
        auth_result = None
        if cookie:
            auth_result = {"cookie": cookie}
        else:
            bot.xcsrf_token = None
            async with aiohttp.ClientSession() as session:
                async with session.get(
                        "https://auth.roblox.com/v2/logout") as r:
                    bot.xcsrf_token = r.headers.get("x-csrf-token")
            auth_result = await get_cookie_from_login(username, password,
                                                      interaction)
            if auth_result.get("captcha"):
                captcha_url = "https://arkoselabs.com/demo"
                captcha_embed = Embed(
                    title="🔐 Solve Captcha",
                    description=
                    f"[Click here to solve captcha]({captcha_url})\nReact with ✅ once solved.",
                    color=discord.Color.gold())
                await init_msg.edit(embed=captcha_embed)
                await init_msg.add_reaction("✅")

                def check_reaction(reaction, user):
                    return reaction.message.id == init_msg.id and user == interaction.user and str(
                        reaction.emoji) == "✅"

                try:
                    await bot.wait_for("reaction_add",
                                       timeout=90.0,
                                       check=check_reaction)
                except asyncio.TimeoutError:
                    await init_msg.edit(embed=Embed(title="⏰ Timed Out",
                                                    color=discord.Color.red()))
                    return
                await init_msg.remove_reaction("✅", interaction.user)
                auth_result = await get_cookie_from_login(
                    username, password, interaction, {
                        "token": "manual_captcha_solved",
                        "id": auth_result["captcha_id"]
                    })
            if not auth_result.get("cookie"):
                await init_msg.edit(
                    embed=Embed(title="❌ Login Failed",
                                description="Invalid credentials.",
                                color=discord.Color.red()))
                return

        info = await fetch_roblox_info(auth_result["cookie"])

        embed = Embed(color=discord.Color.green())
        embed.set_thumbnail(
            url=
            f"https://www.roblox.com/headshot-thumbnail/image?userId={info['userid']}&width=420&height=420&format=png"
        )

        # First row - Username and UserID
        embed.add_field(name="Username", value=info["username"], inline=True)
        embed.add_field(name="UserID", value=str(info["userid"]), inline=True)

        # Second row - Robux and Credit
        embed.add_field(name="Robux | Credit",
                        value=f"{info['robux']} | ${info['credit']}",
                        inline=True)

        # Third row - Email and Phone
        email_status = "Verified" if info["email_verified"] else "Add Email"
        phone_status = "Verified" if info["phone_verified"] else "Add Phone"
        embed.add_field(name="Email | Phone",
                        value=f"{email_status} | {phone_status}",
                        inline=True)

        # Fourth row - Inventory and RAP
        inventory_status = "[Public](https://www.roblox.com/users/{}/inventory/)".format(
            info["userid"]) if info["inv_public"] else "Private"
        embed.add_field(name="Inventory | RAP",
                        value=f"{inventory_status} | {info['rap']}",
                        inline=True)

        # Fifth row - Membership and Primary Group
        premium_status = "Premium" if info["premium"] else "Non Premium"
        if info["group"]:
            group = info["group"]
            group_link = f"[{group['name']}](https://www.roblox.com/groups/{group['id']})"
        else:
            group_link = "N/A"
        embed.add_field(name="Membership | Primary Group",
                        value=f"{premium_status} | {group_link}",
                        inline=True)

        # Sixth row - Description
        description = info['description'] if info['description'] else "N/A"
        embed.add_field(name="Description",
                        value=f"```\n{description}\n```",
                        inline=False)

        embed.set_footer(text="Neroniel")
        embed.timestamp = datetime.now(PH_TIMEZONE)
        await init_msg.edit(embed=embed)

    except Exception as e:
        await init_msg.edit(
            embed=Embed(title="❌ Error",
                        description=f"An error occurred:\n{str(e)}",
                        color=discord.Color.red()))
        print(f"[ERROR] /check: {e}")


# ========== Snipe Command ==========
bot.last_deleted_messages = {}


@bot.event
async def on_message_delete(message):
    # Ignore if message is from bot
    if message.author.bot:
        return

    # Store the deleted message in the dictionary
    bot.last_deleted_messages[message.channel.id] = {
        "author": str(message.author),
        "content": message.content,
        "timestamp": message.created_at,
        "attachments": [attachment.url for attachment in message.attachments]
    }

    # Optional: Delete old entries if needed to keep memory clean
    # For now, we'll just overwrite per channel


@bot.tree.command(name="snipe",
                  description="Show the last deleted message in this channel")
async def snipe(interaction: discord.Interaction):
    channel_id = interaction.channel_id
    if channel_id not in bot.last_deleted_messages:
        await interaction.response.send_message(
            "❌ There are no recently deleted messages in this channel.",
            ephemeral=True)
        return

    msg_data = bot.last_deleted_messages[channel_id]
    author = msg_data["author"]
    content = msg_data["content"] or "[No text content]"
    attachments = msg_data["attachments"]

    # Build embed
    embed = discord.Embed(description=content,
                          color=discord.Color.red(),
                          timestamp=msg_data["timestamp"])
    embed.set_author(name=author)
    embed.set_footer(text="Neroniel | Deleted at:")

    if attachments:
        embed.add_field(name="Attachments",
                        value="\n".join(
                            [f"[Link]({url})" for url in attachments]),
                        inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=False)


# ========== Roblox Command ==========
@bot.tree.command(
    name="roblox",
    description="Get Roblox user information by username or user ID")
@app_commands.describe(user="The Roblox username or user ID to look up")
async def roblox(interaction: discord.Interaction, user: str):
    await interaction.response.defer(ephemeral=False)

    GROUP_ID = 5838002  # Your real group ID

    try:
        async with aiohttp.ClientSession() as session:
            user_id = None
            display_name = None
            full_data = None
            last_online = "N/A"
            status = "Offline"

            # Resolve username or ID
            if user.isdigit():
                user_id = int(user)
                async with session.get(
                        f"https://users.roblox.com/v1/users/{user_id}"
                ) as resp:
                    if resp.status != 200:
                        return await interaction.followup.send(
                            "❌ User not found.", ephemeral=True)
                    full_data = await resp.json()
                    user = full_data['name']
                    display_name = full_data['displayName']
            else:
                resolve_url = "https://users.roblox.com/v1/usernames/users"
                payload = {"usernames": [user]}
                headers = {"Content-Type": "application/json"}
                async with session.post(resolve_url,
                                        json=payload,
                                        headers=headers) as resp:
                    if resp.status != 200:
                        return await interaction.followup.send(
                            "❌ Could not find that Roblox user.",
                            ephemeral=True)
                    data = await resp.json()
                    if not data['data']:
                        return await interaction.followup.send(
                            "❌ User not found.", ephemeral=True)
                    user_data = data['data'][0]
                    user_id = user_data['id']
                    display_name = user_data['displayName']

                async with session.get(
                        f"https://users.roblox.com/v1/users/{user_id}"
                ) as resp:
                    if resp.status != 200:
                        return await interaction.followup.send(
                            "❌ Failed to fetch user details.", ephemeral=True)
                    full_data = await resp.json()

            # Presence
            presence_url = "https://presence.roblox.com/v1/presence/users"
            async with session.post(presence_url, json={"userIds":
                                                        [user_id]}) as resp:
                if resp.status == 200:
                    presence_data = await resp.json()
                    if presence_data.get("userPresences"):
                        p = presence_data["userPresences"][0]
                        is_online = p.get("userPresenceType", 0) != 0
                        last_location = p.get("lastLocation", "Offline")
                        status = last_location if is_online else "Offline"
                        last_online_raw = p.get("lastOnline")
                        if last_online_raw:
                            last_dt = isoparse(last_online_raw)
                            last_online = last_dt.astimezone(
                                PH_TIMEZONE).strftime(
                                    "%A, %d %B %Y • %I:%M %p")

            # Thumbnail
            thumb_url = f"https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={user_id}&size=420x420&format=Png&scale=1"
            async with session.get(thumb_url) as resp:
                if resp.status == 200:
                    thumb_data = await resp.json()
                    image_url = thumb_data['data'][0]['imageUrl']
                else:
                    image_url = "https://www.roblox.com/asset-thumbnail/image?assetId=1&type=HeadShot&width=420&height=420&format=Png"

            # Creation date
            created_at = isoparse(full_data['created'])
            created_unix = int(created_at.timestamp())
            created_str = created_at.astimezone(PH_TIMEZONE).strftime(
                "%A, %d %B %Y • %I:%M %p")

            # Description
            description = full_data.get("description", "N/A") or "N/A"

            # Emojis
            verified = full_data.get('hasVerifiedBadge', False)
            premium = False
            try:
                async with session.get(
                        f"https://premiumfeatures.roblox.com/v1/users/{user_id}/validate-membership",
                        headers={"Cookie":
                                 os.getenv("ROBLOX_COOKIE")}) as resp:
                    if resp.status == 200:
                        premium = await resp.json()
            except:
                pass

            emoji = ""
            if verified:
                emoji += "<:RobloxVerified:1400310297184702564>"
            if premium:
                emoji += "<:RobloxPremium:1400310411550654495>"

            # Connections
            async with session.get(f"https://friends.roblox.com/v1/users/{user_id}/friends/count") as r1, \
                       session.get(f"https://friends.roblox.com/v1/users/{user_id}/followers/count") as r2, \
                       session.get(f"https://friends.roblox.com/v1/users/{user_id}/followings/count") as r3:
                friends = (await r1.json()).get('count',
                                                0) if r1.status == 200 else 0
                followers = (await r2.json()).get('count',
                                                  0) if r2.status == 200 else 0
                followings = (await r3.json()).get(
                    'count', 0) if r3.status == 200 else 0

            # Group role check
            role_name = None
            async with session.get(
                    f"https://groups.roblox.com/v2/users/{user_id}/groups/roles"
            ) as resp:
                if resp.status == 200:
                    groups_data = await resp.json()
                    for g in groups_data.get("data", []):
                        if g["group"]["id"] == GROUP_ID:
                            role_name = g["role"]["name"]
                            break

            # Embed (Status + Role on same block)
            description_text = (
                f"[**{display_name}**](https://www.roblox.com/users/{user_id}/profile) (**{user_id}**)\n"
                f"**@{user}** {emoji}\n"
                f"**Account Created:** <t:{created_unix}:f>\n"
                f"```{description}```\n"
                f"**Connections:** {friends}/{followers}/{followings}\n"
                f"**Status:** {status}" +
                (f" ({last_online})"
                 if status == "Offline" and last_online != "N/A" else ""))

            if role_name:
                description_text += f"\n**Group Role:** {role_name}"

            embed = discord.Embed(description=description_text,
                                  color=discord.Color.from_str("#000001"))
            embed.set_thumbnail(url=image_url)
            embed.set_footer(text="Neroniel")
            embed.timestamp = datetime.now(PH_TIMEZONE)

            await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"❌ An error occurred: `{str(e)}`",
                                        ephemeral=True)

# ========== MEXC Market Command ==========
@bot.tree.command(name="mexc", description="Show top 20 cryptos by volume on MEXC (Spot & Futures)")
async def mexc(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    try:
        # ===== SPOT DATA (Public) =====
        spot_url = "https://api.mexc.com/api/v3/ticker/24hr"
        spot_resp = requests.get(spot_url)
        spot_data = spot_resp.json()
        if not isinstance(spot_data, list):
            raise Exception("Invalid Spot API response")

        usdt_pairs = [item for item in spot_data if item['symbol'].endswith('USDT')]
        sorted_spot = sorted(usdt_pairs, key=lambda x: float(x['quoteVolume']), reverse=True)
        top_spot = sorted_spot[:10]

        spot_lines = []
        for coin in top_spot:
            sym = coin['symbol'].replace('USDT', '')
            price = float(coin['lastPrice'])
            vol = float(coin['quoteVolume'])
            change_pct = float(coin['priceChangePercent'])
            trend = "📈" if change_pct > 0 else "📉" if change_pct < 0 else "⏸️"
            line = f"`{sym:>6}` **${price:,.2f}** • **{vol:,.0f}** • {trend}"
            spot_lines.append(line)
        spot_content = "\n".join(spot_lines) if spot_lines else "No data available."

        # ===== FUTURES DATA (Authenticated) =====
        futures_url = "https://contract.mexc.com/api/v1/contract/depth/BTC_USDT"  # Example symbol
        # If you want ALL symbols, use: https://contract.mexc.com/api/v1/contract/contract/list
        # But for top 10, we'll simulate or fetch from public endpoint since MEXC Futures doesn't have a direct "top 10 by volume" public endpoint

        # Since MEXC Futures doesn't have a simple top-volume public endpoint, let's show BTC/ETH futures stats instead
        futures_symbols = ["BTC_USDT", "ETH_USDT", "SOL_USDT", "BNB_USDT", "DOGE_USDT"]
        futures_lines = []
        for symbol in futures_symbols:
            fut_url = f"https://contract.mexc.com/api/v1/contract/depth/{symbol}"
            fut_resp = requests.get(fut_url)
            fut_data = fut_resp.json()
            if fut_data.get("success") and "asks" in fut_data["data"]:
                best_ask = float(fut_data["data"]["asks"][0][0]) if fut_data["data"]["asks"] else 0
                best_bid = float(fut_data["data"]["bids"][0][0]) if fut_data["data"]["bids"] else 0
                mid_price = (best_ask + best_bid) / 2
                futures_lines.append(f"`{symbol.replace('_USDT', '')}` **${mid_price:,.2f}**")

        futures_content = "\n".join(futures_lines) if futures_lines else "No Futures data available."

        # ===== ACCOUNT BALANCE (Optional - if you want to show user's balance) =====
        # Uncomment below if you want to show balance (requires auth)
        """
        api_key = os.getenv("MEXC_API_KEY")
        secret_key = os.getenv("MEXC_SECRET_KEY")
        if not api_key or not secret_key:
            raise Exception("MEXC API keys not set in environment.")

        # Sign request for balance
        timestamp = str(int(time.time() * 1000))
        signature = hmac.new(secret_key.encode(), f"{timestamp}".encode(), hashlib.sha256).hexdigest()

        headers = {
            'X-MEXC-APIKEY': api_key,
            'X-MEXC-SIGNATURE': signature,
            'X-MEXC-TIMESTAMP': timestamp,
            'Content-Type': 'application/json'
        }

        balance_url = "https://contract.mexc.com/api/v1/contract/account/balance"
        balance_resp = requests.get(balance_url, headers=headers)
        balance_data = balance_resp.json()
        if balance_data.get("success"):
            usdt_balance = balance_data["data"].get("availableBalance", 0)
            futures_content += f"

**Your USDT Balance:** ${usdt_balance:,.2f}"
        """

        # Build Embed
        embed = discord.Embed(
            title="📊 MEXC Market Overview",
            color=discord.Color.from_rgb(0, 0, 0),
            timestamp=datetime.now(PH_TIMEZONE)
        )
        embed.set_footer(text="Data from MEXC API • Neroniel")

        embed.add_field(
            name="🌐 Spot Market (Top 10)",
            value=spot_content[:1020] + "..." if len(spot_content) > 1024 else spot_content,
            inline=False
        )

        embed.add_field(
            name="⚡ Futures Market (Top 5)",
            value=futures_content[:1020] + "..." if len(futures_content) > 1024 else futures_content,
            inline=False
        )

        await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"❌ Error: `{str(e)}`", ephemeral=True)
        print(f"[ERROR] /mexc: {e}")

# ===========================
# Bot Events
# ===========================
@bot.event
async def on_ready():
    bot.xcsrf_token = None
    print(f"Bot is ready! Logged in as {bot.user}")
    await bot.tree.sync()
    print("All commands synced!")
    # Start background tasks after bot is ready
    if reminders_collection is not None:
        if not check_reminders.is_running():
            print("✅ Starting reminder checker...")
            check_reminders.start()

    GROUP_ID = int(os.getenv("GROUP_ID"))

    # Create a persistent ClientSession for this loop
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                # Use aiohttp instead of requests
                async with session.get(
                        f"https://groups.roblox.com/v1/groups/{GROUP_ID}"
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        member_count = data.get('memberCount', 0)
                        await bot.change_presence(
                            status=discord.Status.dnd,
                            activity=discord.Activity(
                                type=discord.ActivityType.watching,
                                name=f"1cy | {member_count:,} Members"))
                    else:
                        print(
                            f"[WARNING] Roblox API returned status {response.status}"
                        )
                        await bot.change_presence(
                            status=discord.Status.dnd,
                            activity=discord.Activity(
                                type=discord.ActivityType.watching,
                                name="1cy"))
            except Exception as e:
                print(f"[ERROR] Failed to fetch group info: {str(e)}")
                await bot.change_presence(
                    status=discord.Status.dnd,
                    activity=discord.Activity(
                        type=discord.ActivityType.watching, name="1cy"))
            # Wait 60 seconds before next update
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
    elif content == "hello":
        await message.channel.send("hello, baby")
    auto_react_channels = [
        1225294057371074760, 1107600826664501258, 1107591404877791242,
        1368123462077513738
    ]
    if message.channel.id in auto_react_channels:
        await message.add_reaction("🎀")
    if message.channel.id == 1107281584337461321:
        await message.add_reaction("<:1cy_heart:1258694384346468362>")


# Run the bot
bot.run(os.getenv('DISCORD_TOKEN'))
