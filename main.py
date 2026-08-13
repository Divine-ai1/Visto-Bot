import os
import json
import random
import re
import asyncio
import html
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import commands, tasks
from discord import app_commands


# ============================================================
# CONFIG
# ============================================================

TOKEN = os.getenv("TOKEN")

PREFIX = "."

# Falcon-style special prefixes for statistics commands only.
STATS_PREFIX = "-"

# Kept for compatibility with the existing config.
# Message tracking now counts messages across the whole server.
MESSAGE_COUNT_CHANNEL_ID = 1536711358433861683

DB_FILE = "visto_data.json"


if not TOKEN:
    raise RuntimeError(
        "TOKEN secret was not found. Make sure your Render Environment Variable is named TOKEN."
    )


# ============================================================
# DATABASE
# ============================================================

DEFAULT_DATABASE = {
    "messages": {},
    "message_daily": {},
    "invites": {},
    "invite_members": {},
    "warnings": {},
    "settings": {},
    "giveaways": {},
    "tickets": {},
    "autoresponders": {}
}


def load_database():
    if not os.path.exists(DB_FILE):
        with open(DB_FILE, "w") as file:
            json.dump(DEFAULT_DATABASE, file, indent=4)

        return DEFAULT_DATABASE.copy()

    try:
        with open(DB_FILE, "r") as file:
            loaded = json.load(file)

        for key in DEFAULT_DATABASE:
            if key not in loaded:
                loaded[key] = {}

        return loaded

    except Exception:
        return DEFAULT_DATABASE.copy()


db = load_database()
DB_LOCK = threading.RLock()


def save_database():
    with DB_LOCK:
        temp_file = DB_FILE + ".tmp"
        with open(temp_file, "w") as file:
            json.dump(db, file, indent=4)
        os.replace(temp_file, DB_FILE)


def get_guild_data(category, guild_id):
    guild_id = str(guild_id)

    if guild_id not in db[category]:
        db[category][guild_id] = {}

    return db[category][guild_id]


# ============================================================
# INTENTS
# ============================================================

intents = discord.Intents.default()

intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True
intents.invites = True


# ============================================================
# BOT
# ============================================================

def get_command_prefix(bot_instance, message):
    # Only the statistics commands use Falcon-style `-i` and `-m`.
    # All other prefix commands continue using the normal `.` prefix.
    content = getattr(message, "content", "") or ""
    stripped = content.lstrip().lower()

    special_commands = ("-i", "-m", "-invited", "-lb")
    if any(
        stripped == command or stripped.startswith(command + " ")
        for command in special_commands
    ):
        return STATS_PREFIX

    return PREFIX


bot = commands.Bot(
    command_prefix=get_command_prefix,
    intents=intents,
    help_command=None
)


# ============================================================
# EMBED HELPERS
# ============================================================

def success_embed(title, description):
    return discord.Embed(
        title=f"✅ {title}",
        description=description,
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc)
    )


def error_embed(title, description):
    return discord.Embed(
        title=f"❌ {title}",
        description=description,
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc)
    )


def info_embed(title, description):
    return discord.Embed(
        title=f"ℹ️ {title}",
        description=description,
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )


def warning_embed(title, description):
    return discord.Embed(
        title=f"⚠️ {title}",
        description=description,
        color=discord.Color.orange(),
        timestamp=datetime.now(timezone.utc)
    )


# ============================================================
# LOGGING
# ============================================================

async def send_log(
    guild,
    title,
    description,
    color=discord.Color.blurple()
):

    if guild is None:
        return

    settings = get_guild_data("settings", guild.id)

    channel_id = settings.get("log_channel")

    if not channel_id:
        return

    channel = guild.get_channel(int(channel_id))

    if channel is None:
        return

    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.now(timezone.utc)
    )

    embed.set_footer(text="Visto Logging")

    try:
        await channel.send(embed=embed)
    except Exception as error:
        print(f"Logging error: {error}")


# ============================================================
# INVITE CACHE
# ============================================================

invite_cache = {}
vanity_cache = {}


async def cache_guild_invites(guild):
    try:
        invites = await guild.invites()

        invite_cache[guild.id] = {
            invite.code: {
                "uses": invite.uses or 0,
                "inviter": invite.inviter.id if invite.inviter else None
            }
            for invite in invites
        }

        # Vanity URL joins are tracked separately and NEVER credited
        # to an inviter.
        vanity_cache[guild.id] = None

        try:
            vanity_invite = await guild.vanity_invite()
            if vanity_invite:
                vanity_cache[guild.id] = {
                    "code": vanity_invite.code,
                    "uses": vanity_invite.uses or 0
                }
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            pass

    except Exception as error:
        print(f"Could not cache invites for {guild.name}: {error}")


async def cache_all_invites():

    for guild in bot.guilds:
        await cache_guild_invites(guild)


# ============================================================
# DURATION PARSER
# ============================================================

def parse_duration(text):

    if not text:
        return None

    text = text.lower().strip()

    pattern = r"(\d+)\s*(mo|y|d|h|m|s)"

    matches = re.findall(pattern, text)

    if not matches:
        return None

    rebuilt = "".join(
        f"{amount}{unit}"
        for amount, unit in matches
    )

    cleaned = re.sub(r"\s+", "", text)

    if rebuilt != cleaned:
        return None

    total_seconds = 0

    for amount, unit in matches:

        amount = int(amount)

        if unit == "s":
            total_seconds += amount

        elif unit == "m":
            total_seconds += amount * 60

        elif unit == "h":
            total_seconds += amount * 60 * 60

        elif unit == "d":
            total_seconds += amount * 24 * 60 * 60

        elif unit == "mo":
            total_seconds += amount * 30 * 24 * 60 * 60

        elif unit == "y":
            total_seconds += amount * 365 * 24 * 60 * 60

    return total_seconds


# ============================================================
# GIVEAWAY STORAGE
# ============================================================

giveaway_tasks = {}


def giveaway_key(message_id):
    return str(message_id)


# ============================================================
# GIVEAWAY VIEW
# ============================================================

class GiveawayView(discord.ui.View):

    def __init__(self, message_id=None, disabled=False):

        super().__init__(
            timeout=None
        )

        self.message_id = message_id

        if disabled:
            self.enter_button.disabled = True

    @discord.ui.button(
        label="Enter Giveaway",
        emoji="🎉",
        style=discord.ButtonStyle.success,
        custom_id="visto_giveaway_enter"
    )
    async def enter_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        message_id = str(
            interaction.message.id
        )

        giveaway = db["giveaways"].get(
            message_id
        )

        if not giveaway:
            return await interaction.response.send_message(
                embed=error_embed(
                    "Giveaway Not Found",
                    "This giveaway no longer exists."
                ),
                ephemeral=True
            )

        if giveaway.get("ended"):
            return await interaction.response.send_message(
                embed=warning_embed(
                    "Giveaway Ended",
                    "This giveaway has already ended."
                ),
                ephemeral=True
            )

        user_id = interaction.user.id

        if user_id in giveaway["entries"]:

            return await interaction.response.send_message(
                embed=warning_embed(
                    "Already Entered",
                    "You are already entered in this giveaway."
                ),
                ephemeral=True
            )

        giveaway["entries"].append(user_id)

        save_database()

        await interaction.response.send_message(
            embed=success_embed(
                "Giveaway Entry",
                "You have successfully entered the giveaway! 🎉"
            ),
            ephemeral=True
        )


# ============================================================
# GIVEAWAY EMBED
# ============================================================

def create_giveaway_embed(giveaway):

    end_time = int(
        giveaway["end_time"]
    )

    entries = len(
        giveaway["entries"]
    )

    embed = discord.Embed(
        title="🎉 GIVEAWAY 🎉",
        description=(
            "Click the **🎉 Enter Giveaway** button below "
            "to participate!"
        ),
        color=discord.Color.gold()
    )

    embed.add_field(
        name="🎁 Prize",
        value=giveaway["prize"],
        inline=False
    )

    embed.add_field(
        name="🏆 Winners",
        value=str(giveaway["winners"]),
        inline=True
    )

    embed.add_field(
        name="👤 Hosted By",
        value=f"<@{giveaway['host_id']}>",
        inline=True
    )

    embed.add_field(
        name="👥 Entries",
        value=f"{entries:,}",
        inline=True
    )

    embed.add_field(
        name="⏰ Ends",
        value=f"<t:{end_time}:R>",
        inline=True
    )

    embed.add_field(
        name="📅 End Time",
        value=f"<t:{end_time}:F>",
        inline=True
    )

    embed.set_footer(
        text="Visto Giveaways"
    )

    return embed


# ============================================================
# GIVEAWAY END FUNCTION
# ============================================================

async def finish_giveaway(
    message_id,
    automatic=False
):

    message_id = str(message_id)

    giveaway = db["giveaways"].get(
        message_id
    )

    if not giveaway:
        return None

    if giveaway.get("ended"):
        return None

    giveaway["ended"] = True

    channel = bot.get_channel(
        int(giveaway["channel_id"])
    )

    winners = []

    entries = list(
        giveaway["entries"]
    )

    if entries:

        winner_count = min(
            giveaway["winners"],
            len(entries)
        )

        winners = random.sample(
            entries,
            winner_count
        )

    if channel:

        try:

            message = await channel.fetch_message(
                int(message_id)
            )

            disabled_view = GiveawayView(
                message_id,
                disabled=True
            )

            embed = create_giveaway_embed(
                giveaway
            )

            embed.title = "🎉 GIVEAWAY ENDED 🎉"
            embed.color = discord.Color.dark_gold()

            embed.set_field_at(
                4,
                name="⏰ Status",
                value="Ended",
                inline=True
            )

            await message.edit(
                embed=embed,
                view=disabled_view
            )

        except Exception as error:
            print(
                f"Could not edit giveaway message: {error}"
            )

        if winners:

            mentions = " ".join(
                f"<@{user_id}>"
                for user_id in winners
            )

            result_embed = discord.Embed(
                title="🎉 GIVEAWAY ENDED",
                description=(
                    f"**Prize:** {giveaway['prize']}\n\n"
                    f"**Winner(s):**\n{mentions}\n\n"
                    "Congratulations! 🎊"
                ),
                color=discord.Color.gold()
            )

        else:

            result_embed = warning_embed(
                "Giveaway Ended",
                "Nobody entered this giveaway."
            )

        try:
            await channel.send(
                embed=result_embed
            )
        except Exception as error:
            print(
                f"Giveaway announcement error: {error}"
            )

    giveaway["winners_selected"] = winners

    save_database()

    guild = bot.get_guild(
        int(giveaway["guild_id"])
    )

    if guild:

        winner_text = (
            " ".join(
                f"<@{uid}>"
                for uid in winners
            )
            if winners
            else "Nobody"
        )

        await send_log(
            guild,
            "🎉 Giveaway Ended",
            (
                f"**Prize:** {giveaway['prize']}\n"
                f"**Winner(s):** {winner_text}\n"
                f"**Message ID:** `{message_id}`"
            ),
            discord.Color.gold()
        )

    return winners


