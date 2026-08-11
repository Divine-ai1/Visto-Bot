import os
import json
import random
import re
import asyncio
from threading import Thread
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import commands, tasks
from discord import app_commands
from flask import Flask


app = Flask(__name__)


@app.route("/")
def home():
    return "VISTO BEAST IS ALIVE"


def keep_alive():
    port = int(os.environ.get("PORT", 8080)) # Render uses PORT env
    Thread(
        target=lambda: app.run(
            host="0.0.0.0",
            port=port
        )
    ).start()


# ============================================================
# CONFIG
# ============================================================

TOKEN = os.getenv("TOKEN")

PREFIX = "."

# Falcon-style special prefixes for statistics commands only.
STATS_PREFIX = "-"

# Kept for compatibility with the existing config.
# Message tracking now counts messages across the whole server.
MESSAGE_COUNT_CHANNEL_ID = 1536682307530924083

DB_FILE = "visto_data.json"


if not TOKEN:
    raise RuntimeError(
        "TOKEN secret was not found. Make sure your Replit Secret is named TOKEN."
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
    "tickets": {}
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


def save_database():
    with open(DB_FILE, "w") as file:
        json.dump(db, file, indent=4)


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

    await bot.process_commands(
        message
    )


# ============================================================
# INVITE TRACKING
# ============================================================

def get_invite_stats(guild_id, inviter_id):
    guild_invites = get_guild_data("invites", guild_id)
    inviter_id = str(inviter_id)

    current = guild_invites.get(inviter_id)

    # Migrate the old format:
    # "inviter_id": 5
    # into the new Falcon-style structure.
    if isinstance(current, (int, float)):
        current = {
            "joins": int(current),
            "left": 0,
            "rejoins": 0,
            "total": int(current)
        }
        guild_invites[inviter_id] = current

    if not isinstance(current, dict):
        current = {}

    current.setdefault("joins", 0)
    current.setdefault("left", 0)
    current.setdefault("rejoins", 0)
    current.setdefault("total", max(0, current["joins"] - current["left"]))

    # Total is active successful invites:
    # first-time joins minus members who have left.
    current["total"] = max(
        0,
        int(current["joins"]) - int(current["left"])
    )

    guild_invites[inviter_id] = current
    return current


def get_invite_member_data(guild_id, member_id):
    guild_members = get_guild_data("invite_members", guild_id)
    member_id = str(member_id)

    if member_id not in guild_members:
        guild_members[member_id] = {}

    return guild_members[member_id]


async def refresh_invite_cache_after_join(guild, new_invites):
    invite_cache[guild.id] = {
        invite.code: {
            "uses": invite.uses or 0,
            "inviter": invite.inviter.id if invite.inviter else None
        }
        for invite in new_invites
    }

    # Refresh vanity cache too, but never credit it.
    try:
        vanity_invite = await guild.vanity_invite()
        vanity_cache[guild.id] = (
            {
                "code": vanity_invite.code,
                "uses": vanity_invite.uses or 0
            }
            if vanity_invite
            else None
        )
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        pass


@bot.event
async def on_member_join(member):
    guild = member.guild
    member_id = str(member.id)

    try:
        old_invites = invite_cache.get(guild.id, {})
        old_vanity = vanity_cache.get(guild.id)

        new_invites = await guild.invites()

        used_invite = None

        for invite in new_invites:
            old = old_invites.get(invite.code, {})
            old_uses = old.get("uses", 0)
            new_uses = invite.uses or 0

            if new_uses > old_uses:
                used_invite = invite
                break

        # Check vanity separately. Vanity joins NEVER get credited.
        vanity_join = False
        try:
            vanity_invite = await guild.vanity_invite()

            if vanity_invite:
                old_vanity_uses = (
                    old_vanity.get("uses", 0)
                    if old_vanity
                    else 0
                )

                if (vanity_invite.uses or 0) > old_vanity_uses:
                    vanity_join = True

        except (discord.Forbidden, discord.HTTPException, AttributeError):
            pass

        await refresh_invite_cache_after_join(guild, new_invites)

        # If this member has previously been tracked, this is a rejoin.
        member_history = get_invite_member_data(
            guild.id,
            member.id
        )

        inviter_id = member_history.get("inviter_id")

        # A member already known to the invite system is a rejoin candidate.
        # If the bot received the leave event, use the real leave timestamp.
        # If the bot was offline during the leave, fall back to the previous
        # join timestamp so the member cannot silently stack another invite.
        if inviter_id and int(member_history.get("join_count", 0)) > 0:
            reference_timestamp = member_history.get("last_leave")
            fallback_rejoin = False

            if reference_timestamp is None:
                reference_timestamp = member_history.get("last_join")
                fallback_rejoin = reference_timestamp is not None

            if reference_timestamp is not None:
                reference_time = datetime.fromtimestamp(
                    float(reference_timestamp),
                    timezone.utc
                )

                days_since_reference = (
                    datetime.now(timezone.utc) - reference_time
                ).total_seconds() / 86400

                if days_since_reference < 7:
                    stats = get_invite_stats(
                        guild.id,
                        inviter_id
                    )

                    stats["rejoins"] += 1

                    # Rejoining does NOT create another invite.
                    stats["total"] = max(
                        0,
                        stats["joins"] - stats["left"]
                    )

                    member_history["last_join"] = datetime.now(
                        timezone.utc
                    ).timestamp()

                    member_history["join_count"] = (
                        int(member_history.get("join_count", 1)) + 1
                    )

                    # Clear the leave marker so another leave can be tracked.
                    member_history.pop("last_leave", None)

                    save_database()

                    await send_log(
                        guild,
                        "🔁 Member Rejoined",
                        (
                            f"**Member:** {member.mention}\n"
                            f"**Original Inviter:** <@{inviter_id}>\n"
                            f"**Rejoin:** Under 7 Days\n"
                            f"**No new invite credited.**"
                        ),
                        discord.Color.blurple()
                    )

                    return

        # A normal invite join. Vanity and unknown joins are not credited.
        if (
            used_invite
            and used_invite.inviter
            and not vanity_join
        ):
            inviter_id = str(used_invite.inviter.id)
            stats = get_invite_stats(
                guild.id,
                inviter_id
            )

            now = datetime.now(timezone.utc).timestamp()
            previous_inviter = member_history.get("inviter_id")
            previous_leave = member_history.get("last_leave")

            # First-time join, or a rejoin after 7+ days.
            # Rejoining after 7+ days is treated as a fresh successful
            # invite and therefore adds one join/total.
            if not previous_inviter or previous_leave:
                stats["joins"] += 1
                stats["total"] = max(
                    0,
                    stats["joins"] - stats["left"]
                )

                member_history["inviter_id"] = inviter_id
                member_history.setdefault("first_join", now)
                member_history["last_join"] = now
                member_history["join_count"] = (
                    int(member_history.get("join_count", 0)) + 1
                )
                member_history.pop("last_leave", None)

                save_database()

                await send_log(
                    guild,
                    "📩 Member Invited",
                    (
                        f"**Member:** {member.mention}\n"
                        f"**Inviter:** {used_invite.inviter.mention}\n"
                        f"**Invite Code:** `{used_invite.code}`\n"
                        f"**Total Invites:** `{stats['total']}`"
                    ),
                    discord.Color.green()
                )

    except Exception as error:
        print(f"Invite tracking error: {error}")


@bot.event
async def on_member_remove(member):
    guild = member.guild
    member_id = str(member.id)

    try:
        member_history = get_invite_member_data(
            guild.id,
            member.id
        )

        inviter_id = member_history.get("inviter_id")

        if not inviter_id:
            return

        # Count every tracked departure once.
        # The next join can then become a rejoin.
        if not member_history.get("last_leave"):
            stats = get_invite_stats(
                guild.id,
                inviter_id
            )

            stats["left"] += 1
            stats["total"] = max(
                0,
                stats["joins"] - stats["left"]
            )

            member_history["last_leave"] = datetime.now(
                timezone.utc
            ).timestamp()

            save_database()

            await send_log(
                guild,
                "📤 Member Left",
                (
                    f"**Member:** <@{member.id}>\n"
                    f"**Original Inviter:** <@{inviter_id}>\n"
                    f"**Left:** `{stats['left']}`\n"
                    f"**Total Invites:** `{stats['total']}`"
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
            "`/reset`"
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
            "`/ticket close`"
        ),
        inline=True
    )

    embed.add_field(
        name="⚙️ Configuration",
        value=(
            "`/setlog`\n"
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
            f"**Fake:** 0\n"
            f"**Rejoins:** {stats['rejoins']} (7d)"
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

# ============================================================
# TICKET CATEGORY CONFIG
# ============================================================

# PUT YOUR CATEGORY IDs HERE
CLAIM_CATEGORY_ID = 1536695031199567882
SUPPORT_CATEGORY_ID = 1536721072622149632
BUY_CATEGORY_ID = 1536720924039184404


# ============================================================
# TICKET PANEL VIEW
# ============================================================

class TicketCreateView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)

    async def create_ticket(self, interaction, ticket_type, category_id, emoji):

        guild = interaction.guild

        # ----------------------------------------------------
        # Check if user already has an open ticket
        # ----------------------------------------------------

        existing = discord.utils.find(
            lambda c: (
                c.topic
                and f"Ticket Owner: {interaction.user.id}" in c.topic
                and c.topic.startswith("Ticket Type:")
            ),
            guild.text_channels
        )

        if existing:
            return await interaction.response.send_message(
                embed=warning_embed(
                    "Ticket Already Open",
                    f"You already have an open ticket: {existing.mention}"
                ),
                ephemeral=True
            )

        # ----------------------------------------------------
        # Get category
        # ----------------------------------------------------

        category = guild.get_channel(category_id)

        if category is None or not isinstance(
            category,
            discord.CategoryChannel
        ):
            return await interaction.response.send_message(
                embed=error_embed(
                    "Category Not Found",
                    (
                        f"The **{ticket_type}** ticket category is not configured "
                        "correctly.\n\n"
                        "Please contact an administrator."
                    )
                ),
                ephemeral=True
            )

        # ----------------------------------------------------
        # Permissions
        # ----------------------------------------------------

        overwrites = {

            guild.default_role:
                discord.PermissionOverwrite(
                    view_channel=False
                ),

            interaction.user:
                discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                    embed_links=True
                ),

            guild.me:
                discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    manage_channels=True,
                    manage_messages=True
                )
        }

        # Give moderators/staff access through Manage Channels
        # or Administrator automatically.

        for role in guild.roles:

            if role.is_default():
                continue

            if (
                role.permissions.manage_channels
                or role.permissions.administrator
            ):
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    manage_messages=True
                )

        # ----------------------------------------------------
        # Ticket name
        # ----------------------------------------------------

        safe_name = re.sub(
            r"[^a-zA-Z0-9-]",
            "-",
            interaction.user.name.lower()
        )

        channel_name = f"{ticket_type.lower()}-{safe_name}"

        # ----------------------------------------------------
        # Create ticket
        # ----------------------------------------------------

        channel = await guild.create_text_channel(
            name=channel_name[:100],
            category=category,
            topic=(
                f"Ticket Type: {ticket_type} | "
                f"Ticket Owner: {interaction.user.id}"
            ),
            overwrites=overwrites
        )

        # ----------------------------------------------------
        # Ticket embed
        # ----------------------------------------------------

        embed = discord.Embed(
            title=f"{emoji} {ticket_type} Ticket",
            description=(
                f"Welcome {interaction.user.mention}!\n\n"
                f"Your **{ticket_type.lower()}** ticket has been created.\n\n"
                "Please explain what you need help with and "
                "a staff member will assist you shortly."
            ),
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc)
        )

        embed.add_field(
            name="🎫 Ticket Type",
            value=f"{emoji} {ticket_type}",
            inline=True
        )

        embed.add_field(
            name="👤 Ticket Owner",
            value=interaction.user.mention,
            inline=True
        )

        embed.set_footer(
            text="Visto Tickets"
        )

        await channel.send(
            content=interaction.user.mention,
            embed=embed,
            view=TicketCloseView()
        )

        # ----------------------------------------------------
        # Response
        # ----------------------------------------------------

        await interaction.response.send_message(
            embed=success_embed(
                "Ticket Created",
                (
                    f"Your **{ticket_type.lower()}** ticket has been created.\n\n"
                    f"🎫 {channel.mention}"
                )
            ),
            ephemeral=True
        )

        # ----------------------------------------------------
        # Logging
        # ----------------------------------------------------

        await send_log(
            guild,
            "🎫 Ticket Created",
            (
                f"**User:** {interaction.user.mention}\n"
                f"**Type:** {emoji} {ticket_type}\n"
                f"**Channel:** {channel.mention}\n"
                f"**Category:** {category.name}"
            ),
            discord.Color.blurple()
        )

    # ========================================================
    # CLAIM
    # ========================================================

    @discord.ui.button(
        label="Claim",
        emoji="🎟️",
        style=discord.ButtonStyle.primary,
        custom_id="visto_ticket_claim"
    )
    async def claim_ticket(
        self,
        interaction,
        button
    ):

        await self.create_ticket(
            interaction,
            "Claim",
            CLAIM_CATEGORY_ID,
            "🎟️"
        )

    # ========================================================
    # SUPPORT
    # ========================================================

    @discord.ui.button(
        label="Support",
        emoji="🛠️",
        style=discord.ButtonStyle.secondary,
        custom_id="visto_ticket_support"
    )
    async def support_ticket(
        self,
        interaction,
        button
    ):

        await self.create_ticket(
            interaction,
            "Support",
            SUPPORT_CATEGORY_ID,
            "🛠️"
        )

    # ========================================================
    # BUY
    # ========================================================

    @discord.ui.button(
        label="Buy",
        emoji="🛒",
        style=discord.ButtonStyle.success,
        custom_id="visto_ticket_buy"
    )
    async def buy_ticket(
        self,
        interaction,
        button
    ):

        await self.create_ticket(
            interaction,
            "Buy",
            BUY_CATEGORY_ID,
            "🛒"
        )


