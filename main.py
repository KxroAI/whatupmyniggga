import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio
import requests
import os
import threading
import math
import random
import time
from flask import Flask
from collections import defaultdict
from dotenv import load_dotenv
import certifi
from pymongo import MongoClient
from datetime import datetime, timedelta
import pytz
from langdetect import detect as langdetect, LangDetectException
from discord.ui import Button, View

# Set timezone to Philippines (GMT+8)
PH_TIMEZONE = pytz.timezone("Asia/Manila")
load_dotenv()

# List of common cities in the Philippines for autocomplete
PHILIPPINE_CITIES = [
    "Manila", "Quezon City", "Caloocan", "Las Piñas", "Makati",
    "Malabon", "Navotas", "Paranaque", "Pasay", "Muntinlupa",
    "Taguig", "Valenzuela", "Marikina", "Pasig", "San Juan",
    "Cavite", "Cebu", "Davao", "Iloilo", "Baguio", "Zamboanga",
    "Angeles", "Bacolod", "Batangas", "Cagayan de Oro", "Cebu City",
    "Davao City", "General Santos", "Iligan", "Kalibo", "Lapu-Lapu City",
    "Lucena", "Mandaue", "Olongapo", "Ormoc", "Oroquieta", "Ozamiz",
    "Palawan", "Puerto Princesa", "Roxas City", "San Pablo", "Silay"
]

# List of major capital cities worldwide
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
# Flask Web Server (Keep Alive)
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
    games_collection = db.games  # NEW - For game stats and streaks

    # Create TTL indexes
    conversations_collection.create_index("timestamp", expireAfterSeconds=604800)  # 7 days
    reminders_collection.create_index("reminder_time", expireAfterSeconds=2592000)  # 30 days
except Exception as e:
    print(f"[!] Failed to connect to MongoDB: {e}")
    client = None
    conversations_collection = None
    reminders_collection = None
    games_collection = None

# ===========================
# Utility Functions
# ===========================

def get_user_pair_key(user1_id, user2_id):
    """Returns a consistent key for a user pair"""
    return tuple(sorted([user1_id, user2_id]))

def update_game_stats(user1_id, user2_id, winner_id, game_type):
    """Tracks game wins, streaks, and logs"""
    if not games_collection:
        return

    key = get_user_pair_key(user1_id, user2_id)

    log_entry = {
        "winner_id": winner_id,
        "timestamp": datetime.now(PH_TIMEZONE),
        "game_type": game_type
    }

    stats = games_collection.find_one({"user_pair": key})

    if stats:
        p1, p2 = key
        current_streaks = stats.get("streaks", {str(p1): 0, str(p2): 0})
        last_log = stats.get("game_history", [])[-1] if "game_history" in stats else None

        if last_log and last_log["winner_id"] == winner_id:
            current_streaks[str(winner_id)] += 1
        else:
            other_id = p1 if winner_id == p2 else p2
            current_streaks[str(other_id)] = 0
            current_streaks[str(winner_id)] = 1

        # Update global streaks
        global_data = games_collection.find_one({"user_id": winner_id}) or {}
        last_global_log = global_data.get("latest_game")

        if last_global_log and last_global_log["winner_id"] == winner_id:
            global_streak = global_data.get("global_streak", 0) + 1
        else:
            global_streak = 1

        max_streak = max(global_streak, global_data.get("max_streak", 0))

        update_data = {
            "$inc": {
                f"wins.{winner_id}": 1,
                "total_games": 1,
                f"game_logs.{game_type}": 1
            },
            "$set": {
                "streaks": current_streaks,
                "latest_game": log_entry,
                "last_updated": datetime.now(PH_TIMEZONE),
                "global_streak": global_streak,
                "max_streak": max_streak
            },
            "$push": {"game_history": log_entry}
        }
        games_collection.update_one({"user_pair": key}, update_data, upsert=True)
    else:
        p1, p2 = key
        new_doc = {
            "user_pair": key,
            "wins": {
                str(p1): 1 if p1 == winner_id else 0,
                str(p2): 1 if p2 == winner_id else 0
            },
            "total_games": 1,
            "game_logs": {game_type: 1},
            "game_history": [log_entry],
            "streaks": {
                str(p1): 1 if p1 == winner_id else 0,
                str(p2): 1 if p2 == winner_id else 0
            },
            "latest_game": log_entry
        }
        games_collection.insert_one(new_doc)

    games_collection.update_one(
        {"user_id": winner_id},
        {
            "$set": {
                "latest_game": log_entry,
                "global_streak": global_streak,
                "last_updated": datetime.now(PH_TIMEZONE),
                "max_streak": max_streak
            }
        },
        upsert=True
    )

# ===========================
# Background Tasks
# ===========================

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
            reminders_collection.delete_one({"_id": reminder["_id"]})
    except Exception as e:
        print(f"[!] Error checking reminders: {e}")