# ============================================================
# GIVEAWAY TASK
# ============================================================

async def giveaway_timer(message_id):

    message_id = str(message_id)

    giveaway = db["giveaways"].get(
        message_id
    )

    if not giveaway:
        return

    while not giveaway.get("ended"):

        current = datetime.now(
            timezone.utc
        ).timestamp()

        remaining = giveaway["end_time"] - current

        if remaining <= 0:
            await finish_giveaway(
                message_id,
                automatic=True
            )
            break

        await asyncio.sleep(
            min(30, max(1, remaining))
        )


def start_giveaway_task(message_id):

    message_id = str(message_id)

    if message_id in giveaway_tasks:
        return

    giveaway_tasks[message_id] = asyncio.create_task(
        giveaway_timer(message_id)
    )


async def restore_giveaways():

    now = datetime.now(
        timezone.utc
    ).timestamp()

    for message_id, giveaway in list(
        db["giveaways"].items()
    ):

        if giveaway.get("ended"):
            continue

        if giveaway["end_time"] <= now:

            await finish_giveaway(
                message_id,
                automatic=True
            )

        else:

            start_giveaway_task(
                message_id
            )


# ============================================================
# READY
# ============================================================

@bot.event
async def on_ready():

    print("=" * 50)
    print(f"Visto connected as {bot.user}")
    print(f"Connected to {len(bot.guilds)} server(s)")
    print("=" * 50)

    try:

        synced = await bot.tree.sync()

        print(
            f"Synced {len(synced)} slash commands."
        )

    except Exception as error:

        print(
            f"Slash sync error: {error}"
        )

    await cache_all_invites()

    await restore_giveaways()

    # Persistent button views survive bot restarts.
    bot.add_view(TicketCreateView())
    bot.add_view(TicketCloseView())
    bot.add_view(ClosedTicketView())
    bot.add_view(GiveawayView())

    start_dashboard()

    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Game(
            name="-i | -m • Visto"
        )
    )


# ============================================================
# MESSAGE COUNT
# ============================================================

@bot.event
async def on_message(message):

    if message.author.bot:
        return

    # Falcon-style message tracking: count every non-bot message in the server.
    if message.guild:
        guild_messages = get_guild_data(
            "messages",
            message.guild.id
        )

        user_id = str(message.author.id)
        guild_messages[user_id] = guild_messages.get(user_id, 0) + 1

        # Keep a separate per-day counter so -m can show
        # both the all-time and today's message totals.
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily = get_guild_data(
            "message_daily",
            message.guild.id
        )
        daily_today = daily.setdefault(today, {})
        daily_today[user_id] = daily_today.get(user_id, 0) + 1

        save_database()

    # Autoresponder runs before prefix commands, but never on bot messages.
    if message.guild:
        responders = get_guild_data("autoresponders", message.guild.id)
        content = message.content.strip().lower()
        response_text = responders.get(content)
        if response_text:
            try:
                await message.channel.send(response_text)
            except discord.HTTPException:
                pass

    await bot.process_commands(message)


# ============================================================
# INVITE TRACKING
# ============================================================

def get_invite_stats(guild_id, inviter_id):
    guild_invites = get_guild_data("invites", guild_id)
    inviter_id = str(inviter_id)
    current = guild_invites.get(inviter_id)

    if isinstance(current, (int, float)):
        current = {
            "joins": int(current),
            "fake": 0,
            "left": 0,
            "rejoins": 0,
            "total": int(current)
        }

    if not isinstance(current, dict):
        current = {}

    current.setdefault("joins", 0)
    current.setdefault("fake", 0)
    current.setdefault("left", 0)
    current.setdefault("rejoins", 0)
    current.setdefault("total", 0)
    current["total"] = max(0, int(current["joins"]) - int(current["left"]))
    guild_invites[inviter_id] = current
    return current


def recompute_inviter_rejoins(guild_id, inviter_id):
    members = get_guild_data("invite_members", guild_id)
    count = 0
    for history in members.values():
        if str(history.get("inviter_id")) == str(inviter_id) and not history.get("currently_left", False) and history.get("has_left", False):
            count += 1
    stats = get_invite_stats(guild_id, inviter_id)
    stats["rejoins"] = count
    return stats


def get_invite_member_data(guild_id, member_id):
    guild_members = get_guild_data("invite_members", guild_id)
    member_id = str(member_id)
    if member_id not in guild_members:
        guild_members[member_id] = {
            "inviter_id": None,
            "join_count": 0,
            "has_left": False,
            "currently_left": False,
            "last_join": None,
            "last_leave": None
        }
    data = guild_members[member_id]
    data.setdefault("inviter_id", None)
    data.setdefault("join_count", 0)
    data.setdefault("has_left", bool(data.get("last_leave")))
    data.setdefault("currently_left", bool(data.get("last_leave")))
    data.setdefault("last_join", None)
    data.setdefault("last_leave", None)
    return data


async def refresh_invite_cache_after_join(guild, new_invites):
    invite_cache[guild.id] = {
        invite.code: {
            "uses": invite.uses or 0,
            "inviter": invite.inviter.id if invite.inviter else None
        }
        for invite in new_invites
    }
    try:
        vanity_invite = await guild.vanity_invite()
        vanity_cache[guild.id] = (
            {"code": vanity_invite.code, "uses": vanity_invite.uses or 0}
            if vanity_invite else None
        )
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        pass


@bot.event
async def on_member_join(member):
    guild = member.guild
    try:
        old_invites = invite_cache.get(guild.id, {})
        old_vanity = vanity_cache.get(guild.id)
        new_invites = await guild.invites()
        used_invite = None

        for invite in new_invites:
            old = old_invites.get(invite.code, {})
            if (invite.uses or 0) > old.get("uses", 0):
                used_invite = invite
                break

        vanity_join = False
        try:
            vanity_invite = await guild.vanity_invite()
            if vanity_invite:
                old_uses = old_vanity.get("uses", 0) if old_vanity else 0
                vanity_join = (vanity_invite.uses or 0) > old_uses
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            pass

        await refresh_invite_cache_after_join(guild, new_invites)

        history = get_invite_member_data(guild.id, member.id)
        inviter_id = history.get("inviter_id")

        # A tracked member who is currently marked as left is a REJOIN.
        # Rejoin is a state (0/1), never a stacking counter.
        if inviter_id and history.get("currently_left"):
            stats = get_invite_stats(guild.id, inviter_id)
            history["currently_left"] = False
            history["last_join"] = datetime.now(timezone.utc).timestamp()
            history["join_count"] = int(history.get("join_count", 1)) + 1
            stats = recompute_inviter_rejoins(guild.id, inviter_id)
            save_database()

            await send_log(
                guild,
                "🔁 Member Rejoined",
                (
                    f"**Member:** {member.mention}\n"
                    f"**Inviter:** <@{inviter_id}>\n\n"
                    f"**Joins:** `{stats['joins']}`\n"
                    f"**Fake:** `{stats['fake']}`\n"
                    f"**Left:** `{stats['left']}`\n"
                    f"**Rejoin:** `1`"
                ),
                discord.Color.blurple()
            )
            return

        # New member / first tracked join. A rejoin is NOT a new join.
        if used_invite and used_invite.inviter and not vanity_join:
            inviter_id = str(used_invite.inviter.id)
            stats = get_invite_stats(guild.id, inviter_id)
            stats["joins"] += 1
            stats["total"] = max(0, stats["joins"] - stats["left"])

            now = datetime.now(timezone.utc).timestamp()
            history["inviter_id"] = inviter_id
            history["join_count"] = int(history.get("join_count", 0)) + 1
            history["last_join"] = now
            history["currently_left"] = False
            history.setdefault("has_left", False)
            stats = recompute_inviter_rejoins(guild.id, inviter_id)
            save_database()

            await send_log(
                guild,
                "📩 Member Joined Through Invite",
                (
                    f"**Member:** {member.mention}\n"
                    f"**Inviter:** {used_invite.inviter.mention}\n"
                    f"**Invite:** `{used_invite.code}`\n\n"
                    f"**Joins:** `{stats['joins']}`\n"
                    f"**Fake:** `{stats['fake']}`\n"
                    f"**Left:** `{stats['left']}`\n"
                    f"**Rejoin:** `0`"
                ),
                discord.Color.green()
            )

    except Exception as error:
        print(f"Invite tracking error: {error}")


@bot.event
async def on_member_remove(member):
    guild = member.guild
    try:
        history = get_invite_member_data(guild.id, member.id)
        inviter_id = history.get("inviter_id")
        if not inviter_id:
            return

        # Count LEFT only once for this member. Every later leave/rejoin
        # cycle toggles Rejoin 0/1 without stacking Left.
        if not history.get("has_left"):
            stats = get_invite_stats(guild.id, inviter_id)
            stats["left"] += 1
            stats["total"] = max(0, stats["joins"] - stats["left"])
            history["has_left"] = True
        else:
            stats = get_invite_stats(guild.id, inviter_id)

        history["currently_left"] = True
        history["last_leave"] = datetime.now(timezone.utc).timestamp()
        stats = recompute_inviter_rejoins(guild.id, inviter_id)
        save_database()

        await send_log(
            guild,
            "📤 Member Left",
            (
                f"**Member:** <@{member.id}>\n"
                f"**Inviter:** <@{inviter_id}>\n\n"
                f"**Joins:** `{stats['joins']}`\n"
                f"**Fake:** `{stats['fake']}`\n"
                f"**Left:** `{stats['left']}`\n"
                f"**Rejoin:** `0`"
            ),
            discord.Color.orange()
        )

    except Exception as error:
        print(f"Invite leave tracking error: {error}")


@bot.event
async def on_invite_create(invite):
    await cache_guild_invites(invite.guild)


@bot.event
async def on_invite_delete(invite):
    await cache_guild_invites(invite.guild)


# ============================================================
# HELP
# ============================================================