# ============================================================
# CLOSE TICKET VIEW
# ============================================================

class TicketCloseView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Close Ticket",
        emoji="🔒",
        style=discord.ButtonStyle.danger,
        custom_id="visto_ticket_close"
    )
    async def close_ticket(
        self,
        interaction,
        button
    ):

        channel = interaction.channel

        if not channel.topic or "Ticket Owner:" not in channel.topic:

            return await interaction.response.send_message(
                embed=error_embed(
                    "Not A Ticket",
                    "This channel isn't a Visto ticket."
                ),
                ephemeral=True
            )

        # Only staff/mods can close
        if not (
            interaction.user.guild_permissions.manage_channels
            or interaction.user.guild_permissions.administrator
        ):

            return await interaction.response.send_message(
                embed=error_embed(
                    "No Permission",
                    "Only moderators can close tickets."
                ),
                ephemeral=True
            )

        await interaction.response.send_message(
            embed=warning_embed(
                "Ticket Closing",
                "This ticket will be closed in **5 seconds**."
            )
        )

        await send_log(
            interaction.guild,
            "🎫 Ticket Closed",
            (
                f"**Channel:** {channel.mention}\n"
                f"**Closed by:** {interaction.user.mention}"
            ),
            discord.Color.orange()
        )

        await asyncio.sleep(5)

        try:
            await channel.delete()
        except Exception as error:
            print(f"Ticket deletion error: {error}")