@check_reminders.before_loop
async def before_check_reminders():
    await bot.wait_until_ready()

if reminders_collection:
    check_reminders.start()

# ===========================
# Game Commands
# ===========================

@bot.tree.command(name="game", description="Play fun mini-games with the bot")
@app_commands.describe(game="Choose a game mode", user="Opponent (optional)")
@app_commands.choices(game=[
    app_commands.Choice(name="Blackjack", value="blackjack"),
    app_commands.Choice(name="Tic Tac Toe", value="tictactoe"),
    app_commands.Choice(name="Game Stats", value="stats"),
    app_commands.Choice(name="Hangman", value="hangman")
])
async def game(interaction: discord.Interaction, game: app_commands.Choice[str], user: discord.Member = None):
    if user and user == interaction.user:
        await interaction.response.send_message("❌ You can't play against yourself!", ephemeral=True)
        return

    if game.value == "blackjack":
        if user:
            await start_blackjack_game(interaction, interaction.user, user)
        else:
            await start_blackjack_solo(interaction)
    elif game.value == "tictactoe":
        if user:
            await start_tictactoe_game(interaction, interaction.user, user)
        else:
            await start_tictactoe_game(interaction, interaction.user, bot.user)
    elif game.value == "hangman":
        if user:
            await interaction.response.send_modal(HangmanChallengeModal(interaction.user, user))
        else:
            await start_hangman_game(interaction)
    elif game.value == "stats":
        target = user or interaction.user
        await show_game_stats(interaction, target)
    else:
        await interaction.response.send_message("❌ Invalid game selected.", ephemeral=True)

# ========== Blackjack ==========

async def start_blackjack_solo(interaction: discord.Interaction):
    player_hand = [random.randint(1, 11), random.randint(1, 10)]
    bot_hand = [random.randint(1, 11), random.randint(1, 10)]

    embed = discord.Embed(title="🃏 Blackjack", color=discord.Color.gold())
    embed.add_field(name="🧑 Your Hand", value=f"{player_hand} = {sum(player_hand)}", inline=False)
    embed.add_field(name="🤖 Bot's Hand", value="[?, ?]", inline=False)

    view = BlackjackSoloView(interaction.user, player_hand, bot_hand)
    await interaction.response.send_message(embed=embed, view=view)

class BlackjackButton(Button):
    def __init__(self, label, emoji, style, action):
        super().__init__(label=label, emoji=emoji, style=style)
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        await self.action(interaction)

class BlackjackSoloView(View):
    def __init__(self, player, player_hand, bot_hand):
        super().__init__(timeout=60)
        self.player = player
        self.player_hand = player_hand
        self.bot_hand = bot_hand

    async def hit(self, interaction: discord.Interaction):
        self.player_hand.append(random.randint(1, 11))
        total = sum(self.player_hand)
        if total > 21:
            embed = discord.Embed(title="💥 Busted!", description="You went over 21 and lost.", color=discord.Color.red())
            update_game_stats(self.player.id, self.bot_hand[0], self.bot_hand[0], "blackjack")  # Simulate bot ID
            await interaction.response.edit_message(embed=embed, view=None)
            return
        embed = discord.Embed(title="🃏 Blackjack", color=discord.Color.gold())
        embed.add_field(name="🧑 Your Hand", value=f"{self.player_hand} = {sum(self.player_hand)}", inline=False)
        embed.add_field(name="🤖 Bot's Hand", value="[?, ?]", inline=False)
        view = BlackjackSoloView(self.player, self.player_hand, self.bot_hand)
        view.add_item(BlackjackButton("Hit", "🟢", discord.ButtonStyle.success, self.hit))
        view.add_item(BlackjackButton("Stand", "🔴", discord.ButtonStyle.danger, self.stand))
        await interaction.response.edit_message(embed=embed, view=view)

    async def stand(self, interaction: discord.Interaction):
        bot_total = sum(self.bot_hand)
        while bot_total < 17:
            self.bot_hand.append(random.randint(1, 10))
            bot_total = sum(self.bot_hand)

        player_total = sum(self.player_hand)
        embed = discord.Embed(title="🎮 Blackjack Result", color=discord.Color.green())

        if player_total > 21:
            embed.description = "💥 You busted."
            embed.color = discord.Color.red()
        elif bot_total > 21 or player_total > bot_total:
            embed.description = f"{interaction.user.mention} wins! 🎉"
            update_game_stats(self.player.id, self.bot_hand[0], self.player.id, "blackjack")
        elif player_total < bot_total:
            embed.description = f"{interaction.user.mention} loses! 😢"
            update_game_stats(self.player.id, self.bot_hand[0], self.bot_hand[0], "blackjack")
        else:
            embed.description = "⚖️ It's a tie!"

        embed.add_field(name="🧑 Your Hand", value=f"{self.player_hand} = {sum(self.player_hand)}", inline=True)
        embed.add_field(name="🤖 Bot's Hand", value=f"{self.bot_hand} = {sum(self.bot_hand)}", inline=True)
        await interaction.response.edit_message(embed=embed, view=None)