def help_embed():

    embed = discord.Embed(
        title="🤖 Visto Help",
        description=(
            "**Visto — All-in-one Discord Bot**\n\n"
            f"Prefix: `{PREFIX}`\n"
            "Slash commands are also supported."
        ),
        color=discord.Color.blurple()
    )

    embed.add_field(
        name="🛡️ Moderation",
        value=(
            "`/ban`\n"
            "`/kick`\n"
            "`/timeout`\n"
            "`/warn`\n"
            "`/warnings`\n"
            "`/purge`\n"
            "`/lock`\n"
            "`/unlock`\n"
            "`/lockdown`\n"
            "`/unlockdown`"
        ),
        inline=True
    )

    embed.add_field(
        name="📊 Statistics",
        value=(
            "`/messages` • `/invites`\n"
            "`-m` • `-i` • `-invited` • `-lb`\n"
            "`/leaderboard messages`\n"
            "`/leaderboard invites`\n"
            "`/add`\n"
            "`/remove`\n"
            "`/reset`\n"
            "`/stats fake`"
        ),
        inline=True
    )

    embed.add_field(
        name="🎉 Giveaways",
        value=(
            "`/giveaway start`\n"
            "`/giveaway end`\n"
            "`/giveaway reroll`"
        ),
        inline=True
    )

    embed.add_field(
        name="🎫 Tickets",
        value=(
            "`/ticket setup`\n"
            "`/ticket close`\n"
            "`/user add`"
        ),
        inline=True
    )

    embed.add_field(
        name="⚙️ Configuration",
        value=(
            "`/setlog`\n"
            "`/autoresponder_add`\n"
            "`/autoresponder_remove`\n"
            "`/autoresponder_list`\n"
            "`/help`"
        ),
        inline=True
    )

    embed.set_footer(
        text="Visto • Professional Discord Bot"
    )

    return embed


@bot.tree.command(
    name="help",
    description="Show Visto commands"
)
async def slash_help(
    interaction: discord.Interaction
):

    await interaction.response.send_message(
        embed=help_embed()
    )


@bot.command(name="help")
async def prefix_help(ctx):

    await ctx.send(
        embed=help_embed()
    )


# ============================================================
# VISTO STATISTICS / FALCON-STYLE UI
# ============================================================

VISTO_STATS_COLOR = discord.Color.red()


def format_stat_number(value):
    return f"{int(value):,}"


# ============================================================
# MESSAGES
# ============================================================