# ============================================================
# /ticket COMMAND GROUP
# ============================================================

ticket_group = app_commands.Group(
    name="ticket",
    description="Visto ticket system"
)


# ============================================================
# /ticket setup
# ============================================================

@ticket_group.command(
    name="setup",
    description="Create the ticket panel"
)
@app_commands.checks.has_permissions(
    manage_channels=True
)
async def ticket_setup(
    interaction
):

    embed = discord.Embed(
        title="🎫 Visto Tickets",
        description=(
            "Need help? Choose the type of ticket you want to create "
            "using the buttons below.\n\n"

            "🎟️ **Claim**\n"
            "Open a ticket regarding a claim.\n\n"

            "🛠️ **Support**\n"
            "Get help from our support team.\n\n"

            "🛒 **Buy**\n"
            "Open a ticket regarding a purchase."
        ),
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )

    embed.set_footer(
        text="Visto • Ticket System"
    )

    await interaction.channel.send(
        embed=embed,
        view=TicketCreateView()
    )

    await interaction.response.send_message(
        embed=success_embed(
            "Ticket Panel Created",
            "The ticket panel has been posted successfully."
        ),
        ephemeral=True
    )


# ============================================================
# /ticket close
# ============================================================

@ticket_group.command(
    name="close",
    description="Close the current ticket"
)
async def ticket_close(
    interaction
):

    channel = interaction.channel

    if not channel.topic or "Ticket Owner:" not in channel.topic:

        return await interaction.response.send_message(
            embed=error_embed(
                "Not A Ticket",
                "This channel isn't a Visto ticket."
            ),
            ephemeral=True
        )

    if not (
        interaction.user.guild_permissions.manage_channels
        or interaction.user.guild_permissions.administrator
    ):

        return await interaction.response.send_message(
            embed=error_embed(
                "No Permission",
                "Only moderators can close tickets."
            ),
            ephemeral=True
        )

    await interaction.response.send_message(
        embed=warning_embed(
            "Ticket Closing",
            "This ticket will be deleted in **5 seconds**."
        )
    )

    await asyncio.sleep(5)

    try:
        await channel.delete()
    except Exception as error:
        print(f"Ticket deletion error: {error}")


# ============================================================
# REGISTER TICKET GROUP
# ============================================================

bot.tree.add_command(
    ticket_group
)



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

    if winners < 1:

        return await interaction.response.send_message(
            embed=error_embed(
                "Invalid Winners",
                "There must be at least 1 winner."
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
# SAY COMMAND
# ============================================================

@bot.command(name="say")
@commands.has_permissions(manage_messages=True)
async def say(ctx, *, message=None):

    if not message:
        return await ctx.send(
            "Usage: `.say <message>`",
            delete_after=5
        )

    try:
        await ctx.message.delete()
    except:
        pass

    await ctx.send(f"{message}")


# ============================================================
# START BOT
# ============================================================

async def main():

    await bot.start(
        TOKEN
    )


if __name__ == "__main__":

    keep_alive()

    Thread(
        target=lambda: asyncio.run(
            main()
        )
    ).start()