# ========== Tic-Tac-Toe ==========

class TicTacToeButton(Button):
    def __init__(self, x, y):
        super().__init__(style=discord.ButtonStyle.secondary, label="\u200b", row=y)
        self.x = x
        self.y = y
        self.mark = None

    async def callback(self, interaction: discord.Interaction):
        view: TicTacToeView = self.view
        if interaction.user != view.current_player:
            await interaction.response.send_message("❌ Not your turn!", ephemeral=True)
            return

        if self.mark is not None:
            await interaction.response.send_message("❌ Already marked!", ephemeral=True)
            return

        self.mark = view.current_mark
        self.label = view.current_mark
        self.style = discord.ButtonStyle.success if view.current_mark == "X" else discord.ButtonStyle.danger
        self.disabled = True

        view.board[self.y][self.x] = view.current_mark
        await view.check_winner(interaction)

class TicTacToeView(View):
    def __init__(self, player1, player2):
        super().__init__(timeout=120)
        self.player1 = player1
        self.player2 = player2
        self.current_player = player1
        self.current_mark = "X"
        self.board = [["" for _ in range(3)] for _ in range(3)]
        for x in range(3):
            for y in range(3):
                self.add_item(TicTacToeButton(x, y))

    async def check_winner(self, interaction: discord.Interaction):
        winning_combos = [
            [(x, y) for x in range(3)],
            [(y, y) for y in range(3)],
            [(2 - y, y) for y in range(3)],
            [(y, x) for x in range(3)],
        ]
        winner = None
        for combo in winning_combos:
            marks = [self.board[x][y] for x, y in combo]
            if all(m == "X" for m in marks):
                winner = self.player1
            elif all(m == "O" for m in marks):
                winner = self.player2

        if winner:
            embed = discord.Embed(
                title="🎮 Tic-Tac-Toe",
                description=f"{winner.mention} wins!",
                color=discord.Color.green()
            )
            update_game_stats(self.player1.id, self.player2.id, winner.id, "tictactoe")
            for child in self.children:
                child.disabled = True
            await interaction.edit_original_response(embed=embed, view=self)
            self.stop()
            return

        if all(cell != "" for row in self.board for cell in row):
            embed = discord.Embed(
                title="🎮 Tic-Tac-Toe",
                description="It's a tie!",
                color=discord.Color.orange()
            )
            await interaction.edit_original_response(embed=embed, view=self)
            self.stop()
            return

        self.current_player = self.player2 if self.current_player == self.player1 else self.player1
        self.current_mark = "O" if self.current_mark == "X" else "X"

        embed = discord.Embed(
            title="🎮 Tic-Tac-Toe",
            description=f"{self.current_player.mention}'s turn ({self.current_mark})",
            color=discord.Color.gold()
        )
        await interaction.edit_original_response(embed=embed, view=self)

async def start_tictactoe_game(interaction: discord.Interaction, player1: discord.Member, player2: discord.Member):
    view = TicTacToeView(player1, player2)
    embed = discord.Embed(
        title="❌⭕ Tic-Tac-Toe",
        description=f"{player1.mention} vs {player2.mention}\n\n{player1.mention}'s turn (X)",
        color=discord.Color.gold()
    )
    for child in view.children:
        child.disabled = False
    await interaction.response.send_message(embed=embed, view=view)

# ========== Hangman ==========

HANGMAN_WORDS = {
    "animals": ["lion", "tiger", "elephant"],
    "fruits": ["apple", "banana", "grape"],
    "countries": ["philippines", "japan", "canada"],
    "movies": ["inception", "titanic", "avatar"],
    "brands": ["apple", "nike", "mcdonalds"]
}

HANGMAN_STAGES = [
    "```\n+---+\n    |\n    |\n    |\n   ===\n```",
    "```\n+---+\nO   |\n    |\n    |\n   ===\n```",
    "```\n+---+\nO   |\n|   |\n    |\n   ===\n```",
    "```\n+---+\n O  |\n/|  |\n    |\n   ===\n```",
    "```\n+---+\n O  |\n/|\\ |\n    |\n   ===\n```",
    "```\n+---+\n O  |\n/|\\ |\n/   |\n   ===\n```",
    "```\n+---+\n O  |\n/|\\ |\n/ \\ |\n   ===\n```"
]