def build_messages_embed(user, count, today_count):
    embed = discord.Embed(
        title=f"{user.display_name}'s Messages",
        description=(
            f"**All time:** {format_stat_number(count)} messages in this server !\n"
            f"**Today:** {format_stat_number(today_count)} messages in this server !\n\n"
            "▶️ Discover new events here!\n\n"
            "Messages are being updated in real-time"
        ),
        color=VISTO_STATS_COLOR
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    return embed


@bot.tree.command(
    name="messages",
    description="Show message statistics"
)
@app_commands.describe(
    user="User to check"
)
async def messages_command(
    interaction: discord.Interaction,
    user: discord.Member = None
):
    user = user or interaction.user
    guild_messages = get_guild_data("messages", interaction.guild.id)
    count = int(guild_messages.get(str(user.id), 0))

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily = get_guild_data("message_daily", interaction.guild.id)
    today_count = int(daily.get(today, {}).get(str(user.id), 0))

    await interaction.response.send_message(
        embed=build_messages_embed(user, count, today_count)
    )


@bot.command(name="m")
async def messages_prefix(ctx, member: discord.Member = None):
    member = member or ctx.author
    guild_messages = get_guild_data("messages", ctx.guild.id)
    count = int(guild_messages.get(str(member.id), 0))

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily = get_guild_data("message_daily", ctx.guild.id)
    today_count = int(daily.get(today, {}).get(str(member.id), 0))

    await ctx.send(embed=build_messages_embed(member, count, today_count))


# ============================================================
# INVITES
# ============================================================


def build_invite_embed(user, stats, requested_by=None):
    embed = discord.Embed(
        title="Invite log",
        description=(
            f"➤ **{user.display_name} has {stats['total']} invites**\n\n"
            f"**Joins:** {stats['joins']}\n"
            f"**Left:** {stats['left']}\n"
            f"**Fake:** {stats.get('fake', 0)}\n"
            f"**Rejoins:** {stats['rejoins']}"
        ),
        color=VISTO_STATS_COLOR
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(
        name="",
        value="▶️ Discover new events here!",
        inline=False
    )
    if requested_by is not None:
        now = datetime.now().strftime("%H:%M")
        embed.set_footer(
            text=f"Requested by {requested_by.display_name} • Today at {now}"
        )
    return embed


@bot.tree.command(
    name="invites",
    description="Show detailed invite statistics"
)
@app_commands.describe(
    user="User to check"
)
async def invites_command(interaction: discord.Interaction, user: discord.Member = None):
    user = user or interaction.user
    stats = get_invite_stats(interaction.guild.id, user.id)
    await interaction.response.send_message(
        embed=build_invite_embed(user, stats, interaction.user)
    )


@bot.command(name="i")
async def invites_prefix(ctx, member: discord.Member = None):
    member = member or ctx.author
    stats = get_invite_stats(ctx.guild.id, member.id)
    await ctx.send(embed=build_invite_embed(member, stats, ctx.author))


class InvitedListView(discord.ui.View):
    def __init__(self, guild, inviter, members):
        super().__init__(timeout=120)
        self.guild = guild
        self.inviter = inviter
        self.members = members
        self.page = 0
        self.per_page = 10
        self.refresh_buttons()

    @property
    def pages(self):
        return max(1, (len(self.members) + self.per_page - 1) // self.per_page)

    def refresh_buttons(self):
        self.first.disabled = self.page <= 0
        self.previous.disabled = self.page <= 0
        self.stop_button.disabled = False
        self.next.disabled = self.page >= self.pages - 1
        self.last.disabled = self.page >= self.pages - 1

    def make_embed(self):
        start = self.page * self.per_page
        chunk = self.members[start:start + self.per_page]

        if chunk:
            lines = [
                f"**#{index}** • {member.mention}"
                for index, member in enumerate(chunk, start=start + 1)
            ]
            description = "\n".join(lines)
        else:
            description = "No currently active members were invited by this user."

        embed = discord.Embed(
            title=f"Invited list of {self.inviter.display_name}",
            description=description,
            color=VISTO_STATS_COLOR
        )
        embed.set_footer(text=f"Page {self.page + 1}/{self.pages}")
        return embed

    async def update(self, interaction):
        self.refresh_buttons()
        await interaction.response.edit_message(
            embed=self.make_embed(),
            view=self
        )

    @discord.ui.button(label="⏮", style=discord.ButtonStyle.secondary)
    async def first(self, interaction, button):
        self.page = 0
        await self.update(interaction)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        self.page = max(0, self.page - 1)
        await self.update(interaction)

    @discord.ui.button(label="⏹", style=discord.ButtonStyle.secondary)
    async def stop_button(self, interaction, button):
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction, button):
        self.page = min(self.pages - 1, self.page + 1)
        await self.update(interaction)

    @discord.ui.button(label="⏭", style=discord.ButtonStyle.secondary)
    async def last(self, interaction, button):
        self.page = self.pages - 1
        await self.update(interaction)


def get_invited_members(guild, inviter_id):
    data = get_guild_data("invite_members", guild.id)
    result = []

    for member_id, history in data.items():
        if str(history.get("inviter_id")) != str(inviter_id):
            continue
        if history.get("last_leave"):
            continue

        member = guild.get_member(int(member_id))
        if member is not None and not member.bot:
            result.append(member)

    result.sort(key=lambda member: member.display_name.lower())
    return result


@bot.command(name="invited")
async def invited_prefix(ctx, member: discord.Member = None):
    member = member or ctx.author
    invited_members = get_invited_members(ctx.guild, member.id)
    view = InvitedListView(ctx.guild, member, invited_members)
    await ctx.send(embed=view.make_embed(), view=view)


# ============================================================
# LEADERBOARDS
# ============================================================


class LeaderboardView(discord.ui.View):
    def __init__(self, ctx, mode):
        super().__init__(timeout=180)
        self.ctx = ctx
        self.guild = ctx.guild
        self.mode = mode
        self.page = 0
        self.per_page = 10
        self.refresh_buttons()

    def get_rows(self):
        if self.mode == "dailymessage":
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            daily = get_guild_data("message_daily", self.guild.id)
            data = daily.get(today, {})
            return sorted(
                [(uid, int(amount)) for uid, amount in data.items() if int(amount) > 0],
                key=lambda item: item[1],
                reverse=True
            )

        if self.mode == "messages":
            data = get_guild_data("messages", self.guild.id)
            return sorted(
                [(uid, int(amount)) for uid, amount in data.items() if int(amount) > 0],
                key=lambda item: item[1],
                reverse=True
            )

        data = get_guild_data("invites", self.guild.id)
        rows = []
        for uid in list(data.keys()):
            stats = get_invite_stats(self.guild.id, uid)
            if stats["total"] > 0:
                rows.append((uid, int(stats["total"])))
        return sorted(rows, key=lambda item: item[1], reverse=True)

    @property
    def rows(self):
        return self.get_rows()

    @property
    def pages(self):
        return max(1, (len(self.rows) + self.per_page - 1) // self.per_page)

    def refresh_buttons(self):
        self.first.disabled = self.page <= 0
        self.previous.disabled = self.page <= 0
        self.stop_button.disabled = False
        self.next.disabled = self.page >= self.pages - 1
        self.last.disabled = self.page >= self.pages - 1

    def make_embed(self):
        rows = self.rows
        start = self.page * self.per_page
        chunk = rows[start:start + self.per_page]

        if self.mode == "dailymessage":
            title = "Daily Messages Leaderboard"
            unit = "messages"
            intro = "The messages are being updated in real-time!"
        elif self.mode == "messages":
            title = "Messages Leaderboard"
            unit = "messages"
            intro = "The messages are being updated in real-time!"
        else:
            title = "Invite Leaderboard"
            unit = "invites"
            intro = "The invites are being updated in real-time!"

        lines = []
        for index, (user_id, amount) in enumerate(chunk, start=start + 1):
            member = self.guild.get_member(int(user_id))
            mention = member.mention if member else f"<@{user_id}>"
            lines.append(f"**#{index}** {mention} • **{amount:,}** {unit}")

        if not lines:
            lines.append("No statistics have been recorded yet.")

        embed = discord.Embed(
            title=title,
            description=intro + "\n\n" + "\n".join(lines),
            color=VISTO_STATS_COLOR
        )
        embed.set_footer(
            text=f"Page {self.page + 1}/{self.pages} | Visto"
        )
        return embed

    async def update(self, interaction):
        self.refresh_buttons()
        await interaction.response.edit_message(
            embed=self.make_embed(),
            view=self
        )

    @discord.ui.button(label="⏮", style=discord.ButtonStyle.secondary)
    async def first(self, interaction, button):
        self.page = 0
        await self.update(interaction)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        self.page = max(0, self.page - 1)
        await self.update(interaction)

    @discord.ui.button(label="⏹", style=discord.ButtonStyle.secondary)
    async def stop_button(self, interaction, button):
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction, button):
        self.page = min(self.pages - 1, self.page + 1)
        await self.update(interaction)

    @discord.ui.button(label="⏭", style=discord.ButtonStyle.secondary)
    async def last(self, interaction, button):
        self.page = self.pages - 1
        await self.update(interaction)


@bot.command(name="lb")
async def leaderboard_prefix_visto(ctx, mode=None):
    mode = (mode or "dailymessage").lower()

    aliases = {
        "daily": "dailymessage",
        "dailymessages": "dailymessage",
        "daily-message": "dailymessage",
        "daily-messages": "dailymessage",
        "dailymessage": "dailymessage",
        "message": "messages",
        "messages": "messages",
        "invite": "invites",
        "invites": "invites",
    }
    mode = aliases.get(mode)

    if mode is None:
        return await ctx.send(
            embed=discord.Embed(
                title="Visto Leaderboard",
                description=(
                    "Use:\n"
                    "`-lb dailymessage`\n"
                    "`-lb messages`\n"
                    "`-lb invites`"
                ),
                color=VISTO_STATS_COLOR
            )
        )

    view = LeaderboardView(ctx, mode)
    await ctx.send(embed=view.make_embed(), view=view)


# Slash leaderboards remain available as part of the existing bot.
async def leaderboard_response(interaction, stat_type):
    guild_data = get_guild_data(stat_type, interaction.guild.id)

    if stat_type == "invites":
        for user_id in list(guild_data.keys()):
            get_invite_stats(interaction.guild.id, user_id)
        sorted_users = sorted(
            guild_data.items(),
            key=lambda item: (
                item[1].get("total", 0) if isinstance(item[1], dict) else item[1]
            ),
            reverse=True
        )[:10]
    else:
        sorted_users = sorted(
            guild_data.items(),
            key=lambda item: item[1],
            reverse=True
        )[:10]

    if not sorted_users:
        return await interaction.response.send_message(
            embed=discord.Embed(
                title="Leaderboard",
                description=f"There are no {stat_type} recorded yet.",
                color=VISTO_STATS_COLOR
            )
        )

    lines = []
    for position, (user_id, amount) in enumerate(sorted_users, start=1):
        member = interaction.guild.get_member(int(user_id))
        name = member.mention if member else f"<@{user_id}>"
        if stat_type == "invites":
            amount = amount.get("total", 0) if isinstance(amount, dict) else amount
        lines.append(f"**#{position}** {name} • **{amount:,}** {stat_type}")

    embed = discord.Embed(
        title=("Daily Messages Leaderboard" if stat_type == "messages" else "Invite Leaderboard"),
        description=(
            "The statistics are being updated in real-time!\n\n" + "\n".join(lines)
        ),
        color=VISTO_STATS_COLOR
    )
    embed.set_footer(text="Visto")
    await interaction.response.send_message(embed=embed)


leaderboard_group = app_commands.Group(
    name="leaderboard",
    description="View Visto leaderboards"
)


@leaderboard_group.command(name="messages", description="Message leaderboard")
async def leaderboard_messages(interaction: discord.Interaction):
    await leaderboard_response(interaction, "messages")


@leaderboard_group.command(name="invites", description="Invite leaderboard")
async def leaderboard_invites(interaction: discord.Interaction):
    await leaderboard_response(interaction, "invites")


bot.tree.add_command(leaderboard_group)


@bot.command(name="leaderboard")
async def leaderboard_prefix(ctx, stat_type=None):
    if stat_type not in ["messages", "invites"]:
        return await ctx.send(
            embed=error_embed(
                "Invalid Leaderboard",
                "Use `.leaderboard messages` or `.leaderboard invites`."
            )
        )

    guild_data = get_guild_data(stat_type, ctx.guild.id)
    if stat_type == "invites":
        for user_id in list(guild_data.keys()):
            get_invite_stats(ctx.guild.id, user_id)
        sorted_users = sorted(
            guild_data.items(),
            key=lambda item: (
                item[1].get("total", 0) if isinstance(item[1], dict) else item[1]
            ),
            reverse=True
        )[:10]
    else:
        sorted_users = sorted(guild_data.items(), key=lambda item: item[1], reverse=True)[:10]

    lines = []
    for position, (user_id, amount) in enumerate(sorted_users, start=1):
        if stat_type == "invites":
            amount = amount.get("total", 0) if isinstance(amount, dict) else amount
        lines.append(f"**#{position}** <@{user_id}> • **{amount:,}** {stat_type}")

    await ctx.send(
        embed=discord.Embed(
            title=f"{stat_type.title()} Leaderboard",
            description="\n".join(lines) if lines else "No data recorded yet.",
            color=VISTO_STATS_COLOR
        )
    )


# ============================================================
# ADD / REMOVE / RESET
# ============================================================

stats_group = app_commands.Group(
    name="stats",
    description="Manage message and invite statistics"
)


@stats_group.command(
    name="add",
    description="Add messages or invites"
)
@app_commands.describe(
    type="messages or invites",
    user="User",
    amount="Amount"
)
@app_commands.choices(
    type=[
        app_commands.Choice(
            name="Messages",
            value="messages"
        ),
        app_commands.Choice(
            name="Invites",
            value="invites"
        )
    ]
)
@app_commands.checks.has_permissions(
    manage_guild=True
)
async def stats_add(
    interaction,
    type: app_commands.Choice[str],
    user: discord.Member,
    amount: int
):

    if amount < 1:

        return await interaction.response.send_message(
            embed=error_embed(
                "Invalid Amount",
                "Amount must be at least 1."
            ),
            ephemeral=True
        )

    category = get_guild_data(
        type.value,
        interaction.guild.id
    )

    uid = str(user.id)

    if type.value == "invites":
        stats = get_invite_stats(
            interaction.guild.id,
            user.id
        )
        stats["joins"] += amount
        stats["total"] = max(
            0,
            stats["joins"] - stats["left"]
        )
    else:
        category[uid] = (
            category.get(uid, 0)
            + amount
        )

    save_database()

    await interaction.response.send_message(
        embed=success_embed(
            "Statistics Updated",
            (
                f"Added **{amount:,} {type.value}** "
                f"to {user.mention}."
            )
        )
    )


@stats_group.command(
    name="remove",
    description="Remove messages or invites"
)
@app_commands.describe(
    type="messages or invites",
    user="User",
    amount="Amount"
)
@app_commands.choices(
    type=[
        app_commands.Choice(
            name="Messages",
            value="messages"
        ),
        app_commands.Choice(
            name="Invites",
            value="invites"
        )
    ]
)
@app_commands.checks.has_permissions(
    manage_guild=True
)
async def stats_remove(
    interaction,
    type: app_commands.Choice[str],
    user: discord.Member,
    amount: int
):

    if amount < 1:

        return await interaction.response.send_message(
            embed=error_embed(
                "Invalid Amount",
                "Amount must be at least 1."
            ),
            ephemeral=True
        )

    category = get_guild_data(
        type.value,
        interaction.guild.id
    )

    uid = str(user.id)

    if type.value == "invites":
        stats = get_invite_stats(
            interaction.guild.id,
            user.id
        )
        stats["joins"] = max(
            0,
            stats["joins"] - amount
        )
        stats["total"] = max(
            0,
            stats["joins"] - stats["left"]
        )
    else:
        category[uid] = max(
            0,
            category.get(uid, 0) - amount
        )

    save_database()

    await interaction.response.send_message(
        embed=success_embed(
            "Statistics Updated",
            (
                f"Removed **{amount:,} {type.value}** "
                f"from {user.mention}."
            )
        )
    )


@stats_group.command(
    name="reset",
    description="Reset messages or invites"
)
@app_commands.describe(
    type="messages or invites",
    user="User"
)
@app_commands.choices(
    type=[
        app_commands.Choice(
            name="Messages",
            value="messages"
        ),
        app_commands.Choice(
            name="Invites",
            value="invites"
        )
    ]
)
@app_commands.checks.has_permissions(
    manage_guild=True
)
async def stats_reset(
    interaction,
    type: app_commands.Choice[str],
    user: discord.Member
):

    category = get_guild_data(
        type.value,
        interaction.guild.id
    )

    if type.value == "invites":
        stats = get_invite_stats(
            interaction.guild.id,
            user.id
        )
        stats["joins"] = 0
        stats["left"] = 0
        stats["rejoins"] = 0
        stats["total"] = 0
    else:
        category[str(user.id)] = 0

    save_database()

    await interaction.response.send_message(
        embed=success_embed(
            "Statistics Reset",
            f"Reset {type.value} for {user.mention}."
        )
    )


@stats_group.command(
    name="fake",
    description="Add or remove fake invite count"
)
@app_commands.describe(
    user="Inviter",
    amount="Amount to add to Fake"
)
@app_commands.checks.has_permissions(manage_guild=True)
async def stats_fake(interaction, user: discord.Member, amount: int):
    if amount < 1:
        return await interaction.response.send_message(embed=error_embed("Invalid Amount", "Amount must be at least 1."), ephemeral=True)
    stats = get_invite_stats(interaction.guild.id, user.id)
    stats["fake"] += amount
    save_database()
    await interaction.response.send_message(embed=success_embed("Fake Invites Updated", f"Added **{amount}** fake invite(s) to {user.mention}."))


bot.tree.add_command(
    stats_group
)


# ============================================================
# PREFIX STATS
# ============================================================

@bot.command(name="add")
@commands.has_guild_permissions(
    manage_guild=True
)
async def prefix_add(
    ctx,
    stat_type=None,
    member: discord.Member = None,
    amount: int = None
):

    if (
        stat_type not in [
            "messages",
            "invites"
        ]
        or member is None
        or amount is None
        or amount < 1
    ):

        return await ctx.send(
            embed=error_embed(
                "Usage",
                "`.add messages @user 100`"
            )
        )

    category = get_guild_data(
        stat_type,
        ctx.guild.id
    )

    uid = str(member.id)

    if stat_type == "invites":
        stats = get_invite_stats(
            ctx.guild.id,
            member.id
        )
        stats["joins"] += amount
        stats["total"] = max(
            0,
            stats["joins"] - stats["left"]
        )
    else:
        category[uid] = (
            category.get(uid, 0)
            + amount
        )

    save_database()

    await ctx.send(
        embed=success_embed(
            "Statistics Updated",
            (
                f"Added **{amount:,} {stat_type}** "
                f"to {member.mention}."
            )
        )
    )


@bot.command(name="remove")
@commands.has_guild_permissions(
    manage_guild=True
)
async def prefix_remove(
    ctx,
    stat_type=None,
    member: discord.Member = None,
    amount: int = None
):

    if (
        stat_type not in [
            "messages",
            "invites"
        ]
        or member is None
        or amount is None
        or amount < 1
    ):

        return await ctx.send(
            embed=error_embed(
                "Usage",
                "`.remove messages @user 100`"
            )
        )

    category = get_guild_data(
        stat_type,
        ctx.guild.id
    )

    uid = str(member.id)

    if stat_type == "invites":
        stats = get_invite_stats(
            ctx.guild.id,
            member.id
        )
        stats["joins"] = max(
            0,
            stats["joins"] - amount
        )
        stats["total"] = max(
            0,
            stats["joins"] - stats["left"]
        )
    else:
        category[uid] = max(
            0,
            category.get(uid, 0) - amount
        )

    save_database()

    await ctx.send(
        embed=success_embed(
            "Statistics Updated",
            (
                f"Removed **{amount:,} {stat_type}** "
                f"from {member.mention}."
            )
        )
    )


@bot.command(name="reset")
@commands.has_guild_permissions(
    manage_guild=True
)
async def prefix_reset(
    ctx,
    stat_type=None,
    member: discord.Member = None
):

    if (
        stat_type not in [
            "messages",
            "invites"
        ]
        or member is None
    ):

        return await ctx.send(
            embed=error_embed(
                "Usage",
                "`.reset messages @user`"
            )
        )

    category = get_guild_data(
        stat_type,
        ctx.guild.id
    )

    if stat_type == "invites":
        stats = get_invite_stats(
            ctx.guild.id,
            member.id
        )
        stats["joins"] = 0
        stats["left"] = 0
        stats["rejoins"] = 0
        stats["total"] = 0
    else:
        category[str(member.id)] = 0

    save_database()

    await ctx.send(
        embed=success_embed(
            "Statistics Reset",
            f"Reset {stat_type} for {member.mention}."
        )
    )


# ============================================================
# SETLOG
# ============================================================

@bot.tree.command(
    name="setlog",
    description="Set the Visto logging channel"
)
@app_commands.describe(
    channel="Logging channel"
)
@app_commands.checks.has_permissions(
    manage_guild=True
)
async def setlog(
    interaction,
    channel: discord.TextChannel
):

    settings = get_guild_data(
        "settings",
        interaction.guild.id
    )

    settings["log_channel"] = channel.id

    save_database()

    await interaction.response.send_message(
        embed=success_embed(
            "Logging Channel Updated",
            (
                f"Visto logs will now be sent to "
                f"{channel.mention}."
            )
        )
    )


# ============================================================
# BAN
# ============================================================

@bot.tree.command(
    name="ban",
    description="Ban a member"
)
@app_commands.describe(
    user="Member",
    reason="Reason"
)
@app_commands.checks.has_permissions(
    ban_members=True
)
async def ban(
    interaction,
    user: discord.Member,
    reason: str = "No reason provided"
):

    try:

        await user.ban(
            reason=reason
        )

        await interaction.response.send_message(
            embed=success_embed(
                "Member Banned",
                (
                    f"**Member:** {user.mention}\n"
                    f"**Reason:** {reason}"
                )
            )
        )

        await send_log(
            interaction.guild,
            "🔨 Member Banned",
            (
                f"**Member:** {user.mention}\n"
                f"**Moderator:** {interaction.user.mention}\n"
                f"**Reason:** {reason}"
            ),
            discord.Color.red()
        )

    except discord.Forbidden:

        await interaction.response.send_message(
            embed=error_embed(
                "Ban Failed",
                "I don't have permission to ban this member."
            ),
            ephemeral=True
        )


# ============================================================
# UNBAN
# ============================================================

@bot.tree.command(
    name="unban",
    description="Unban a user"
)
@app_commands.describe(
    user_id="User ID of the banned user",
    reason="Reason"
)
@app_commands.checks.has_permissions(
    ban_members=True
)
async def unban(
    interaction,
    user_id: str,
    reason: str = "No reason provided"
):
    try:
        target_id = int(user_id)
    except ValueError:
        return await interaction.response.send_message(
            embed=error_embed(
                "Invalid User ID",
                "Please provide a valid Discord user ID."
            ),
            ephemeral=True
        )

    try:
        user = await bot.fetch_user(target_id)

        await interaction.guild.unban(
            user,
            reason=reason
        )

        await interaction.response.send_message(
            embed=success_embed(
                "Member Unbanned",
                (
                    f"**User:** {user.mention}\n"
                    f"**Reason:** {reason}"
                )
            )
        )

        await send_log(
            interaction.guild,
            "🔓 Member Unbanned",
            (
                f"**User:** {user.mention}\n"
                f"**Moderator:** {interaction.user.mention}\n"
                f"**Reason:** {reason}"
            ),
            discord.Color.green()
        )

    except discord.NotFound:
        await interaction.response.send_message(
            embed=error_embed(
                "User Not Banned",
                "That user is not currently banned from this server."
            ),
            ephemeral=True
        )

    except discord.Forbidden:
        await interaction.response.send_message(
            embed=error_embed(
                "Unban Failed",
                "I don't have permission to unban this user."
            ),
            ephemeral=True
        )

    except discord.HTTPException as error:
        await interaction.response.send_message(
            embed=error_embed(
                "Unban Failed",
                f"Discord rejected the unban request: `{error}`"
            ),
            ephemeral=True
        )


# ============================================================
# KICK
# ============================================================

@bot.tree.command(
    name="kick",
    description="Kick a member"
)
@app_commands.describe(
    user="Member",
    reason="Reason"
)
@app_commands.checks.has_permissions(
    kick_members=True
)
async def kick(
    interaction,
    user: discord.Member,
    reason: str = "No reason provided"
):

    try:

        await user.kick(
            reason=reason
        )

        await interaction.response.send_message(
            embed=success_embed(
                "Member Kicked",
                (
                    f"**Member:** {user.mention}\n"
                    f"**Reason:** {reason}"
                )
            )
        )

        await send_log(
            interaction.guild,
            "👢 Member Kicked",
            (
                f"**Member:** {user.mention}\n"
                f"**Moderator:** {interaction.user.mention}\n"
                f"**Reason:** {reason}"
            ),
            discord.Color.orange()
        )

    except discord.Forbidden:

        await interaction.response.send_message(
            embed=error_embed(
                "Kick Failed",
                "I don't have permission to kick this member."
            ),
            ephemeral=True
        )


# ============================================================
# TIMEOUT
# ============================================================

@bot.tree.command(
    name="timeout",
    description="Timeout a member"
)
@app_commands.describe(
    user="Member",
    duration="Duration such as 10m, 2h, 1d",
    reason="Reason"
)
@app_commands.checks.has_permissions(
    moderate_members=True
)
async def timeout(
    interaction,
    user: discord.Member,
    duration: str,
    reason: str = "No reason provided"
):

    seconds = parse_duration(
        duration
    )

    if seconds is None:

        return await interaction.response.send_message(
            embed=error_embed(
                "Invalid Duration",
                (
                    "Use formats like:\n"
                    "`10s` • `5m` • `2h` • `3d` • `2mo` • `1y`"
                )
            ),
            ephemeral=True
        )

    if seconds < 1 or seconds > 28 * 24 * 60 * 60:

        return await interaction.response.send_message(
            embed=error_embed(
                "Invalid Duration",
                "Discord timeouts cannot exceed 28 days."
            ),
            ephemeral=True
        )

    try:

        await user.timeout(
            timedelta(
                seconds=seconds
            ),
            reason=reason
        )

        await interaction.response.send_message(
            embed=success_embed(
                "Member Timed Out",
                (
                    f"**Member:** {user.mention}\n"
                    f"**Duration:** `{duration}`\n"
                    f"**Reason:** {reason}"
                )
            )
        )

        await send_log(
            interaction.guild,
            "⏱️ Member Timed Out",
            (
                f"**Member:** {user.mention}\n"
                f"**Moderator:** {interaction.user.mention}\n"
                f"**Duration:** `{duration}`\n"
                f"**Reason:** {reason}"
            ),
            discord.Color.orange()
        )

    except discord.Forbidden:

        await interaction.response.send_message(
            embed=error_embed(
                "Timeout Failed",
                "I don't have permission to timeout this member."
            ),
            ephemeral=True
        )


# ============================================================
# WARN
# ============================================================

@bot.tree.command(
    name="warn",
    description="Warn a member"
)
@app_commands.describe(
    user="Member",
    reason="Reason"
)
@app_commands.checks.has_permissions(
    moderate_members=True
)
async def warn(
    interaction,
    user: discord.Member,
    reason: str
):

    warnings_data = get_guild_data(
        "warnings",
        interaction.guild.id
    )

    uid = str(user.id)

    if uid not in warnings_data:
        warnings_data[uid] = []

    warnings_data[uid].append({
        "reason": reason,
        "moderator": interaction.user.id,
        "timestamp": int(
            datetime.now(
                timezone.utc
            ).timestamp()
        )
    })

    save_database()

    embed = warning_embed(
        "Member Warned",
        (
            f"**Member:** {user.mention}\n"
            f"**Reason:** {reason}\n"
            f"**Moderator:** {interaction.user.mention}"
        )
    )

    await interaction.response.send_message(
        embed=embed
    )

    dm_embed = discord.Embed(
        title="⚠️ You Have Been Warned",
        description=(
            f"You have received a warning in "
            f"**{interaction.guild.name}**."
        ),
        color=discord.Color.orange(),
        timestamp=datetime.now(timezone.utc)
    )

    dm_embed.add_field(
        name="Reason",
        value=reason,
        inline=False
    )

    dm_embed.add_field(
        name="Moderator",
        value=interaction.user.mention,
        inline=True
    )

    dm_embed.set_footer(
        text="Visto Moderation"
    )

    try:

        await user.send(
            embed=dm_embed
        )

    except discord.Forbidden:

        pass

    await send_log(
        interaction.guild,
        "⚠️ Member Warned",
        (
            f"**Member:** {user.mention}\n"
            f"**Moderator:** {interaction.user.mention}\n"
            f"**Reason:** {reason}"
        ),
        discord.Color.orange()
    )


# ============================================================
# WARNINGS
# ============================================================

@bot.tree.command(
    name="warnings",
    description="View a member's warnings"
)
@app_commands.describe(
    user="Member"
)
@app_commands.checks.has_permissions(
    moderate_members=True
)
async def warnings(
    interaction,
    user: discord.Member
):

    warnings_data = get_guild_data(
        "warnings",
        interaction.guild.id
    )

    warning_list = warnings_data.get(
        str(user.id),
        []
    )

    if not warning_list:

        return await interaction.response.send_message(
            embed=info_embed(
                "Warnings",
                f"{user.mention} has no warnings."
            )
        )

    lines = []

    for number, warning in enumerate(
        warning_list,
        start=1
    ):

        lines.append(
            f"**{number}.** {warning['reason']}"
        )

    embed = warning_embed(
        f"Warnings — {user.display_name}",
        "\n".join(lines)
    )

    await interaction.response.send_message(
        embed=embed
    )


# ============================================================
# PURGE
# ============================================================

@bot.tree.command(
    name="purge",
    description="Delete messages"
)
@app_commands.describe(
    amount="Number of messages"
)
@app_commands.checks.has_permissions(
    manage_messages=True
)
async def purge(
    interaction,
    amount: int
):

    if amount < 1 or amount > 100:

        return await interaction.response.send_message(
            embed=error_embed(
                "Invalid Amount",
                "Choose between 1 and 100 messages."
            ),
            ephemeral=True
        )

    await interaction.response.defer(
        ephemeral=True
    )

    deleted = await interaction.channel.purge(
        limit=amount
    )

    await interaction.followup.send(
        embed=success_embed(
            "Messages Purged",
            f"Deleted **{len(deleted)} messages**."
        ),
        ephemeral=True
    )

    await send_log(
        interaction.guild,
        "🧹 Messages Purged",
        (
            f"**Moderator:** {interaction.user.mention}\n"
            f"**Channel:** {interaction.channel.mention}\n"
            f"**Amount:** {len(deleted)}"
        ),
        discord.Color.orange()
    )


# ============================================================
# LOCK
# ============================================================

@bot.tree.command(
    name="lock",
    description="Lock the current channel"
)
@app_commands.checks.has_permissions(
    manage_channels=True
)
async def lock(
    interaction
):

    overwrite = interaction.channel.overwrites_for(
        interaction.guild.default_role
    )

    overwrite.send_messages = False

    await interaction.channel.set_permissions(
        interaction.guild.default_role,
        overwrite=overwrite
    )

    await interaction.response.send_message(
        embed=success_embed(
            "Channel Locked",
            f"{interaction.channel.mention} has been locked."
        )
    )

    await send_log(
        interaction.guild,
        "🔒 Channel Locked",
        (
            f"**Channel:** {interaction.channel.mention}\n"
            f"**Moderator:** {interaction.user.mention}"
        ),
        discord.Color.orange()
    )


# ============================================================
# UNLOCK
# ============================================================

@bot.tree.command(
    name="unlock",
    description="Unlock the current channel"
)
@app_commands.checks.has_permissions(
    manage_channels=True
)
async def unlock(
    interaction
):

    overwrite = interaction.channel.overwrites_for(
        interaction.guild.default_role
    )

    overwrite.send_messages = None

    await interaction.channel.set_permissions(
        interaction.guild.default_role,
        overwrite=overwrite
    )

    await interaction.response.send_message(
        embed=success_embed(
            "Channel Unlocked",
            f"{interaction.channel.mention} has been unlocked."
        )
    )

    await send_log(
        interaction.guild,
        "🔓 Channel Unlocked",
        (
            f"**Channel:** {interaction.channel.mention}\n"
            f"**Moderator:** {interaction.user.mention}"
        ),
        discord.Color.green()
    )


# ============================================================
# LOCKDOWN
# ============================================================

@bot.tree.command(
    name="lockdown",
    description="Lock every text channel in the server"
)
@app_commands.checks.has_permissions(
    administrator=True
)
async def lockdown(
    interaction
):

    await interaction.response.defer()

    changed = 0

    for channel in interaction.guild.text_channels:

        try:

            overwrite = channel.overwrites_for(
                interaction.guild.default_role
            )

            overwrite.send_messages = False

            await channel.set_permissions(
                interaction.guild.default_role,
                overwrite=overwrite,
                reason=(
                    f"Server lockdown by "
                    f"{interaction.user}"
                )
            )

            changed += 1

        except (
            discord.Forbidden,
            discord.HTTPException
        ):

            continue

    await interaction.followup.send(
        embed=success_embed(
            "Server Lockdown Enabled",
            (
                f"🔒 Locked **{changed} text channels**.\n\n"
                "Members cannot send messages until the "
                "channels are unlocked."
            )
        )
    )

    await send_log(
        interaction.guild,
        "🚨 Server Lockdown Enabled",
        (
            f"**Moderator:** {interaction.user.mention}\n"
            f"**Channels locked:** {changed}"
        ),
        discord.Color.red()
    )


# ============================================================
# UNLOCKDOWN
# ============================================================

@bot.tree.command(
    name="unlockdown",
    description="Unlock every text channel in the server"
)
@app_commands.checks.has_permissions(
    administrator=True
)
async def unlockdown(
    interaction
):

    await interaction.response.defer()

    changed = 0

    for channel in interaction.guild.text_channels:

        try:

            overwrite = channel.overwrites_for(
                interaction.guild.default_role
            )

            overwrite.send_messages = None

            await channel.set_permissions(
                interaction.guild.default_role,
                overwrite=overwrite,
                reason=(
                    f"Server lockdown removed by "
                    f"{interaction.user}"
                )
            )

            changed += 1

        except (
            discord.Forbidden,
            discord.HTTPException
        ):

            continue

    await interaction.followup.send(
        embed=success_embed(
            "Server Lockdown Disabled",
            (
                f"🔓 Unlocked **{changed} text channels**."
            )
        )
    )

    await send_log(
        interaction.guild,
        "🔓 Server Lockdown Disabled",
        (
            f"**Moderator:** {interaction.user.mention}\n"
            f"**Channels unlocked:** {changed}"
        ),
        discord.Color.green()
    )


# ============================================================
# TICKET SYSTEM
# ============================================================

def ticket_settings(guild):
    settings = get_guild_data("settings", guild.id)
    settings.setdefault("ticket_categories", {})
    settings["ticket_categories"].setdefault("buy", None)
    settings["ticket_categories"].setdefault("claim", None)
    settings["ticket_categories"].setdefault("support", None)
    settings.setdefault("ticket_staff_role", None)
    return settings


def is_ticket_channel(channel):
    return bool(channel and channel.topic and "Ticket Owner:" in channel.topic)


def ticket_owner_id(channel):
    if not is_ticket_channel(channel):
        return None
    match = re.search(r"Ticket Owner:\s*(\d+)", channel.topic or "")
    return int(match.group(1)) if match else None


def is_ticket_staff(interaction):
    if interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator:
        return True
    settings = ticket_settings(interaction.guild)
    role_id = settings.get("ticket_staff_role")
    return bool(role_id and any(role.id == int(role_id) for role in interaction.user.roles))


class TicketCreateView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def create_ticket(self, interaction, ticket_type, emoji):
        guild = interaction.guild
        settings = ticket_settings(guild)
        category_id = settings["ticket_categories"].get(ticket_type.lower())

        existing = discord.utils.find(
            lambda c: is_ticket_channel(c) and ticket_owner_id(c) == interaction.user.id and not c.name.startswith("closed-"),
            guild.text_channels
        )
        if existing:
            return await interaction.response.send_message(
                embed=warning_embed("Ticket Already Open", f"You already have {existing.mention}."),
                ephemeral=True
            )

        category = guild.get_channel(int(category_id)) if category_id else None
        if not isinstance(category, discord.CategoryChannel):
            return await interaction.response.send_message(
                embed=error_embed(
                    "Ticket Category Not Set",
                    f"The **{ticket_type}** ticket category is not configured. An admin can set it from the dashboard."
                ),
                ephemeral=True
            )

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True, manage_messages=True)
        }

        settings_role_id = settings.get("ticket_staff_role")
        if settings_role_id:
            role = guild.get_role(int(settings_role_id))
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)
        else:
            for role in guild.roles:
                if not role.is_default() and (role.permissions.manage_channels or role.permissions.administrator):
                    overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)

        safe_name = re.sub(r"[^a-zA-Z0-9-]", "-", interaction.user.name.lower()).strip("-") or "user"
        channel = await guild.create_text_channel(
            name=f"{ticket_type.lower()}-{safe_name}"[:100],
            category=category,
            topic=f"Ticket Type: {ticket_type} | Ticket Owner: {interaction.user.id} | Status: Open",
            overwrites=overwrites
        )

        embed = discord.Embed(
            title=f"{emoji} {ticket_type} Ticket",
            description=(
                f"Welcome {interaction.user.mention}!\n\n"
                f"Your **{ticket_type.lower()}** ticket has been created.\n"
                "Please explain what you need and a staff member will assist you.\n\n"
                "When you're finished, use **Close Ticket** below."
            ),
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Ticket Owner", value=interaction.user.mention, inline=True)
        embed.add_field(name="Ticket Type", value=f"{emoji} {ticket_type}", inline=True)
        embed.set_footer(text="Visto Tickets")

        await channel.send(
            content=interaction.user.mention,
            embed=embed,
            view=TicketCloseView()
        )

        await interaction.response.send_message(
            embed=success_embed("Ticket Created", f"Your ticket is {channel.mention}."),
            ephemeral=True
        )

        await send_log(
            guild,
            "🎫 Ticket Created",
            f"**User:** {interaction.user.mention}\n**Type:** {emoji} {ticket_type}\n**Channel:** {channel.mention}\n**Category:** {category.mention}",
            discord.Color.blurple()
        )

    @discord.ui.button(label="Buy", emoji="🛒", style=discord.ButtonStyle.success, custom_id="visto_ticket_buy")
    async def buy(self, interaction, button):
        await self.create_ticket(interaction, "Buy", "🛒")

    @discord.ui.button(label="Claim", emoji="🎁", style=discord.ButtonStyle.primary, custom_id="visto_ticket_claim")
    async def claim(self, interaction, button):
        await self.create_ticket(interaction, "Claim", "🎁")

    @discord.ui.button(label="Support", emoji="🛠️", style=discord.ButtonStyle.secondary, custom_id="visto_ticket_support")
    async def support(self, interaction, button):
        await self.create_ticket(interaction, "Support", "🛠️")


class TicketConfirmCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="Confirm Close", emoji="✅", style=discord.ButtonStyle.danger, custom_id="visto_ticket_confirm_close")
    async def confirm(self, interaction, button):
        if not is_ticket_staff(interaction):
            return await interaction.response.send_message(embed=error_embed("No Permission", "Only ticket staff can close tickets."), ephemeral=True)

        channel = interaction.channel
        owner_id = ticket_owner_id(channel)
        settings = ticket_settings(interaction.guild)
        closed_role_id = settings.get("ticket_staff_role")

        await interaction.response.defer()

        overwrite = channel.overwrites_for(interaction.guild.default_role)
        overwrite.view_channel = False
        overwrite.send_messages = False
        await channel.set_permissions(interaction.guild.default_role, overwrite=overwrite)

        if owner_id:
            owner = interaction.guild.get_member(owner_id)
            if owner:
                owner_overwrite = channel.overwrites_for(owner)
                owner_overwrite.send_messages = False
                owner_overwrite.view_channel = True
                await channel.set_permissions(owner, overwrite=owner_overwrite)

        channel.topic = (channel.topic or "") + f" | Status: Closed | Closed By: {interaction.user.id}"
        new_name = channel.name if channel.name.startswith("closed-") else f"closed-{channel.name}"
        try:
            await channel.edit(name=new_name[:100], topic=channel.topic)
        except discord.HTTPException:
            pass

        embed = warning_embed(
            "Ticket Closed",
            f"This ticket has been closed by {interaction.user.mention}.\n\nUse the button below to delete it permanently."
        )
        await channel.send(embed=embed, view=ClosedTicketView())

        await interaction.followup.send(embed=success_embed("Ticket Closed", "The ticket has been locked and marked as closed."), ephemeral=True)

        await send_log(
            interaction.guild,
            "🔒 Ticket Closed",
            f"**Channel:** {channel.mention}\n**Closed by:** {interaction.user.mention}",
            discord.Color.orange()
        )

    @discord.ui.button(label="Cancel", emoji="❌", style=discord.ButtonStyle.secondary, custom_id="visto_ticket_cancel_close")
    async def cancel(self, interaction, button):
        await interaction.response.edit_message(
            embed=info_embed("Close Cancelled", "The ticket will remain open."),
            view=None
        )


class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="visto_ticket_close")
    async def close(self, interaction, button):
        if not is_ticket_channel(interaction.channel):
            return await interaction.response.send_message(embed=error_embed("Not A Ticket", "This channel isn't a Visto ticket."), ephemeral=True)
        if not is_ticket_staff(interaction):
            return await interaction.response.send_message(embed=error_embed("No Permission", "Only ticket staff can close tickets."), ephemeral=True)
        await interaction.response.send_message(
            embed=warning_embed("Confirm Ticket Close", "Are you sure you want to close this ticket? This will lock it but will not delete it."),
            view=TicketConfirmCloseView(),
            ephemeral=True
        )


class ClosedTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Delete Ticket", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="visto_ticket_delete")
    async def delete(self, interaction, button):
        if not is_ticket_staff(interaction):
            return await interaction.response.send_message(embed=error_embed("No Permission", "Only ticket staff can delete tickets."), ephemeral=True)
        await interaction.response.send_message(embed=warning_embed("Deleting Ticket", "Deleting this ticket in **5 seconds**."))
        await send_log(interaction.guild, "🗑️ Ticket Deleted", f"**Channel:** {interaction.channel.name}\n**Deleted by:** {interaction.user.mention}", discord.Color.red())
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete()
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Transcript", emoji="📜", style=discord.ButtonStyle.secondary, custom_id="visto_ticket_transcript")
    async def transcript(self, interaction, button):
        if not is_ticket_staff(interaction):
            return await interaction.response.send_message(embed=error_embed("No Permission", "Only ticket staff can create transcripts."), ephemeral=True)
        messages = []
        async for msg in interaction.channel.history(limit=500, oldest_first=True):
            messages.append(f"[{msg.created_at:%Y-%m-%d %H:%M:%S}] {msg.author}: {msg.content}")
        transcript = "\n".join(messages) or "No messages."
        filename = f"transcript-{interaction.channel.id}.txt"
        with open(filename, "w", encoding="utf-8") as file:
            file.write(transcript)
        await interaction.response.send_message(file=discord.File(filename), ephemeral=True)
        try:
            os.remove(filename)
        except OSError:
            pass