class HangmanLetterButton(Button):
    def __init__(self, letter, hangman_view):
        super().__init__(label=letter.upper(), style=discord.ButtonStyle.secondary, row=int(ord(letter) // 9))
        self.letter = letter.lower()
        self.hangman_view = hangman_view

    async def callback(self, interaction: discord.Interaction):
        await self.hangman_view.handle_letter(interaction, self.letter)
        self.disabled = True
        await interaction.message.edit(view=self.hangman_view)

class HangmanChallengeModal(discord.ui.Modal, title="🧍 Hangman Challenge"):
    word = discord.ui.TextInput(label="Secret Word", placeholder="Enter a word for the opponent to guess", required=True)

    def __init__(self, challenger, opponent):
        super().__init__()
        self.challenger = challenger
        self.opponent = opponent

    async def on_submit(self, interaction: discord.Interaction):
        word = self.word.value.strip().lower()
        hidden = ["_" for _ in word]
        guessed_letters = set()
        attempts = 0

        embed = discord.Embed(title="🧍 Hangman", description="Guess the word!", color=discord.Color.gold())
        embed.add_field(name="Word", value=" ".join(hidden), inline=False)
        embed.add_field(name="Attempts", value=str(attempts), inline=True)
        embed.add_field(name="Guessed Letters", value="None", inline=True)
        embed.add_field(name="Status", value=HANGMAN_STAGES[attempts], inline=False)

        view = HangmanChallengeView(self.challenger, self.opponent, word, hidden, guessed_letters, attempts)
        for letter in "abcdefghijklmnopqrstuvwxyz":
            view.add_item(HangmanLetterButton(letter, view))
        await interaction.response.send_message(embed=embed, view=view)

class HangmanChallengeView(View):
    def __init__(self, challenger, opponent, word, hidden, guessed_letters, attempts):
        super().__init__(timeout=120)
        self.challenger = challenger
        self.opponent = opponent
        self.word = word
        self.hidden = hidden
        self.guessed_letters = guessed_letters
        self.attempts = attempts
        self.current_turn = opponent
        self.buttons = {}

    async def handle_letter(self, interaction: discord.Interaction, letter: str):
        if letter in self.guessed_letters:
            await interaction.response.send_message("⚠️ Already guessed that letter.", ephemeral=True)
            return

        self.guessed_letters.add(letter)

        if letter in self.word:
            for i in range(len(self.word)):
                if self.word[i] == letter:
                    self.hidden[i] = letter
        else:
            self.attempts += 1

        guessed_str = ", ".join(sorted(self.guessed_letters)) or "None"
        word_display = " ".join(self.hidden)

        embed = discord.Embed(title="🧍 Hangman", color=discord.Color.gold())
        embed.add_field(name="Word", value=word_display, inline=False)
        embed.add_field(name="Attempts", value=str(self.attempts), inline=True)
        embed.add_field(name="Guessed Letters", value=guessed_str, inline=True)
        embed.add_field(name="Status", value=HANGMAN_STAGES[self.attempts], inline=False)

        if "_" not in self.hidden:
            embed.description = f"🎉 {interaction.user.mention} wins!"
            embed.color = discord.Color.green()
            update_game_stats(self.challenger.id, self.opponent.id, interaction.user.id, "hangman")
            for child in self.children:
                child.disabled = True
        elif self.attempts >= len(HANGMAN_STAGES) - 1:
            embed.description = f"💀 You lose! The word was: `{self.word}`"
            embed.color = discord.Color.red()
            update_game_stats(self.challenger.id, self.opponent.id, self.opponent.id, "hangman")
            for child in self.children:
                child.disabled = True
        else:
            await interaction.response.defer()
            await interaction.edit_original_response(embed=embed, view=self)
            return

        await interaction.edit_original_response(embed=embed, view=None)

# ========== Game Stats ==========

@bot.tree.command(name="gamestreak", description="Check current win streak between you and another user")
@app_commands.describe(user="The user to compare streak with")
async def gamestreak(interaction: discord.Interaction, user: discord.Member):
    if user == interaction.user:
        await interaction.response.send_message("❌ You can't check streak with yourself!", ephemeral=True)
        return

    key = get_user_pair_key(interaction.user.id, user.id)
    stats = games_collection.find_one({"user_pair": key})
    
    if not stats:
        await interaction.response.send_message(f"📊 No recorded games between you and {user.mention}.", ephemeral=True)
        return

    streak_p1 = stats["streaks"].get(str(interaction.user.id), 0)
    streak_p2 = stats["streaks"].get(str(user.id), 0)

    embed = discord.Embed(
        title=f"🔥 Win Streak: {interaction.user.display_name} vs {user.display_name}",
        color=discord.Color.blue()
    )
    embed.add_field(name=interaction.user.display_name, value=f"Current Streak: {streak_p1}", inline=True)
    embed.add_field(name=user.display_name, value=f"Current Streak: {streak_p2}", inline=True)

    if streak_p1 > streak_p2:
        embed.description = f"{interaction.user.mention} has the longer streak!"
    elif streak_p2 > streak_p1:
        embed.description = f"{user.mention} has the longer streak!"
    else:
        embed.description = "You're tied in win streaks!"

    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    embed.set_footer(text="Neroniel")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="gamelb", description="Show top players by win streak or game stats")
@app_commands.describe(mode="pairs | global | blackjack | tictactoe | hangman")
async def gamelb(interaction: discord.Interaction, mode: str = "pairs"):
    if not games_collection:
        await interaction.response.send_message("❌ Game data is not available.", ephemeral=True)
        return

    if mode in ["global", "streak"]:
        cursor = games_collection.find({"max_streak": {"$exists": True}})
        streak_list = []
        for doc in cursor:
            user_id = doc["user_id"]
            try:
                user = await bot.fetch_user(user_id)
            except:
                continue
            max_streak = doc.get("max_streak", 0)
            streak_list.append({
                "user": user,
                "max_streak": max_streak
            })
        streak_list.sort(key=lambda x: x["max_streak"], reverse=True)

        embed = discord.Embed(title="🔥 Global Win Streak Leaderboard", color=discord.Color.gold())
        for i, entry in enumerate(streak_list[:10], 1):
            embed.add_field(
                name=f"{i}. {entry['user'].display_name}",
                value=f"Longest Streak: **{entry['max_streak']}**",
                inline=False
            )
        await interaction.response.send_message(embed=embed)
    else:
        game_filter = mode if mode in ["blackjack", "tictactoe", "hangman"] else None
        cursor = games_collection.find({})
        pair_scores = []

        for doc in cursor:
            p1, p2 = doc["user_pair"]
            wins_p1 = doc["wins"].get(str(p1), 0)
            wins_p2 = doc["wins"].get(str(p2), 0)
            total_wins = wins_p1 + wins_p2
            if game_filter and doc["game_logs"].get(game_filter, 0) == 0:
                continue
            pair_scores.append({
                "pair": doc["user_pair"],
                "total_wins": total_wins,
                "wins": doc["wins"],
                "streaks": doc.get("streaks", {}),
                "game_type": game_filter or "all"
            })

        pair_scores.sort(key=lambda x: x["total_wins"], reverse=True)

        embed = discord.Embed(
            title=f"🏆 Top Game Pairs ({game_filter.capitalize() if game_filter else 'All Games'})",
            color=discord.Color.gold()
        )

        for i, entry in enumerate(pair_scores[:10], 1):
            try:
                u1 = await bot.fetch_user(entry["pair"][0])
                u2 = await bot.fetch_user(entry["pair"][1])
            except:
                continue

            w1 = entry["wins"].get(str(entry["pair"][0]), 0)
            w2 = entry["wins"].get(str(entry["pair"][1]), 0)
            s1 = entry["streaks"].get(str(entry["pair"][0]), 0)
            s2 = entry["streaks"].get(str(entry["pair"][1]), 0)

            longest_streak = max(s1, s2)
            longest_streak_user = u1.display_name if s1 > s2 else u2.display_name

            embed.add_field(
                name=f"{i}. {u1.display_name} & {u2.display_name}",
                value=f"{u1.display_name}: {w1} win(s)\n"
                      f"{u2.display_name}: {w2} win(s)\n"
                      f"🔥 Longest Streak: {longest_streak} by {longest_streak_user}",
                inline=False
            )

        await interaction.response.send_message(embed=embed)

# ========== Show Game Stats ==========

async def show_game_stats(interaction: discord.Interaction, user: discord.Member):
    if user == interaction.user:
        await interaction.response.send_message("❌ Can't check stats with yourself!", ephemeral=True)
        return

    key = get_user_pair_key(interaction.user.id, user.id)
    stats = games_collection.find_one({"user_pair": key})

    if not stats:
        await interaction.response.send_message(f"📊 No recorded games between you and {user.mention}.", ephemeral=True)
        return

    w1 = stats["wins"].get(str(interaction.user.id), 0)
    w2 = stats["wins"].get(str(user.id), 0)
    total = stats.get("total_games", 0)
    win_rate = (w1 / total * 100) if total > 0 else 0

    embed = discord.Embed(
        title=f"🎮 Game Stats: {interaction.user.display_name} vs {user.display_name}",
        color=discord.Color.blue()
    )
    embed.add_field(name="Total Games", value=str(total), inline=False)
    embed.add_field(name=interaction.user.display_name, value=f"Wins: {w1}", inline=True)
    embed.add_field(name=user.display_name, value=f"Wins: {w2}", inline=True)
    embed.add_field(name="Win Rate", value=f"{win_rate:.1f}% wins", inline=False)
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    embed.set_footer(text="Neroniel")
    await interaction.response.send_message(embed=embed)

# ========== Currency Conversion Commands ==========

# Payout
@bot.tree.command(name="payout", description="Convert Robux to PHP based on Payout rate (₱320 for 1000 Robux)")
@app_commands.describe(robux="How much Robux do you want to convert?")
async def payout(interaction: discord.Interaction, robux: int):
    if robux <= 0:
        await interaction.response.send_message("❗ Robux amount must be greater than zero.")
        return
    php = robux * (320 / 1000)
    await interaction.response.send_message(f"💵 {robux} Robux = **₱{php:.2f} PHP**")

# Gift
@bot.tree.command(name="gift", description="Convert Robux to PHP based on Gift rate (₱250 for 1000 Robux)")
@app_commands.describe(robux="How much Robux do you want to convert?")
async def gift(interaction: discord.Interaction, robux: int):
    if robux <= 0:
        await interaction.response.send_message("❗ Robux amount must be greater than zero.")
        return
    php = robux * (250 / 1000)
    await interaction.response.send_message(f"🎁 {robux} Robux = **₱{php:.2f} PHP**")

# NCT
@bot.tree.command(name="nct", description="Convert Robux to PHP based on NCT rate (₱240/1k)")
@app_commands.describe(robux="How much Robux do you want to convert?")
async def nct(interaction: discord.Interaction, robux: int):
    if robux <= 0:
        await interaction.response.send_message("❗ Invalid input.")
        return
    php = robux * (240 / 1000)
    await interaction.response.send_message(f"💵 {robux} Robux = **₱{php:.2f} PHP**")

# CT
@bot.tree.command(name="ct", description="Convert Robux to PHP based on CT rate (₱340/1k)")
@app_commands.describe(robux="How much Robux do you want to convert?")
async def ct(interaction: discord.Interaction, robux: int):
    if robux <= 0:
        await interaction.response.send_message("❗ Robux amount must be greater than zero.")
        return
    php = robux * (340 / 1000)
    await interaction.response.send_message(f"💵 {robux} Robux = **₱{php:.2f} PHP**")

# All Rates
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

# Before Tax
@bot.tree.command(name="beforetax", description="Calculate how much Robux you'll receive after 30% tax")
@app_commands.describe(robux="How much Robux is being sent?")
async def beforetax(interaction: discord.Interaction, robux: int):
    if robux <= 0:
        await interaction.response.send_message("❗ Robux amount must be greater than zero.")
        return
    received = math.floor(robux * 0.7)
    await interaction.response.send_message(f"📤 Sending **{robux} Robux** → You will receive **{received} Robux** after tax.")

# After Tax
@bot.tree.command(name="aftertax", description="Calculate how much Robux to send to receive desired amount after 30% tax")
@app_commands.describe(target="How much Robux do you want to receive *after* tax?")
async def aftertax(interaction: discord.Interaction, target: int):
    if target <= 0:
        await interaction.response.send_message("❗ Target Robux must be greater than zero.")
        return
    sent = math.ceil(target / 0.7)
    await interaction.response.send_message(f"📬 To receive **{target} Robux**, send **{sent} Robux** (30% tax).")

# ConvertCurrency
@bot.tree.command(name="convertcurrency", description="Convert between two currencies")
@app_commands.describe(
    amount="Amount to convert",
    from_currency="Currency to convert from (e.g., USD)",
    to_currency="Currency to convert to (e.g., PHP)"
)
async def convertcurrency(interaction: discord.Interaction, amount: float, from_currency: str, to_currency: str):
    api_key = os.getenv("CURRENCY_API_KEY")
    if not api_key:
        await interaction.response.send_message("❌ `CURRENCY_API_KEY` is missing in environment variables.")
        return
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()
    url = f"https://api.currencyapi.com/v3/latest?apikey={api_key}&currencies={to_currency}&base_currency={from_currency}"
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
        embed = discord.Embed(color=discord.Color.gold())
        embed.title = f"💱 Currency Conversion from {from_currency}"
        embed.add_field(name="📥 Input", value=f"`{amount} {from_currency}`", inline=False)
        embed.add_field(name="📉 Rate", value=f"`1 {from_currency} = {rate:.4f} {to_currency}`", inline=False)
        embed.add_field(name="📤 Result", value=f"≈ **{result:.2f} {to_currency}**", inline=False)
        embed.set_footer(text="Neroniel")
        embed.timestamp = datetime.now(PH_TIMEZONE)
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error during conversion: {str(e)}")
        print("Exception Details:", str(e))

# ========== Weather Command ==========

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
        condition = current["condition"]["text"]
        icon_url = f"https:{current['condition']['icon']}"

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
        await interaction.response.send_message(f"❌ Error fetching weather data: {str(e)}", ephemeral=True)

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
        for c in filtered[:25]  # Max 25 choices
    ]

# ========== Utility Commands ==========

# User Info
@bot.tree.command(name="userinfo", description="Display detailed info about a user")
@app_commands.describe(member="Optional, defaults to you")
async def userinfo(interaction: discord.Interaction, member: discord.Member = None):
    if member is None:
        member = interaction.user

    created_at = member.created_at.astimezone(PH_TIMEZONE).strftime("%B %d, %Y • %I:%M %p GMT+8")
    joined_at = member.joined_at.astimezone(PH_TIMEZONE).strftime("%B %d, %Y • %I:%M %p GMT+8") if member.joined_at else "Unknown"
    roles = [role.mention for role in member.roles if not role.is_default()]
    roles_str = ", ".join(roles) if roles else "No Roles"
    boost_since = member.premium_since.astimezone(PH_TIMEZONE).strftime("%B %d, %Y • %I:%M %p GMT+8") if member.premium_since else "Not Boosting"

    embed = discord.Embed(title=f"👤 User Info for {member}", color=discord.Color.green())
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

# Group Info
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
        embed.add_field(name="Description", value=f"```\n{data['description'] or 'No description'}\n```", inline=False)
        embed.add_field(name="Group ID", value=str(data['id']), inline=True)
        owner = data['owner']
        owner_link = f"[{owner['username']}](https://www.roblox.com/users/{owner['userId']}/profile)" if owner else "No owner"
        embed.add_field(name="Owner", value=owner_link, inline=True)
        embed.add_field(name="Members", value=formatted_members, inline=True)
        embed.set_footer(text="Neroniel")
        embed.timestamp = discord.utils.utcnow()
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error fetching group info: {e}", ephemeral=True)