class UserAddModal(discord.ui.Modal, title="Add User To Ticket"):
    user_id = discord.ui.TextInput(label="User ID", placeholder="Enter the Discord user ID", required=True, max_length=30)

    async def on_submit(self, interaction):
        if not is_ticket_staff(interaction):
            return await interaction.response.send_message(embed=error_embed("No Permission", "Only ticket staff can add users."), ephemeral=True)
        try:
            user = await interaction.guild.fetch_member(int(self.user_id.value.strip()))
        except (ValueError, discord.NotFound, discord.HTTPException):
            return await interaction.response.send_message(embed=error_embed("User Not Found", "Enter a valid member ID from this server."), ephemeral=True)
        await interaction.channel.set_permissions(user, view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)
        await interaction.response.send_message(embed=success_embed("User Added", f"{user.mention} can now access this ticket."))


user_group = app_commands.Group(name="user", description="Manage users in the current ticket")

@user_group.command(name="add", description="Add a user to the current ticket")
@app_commands.describe(user="Member to add to this ticket")
async def user_add(interaction, user: discord.Member):
    if not is_ticket_channel(interaction.channel):
        return await interaction.response.send_message(embed=error_embed("Not A Ticket", "Use this command inside a ticket."), ephemeral=True)
    if not is_ticket_staff(interaction):
        return await interaction.response.send_message(embed=error_embed("No Permission", "Only ticket staff can add users."), ephemeral=True)
    await interaction.channel.set_permissions(user, view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)
    await interaction.response.send_message(embed=success_embed("User Added", f"{user.mention} has been added to {interaction.channel.mention}."))

bot.tree.add_command(user_group)


ticket_group = app_commands.Group(name="ticket", description="Visto ticket system")

@ticket_group.command(name="setup", description="Create the ticket panel")
@app_commands.checks.has_permissions(manage_channels=True)
async def ticket_setup(interaction):
    embed = discord.Embed(
        title="🎫 Visto Tickets",
        description=(
            "Choose the type of ticket you want to open.\n\n"
            "🛒 **Buy** — Purchase or order help.\n"
            "🎁 **Claim** — Claim/reward help.\n"
            "🛠️ **Support** — General support.\n\n"
            "Each ticket type is created in its configured category."
        ),
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text="Visto Tickets")
    await interaction.channel.send(embed=embed, view=TicketCreateView())
    await interaction.response.send_message(embed=success_embed("Ticket Panel Created", "The ticket panel has been posted."), ephemeral=True)

@ticket_group.command(name="close", description="Close the current ticket")
async def ticket_close(interaction):
    if not is_ticket_channel(interaction.channel):
        return await interaction.response.send_message(embed=error_embed("Not A Ticket", "This channel isn't a Visto ticket."), ephemeral=True)
    if not is_ticket_staff(interaction):
        return await interaction.response.send_message(embed=error_embed("No Permission", "Only ticket staff can close tickets."), ephemeral=True)
    await interaction.response.send_message(embed=warning_embed("Confirm Ticket Close", "Are you sure you want to close this ticket?"), view=TicketConfirmCloseView(), ephemeral=True)

bot.tree.add_command(ticket_group)


# ============================================================
# GIVEAWAY COMMAND GROUP
# ============================================================

giveaway_group = app_commands.Group(
    name="giveaway",
    description="Visto giveaway system"
)


@giveaway_group.command(
    name="start",
    description="Start a giveaway"
)
@app_commands.describe(
    prize="Giveaway prize",
    duration="Examples: 10s, 5m, 2h, 3d, 2mo, 1y",
    winners="Number of winners"
)
@app_commands.checks.has_permissions(
    manage_guild=True
)
async def giveaway_start(
    interaction,
    prize: str,
    duration: str,
    winners: int
):

    if winners < 1 or winners > 25:

        return await interaction.response.send_message(
            embed=error_embed(
                "Invalid Winners",
                "Winners must be between 1 and 25."
            ),
            ephemeral=True
        )

    seconds = parse_duration(
        duration
    )

    if seconds is None:

        return await interaction.response.send_message(
            embed=error_embed(
                "Invalid Duration",
                (
                    "Use formats like:\n"
                    "`10s` • `5m` • `2h` • `3d` • `2mo` • `1y`\n\n"
                    "You can combine them:\n"
                    "`1d 5h 30m`"
                )
            ),
            ephemeral=True
        )

    if seconds < 10:

        return await interaction.response.send_message(
            embed=error_embed(
                "Duration Too Short",
                "Giveaways must last at least 10 seconds."
            ),
            ephemeral=True
        )

    end_time = int(
        datetime.now(
            timezone.utc
        ).timestamp()
    ) + seconds

    giveaway_data = {
        "guild_id": interaction.guild.id,
        "channel_id": interaction.channel.id,
        "prize": prize,
        "winners": winners,
        "host_id": interaction.user.id,
        "end_time": end_time,
        "entries": [],
        "ended": False,
        "winners_selected": []
    }

    await interaction.response.send_message(
        embed=info_embed(
            "Creating Giveaway",
            "Please wait..."
        ),
        ephemeral=True
    )

    message = await interaction.channel.send(
        embed=create_giveaway_embed(
            giveaway_data
        ),
        view=GiveawayView()
    )

    db["giveaways"][
        str(message.id)
    ] = giveaway_data

    save_database()

    start_giveaway_task(
        message.id
    )

    await interaction.edit_original_response(
        embed=success_embed(
            "Giveaway Started",
            (
                f"Your giveaway has been created.\n\n"
                f"**Message ID:** `{message.id}`"
            )
        )
    )

    await send_log(
        interaction.guild,
        "🎉 Giveaway Started",
        (
            f"**Prize:** {prize}\n"
            f"**Winners:** {winners}\n"
            f"**Duration:** `{duration}`\n"
            f"**Host:** {interaction.user.mention}\n"
            f"**Message ID:** `{message.id}`"
        ),
        discord.Color.gold()
    )


@giveaway_group.command(
    name="end",
    description="End a giveaway"
)
@app_commands.describe(
    message_id="Giveaway message ID"
)
@app_commands.checks.has_permissions(
    manage_guild=True
)
async def giveaway_end(
    interaction,
    message_id: str
):

    giveaway = db["giveaways"].get(
        message_id
    )

    if not giveaway:

        return await interaction.response.send_message(
            embed=error_embed(
                "Giveaway Not Found",
                "I couldn't find a giveaway with that message ID."
            ),
            ephemeral=True
        )

    if giveaway.get("ended"):

        return await interaction.response.send_message(
            embed=warning_embed(
                "Already Ended",
                "This giveaway has already ended."
            ),
            ephemeral=True
        )

    await interaction.response.defer()

    winners = await finish_giveaway(
        message_id
    )

    if winners is None:

        return await interaction.followup.send(
            embed=error_embed(
                "Unable To End",
                "The giveaway could not be ended."
            )
        )

    await interaction.followup.send(
        embed=success_embed(
            "Giveaway Ended",
            f"Giveaway `{message_id}` has been ended."
        )
    )


@giveaway_group.command(
    name="reroll",
    description="Reroll a giveaway"
)
@app_commands.describe(
    message_id="Ended giveaway message ID"
)
@app_commands.checks.has_permissions(
    manage_guild=True
)
async def giveaway_reroll(
    interaction,
    message_id: str
):

    giveaway = db["giveaways"].get(
        message_id
    )

    if not giveaway:

        return await interaction.response.send_message(
            embed=error_embed(
                "Giveaway Not Found",
                "I couldn't find that giveaway."
            ),
            ephemeral=True
        )

    if not giveaway.get("ended"):

        return await interaction.response.send_message(
            embed=warning_embed(
                "Giveaway Still Active",
                "You can only reroll an ended giveaway."
            ),
            ephemeral=True
        )

    entries = list(
        giveaway.get("entries", [])
    )

    if not entries:

        return await interaction.response.send_message(
            embed=error_embed(
                "No Entries",
                "There are no entries to reroll."
            ),
            ephemeral=True
        )

    previous_winners = set(
        giveaway.get(
            "winners_selected",
            []
        )
    )

    available = [
        user_id
        for user_id in entries
        if user_id not in previous_winners
    ]

    if not available:
        available = entries

    winner_count = min(
        giveaway["winners"],
        len(available)
    )

    winners = random.sample(
        available,
        winner_count
    )

    giveaway["winners_selected"] = winners
    giveaway["rerolled"] = True

    save_database()

    mentions = " ".join(
        f"<@{user_id}>"
        for user_id in winners
    )

    embed = discord.Embed(
        title="🎉 GIVEAWAY REROLLED",
        description=(
            f"**Prize:** {giveaway['prize']}\n\n"
            f"**New Winner(s):**\n{mentions}\n\n"
            "Congratulations! 🎊"
        ),
        color=discord.Color.gold()
    )

    await interaction.response.send_message(
        embed=embed
    )

    guild = interaction.guild

    await send_log(
        guild,
        "🔄 Giveaway Rerolled",
        (
            f"**Prize:** {giveaway['prize']}\n"
            f"**New Winner(s):** {mentions}\n"
            f"**Moderator:** {interaction.user.mention}"
        ),
        discord.Color.gold()
    )