# ========== Fun Commands ==========

# Poll
@bot.tree.command(name="poll", description="Create a poll with up/down votes")
@app_commands.describe(question="Poll question", amount="Duration amount", unit="Time unit (seconds, minutes, hours)")
@app_commands.choices(unit=[
    app_commands.Choice(name="Seconds", value="seconds"),
    app_commands.Choice(name="Minutes", value="minutes"),
    app_commands.Choice(name="Hours", value="hours")
])
async def poll(interaction: discord.Interaction, question: str, amount: int, unit: app_commands.Choice[str]):
    total_seconds = {"seconds": amount, "minutes": amount * 60, "hours": amount * 3600}.get(unit.value, 0)
    if total_seconds <= 0:
        await interaction.response.send_message("❗ Invalid time unit.", ephemeral=True)
        return
    if total_seconds > 86400:
        await interaction.response.send_message("❗ Duration cannot exceed 24 hours.", ephemeral=True)
        return

    embed = discord.Embed(title="📊 Poll", description=question, color=discord.Color.orange())
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)

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
    result_embed.timestamp = datetime.now(PH_TIMEZONE)

    await message.edit(embed=result_embed)

# Remind Me
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
    await interaction.response.send_message(f"⏰ I'll remind you in `{minutes}` minutes: `{note}`", ephemeral=True)