bot.tree.add_command(
    giveaway_group
)


# ============================================================
# SAY COMMAND
# ============================================================

@bot.command(name="say")
@commands.has_permissions(manage_messages=True)
async def say(ctx, *, message=None):
    if not message:
        return await ctx.send(embed=error_embed("Usage", "Use `.say <message>`."), delete_after=5)
    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass
    await ctx.send(f"Bot: {message}")


# ============================================================
# AUTORESPONDER
# ============================================================

@bot.tree.command(name="autoresponder_add", description="Add an autoresponder")
@app_commands.describe(trigger="Message trigger", response="Bot response")
@app_commands.checks.has_permissions(manage_guild=True)
async def autoresponder_add(interaction, trigger: str, response: str):
    trigger = trigger.strip().lower()
    if not trigger or len(trigger) > 100:
        return await interaction.response.send_message(embed=error_embed("Invalid Trigger", "Trigger must be 1-100 characters."), ephemeral=True)
    responders = get_guild_data("autoresponders", interaction.guild.id)
    responders[trigger] = response
    save_database()
    await interaction.response.send_message(embed=success_embed("Autoresponder Added", f"`{trigger}` will now respond automatically."))

@bot.tree.command(name="autoresponder_remove", description="Remove an autoresponder")
@app_commands.describe(trigger="Trigger to remove")
@app_commands.checks.has_permissions(manage_guild=True)
async def autoresponder_remove(interaction, trigger: str):
    trigger = trigger.strip().lower()
    responders = get_guild_data("autoresponders", interaction.guild.id)
    if trigger not in responders:
        return await interaction.response.send_message(embed=error_embed("Not Found", f"No autoresponder exists for `{trigger}`."), ephemeral=True)
    del responders[trigger]
    save_database()
    await interaction.response.send_message(embed=success_embed("Autoresponder Removed", f"Removed `{trigger}`."))

@bot.tree.command(name="autoresponder_list", description="List autoresponders")
@app_commands.checks.has_permissions(manage_guild=True)
async def autoresponder_list(interaction):
    responders = get_guild_data("autoresponders", interaction.guild.id)
    if not responders:
        return await interaction.response.send_message(embed=info_embed("Autoresponders", "No autoresponders are configured."), ephemeral=True)
    lines = [f"**{i}.** `{trigger}` → {discord.utils.escape_markdown(response)[:150]}" for i, (trigger, response) in enumerate(responders.items(), 1)]
    await interaction.response.send_message(embed=info_embed("Autoresponders", "\n".join(lines[:25])))


# ============================================================
# ERROR HANDLING
# ============================================================


@bot.event
async def on_command_error(
    ctx,
    error
):

    if isinstance(
        error,
        commands.CommandNotFound
    ):
        return

    if isinstance(
        error,
        commands.MissingPermissions
    ):

        return await ctx.send(
            embed=error_embed(
                "Permission Denied",
                "You don't have permission to use this command."
            )
        )

    if isinstance(
        error,
        commands.MissingRequiredArgument
    ):

        return await ctx.send(
            embed=error_embed(
                "Missing Argument",
                "You're missing a required argument."
            )
        )

    print(
        f"Prefix command error: {error}"
    )


@bot.tree.error
async def on_app_command_error(
    interaction,
    error
):

    if isinstance(
        error,
        app_commands.errors.MissingPermissions
    ):

        embed = error_embed(
            "Permission Denied",
            "You don't have permission to use this command."
        )

        if interaction.response.is_done():

            await interaction.followup.send(
                embed=embed,
                ephemeral=True
            )

        else:

            await interaction.response.send_message(
                embed=embed,
                ephemeral=True
            )

        return

    print(
        f"Slash command error: {error}"
    )


# ============================================================
# SIMPLE BOT DASHBOARD
# ============================================================

DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD") or secrets.token_urlsafe(18)
DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = int(os.getenv("PORT", "10000"))
_dashboard_server = None


def dashboard_page(title, body):
    return f"""<!doctype html>
<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{html.escape(title)}</title>
<style>body{{font-family:Arial,sans-serif;background:#111827;color:#f9fafb;margin:0;padding:24px}} .wrap{{max-width:1100px;margin:auto}} .card{{background:#1f2937;border:1px solid #374151;border-radius:14px;padding:18px;margin:14px 0}} input,select{{width:100%;box-sizing:border-box;padding:10px;margin:6px 0 12px;border-radius:8px;border:1px solid #4b5563;background:#111827;color:#fff}} button{{padding:10px 16px;border:0;border-radius:8px;background:#5865f2;color:white;cursor:pointer}} h1,h2{{margin-top:0}} .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}} .muted{{color:#9ca3af}}</style></head>
<body><div class='wrap'>{body}</div></body></html>"""


def dashboard_stats():
    guild_count = len(bot.guilds)
    tickets = sum(len(data) for data in db.get("tickets", {}).values() if isinstance(data, dict))
    giveaways = sum(len(data) for data in db.get("giveaways", {}).values() if isinstance(data, dict))
    responders = sum(len(data) for data in db.get("autoresponders", {}).values() if isinstance(data, dict))
    return guild_count, tickets, giveaways, responders


class DashboardHandler(BaseHTTPRequestHandler):
    def _auth_ok(self):
        query = parse_qs(urlparse(self.path).query)
        return query.get("key", [None])[0] == DASHBOARD_PASSWORD

    def _send(self, status, body, content_type="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if urlparse(self.path).path == "/health":
            return self._send(200, "OK", "text/plain")
        if not self._auth_ok():
            return self._send(401, "Unauthorized. Add ?key=YOUR_DASHBOARD_PASSWORD")

        guild_count, tickets, giveaways, responders = dashboard_stats()
        cards = f"""
<h1>𖦹 D ! V ! N Σ 𖦹 Dashboard</h1>
<p class='muted'>Bot configuration dashboard</p>
<div class='grid'>
<div class='card'><h2>Servers</h2><b>{guild_count}</b></div>
<div class='card'><h2>Tickets</h2><b>{tickets}</b></div>
<div class='card'><h2>Giveaways</h2><b>{giveaways}</b></div>
<div class='card'><h2>Autoresponders</h2><b>{responders}</b></div>
</div>
<div class='card'><h2>Server Settings</h2>
<form method='POST' action='/settings?key={html.escape(DASHBOARD_PASSWORD)}'>
<label>Guild ID</label><input name='guild_id' required>
<label>Log Channel ID</label><input name='log_channel'>
<label>Buy Category ID</label><input name='buy_category'>
<label>Claim Category ID</label><input name='claim_category'>
<label>Support Category ID</label><input name='support_category'>
<label>Ticket Staff Role ID</label><input name='staff_role'>
<button>Save Settings</button>
</form></div>
<div class='card'><h2>Autoresponder</h2>
<form method='POST' action='/autoresponder?key={html.escape(DASHBOARD_PASSWORD)}'>
<label>Guild ID</label><input name='guild_id' required>
<label>Trigger</label><input name='trigger' required>
<label>Response</label><input name='response' required>
<button>Save Autoresponder</button>
</form></div>
<div class='card'><h2>Invite Tracking</h2><p class='muted'>The bot tracks one permanent record per invited member. Rejoin is a 0/1 state and does not stack. Device/IP identity is not available through Discord's bot API.</p></div>
"""
        self._send(200, dashboard_page("Visto Dashboard", cards))

    def do_POST(self):
        if not self._auth_ok():
            return self._send(401, "Unauthorized")
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        data = {k: v[0] for k, v in parse_qs(raw).items() if v}
        path = urlparse(self.path).path

        try:
            guild_id = int(data.get("guild_id", "0"))
        except ValueError:
            return self._send(400, "Invalid guild ID")

        if path == "/settings":
            settings = get_guild_data("settings", guild_id)
            for key in ("log_channel", "buy_category", "claim_category", "support_category", "staff_role"):
                value = data.get(key, "").strip()
                if value:
                    try:
                        settings_key = {
                            "log_channel": "log_channel",
                            "buy_category": "ticket_buy_category",
                            "claim_category": "ticket_claim_category",
                            "support_category": "ticket_support_category",
                            "staff_role": "ticket_staff_role"
                        }[key]
                        settings[settings_key] = int(value)
                    except ValueError:
                        return self._send(400, f"Invalid {key}")
            settings.setdefault("ticket_categories", {})
            if data.get("buy_category", "").strip(): settings["ticket_categories"]["buy"] = int(data["buy_category"])
            if data.get("claim_category", "").strip(): settings["ticket_categories"]["claim"] = int(data["claim_category"])
            if data.get("support_category", "").strip(): settings["ticket_categories"]["support"] = int(data["support_category"])
            save_database()
            return self._send(200, dashboard_page("Saved", "<h1>Saved</h1><p>Settings updated.</p><a href='/'>Back</a>"))

        if path == "/autoresponder":
            trigger = data.get("trigger", "").strip().lower()
            response = data.get("response", "").strip()
            if not trigger or not response:
                return self._send(400, "Trigger and response are required")
            responders = get_guild_data("autoresponders", guild_id)
            responders[trigger] = response
            save_database()
            return self._send(200, dashboard_page("Saved", "<h1>Saved</h1><p>Autoresponder updated.</p><a href='/'>Back</a>"))

        return self._send(404, "Not found")

    def log_message(self, format, *args):
        return


def start_dashboard():
    global _dashboard_server
    if _dashboard_server is not None:
        return
    try:
        _dashboard_server = ThreadingHTTPServer((DASHBOARD_HOST, DASHBOARD_PORT), DashboardHandler)
        thread = threading.Thread(target=_dashboard_server.serve_forever, daemon=True)
        thread.start()
        print(f"Dashboard running on port {DASHBOARD_PORT}")
        if not os.getenv("DASHBOARD_PASSWORD"):
            print(f"DASHBOARD_PASSWORD was not set. Temporary dashboard key: {DASHBOARD_PASSWORD}")
    except Exception as error:
        print(f"Dashboard failed to start: {error}")


# ============================================================
# START BOT
# ============================================================


async def main():
    start_dashboard()
    await bot.start(TOKEN)


if __name__ == "__main__":

    asyncio.run(
        main()
    )