# Donate
@bot.tree.command(name="donate", description="Donate Robux to a Discord user. (Only for fun!)")
@app_commands.describe(user="The user to donate to.", robux="The amount of Robux to donate.")
async def donate(interaction: discord.Interaction, user: discord.Member, robux: int):
    if robux <= 0:
        await interaction.response.send_message("❗ Robux amount must be greater than zero.", ephemeral=True)
        return
    await interaction.response.send_message(f"`{interaction.user.name}` just donated **{robux:,} Robux** to {user.mention}!")

# Say
@bot.tree.command(name="say", description="Make the bot say something in chat (no @everyone/@here allowed)")
@app_commands.describe(message="Message for the bot to say")
async def say(interaction: discord.Interaction, message: str):
    if "@everyone" in message or "@here" in message:
        await interaction.response.send_message("❌ You cannot use `@everyone` or `@here`.", ephemeral=True)
        return
    await interaction.response.send_message(message)

# Calculator
@bot.tree.command(name="calculator", description="Perform basic math operations (+, -, *, /)")
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
        await interaction.response.send_message(f"Result: `{num1} {symbol} {num2} = {result}`")
    except Exception as e:
        await interaction.response.send_message(f"⚠️ An error occurred: {str(e)}")

# List All Commands
@bot.tree.command(name="listallcommands", description="List all available slash commands")
async def listallcommands(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📚 All Available Commands",
        description="A categorized list of all commands for easy navigation.",
        color=discord.Color.blue()
    )

    # 🤖 AI Assistant
    embed.add_field(
        name="🤖 AI Assistant",
        value="""
        `/ask <prompt>` - Chat with Llama 3 AI  
        `/clearhistory` - Clear your AI conversation history
        """,
        inline=False
    )

    # 💰 Currency Conversion
    embed.add_field(
        name="💰 Currency & Robux Conversion",
        value="""
        `/payout <robux>` - Convert Robux to PHP (Payout rate)  
        `/payoutreverse <php>` - Convert PHP to Robux (Payout rate)  
        `/gift <robux>` - Convert Robux to PHP (Gift rate)  
        `/giftreverse <php>` - Convert PHP to Robux (Gift rate)  
        `/nct <robux>` - Convert Robux to PHP (NCT rate)  
        `/nctreverse <php>` - Convert PHP to Robux (NCT rate)  
        `/ct <robux>` - Convert Robux to PHP (CT rate)  
        `/ctreverse <php>` - Convert PHP to Robux (CT rate)
        """,
        inline=False
    )

    # 📊 Comparison & Tax
    embed.add_field(
        name="📊 Comparison & Tax Calculations",
        value="""
        `/allrates <robux>` - Compare PHP values across all rates  
        `/allratesreverse <php>` - Compare Robux needed across all rates  
        `/beforetax <robux>` - How much you'll receive after tax  
        `/aftertax <target>` - How much to send to get desired amount
        """,
        inline=False
    )

    # 🛠️ Utility Tools
    embed.add_field(
        name="🛠️ Utility Tools",
        value="""
        `/userinfo [user]` - View detailed info about a user  
        `/purge <amount>` - Delete messages (requires mod permissions)  
        `/calculator <num1> <operation> <num2>` - Perform math operations  
        `/group` - Show info about the 1cy Roblox group  
        `/convertcurrency <amount> <from> <to>` - Convert between currencies  
        `/weather <city> [unit]` - Get weather in a city (supports autocomplete)
        """,
        inline=False
    )

    # 🎮 Game Commands
    embed.add_field(
        name="🎮 Mini-Games",
        value="""
        `/game blackjack [@user]` - Play Blackjack  
        `/game tictactoe [@user]` - Play Tic-Tac-Toe  
        `/game hangman [@user]` - Play Hangman  
        `/game stats [@user]` - View game stats between you and another  
        `/gamelb [mode]` - Game leaderboard (supports filtering by game type)  
        `/gamestreak [@user]` - Check win streaks with another player
        """,
        inline=False
    )

    # 🕒 Reminders & Polls
    embed.add_field(
        name="⏰ Reminders & Polls",
        value="""
        `/remindme <minutes> <note>` - Set a personal reminder  
        `/poll <question> <time> <unit>` - Create a timed poll  
        """,
        inline=False
    )

    # 🎁 Fun Commands
    embed.add_field(
        name="🎉 Fun",
        value="""
        `/donate <user> <amount>` - Donate Robux to someone (for fun)  
        `/say <message>` - Make the bot say something (no @everyone/@here)
        """,
        inline=False
    )

    # ⚙️ Developer Tools
    embed.add_field(
        name="🔧 Developer Tools",
        value="""
        `/sync` - Sync slash commands (owner only)  
        `/reload` - Reload cogs (owner only)
        """,
        inline=False
    )

    # Footer
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)

    await interaction.response.send_message(embed=embed)

# ===========================
# Start the Bot
# ===========================
@bot.event
async def on_ready():
    print(f"Bot is ready! Logged in as {bot.user}")
    await bot.tree.sync()
    print("All commands synced!")
    group_id = 5838002
    while True:
        try:
            response = requests.get(f"https://groups.roblox.com/v1/groups/{group_id}")
            data = response.json()
            member_count = "{:,}".format(data['memberCount'])
            await bot.change_presence(status=discord.Status.dnd, activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=f"1cy | {member_count} Members"
            ))
        except Exception as e:
            print(f"Error fetching group info: {str(e)}")
            await bot.change_presence(status=discord.Status.dnd, activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="1cy"
            ))
        await asyncio.sleep(60)

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    content = message.content.lower()
    if content == "hobie":
        await message.reply("mapanghe")
    elif content == "neroniel":
        await message.reply("masarap")
    elif content == "hi":
        reply = (
            "hi tapos ano? magiging friends tayo? lagi tayong mag-uusap mula umaga hanggang madaling araw? "
            "tas magiging close tayo? sa sobrang close natin nahuhulog na tayo sa isa't isa, tapos ano? "
            "liligawan mo ko ako? sasagutin naman kita. paplanuhin natin yung pangarap natin sa isa't isa "
            "tapos ano? may makikita kang iba. magsasawa ka na, iiwan mo ako. tapos ano? magmamakaawa ako sayo "
            "kasi mahal kita pero ano? wala kang gagawin, hahayaan mo lang akong umiiyak while begging you to stay. kaya wag na lang. thanks nalang sa hi mo"
        )
        await message.reply(reply)

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
    await bot.process_commands(message)

# Run the bot
bot.run(os.getenv("DISCORD_TOKEN"))
