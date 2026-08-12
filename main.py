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
from discord.ext import commands
from discord import app_commands

# ============================================================
# CONFIG — EDIT THESE IN THIS FILE
# ============================================================

# Keep the Discord token in Render's secret/environment.
TOKEN = os.getenv("TOKEN")

PREFIX = "."
STATS_PREFIX = "-"

# MESSAGE COUNTING: ONLY this ONE channel counts.
# Put your #general channel ID here. This is FILE-ONLY; dashboard settings are ignored.
GENERAL_CHANNEL_ID = 1536711358433861683

# Put your server ID here for instant slash-command updates.
# Set to 0 if you want global-only sync (global commands can take time to appear).
GUILD_ID = 0

# DASHBOARD: edit the password HERE, not in an environment variable.
DASHBOARD_PASSWORD = "VistoBot67"
DASHBOARD_HOST = "0.0.0.0"
# Render exposes the web service port. The app still needs Render's PORT.
DASHBOARD_PORT = int(os.getenv("PORT", "10000"))

DB_FILE = "visto_data.json"

# Optional ticket configuration. The dashboard can also update these values.
BUY_CATEGORY_ID = 1536720924039184404
CLAIM_CATEGORY_ID = 1536695031199567882
SUPPORT_CATEGORY_ID = 1536721072622149632
TICKET_STAFF_ROLE_ID = 1536708388635803658

if not TOKEN:
    raise RuntimeError(
        "TOKEN was not found. Add TOKEN in Render Environment Variables."
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
    "autoresponders": {},
}

DB_LOCK = threading.RLock()


def load_database():
    if not os.path.exists(DB_FILE):
        with open(DB_FILE, "w", encoding="utf-8") as file:
            json.dump(DEFAULT_DATABASE, file, indent=4)
        return {k: {} for k in DEFAULT_DATABASE}

    try:
        with open(DB_FILE, "r", encoding="utf-8") as file:
            loaded = json.load(file)
        for key in DEFAULT_DATABASE:
            loaded.setdefault(key, {})
        return loaded
    except Exception as error:
        print(f"Database load error: {error}")
        return {k: {} for k in DEFAULT_DATABASE}


db = load_database()


def save_database():
    with DB_LOCK:
        temporary = DB_FILE + ".tmp"
        with open(temporary, "w", encoding="utf-8") as file:
            json.dump(db, file, indent=4)
        os.replace(temporary, DB_FILE)


def get_guild_data(category, guild_id):
    guild_id = str(guild_id)
    db.setdefault(category, {})
    db[category].setdefault(guild_id, {})
    return db[category][guild_id]


# ============================================================
# DISCORD CLIENT
# ============================================================

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True
intents.invites = True


def get_command_prefix(bot_instance, message):
    content = (getattr(message, "content", "") or "").lstrip().lower()
    specials = ("-i", "-m", "-invited", "-lb")
    for command in specials:
        if content == command or content.startswith(command + " "):
            return STATS_PREFIX
    return PREFIX


bot = commands.Bot(
    command_prefix=get_command_prefix,
    intents=intents,
    help_command=None,
)


# ============================================================
# EMBEDS / UTILS
# ============================================================

VISTO_COLOR = discord.Color.red()


def success_embed(title, description):
    return discord.Embed(
        title=f"✅ {title}",
        description=description,
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )


def error_embed(title, description):
    return discord.Embed(
        title=f"❌ {title}",
        description=description,
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )


def info_embed(title, description):
    return discord.Embed(
        title=f"ℹ️ {title}",
        description=description,
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )


def warning_embed(title, description):
    return discord.Embed(
        title=f"⚠️ {title}",
        description=description,
        color=discord.Color.orange(),
        timestamp=datetime.now(timezone.utc),
    )


def duration_parser(text):
    if not text:
        return None
    text = text.lower().strip()
    matches = re.findall(r"(\d+)\s*(mo|y|d|h|m|s)", text)
    if not matches:
        return None
    rebuilt = "".join(f"{a}{u}" for a, u in matches)
    if rebuilt != re.sub(r"\s+", "", text):
        return None
    total = 0
    for amount, unit in matches:
        n = int(amount)
        total += {
            "s": n,
            "m": n * 60,
            "h": n * 3600,
            "d": n * 86400,
            "mo": n * 30 * 86400,
            "y": n * 365 * 86400,
        }[unit]
    return total


async def safe_dm(user, embed):
    try:
        await user.send(embed=embed)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


async def send_log(guild, title, description, color=discord.Color.blurple()):
    if guild is None:
        return
    settings = get_guild_data("settings", guild.id)
    channel_id = settings.get("log_channel")
    if not channel_id:
        return
    channel = guild.get_channel(int(channel_id))
    if not channel:
        return
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="Visto Logging")
    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        pass


async def ask_reason(interaction, title, prompt):
    class ReasonModal(discord.ui.Modal, title=title):
        reason = discord.ui.TextInput(
            label="Reason",
            placeholder=prompt,
            required=True,
            max_length=1000,
            style=discord.TextStyle.paragraph,
        )

        async def on_submit(self, modal_interaction):
            await modal_interaction.response.defer(ephemeral=True)
            self.result = str(self.reason.value)
            self.done.set()

    modal = ReasonModal()
    modal.done = asyncio.Event()
    modal.result = None
    await interaction.response.send_modal(modal)
    await modal.done.wait()
    return modal.result


# ============================================================
# HELP
# ============================================================

def help_embed():
    embed = discord.Embed(
        title="🤖 Visto",
        description=f"All-in-one Discord bot\nPrefix: `{PREFIX}`",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="🛡️ Moderation",
        value="`/ban` ` /unban` `/kick` `/timeout` `/warn` `/warnings` `/delwarn` `/purge` `/lock` `/unlock` `/lockdown` `/unlockdown`",
        inline=False,
    )
    embed.add_field(
        name="📊 Stats",
        value="`/messages` `-m` `.m` `-i` `.i` `-lb invites` `-lb messages` `.resetall messages` `.resetall invites`",
        inline=False,
    )
    embed.add_field(
        name="🎫 Tickets",
        value="`/ticket setup` `/ticket close` `/user add`",
        inline=False,
    )
    embed.add_field(
        name="🎉 Giveaways",
        value="`/giveaway start` `/giveaway end` `/giveaway reroll` `/giveaway pause` `/giveaway resume` `/giveaway delete`",
        inline=False,
    )
    embed.add_field(
        name="🤖 Utility",
        value="`.say` `/autoresponder add` `/autoresponder remove` `/autoresponder list`",
        inline=False,
    )
    return embed


@bot.tree.command(name="help", description="Show bot commands")
async def slash_help(interaction):
    await interaction.response.send_message(embed=help_embed())


@bot.command(name="help")
async def prefix_help(ctx):
    await ctx.send(embed=help_embed())


# ============================================================
# MESSAGE SYSTEM — GENERAL CHANNEL ONLY
# ============================================================

def get_message_count_channel(guild):
    # FILE-ONLY CONFIGURATION:
    # Only the channel ID in GENERAL_CHANNEL_ID is counted.
    # Dashboard settings are intentionally ignored for message counting.
    if not GENERAL_CHANNEL_ID or GENERAL_CHANNEL_ID == 123456789012345678:
        return None

    return guild.get_channel(int(GENERAL_CHANNEL_ID))


def get_message_count(guild_id, user_id):
    return int(get_guild_data("messages", guild_id).get(str(user_id), 0))


def get_daily_message_count(guild_id, user_id):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return int(
        get_guild_data("message_daily", guild_id)
        .get(today, {})
        .get(str(user_id), 0)
    )


def build_messages_embed(user, count, today_count, channel):
    embed = discord.Embed(
        title=f"📊 {user.display_name}'s Messages",
        description=(
            f"**All Time:** `{count:,}` messages\n"
            f"**Daily:** `{today_count:,}` messages\n\n"
            f"📍 Counting Channel: {channel.mention if channel else 'Not configured'}"
        ),
        color=VISTO_COLOR,
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text="Visto Message Statistics")
    return embed


class MessageLeaderboardView(discord.ui.View):
    def __init__(self, ctx):
        super().__init__(timeout=180)
        self.guild = ctx.guild
        self.page = 0
        self.per_page = 10
        self.refresh_buttons()

    def rows(self):
        data = get_guild_data("messages", self.guild.id)
        return sorted(
            [(uid, int(v)) for uid, v in data.items() if int(v) > 0],
            key=lambda x: x[1],
            reverse=True,
        )

    def pages(self):
        rows = self.rows()
        return max(1, (len(rows) + self.per_page - 1) // self.per_page)

    def refresh_buttons(self):
        pages = self.pages()
        self.first.disabled = self.page <= 0
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= pages - 1
        self.last.disabled = self.page >= pages - 1

    def make_embed(self):
        rows = self.rows()
        pages = self.pages()
        chunk = rows[self.page * self.per_page : (self.page + 1) * self.per_page]
        lines = []
        for idx, (uid, amount) in enumerate(chunk, start=self.page * self.per_page + 1):
            member = self.guild.get_member(int(uid))
            mention = member.mention if member else f"<@{uid}>"
            lines.append(f"**#{idx}** {mention} • `{amount:,}` messages")
        if not lines:
            lines = ["No message statistics recorded yet."]
        channel = get_message_count_channel(self.guild)
        embed = discord.Embed(
            title="📊 Message Leaderboard",
            description=(
                f"Counting only {channel.mention if channel else 'the configured General channel'}.\n\n"
                + "\n".join(lines)
            ),
            color=VISTO_COLOR,
        )
        embed.set_footer(text=f"Page {self.page + 1}/{pages} • Visto")
        return embed

    async def update(self, interaction):
        self.refresh_buttons()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

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
        self.page = min(self.pages() - 1, self.page + 1)
        await self.update(interaction)

    @discord.ui.button(label="⏭", style=discord.ButtonStyle.secondary)
    async def last(self, interaction, button):
        self.page = self.pages() - 1
        await self.update(interaction)


@bot.tree.command(name="messages", description="Show message statistics")
@app_commands.describe(user="User to check")
async def messages_command(interaction, user: discord.Member = None):
    user = user or interaction.user
    channel = get_message_count_channel(interaction.guild)
    await interaction.response.send_message(
        embed=build_messages_embed(
            user,
            get_message_count(interaction.guild.id, user.id),
            get_daily_message_count(interaction.guild.id, user.id),
            channel,
        )
    )


@bot.command(name="m")
async def messages_prefix(ctx, member: discord.Member = None):
    member = member or ctx.author
    channel = get_message_count_channel(ctx.guild)
    await ctx.send(
        embed=build_messages_embed(
            member,
            get_message_count(ctx.guild.id, member.id),
            get_daily_message_count(ctx.guild.id, member.id),
            channel,
        )
    )


@bot.command(name="messageleaderboard", aliases=["mlb"])
async def message_leaderboard(ctx):
    view = MessageLeaderboardView(ctx)
    await ctx.send(embed=view.make_embed(), view=view)


class ResetAllView(discord.ui.View):
    def __init__(self, reset_type, guild_id):
        super().__init__(timeout=30)
        self.reset_type = reset_type
        self.guild_id = guild_id

    @discord.ui.button(label="Confirm Reset", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                embed=error_embed("No Permission", "Only administrators can do this."),
                ephemeral=True,
            )
        if self.reset_type == "messages":
            db["messages"][str(self.guild_id)] = {}
            db["message_daily"][str(self.guild_id)] = {}
            message = "All-time and daily message statistics for **everyone** have been reset."
        else:
            db["invites"][str(self.guild_id)] = {}
            db["invite_members"][str(self.guild_id)] = {}
            message = "Invite statistics for **everyone** have been reset."
        save_database()
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(embed=success_embed("Reset Complete", message), view=self)
        await send_log(
            interaction.guild,
            "🗑️ Statistics Reset",
            f"**Type:** {self.reset_type}\n**By:** {interaction.user.mention}\n**Scope:** Everyone",
            discord.Color.red(),
        )

    @discord.ui.button(label="Cancel", emoji="❌", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(embed=info_embed("Cancelled", "Nothing was reset."), view=self)


@bot.command(name="resetall")
@commands.has_permissions(administrator=True)
async def resetall(ctx, reset_type=None):
    reset_type = (reset_type or "").lower()
    if reset_type not in ("messages", "invites"):
        return await ctx.send(
            embed=info_embed(
                "Reset All",
                "Use `.resetall messages` or `.resetall invites`.\n\nThis affects **everyone** in the server.",
            )
        )
    await ctx.send(
        embed=warning_embed(
            f"Reset ALL {reset_type.title()}?",
            f"This permanently resets {reset_type} statistics for **everyone** in this server.\n\nThis cannot be undone.",
        ),
        view=ResetAllView(reset_type, ctx.guild.id),
    )


# ============================================================
# MESSAGE EVENT — COUNT ONLY THE CONFIGURED GENERAL CHANNEL
# ============================================================

@bot.event
async def on_message(message):
    # Always ignore bot messages, but still let commands from users through.
    if message.author.bot:
        return

    if message.guild is not None:
        general_channel = get_message_count_channel(message.guild)

        # ONLY count messages sent in the configured General channel.
        if general_channel is not None and message.channel.id == general_channel.id:
            guild_messages = get_guild_data("messages", message.guild.id)
            user_id = str(message.author.id)
            guild_messages[user_id] = int(guild_messages.get(user_id, 0)) + 1

            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            daily = get_guild_data("message_daily", message.guild.id)
            today_data = daily.setdefault(today, {})
            today_data[user_id] = int(today_data.get(user_id, 0)) + 1

            save_database()

    # Preserve all prefix commands and autoresponders.
    responders = get_guild_data("autoresponders", message.guild.id) if message.guild else {}
    content = message.content.strip().lower()
    if message.guild and content in responders:
        try:
            await message.channel.send(responders[content])
        except discord.HTTPException:
            pass

    await bot.process_commands(message)


@bot.tree.command(name="resetall", description="Reset statistics for everyone in this server")
@app_commands.describe(type="What to reset")
@app_commands.choices(type=[
    app_commands.Choice(name="Messages", value="messages"),
    app_commands.Choice(name="Invites", value="invites"),
])
@app_commands.checks.has_permissions(administrator=True)
async def resetall_slash(interaction, type: app_commands.Choice[str]):
    await interaction.response.send_message(
        embed=warning_embed(
            f"Reset ALL {type.name}?",
            f"This will permanently reset **{type.name.lower()}** statistics for **everyone** in this server.\n\nConfirm below."
        ),
        view=ResetAllView(type.value, interaction.guild.id),
        ephemeral=True,
    )


# ============================================================
# INVITE SYSTEM — NO STACKING REJOINS
# ============================================================

invite_cache = {}
vanity_cache = {}


def get_invite_stats(guild_id, inviter_id):
    data = get_guild_data("invites", guild_id)
    uid = str(inviter_id)
    current = data.get(uid)
    if isinstance(current, (int, float)):
        current = {"joins": int(current), "fake": 0, "left": 0, "rejoins": 0, "total": int(current)}
    if not isinstance(current, dict):
        current = {}
    current.setdefault("joins", 0)
    current.setdefault("fake", 0)
    current.setdefault("left", 0)
    current.setdefault("rejoins", 0)
    current.setdefault("total", 0)
    current["total"] = max(0, int(current["joins"]) - int(current["left"]))
    data[uid] = current
    return current


def get_invite_member_data(guild_id, member_id):
    data = get_guild_data("invite_members", guild_id)
    uid = str(member_id)
    data.setdefault(uid, {})
    h = data[uid]
    h.setdefault("inviter_id", None)
    h.setdefault("has_left", False)
    h.setdefault("currently_left", False)
    h.setdefault("last_join", None)
    h.setdefault("last_leave", None)
    return h


def recompute_rejoins(guild_id, inviter_id):
    stats = get_invite_stats(guild_id, inviter_id)
    count = 0
    for history in get_guild_data("invite_members", guild_id).values():
        if str(history.get("inviter_id")) == str(inviter_id):
            if history.get("has_left") and not history.get("currently_left"):
                count += 1
    stats["rejoins"] = count
    return stats


async def cache_guild_invites(guild):
    try:
        invites = await guild.invites()
        invite_cache[guild.id] = {
            x.code: {"uses": x.uses or 0, "inviter": x.inviter.id if x.inviter else None}
            for x in invites
        }
        vanity_cache[guild.id] = None
        try:
            vanity = await guild.vanity_invite()
            if vanity:
                vanity_cache[guild.id] = {"code": vanity.code, "uses": vanity.uses or 0}
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            pass
    except Exception as error:
        print(f"Invite cache error: {error}")


async def cache_all_invites():
    for guild in bot.guilds:
        await cache_guild_invites(guild)


@bot.event
async def on_member_join(member):
    guild = member.guild
    try:
        old_invites = invite_cache.get(guild.id, {})
        new_invites = await guild.invites()
        used_invite = None
        for invite in new_invites:
            if (invite.uses or 0) > old_invites.get(invite.code, {}).get("uses", 0):
                used_invite = invite
                break

        vanity_join = False
        old_vanity = vanity_cache.get(guild.id)
        try:
            vanity = await guild.vanity_invite()
            if vanity:
                vanity_join = (vanity.uses or 0) > (old_vanity.get("uses", 0) if old_vanity else 0)
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            pass

        await cache_guild_invites(guild)

        history = get_invite_member_data(guild.id, member.id)
        inviter_id = history.get("inviter_id")

        # Existing member who left before = REJOIN.
        # It NEVER increases joins and NEVER makes rejoin 2/3/4.
        if inviter_id and history.get("currently_left"):
            history["currently_left"] = False
            history["last_join"] = datetime.now(timezone.utc).timestamp()
            stats = recompute_rejoins(guild.id, inviter_id)
            save_database()
            await send_log(
                guild,
                "🔁 Member Rejoined",
                f"**Member:** {member.mention}\n**Inviter:** <@{inviter_id}>\n\n**Joins:** `{stats['joins']}`\n**Fake:** `{stats['fake']}`\n**Left:** `{stats['left']}`\n**Rejoin:** `1`",
                discord.Color.blurple(),
            )
            return

        if used_invite and used_invite.inviter and not vanity_join:
            inviter_id = str(used_invite.inviter.id)
            stats = get_invite_stats(guild.id, inviter_id)
            stats["joins"] += 1
            stats["total"] = max(0, stats["joins"] - stats["left"])
            history["inviter_id"] = inviter_id
            history["last_join"] = datetime.now(timezone.utc).timestamp()
            history["currently_left"] = False
            history.setdefault("has_left", False)
            recompute_rejoins(guild.id, inviter_id)
            save_database()
            await send_log(
                guild,
                "📩 Member Joined Through Invite",
                f"**Member:** {member.mention}\n**Inviter:** {used_invite.inviter.mention}\n\n**Joins:** `{stats['joins']}`\n**Fake:** `{stats['fake']}`\n**Left:** `{stats['left']}`\n**Rejoin:** `0`",
                discord.Color.green(),
            )
    except Exception as error:
        print(f"Invite join error: {error}")


@bot.event
async def on_member_remove(member):
    guild = member.guild
    try:
        history = get_invite_member_data(guild.id, member.id)
        inviter_id = history.get("inviter_id")
        if not inviter_id:
            return

        stats = get_invite_stats(guild.id, inviter_id)
        # Each invited member contributes max ONE permanent left.
        if not history.get("has_left"):
            stats["left"] += 1
            history["has_left"] = True
        history["currently_left"] = True
        history["last_leave"] = datetime.now(timezone.utc).timestamp()
        recompute_rejoins(guild.id, inviter_id)
        stats["total"] = max(0, stats["joins"] - stats["left"])
        save_database()
        await send_log(
            guild,
            "📤 Member Left",
            f"**Member:** <@{member.id}>\n**Inviter:** <@{inviter_id}>\n\n**Joins:** `{stats['joins']}`\n**Fake:** `{stats['fake']}`\n**Left:** `{stats['left']}`\n**Rejoin:** `0`",
            discord.Color.orange(),
        )
    except Exception as error:
        print(f"Invite leave error: {error}")


@bot.event
async def on_invite_create(invite):
    await cache_guild_invites(invite.guild)


@bot.event
async def on_invite_delete(invite):
    await cache_guild_invites(invite.guild)


def build_invite_embed(user, stats):
    embed = discord.Embed(
        title="📨 Invite Log",
        description=(
            f"**{user.display_name}** has **{stats['total']}** active invites\n\n"
            f"**Joins:** `{stats['joins']}`\n"
            f"**Fake:** `{stats.get('fake', 0)}`\n"
            f"**Left:** `{stats['left']}`\n"
            f"**Rejoins:** `{stats['rejoins']}`"
        ),
        color=VISTO_COLOR,
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    return embed


@bot.tree.command(name="invites", description="Show invite statistics")
@app_commands.describe(user="User to check")
async def invites_command(interaction, user: discord.Member = None):
    user = user or interaction.user
    await interaction.response.send_message(embed=build_invite_embed(user, get_invite_stats(interaction.guild.id, user.id)))


@bot.command(name="i")
async def invites_prefix(ctx, member: discord.Member = None):
    member = member or ctx.author
    await ctx.send(embed=build_invite_embed(member, get_invite_stats(ctx.guild.id, member.id)))
    # ============================================================
# INVITED COMMANDS
# ============================================================

def get_invited_members(guild, inviter_id):
    data = get_guild_data(
        "invite_members",
        guild.id
    )

    members = []

    for member_id, history in data.items():

        if str(history.get("inviter_id")) != str(inviter_id):
            continue

        # Don't show members who have currently left
        if history.get("currently_left", False):
            continue

        member = guild.get_member(
            int(member_id)
        )

        if member and not member.bot:
            members.append(member)

    members.sort(
        key=lambda member: member.display_name.lower()
    )

    return members


# ============================================================
# -invited
# ============================================================

@bot.command(name="invited")
async def invited_prefix(ctx, member: discord.Member = None):

    member = member or ctx.author

    invited_members = get_invited_members(
        ctx.guild,
        member.id
    )

    if not invited_members:
        return await ctx.send(
            embed=info_embed(
                "Invited Members",
                f"{member.mention} has not invited any currently active members."
            )
        )

    lines = []

    for number, invited in enumerate(
        invited_members,
        start=1
    ):
        lines.append(
            f"**#{number}** {invited.mention}"
        )

    embed = discord.Embed(
        title=f"📨 Members Invited by {member.display_name}",
        description="\n".join(lines[:50]),
        color=VISTO_COLOR
    )

    embed.set_footer(
        text=f"Total: {len(invited_members)} • Visto Bot"
    )

    await ctx.send(
        embed=embed
    )


# ============================================================
# /invited
# ============================================================

@bot.tree.command(
    name="invited",
    description="Show the members invited by a user"
)
@app_commands.describe(
    user="User whose invited members you want to see"
)
async def invited_slash(
    interaction: discord.Interaction,
    user: discord.Member = None
):

    user = user or interaction.user

    invited_members = get_invited_members(
        interaction.guild,
        user.id
    )

    if not invited_members:
        return await interaction.response.send_message(
            embed=info_embed(
                "Invited Members",
                f"{user.mention} has not invited any currently active members."
            )
        )

    lines = []

    for number, invited in enumerate(
        invited_members,
        start=1
    ):
        lines.append(
            f"**#{number}** {invited.mention}"
        )

    embed = discord.Embed(
        title=f"📨 Members Invited by {user.display_name}",
        description="\n".join(lines[:50]),
        color=VISTO_COLOR
    )

    embed.set_footer(
        text=f"Total: {len(invited_members)} • Visto Bot"
    )

    await interaction.response.send_message(
        embed=embed
    )


# ============================================================
# MODERATION + DMs
# ============================================================

@bot.tree.command(name="ban", description="Ban a member")
@app_commands.describe(user="Member", reason="Reason")
@app_commands.checks.has_permissions(ban_members=True)
async def ban(interaction, user: discord.Member, reason: str = "No reason provided"):
    dm = discord.Embed(title="🔨 You were banned", description=f'You were banned from **{interaction.guild.name}** for "{reason}".', color=discord.Color.red())
    await safe_dm(user, dm)
    try:
        await user.ban(reason=reason)
        await interaction.response.send_message(embed=success_embed("Member Banned", f"**Member:** {user.mention}\n**Reason:** {reason}"))
        await send_log(interaction.guild, "🔨 Member Banned", f"**Member:** {user.mention}\n**Moderator:** {interaction.user.mention}\n**Reason:** {reason}", discord.Color.red())
    except discord.Forbidden:
        await interaction.response.send_message(embed=error_embed("Ban Failed", "I don't have permission to ban that member."), ephemeral=True)


@bot.tree.command(name="unban", description="Unban a user")
@app_commands.describe(user_id="User ID", reason="Reason")
@app_commands.checks.has_permissions(ban_members=True)
async def unban(interaction, user_id: str, reason: str = "No reason provided"):
    try:
        target = await bot.fetch_user(int(user_id))
        await interaction.guild.unban(target, reason=reason)
        await safe_dm(target, discord.Embed(title="🔓 You were unbanned", description=f'You were unbanned from **{interaction.guild.name}** for "{reason}".', color=discord.Color.green()))
        await interaction.response.send_message(embed=success_embed("Member Unbanned", f"**User:** {target.mention}\n**Reason:** {reason}"))
    except (ValueError, discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
        await interaction.response.send_message(embed=error_embed("Unban Failed", f"Could not unban that user. `{error}`"), ephemeral=True)


@bot.tree.command(name="kick", description="Kick a member")
@app_commands.describe(user="Member", reason="Reason")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction, user: discord.Member, reason: str = "No reason provided"):
    await safe_dm(user, discord.Embed(title="👢 You were kicked", description=f'You were kicked from **{interaction.guild.name}** for "{reason}".', color=discord.Color.orange()))
    try:
        await user.kick(reason=reason)
        await interaction.response.send_message(embed=success_embed("Member Kicked", f"**Member:** {user.mention}\n**Reason:** {reason}"))
        await send_log(interaction.guild, "👢 Member Kicked", f"**Member:** {user.mention}\n**Moderator:** {interaction.user.mention}\n**Reason:** {reason}", discord.Color.orange())
    except discord.Forbidden:
        await interaction.response.send_message(embed=error_embed("Kick Failed", "I don't have permission."), ephemeral=True)


@bot.tree.command(name="timeout", description="Timeout a member")
@app_commands.describe(user="Member", duration="10m, 2h, 1d", reason="Reason")
@app_commands.checks.has_permissions(moderate_members=True)
async def timeout(interaction, user: discord.Member, duration: str, reason: str = "No reason provided"):
    seconds = duration_parser(duration)
    if seconds is None or seconds < 1 or seconds > 28 * 86400:
        return await interaction.response.send_message(embed=error_embed("Invalid Duration", "Use `10m`, `2h`, `1d`, etc. Maximum is 28 days."), ephemeral=True)
    await safe_dm(user, discord.Embed(title="⏱️ You were timed out", description=f'You were timed out in **{interaction.guild.name}** for **{duration}** for "{reason}".', color=discord.Color.orange()))
    try:
        await user.timeout(timedelta(seconds=seconds), reason=reason)
        await interaction.response.send_message(embed=success_embed("Member Timed Out", f"**Member:** {user.mention}\n**Duration:** `{duration}`\n**Reason:** {reason}"))
    except discord.Forbidden:
        await interaction.response.send_message(embed=error_embed("Timeout Failed", "I don't have permission."), ephemeral=True)


@bot.tree.command(name="warn", description="Warn a member")
@app_commands.describe(user="Member", reason="Reason")
@app_commands.checks.has_permissions(moderate_members=True)
async def warn(interaction, user: discord.Member, reason: str):
    warnings = get_guild_data("warnings", interaction.guild.id)
    uid = str(user.id)
    warnings.setdefault(uid, []).append({
        "reason": reason,
        "moderator": interaction.user.id,
        "timestamp": int(datetime.now(timezone.utc).timestamp()),
    })
    save_database()
    await safe_dm(user, discord.Embed(title="⚠️ You were warned", description=f'You were warned in **{interaction.guild.name}** for "{reason}".', color=discord.Color.orange()))
    await interaction.response.send_message(embed=warning_embed("Member Warned", f"**Member:** {user.mention}\n**Reason:** {reason}"))
    await send_log(interaction.guild, "⚠️ Member Warned", f"**Member:** {user.mention}\n**Moderator:** {interaction.user.mention}\n**Reason:** {reason}", discord.Color.orange())


@bot.tree.command(name="warnings", description="View warnings")
@app_commands.describe(user="Member")
@app_commands.checks.has_permissions(moderate_members=True)
async def warnings(interaction, user: discord.Member):
    items = get_guild_data("warnings", interaction.guild.id).get(str(user.id), [])
    if not items:
        return await interaction.response.send_message(embed=info_embed("Warnings", f"{user.mention} has no warnings."))
    lines = [f"**{i}.** {x['reason']} — <@{x['moderator']}>" for i, x in enumerate(items, 1)]
    await interaction.response.send_message(embed=warning_embed(f"Warnings — {user.display_name}", "\n".join(lines[:25])))


@bot.tree.command(name="delwarn", description="Delete a specific warning")
@app_commands.describe(user="Member", number="Warning number, starting at 1")
@app_commands.checks.has_permissions(moderate_members=True)
async def delwarn(interaction, user: discord.Member, number: int):
    warnings = get_guild_data("warnings", interaction.guild.id)
    items = warnings.get(str(user.id), [])
    if number < 1 or number > len(items):
        return await interaction.response.send_message(embed=error_embed("Invalid Warning", "That warning number does not exist."), ephemeral=True)
    removed = items.pop(number - 1)
    if items:
        warnings[str(user.id)] = items
    else:
        warnings.pop(str(user.id), None)
    save_database()
    await interaction.response.send_message(embed=success_embed("Warning Deleted", f"Deleted warning **#{number}** from {user.mention}.\n**Reason:** {removed['reason']}"))


@bot.command(name="delwarn")
@commands.has_permissions(moderate_members=True)
async def delwarn_prefix(ctx, user: discord.Member = None, number: int = None):
    if user is None or number is None:
        return await ctx.send(embed=info_embed("Usage", "Use `.delwarn @user <warning number>`"))

    warnings = get_guild_data("warnings", ctx.guild.id)
    items = warnings.get(str(user.id), [])

    if number < 1 or number > len(items):
        return await ctx.send(embed=error_embed("Invalid Warning", "That warning number does not exist."))

    removed = items.pop(number - 1)
    if items:
        warnings[str(user.id)] = items
    else:
        warnings.pop(str(user.id), None)

    save_database()

    await ctx.send(
        embed=success_embed(
            "Warning Deleted",
            f"Deleted warning **#{number}** from {user.mention}.\n**Reason:** {removed['reason']}"
        )
    )


@bot.tree.command(name="purge", description="Delete messages")
@app_commands.describe(amount="1-100")
@app_commands.checks.has_permissions(manage_messages=True)
async def purge(interaction, amount: int):
    if amount < 1 or amount > 100:
        return await interaction.response.send_message(embed=error_embed("Invalid Amount", "Choose 1-100."), ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(embed=success_embed("Messages Purged", f"Deleted **{len(deleted)}** messages."), ephemeral=True)


@bot.tree.command(name="lock", description="Lock current channel")
@app_commands.checks.has_permissions(manage_channels=True)
async def lock(interaction):
    overwrite = interaction.channel.overwrites_for(interaction.guild.default_role)
    overwrite.send_messages = False
    await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=overwrite)
    await interaction.response.send_message(embed=success_embed("Channel Locked", f"{interaction.channel.mention} is locked."))


@bot.tree.command(name="unlock", description="Unlock current channel")
@app_commands.checks.has_permissions(manage_channels=True)
async def unlock(interaction):
    overwrite = interaction.channel.overwrites_for(interaction.guild.default_role)
    overwrite.send_messages = None
    await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=overwrite)
    await interaction.response.send_message(embed=success_embed("Channel Unlocked", f"{interaction.channel.mention} is unlocked."))


@bot.tree.command(name="lockdown", description="Lock all text channels")
@app_commands.checks.has_permissions(administrator=True)
async def lockdown(interaction):
    await interaction.response.defer()
    changed = 0
    for channel in interaction.guild.text_channels:
        try:
            overwrite = channel.overwrites_for(interaction.guild.default_role)
            overwrite.send_messages = False
            await channel.set_permissions(interaction.guild.default_role, overwrite=overwrite)
            changed += 1
        except (discord.Forbidden, discord.HTTPException):
            pass
    await interaction.followup.send(embed=success_embed("Server Lockdown Enabled", f"Locked **{changed}** text channels."))


@bot.tree.command(name="unlockdown", description="Unlock all text channels")
@app_commands.checks.has_permissions(administrator=True)
async def unlockdown(interaction):
    await interaction.response.defer()
    changed = 0
    for channel in interaction.guild.text_channels:
        try:
            overwrite = channel.overwrites_for(interaction.guild.default_role)
            overwrite.send_messages = None
            await channel.set_permissions(interaction.guild.default_role, overwrite=overwrite)
            changed += 1
        except (discord.Forbidden, discord.HTTPException):
            pass
    await interaction.followup.send(embed=success_embed("Server Lockdown Disabled", f"Unlocked **{changed}** text channels."))


# ============================================================
# SAY
# ============================================================

@bot.command(name="say")
@commands.has_permissions(manage_messages=True)
async def say(ctx, *, message=None):
    if not message:
        return await ctx.send("Usage: `.say <message>`", delete_after=5)
    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass
    await ctx.send(f"{message}")


# ============================================================
# AUTORESPONDER
# ============================================================

@bot.tree.command(name="autoresponder_add", description="Add an autoresponder")
@app_commands.describe(trigger="Exact trigger text", response="Bot response")
@app_commands.checks.has_permissions(manage_guild=True)
async def autoresponder_add(interaction, trigger: str, response: str):
    data = get_guild_data("autoresponders", interaction.guild.id)
    data[trigger.strip().lower()] = response
    save_database()
    await interaction.response.send_message(embed=success_embed("Autoresponder Added", f"`{trigger}` → {response}"), ephemeral=True)


@bot.tree.command(name="autoresponder_remove", description="Remove an autoresponder")
@app_commands.describe(trigger="Exact trigger text")
@app_commands.checks.has_permissions(manage_guild=True)
async def autoresponder_remove(interaction, trigger: str):
    data = get_guild_data("autoresponders", interaction.guild.id)
    trigger = trigger.strip().lower()
    if trigger not in data:
        return await interaction.response.send_message(embed=error_embed("Not Found", f"No autoresponder exists for `{trigger}`."), ephemeral=True)
    del data[trigger]
    save_database()
    await interaction.response.send_message(embed=success_embed("Autoresponder Removed", f"Removed `{trigger}`."), ephemeral=True)


@bot.tree.command(name="autoresponder_list", description="List autoresponders")
@app_commands.checks.has_permissions(manage_guild=True)
async def autoresponder_list(interaction):
    data = get_guild_data("autoresponders", interaction.guild.id)
    if not data:
        return await interaction.response.send_message(embed=info_embed("Autoresponders", "None configured."), ephemeral=True)
    lines = [f"**{i}.** `{k}` → {v[:150]}" for i, (k, v) in enumerate(data.items(), 1)]
    await interaction.response.send_message(embed=info_embed("Autoresponders", "\n".join(lines[:25])))


# ============================================================
# TICKET SYSTEM
# ============================================================

def ticket_settings(guild):
    settings = get_guild_data("settings", guild.id)
    return {
        "buy": int(settings.get("ticket_buy_category", BUY_CATEGORY_ID)),
        "claim": int(settings.get("ticket_claim_category", CLAIM_CATEGORY_ID)),
        "support": int(settings.get("ticket_support_category", SUPPORT_CATEGORY_ID)),
        "staff_role": int(settings.get("ticket_staff_role", TICKET_STAFF_ROLE_ID)),
    }


def is_ticket_channel(channel):
    return isinstance(channel, discord.TextChannel) and channel.topic and channel.topic.startswith("VISTO_TICKET|")


def ticket_owner_id(channel):
    try:
        parts = channel.topic.split("|")
        return int(parts[1])
    except Exception:
        return None


def ticket_type(channel):
    try:
        return channel.topic.split("|")[2]
    except Exception:
        return "support"


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def create_ticket(self, interaction, kind, emoji):
        guild = interaction.guild
        settings = ticket_settings(guild)
        category_id = settings[kind]
        category = guild.get_channel(category_id) if category_id else None
        if not isinstance(category, discord.CategoryChannel):
            return await interaction.response.send_message(embed=error_embed("Category Not Set", f"Set the `{kind}` ticket category in the main.py config."), ephemeral=True)

        existing = [c for c in guild.text_channels if is_ticket_channel(c) and ticket_owner_id(c) == interaction.user.id and c.name.startswith(f"{kind}-")]
        if existing:
            return await interaction.response.send_message(embed=warning_embed("Ticket Already Open", f"You already have {existing[0].mention}."), ephemeral=True)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, manage_messages=True),
        }
        if settings["staff_role"]:
            role = guild.get_role(settings["staff_role"])
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)

        channel = await guild.create_text_channel(
            name=f"{kind}-{interaction.user.name}"[:100],
            category=category,
            topic=f"VISTO_TICKET|{interaction.user.id}|{kind}|open",
            overwrites=overwrites,
        )
        db["tickets"].setdefault(str(guild.id), {})[str(channel.id)] = {
            "owner_id": interaction.user.id,
            "type": kind,
            "closed": False,
            "created": int(datetime.now(timezone.utc).timestamp()),
        }
        save_database()

        embed = discord.Embed(
            title=f"{emoji} {kind.title()} Ticket",
            description=(
                f"Welcome {interaction.user.mention}!\n\n"
                "A staff member will assist you.\n\n"
                "Use **Close Ticket** when you are finished."
            ),
            color=discord.Color.blurple(),
        )
        await channel.send(embed=embed, view=TicketControlsView())
        await interaction.response.send_message(embed=success_embed("Ticket Created", f"Your ticket is {channel.mention}."), ephemeral=True)
        await send_log(guild, "🎫 Ticket Created", f"**Type:** {kind}\n**User:** {interaction.user.mention}\n**Channel:** {channel.mention}")

    @discord.ui.button(label="Buy", emoji="🛒", style=discord.ButtonStyle.success, custom_id="visto_ticket_buy")
    async def buy(self, interaction, button):
        await self.create_ticket(interaction, "buy", "🛒")

    @discord.ui.button(label="Claim", emoji="🎁", style=discord.ButtonStyle.primary, custom_id="visto_ticket_claim")
    async def claim(self, interaction, button):
        await self.create_ticket(interaction, "claim", "🎁")

    @discord.ui.button(label="Support", emoji="🛠️", style=discord.ButtonStyle.secondary, custom_id="visto_ticket_support")
    async def support(self, interaction, button):
        await self.create_ticket(interaction, "support", "🛠️")


class TicketControlsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="visto_ticket_close")
    async def close(self, interaction, button):
        if not is_ticket_channel(interaction.channel):
            return await interaction.response.send_message(embed=error_embed("Not A Ticket", "This is not a Visto ticket."), ephemeral=True)
        settings = ticket_settings(interaction.guild)
        staff_role = interaction.guild.get_role(settings["staff_role"]) if settings["staff_role"] else None
        allowed = interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator or (staff_role and staff_role in interaction.user.roles)
        if not allowed:
            return await interaction.response.send_message(embed=error_embed("No Permission", "Only staff can close tickets."), ephemeral=True)
        await interaction.response.send_message(embed=warning_embed("Confirm Ticket Close", "Are you sure you want to close this ticket?"), view=TicketCloseConfirmView(), ephemeral=True)


class TicketCloseConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="Confirm Close", emoji="✅", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        reason = await ask_reason(interaction, "Close Ticket", "Enter the ticket closing reason")
        channel = interaction.channel
        owner_id = ticket_owner_id(channel)
        owner = interaction.guild.get_member(owner_id) if owner_id else None

        if owner:
            await safe_dm(owner, discord.Embed(title="🔒 Ticket Closed", description=f'Your **{ticket_type(channel).title()}** ticket in **{interaction.guild.name}** was closed by {interaction.user.mention} for "{reason}".', color=discord.Color.orange()))

        # Lock ticket rather than instantly deleting it.
        if owner:
            await channel.set_permissions(owner, view_channel=False, send_messages=False)
        settings = ticket_settings(interaction.guild)
        if settings["staff_role"]:
            role = interaction.guild.get_role(settings["staff_role"])
            if role:
                await channel.set_permissions(role, view_channel=True, send_messages=False, read_message_history=True)

        await channel.edit(name=f"closed-{channel.name}"[:100], topic=f"VISTO_TICKET|{owner_id}|{ticket_type(channel)}|closed|{reason[:800]}")
        db["tickets"].setdefault(str(interaction.guild.id), {}).setdefault(str(channel.id), {})["closed"] = True
        db["tickets"][str(interaction.guild.id)][str(channel.id)]["close_reason"] = reason
        save_database()

        embed = discord.Embed(title="🔒 Ticket Closed", description=f"**Closed by:** {interaction.user.mention}\n**Reason:** {reason}\n\nThis ticket is now closed.", color=discord.Color.orange())
        await channel.send(embed=embed, view=ClosedTicketView())
        await interaction.response.edit_message(embed=success_embed("Ticket Closed", f"Closed for \"{reason}\"."), view=None)
        await send_log(interaction.guild, "🔒 Ticket Closed", f"**Channel:** {channel.mention}\n**Closed by:** {interaction.user.mention}\n**Reason:** {reason}", discord.Color.orange())

    @discord.ui.button(label="Cancel", emoji="❌", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await interaction.response.edit_message(embed=info_embed("Close Cancelled", "The ticket remains open."), view=None)


class ClosedTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Delete Ticket", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="visto_ticket_delete")
    async def delete(self, interaction, button):
        if not interaction.user.guild_permissions.manage_channels and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(embed=error_embed("No Permission", "Only staff can delete closed tickets."), ephemeral=True)
        await interaction.response.send_message(embed=warning_embed("Deleting Ticket", "Deleting this ticket in 3 seconds."))
        await asyncio.sleep(3)
        await interaction.channel.delete()

    @discord.ui.button(label="Transcript", emoji="📜", style=discord.ButtonStyle.secondary, custom_id="visto_ticket_transcript")
    async def transcript(self, interaction, button):
        messages = []
        async for message in interaction.channel.history(limit=200, oldest_first=True):
            messages.append(f"[{message.created_at:%Y-%m-%d %H:%M}] {message.author}: {message.content}")
        text = "\n".join(messages) or "No messages."
        data = discord.File(fp=__import__("io").BytesIO(text.encode("utf-8")), filename=f"{interaction.channel.name}-transcript.txt")
        await interaction.response.send_message(file=data, ephemeral=True)


@bot.tree.command(name="ticket_setup", description="Post the ticket panel")
@app_commands.checks.has_permissions(manage_channels=True)
async def ticket_setup(interaction):
    embed = discord.Embed(
        title="🎫 Visto Tickets",
        description=(
            "Choose what you need below.\n\n"
            "🛒 **Buy** — purchase help\n"
            "🎁 **Claim** — claim help\n"
            "🛠️ **Support** — general support"
        ),
        color=discord.Color.blurple(),
    )
    await interaction.channel.send(embed=embed, view=TicketPanelView())
    await interaction.response.send_message(embed=success_embed("Ticket Panel", "Panel posted."), ephemeral=True)


# Alias group /ticket setup and /ticket close.
ticket_group = app_commands.Group(name="ticket", description="Ticket system")

@ticket_group.command(name="setup", description="Post the ticket panel")
@app_commands.checks.has_permissions(manage_channels=True)
async def ticket_group_setup(interaction):
    await ticket_setup.callback(interaction)

@ticket_group.command(name="close", description="Close current ticket")
async def ticket_group_close(interaction):
    if not is_ticket_channel(interaction.channel):
        return await interaction.response.send_message(embed=error_embed("Not A Ticket", "This is not a Visto ticket."), ephemeral=True)
    await interaction.response.send_message(embed=warning_embed("Confirm Ticket Close", "Are you sure?"), view=TicketCloseConfirmView(), ephemeral=True)

bot.tree.add_command(ticket_group)


# ============================================================
# /user add
# ============================================================

user_group = app_commands.Group(name="user", description="Ticket user management")

@user_group.command(name="add", description="Add a user to the current ticket")
@app_commands.describe(user="User to add")
@app_commands.checks.has_permissions(manage_channels=True)
async def user_add(interaction, user: discord.Member):
    if not is_ticket_channel(interaction.channel):
        return await interaction.response.send_message(embed=error_embed("Not A Ticket", "Use this inside a ticket."), ephemeral=True)
    await interaction.channel.set_permissions(user, view_channel=True, send_messages=True, read_message_history=True, attach_files=True)
    await interaction.response.send_message(embed=success_embed("User Added", f"Added {user.mention} to {interaction.channel.mention}."))
    await safe_dm(user, discord.Embed(title="🎫 Added To Ticket", description=f"You were added to {interaction.channel.mention} in **{interaction.guild.name}**.", color=discord.Color.blurple()))

bot.tree.add_command(user_group)


# ============================================================
# GIVEAWAYS — ADVANCED
# ============================================================

giveaway_tasks = {}


def create_giveaway_embed(data):
    end_time = int(data["end_time"])
    embed = discord.Embed(
        title=data.get("title") or "🎉 GIVEAWAY 🎉",
        description=data.get("description") or "Click **🎉 Enter Giveaway** to participate!",
        color=int(data.get("color", 15844367)),
    )
    embed.add_field(name="🎁 Prize", value=data["prize"], inline=False)
    embed.add_field(name="🏆 Winners", value=str(data["winners"]), inline=True)
    embed.add_field(name="👤 Host", value=data.get("host_text") or f"<@{data['host_id']}>" , inline=True)
    embed.add_field(name="👥 Entries", value=str(len(data.get("entries", []))), inline=True)
    embed.add_field(name="⏰ Ends", value=f"<t:{end_time}:R>", inline=True)
    if data.get("required_role_id"):
        embed.add_field(name="🎭 Required Role", value=f"<@&{data['required_role_id']}>" , inline=True)
    if data.get("blacklisted_role_id"):
        embed.add_field(name="🚫 Blacklisted Role", value=f"<@&{data['blacklisted_role_id']}>" , inline=True)
    if data.get("extra_role_id") and int(data.get("extra_entries", 0)) > 0:
        embed.add_field(name="✨ Extra Entries", value=f"<@&{data['extra_role_id']}> = {data['extra_entries']} total entries", inline=False)
    if data.get("image"):
        embed.set_image(url=data["image"])
    if data.get("thumbnail"):
        embed.set_thumbnail(url=data["thumbnail"])
    embed.set_footer(text="Visto Giveaways")
    return embed


class GiveawayView(discord.ui.View):
    def __init__(self, disabled=False):
        super().__init__(timeout=None)
        self.enter_button.disabled = disabled

    @discord.ui.button(label="Enter Giveaway", emoji="🎉", style=discord.ButtonStyle.success, custom_id="visto_giveaway_enter")
    async def enter_button(self, interaction, button):
        gid = str(interaction.message.id)
        data = db["giveaways"].get(gid)
        if not data or data.get("ended"):
            return await interaction.response.send_message(embed=warning_embed("Giveaway Ended", "This giveaway is no longer active."), ephemeral=True)
        if interaction.user.bot:
            return
        entries = data.setdefault("entries", [])
        if interaction.user.id in entries:
            return await interaction.response.send_message(embed=warning_embed("Already Entered", "You're already entered."), ephemeral=True)

        required = data.get("required_role_id")
        if required and not any(r.id == int(required) for r in interaction.user.roles):
            return await interaction.response.send_message(embed=error_embed("Entry Denied", f"You need <@&{required}> to enter."), ephemeral=True)
        blacklisted = data.get("blacklisted_role_id")
        if blacklisted and any(r.id == int(blacklisted) for r in interaction.user.roles):
            return await interaction.response.send_message(embed=error_embed("Entry Denied", "Your role is not allowed to enter."), ephemeral=True)
        account_age_days = int(data.get("min_account_age_days", 0))
        if account_age_days:
            age = (datetime.now(timezone.utc) - interaction.user.created_at).days
            if age < account_age_days:
                return await interaction.response.send_message(embed=error_embed("Entry Denied", f"Your account must be at least {account_age_days} days old."), ephemeral=True)
        member_since_days = int(data.get("min_server_days", 0))
        if member_since_days and isinstance(interaction.user, discord.Member):
            if not interaction.user.joined_at or (datetime.now(timezone.utc) - interaction.user.joined_at).days < member_since_days:
                return await interaction.response.send_message(embed=error_embed("Entry Denied", f"You must be in the server for at least {member_since_days} days."), ephemeral=True)

        weight = 1
        extra_role = data.get("extra_role_id")
        if extra_role and any(r.id == int(extra_role) for r in interaction.user.roles):
            weight = max(1, int(data.get("extra_entries", 0)))
        for _ in range(weight):
            entries.append(interaction.user.id)
        save_database()
        await interaction.response.send_message(embed=success_embed("Giveaway Entry", data.get("entry_confirmation") or "You entered the giveaway! 🎉"), ephemeral=True)


async def finish_giveaway(message_id, automatic=False):
    key = str(message_id)
    data = db["giveaways"].get(key)
    if not data or data.get("ended"):
        return None
    data["ended"] = True
    entries = list(data.get("entries", []))
    unique = list(dict.fromkeys(entries))
    winners = random.sample(unique, min(int(data["winners"]), len(unique))) if unique else []
    data["winners_selected"] = winners
    save_database()

    channel = bot.get_channel(int(data["channel_id"]))
    if channel:
        try:
            message = await channel.fetch_message(int(message_id))
            ended_embed = create_giveaway_embed(data)
            ended_embed.title = "🎉 GIVEAWAY ENDED 🎉"
            ended_embed.color = discord.Color.dark_gold()
            ended_embed.add_field(name="Status", value="Ended", inline=True)
            await message.edit(embed=ended_embed, view=GiveawayView(disabled=True))
        except (discord.NotFound, discord.HTTPException):
            pass
        mentions = " ".join(f"<@{uid}>" for uid in winners) or "Nobody"
        await channel.send(embed=discord.Embed(title="🏆 Giveaway Winner(s)", description=f"**Prize:** {data['prize']}\n**Winner(s):** {mentions}", color=discord.Color.gold()))

        for uid in winners:
            try:
                user = await bot.fetch_user(uid)
                msg = data.get("winner_dm") or f"You won **{data['prize']}** in {channel.guild.name}!"
                await user.send(msg)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
    return winners


async def giveaway_timer(message_id):
    while True:
        data = db["giveaways"].get(str(message_id))
        if not data or data.get("ended") or data.get("paused"):
            return
        remaining = data["end_time"] - datetime.now(timezone.utc).timestamp()
        if remaining <= 0:
            await finish_giveaway(message_id, automatic=True)
            return
        await asyncio.sleep(min(30, max(1, remaining)))


def start_giveaway_task(message_id):
    key = str(message_id)
    if key not in giveaway_tasks or giveaway_tasks[key].done():
        giveaway_tasks[key] = asyncio.create_task(giveaway_timer(message_id))


giveaway_group = app_commands.Group(name="giveaway", description="Giveaway system")

@giveaway_group.command(name="start", description="Start an advanced giveaway")
@app_commands.describe(
    prize="Prize",
    duration="Examples: 10m, 2h, 3d, 1d 5h 30m",
    winners="Number of winners",
    channel="Channel for the giveaway",
    host="Optional host text",
    description="Optional giveaway description",
    image="Optional image URL",
    thumbnail="Optional thumbnail URL",
    required_role="Optional required role",
    blacklisted_role="Optional blacklisted role",
    extra_role="Optional role that receives extra entries",
    extra_entries="Number of entries for the extra-entry role",
    min_account_age_days="Minimum account age in days",
    min_server_days="Minimum time in server in days",
    winner_dm="Optional DM sent to winners",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def giveaway_start(
    interaction,
    prize: str,
    duration: str,
    winners: int,
    channel: discord.TextChannel = None,
    host: str = None,
    description: str = None,
    image: str = None,
    thumbnail: str = None,
    required_role: discord.Role = None,
    blacklisted_role: discord.Role = None,
    extra_role: discord.Role = None,
    extra_entries: int = 0,
    min_account_age_days: int = 0,
    min_server_days: int = 0,
    winner_dm: str = None,
):
    seconds = duration_parser(duration)
    if seconds is None or seconds < 60 or seconds > 365 * 86400:
        return await interaction.response.send_message(embed=error_embed("Invalid Duration", "Use a duration between 1 minute and 1 year, e.g. `2h`, `3d`, `1d 5h 30m`."), ephemeral=True)
    if winners < 1 or winners > 100:
        return await interaction.response.send_message(embed=error_embed("Invalid Winners", "Winners must be between 1 and 100."), ephemeral=True)
    if extra_entries < 0 or extra_entries > 100:
        return await interaction.response.send_message(embed=error_embed("Invalid Extra Entries", "Use 0-100."), ephemeral=True)
    if min_account_age_days < 0 or min_server_days < 0:
        return await interaction.response.send_message(embed=error_embed("Invalid Requirement", "Days cannot be negative."), ephemeral=True)

    target = channel or interaction.channel
    data = {
        "guild_id": interaction.guild.id,
        "channel_id": target.id,
        "prize": prize,
        "winners": winners,
        "host_id": interaction.user.id,
        "host_text": host,
        "description": description,
        "image": image,
        "thumbnail": thumbnail,
        "required_role_id": required_role.id if required_role else None,
        "blacklisted_role_id": blacklisted_role.id if blacklisted_role else None,
        "extra_role_id": extra_role.id if extra_role else None,
        "extra_entries": extra_entries,
        "min_account_age_days": min_account_age_days,
        "min_server_days": min_server_days,
        "winner_dm": winner_dm,
        "entry_confirmation": "You entered the giveaway! 🎉",
        "entries": [],
        "winners_selected": [],
        "end_time": datetime.now(timezone.utc).timestamp() + seconds,
        "ended": False,
        "paused": False,
        "duration": duration,
    }
    await interaction.response.defer(ephemeral=True)
    message = await target.send(embed=create_giveaway_embed(data), view=GiveawayView())
    db["giveaways"][str(message.id)] = data
    save_database()
    start_giveaway_task(message.id)
    await interaction.followup.send(embed=success_embed("Giveaway Started", f"Created in {target.mention}.\n**Message ID:** `{message.id}`"), ephemeral=True)


@giveaway_group.command(name="end", description="End a giveaway")
@app_commands.describe(message_id="Giveaway message ID")
@app_commands.checks.has_permissions(manage_guild=True)
async def giveaway_end(interaction, message_id: str):
    result = await finish_giveaway(message_id)
    if result is None:
        return await interaction.response.send_message(embed=error_embed("Not Found", "That giveaway was not found or already ended."), ephemeral=True)
    await interaction.response.send_message(embed=success_embed("Giveaway Ended", f"Ended `{message_id}`."))


@giveaway_group.command(name="reroll", description="Reroll a giveaway")
@app_commands.describe(message_id="Ended giveaway message ID")
@app_commands.checks.has_permissions(manage_guild=True)
async def giveaway_reroll(interaction, message_id: str):
    data = db["giveaways"].get(str(message_id))
    if not data or not data.get("ended"):
        return await interaction.response.send_message(embed=error_embed("Not Found", "That ended giveaway could not be found."), ephemeral=True)
    entries = list(dict.fromkeys(data.get("entries", [])))
    if not entries:
        return await interaction.response.send_message(embed=error_embed("No Entries", "Nobody entered."), ephemeral=True)
    winners = random.sample(entries, min(int(data["winners"]), len(entries)))
    data["winners_selected"] = winners
    save_database()
    mentions = " ".join(f"<@{uid}>" for uid in winners)
    await interaction.response.send_message(embed=discord.Embed(title="🔄 Giveaway Rerolled", description=f"**Prize:** {data['prize']}\n**New winner(s):** {mentions}", color=discord.Color.gold()))


@giveaway_group.command(name="pause", description="Pause an active giveaway")
@app_commands.describe(message_id="Giveaway message ID")
@app_commands.checks.has_permissions(manage_guild=True)
async def giveaway_pause(interaction, message_id: str):
    data = db["giveaways"].get(str(message_id))
    if not data or data.get("ended"):
        return await interaction.response.send_message(embed=error_embed("Not Found", "Active giveaway not found."), ephemeral=True)
    data["paused"] = True
    save_database()
    await interaction.response.send_message(embed=success_embed("Giveaway Paused", f"Paused `{message_id}`."))


@giveaway_group.command(name="resume", description="Resume a paused giveaway")
@app_commands.describe(message_id="Giveaway message ID")
@app_commands.checks.has_permissions(manage_guild=True)
async def giveaway_resume(interaction, message_id: str):
    data = db["giveaways"].get(str(message_id))
    if not data or data.get("ended"):
        return await interaction.response.send_message(embed=error_embed("Not Found", "Giveaway not found."), ephemeral=True)
    data["paused"] = False
    save_database()
    start_giveaway_task(message_id)
    await interaction.response.send_message(embed=success_embed("Giveaway Resumed", f"Resumed `{message_id}`."))


@giveaway_group.command(name="delete", description="Delete giveaway data")
@app_commands.describe(message_id="Giveaway message ID")
@app_commands.checks.has_permissions(manage_guild=True)
async def giveaway_delete(interaction, message_id: str):
    if str(message_id) not in db["giveaways"]:
        return await interaction.response.send_message(embed=error_embed("Not Found", "Giveaway not found."), ephemeral=True)
    db["giveaways"].pop(str(message_id), None)
    save_database()
    await interaction.response.send_message(embed=success_embed("Giveaway Deleted", f"Deleted data for `{message_id}`."))

bot.tree.add_command(giveaway_group)


# ============================================================
# /STATS
# ============================================================

stats_group = app_commands.Group(
    name="stats",
    description="Manage message and invite statistics"
)

# ...paste the rest of the code here...


bot.tree.add_command(stats_group)


# ============================================================
# NEXT SECTION
# ============================================================


# ============================================================
# LEADERBOARD SHORTCUTS
# ============================================================

@bot.command(name="lb")
async def lb(ctx, mode=None):
    mode = (mode or "messages").lower()
    if mode == "messages":
        view = MessageLeaderboardView(ctx)
        return await ctx.send(embed=view.make_embed(), view=view)
    if mode == "invites":
        data = get_guild_data("invites", ctx.guild.id)
        rows = []
        for uid in data:
            rows.append((uid, get_invite_stats(ctx.guild.id, uid)["total"]))
        rows.sort(key=lambda x: x[1], reverse=True)
        lines = [f"**#{i}** <@{uid}> • `{amount}` invites" for i, (uid, amount) in enumerate(rows[:10], 1)]
        return await ctx.send(embed=info_embed("Invite Leaderboard", "\n".join(lines) if lines else "No data."))
    await ctx.send(embed=error_embed("Usage", "Use `-lb messages` or `-lb invites`."))


# ============================================================
# SERVER MESSAGE/INVITE CONFIG
# ============================================================

@bot.tree.command(name="setgeneral", description="Set the message-counting channel")
@app_commands.describe(channel="General channel")
@app_commands.checks.has_permissions(manage_guild=True)
async def setgeneral(interaction, channel: discord.TextChannel):
    settings = get_guild_data("settings", interaction.guild.id)
    settings["general_channel"] = channel.id
    save_database()
    await interaction.response.send_message(embed=success_embed("General Channel Set", f"Messages will now be counted only in {channel.mention}."))


@bot.tree.command(name="setlog", description="Set logging channel")
@app_commands.describe(channel="Logging channel")
@app_commands.checks.has_permissions(manage_guild=True)
async def setlog(interaction, channel: discord.TextChannel):
    settings = get_guild_data("settings", interaction.guild.id)
    settings["log_channel"] = channel.id
    save_database()
    await interaction.response.send_message(embed=success_embed("Log Channel Set", f"Logs will go to {channel.mention}."))


@bot.tree.command(name="set_ticket_categories", description="Set ticket categories")
@app_commands.describe(buy="Buy category", claim="Claim category", support="Support category", staff_role="Ticket staff role")
@app_commands.checks.has_permissions(manage_guild=True)
async def set_ticket_categories(interaction, buy: discord.CategoryChannel, claim: discord.CategoryChannel, support: discord.CategoryChannel, staff_role: discord.Role = None):
    settings = get_guild_data("settings", interaction.guild.id)
    settings["ticket_buy_category"] = buy.id
    settings["ticket_claim_category"] = claim.id
    settings["ticket_support_category"] = support.id
    if staff_role:
        settings["ticket_staff_role"] = staff_role.id
    save_database()
    await interaction.response.send_message(embed=success_embed("Ticket Settings Updated", "Buy / Claim / Support categories were saved."))


# ============================================================
# PROFESSIONAL VISTO BOT DASHBOARD
# ============================================================

_dashboard_server = None


# ------------------------------------------------------------
# DATABASE BACKUP
# ------------------------------------------------------------

def dashboard_backup_database():
    try:
        if os.path.exists(DB_FILE):
            backup_name = (
                f"{DB_FILE}.backup"
            )

            with open(
                DB_FILE,
                "rb"
            ) as source:

                with open(
                    backup_name,
                    "wb"
                ) as backup:

                    backup.write(
                        source.read()
                    )

    except Exception as error:
        print(
            f"Dashboard backup error: {error}"
        )


# ------------------------------------------------------------
# HTML
# ------------------------------------------------------------

def dashboard_page(
    title,
    body,
    guild_id=None
):

    navigation = f"""
    <aside class="sidebar">

        <div class="brand">
            <div class="brand-icon">V</div>
            <div>
                <div class="brand-name">
                    Visto Bot
                </div>
                <div class="brand-sub">
                    Control Panel
                </div>
            </div>
        </div>

        <a href="?key={html.escape(DASHBOARD_PASSWORD)}"
           class="nav-link">
            🏠 Overview
        </a>

        <a href="/settings?key={html.escape(DASHBOARD_PASSWORD)}"
           class="nav-link">
            ⚙️ Server Settings
        </a>

        <a href="/moderation?key={html.escape(DASHBOARD_PASSWORD)}"
           class="nav-link">
            🛡️ Moderation
        </a>

        <a href="/giveaways?key={html.escape(DASHBOARD_PASSWORD)}"
           class="nav-link">
            🎉 Giveaways
        </a>

        <a href="/tickets?key={html.escape(DASHBOARD_PASSWORD)}"
           class="nav-link">
            🎫 Tickets
        </a>

        <a href="/autoresponders?key={html.escape(DASHBOARD_PASSWORD)}"
           class="nav-link">
            🤖 Autoresponders
        </a>

        <a href="/statistics?key={html.escape(DASHBOARD_PASSWORD)}"
           class="nav-link">
            📊 Statistics
        </a>

        <div class="sidebar-bottom">
            <a href="/health" class="nav-link">
                🟢 Bot Health
            </a>
        </div>

    </aside>
    """

    return f"""
<!doctype html>

<html>

<head>

<meta charset="utf-8">

<meta name="viewport"
      content="width=device-width, initial-scale=1">

<title>
{html.escape(title)} • Visto Bot
</title>

<style>

* {{
    box-sizing: border-box;
}}

body {{
    margin: 0;
    font-family:
        Inter,
        ui-sans-serif,
        system-ui,
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;

    background:
        #0b1020;

    color:
        #f8fafc;
}}

a {{
    text-decoration: none;
}}

.layout {{
    display: flex;
    min-height: 100vh;
}}

.sidebar {{
    width: 250px;
    background: #0f172a;
    border-right: 1px solid #1e293b;
    padding: 24px 16px;
    position: fixed;
    top: 0;
    left: 0;
    bottom: 0;
    overflow-y: auto;
}}

.brand {{
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 30px;
    padding: 8px;
}}

.brand-icon {{
    width: 42px;
    height: 42px;
    border-radius: 12px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: linear-gradient(
        135deg,
        #6366f1,
        #8b5cf6
    );
    font-weight: 900;
    font-size: 20px;
}}

.brand-name {{
    font-weight: 800;
    font-size: 17px;
}}

.brand-sub {{
    color: #64748b;
    font-size: 12px;
    margin-top: 2px;
}}

.nav-link {{
    display: block;
    color: #cbd5e1;
    padding: 12px 14px;
    border-radius: 10px;
    margin-bottom: 5px;
    transition: 0.15s;
}}

.nav-link:hover {{
    background: #1e293b;
    color: white;
}}

.sidebar-bottom {{
    margin-top: 25px;
    padding-top: 20px;
    border-top: 1px solid #1e293b;
}}

.main {{
    margin-left: 250px;
    width: calc(100% - 250px);
    padding: 30px;
}}

.topbar {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 15px;
    margin-bottom: 28px;
}}

.page-title {{
    font-size: 28px;
    font-weight: 800;
}}

.page-subtitle {{
    color: #94a3b8;
    margin-top: 5px;
}}

.server-select {{
    background: #111827;
    border: 1px solid #334155;
    color: white;
    padding: 11px 14px;
    border-radius: 10px;
    min-width: 240px;
}}

.stats-grid {{
    display: grid;
    grid-template-columns:
        repeat(
            auto-fit,
            minmax(190px, 1fr)
        );
    gap: 15px;
    margin-bottom: 20px;
}}

.stat-card {{
    background: #111827;
    border: 1px solid #1e293b;
    border-radius: 15px;
    padding: 18px;
}}

.stat-label {{
    color: #94a3b8;
    font-size: 13px;
}}

.stat-value {{
    font-size: 28px;
    font-weight: 800;
    margin-top: 7px;
}}

.grid {{
    display: grid;
    grid-template-columns:
        repeat(
            auto-fit,
            minmax(330px, 1fr)
        );
    gap: 18px;
}}

.card {{
    background: #111827;
    border: 1px solid #1e293b;
    border-radius: 15px;
    padding: 20px;
}}

.card h2 {{
    margin-top: 0;
    margin-bottom: 5px;
    font-size: 18px;
}}

.muted {{
    color: #94a3b8;
    font-size: 13px;
}}

label {{
    display: block;
    color: #cbd5e1;
    font-size: 13px;
    margin-top: 14px;
    margin-bottom: 6px;
}}

input,
select,
textarea {{
    width: 100%;
    background: #0b1220;
    border: 1px solid #334155;
    border-radius: 9px;
    padding: 11px;
    color: white;
    outline: none;
}}

textarea {{
    resize: vertical;
    min-height: 90px;
}}

input:focus,
select:focus,
textarea:focus {{
    border-color: #6366f1;
}}

button,
.btn {{
    display: inline-block;
    border: 0;
    border-radius: 9px;
    padding: 11px 16px;
    background: #6366f1;
    color: white;
    cursor: pointer;
    font-weight: 700;
    margin-top: 14px;
}}

.btn-danger {{
    background: #dc2626;
}}

.btn-success {{
    background: #16a34a;
}}

.btn-secondary {{
    background: #334155;
}}

hr {{
    border: 0;
    border-top: 1px solid #1e293b;
    margin: 20px 0;
}}

.notice {{
    background: #172554;
    border: 1px solid #1d4ed8;
    color: #bfdbfe;
    padding: 13px;
    border-radius: 10px;
    margin-bottom: 18px;
}}

.warning {{
    background: #451a03;
    border: 1px solid #92400e;
    color: #fed7aa;
    padding: 13px;
    border-radius: 10px;
    margin-bottom: 18px;
}}

table {{
    width: 100%;
    border-collapse: collapse;
}}

th,
td {{
    text-align: left;
    padding: 11px;
    border-bottom: 1px solid #1e293b;
}}

th {{
    color: #94a3b8;
    font-size: 12px;
}}

@media (
    max-width: 800px
) {{

    .sidebar {{
        position: static;
        width: 100%;
        height: auto;
    }}

    .layout {{
        display: block;
    }}

    .main {{
        margin-left: 0;
        width: 100%;
        padding: 18px;
    }}

    .topbar {{
        flex-direction: column;
        align-items: stretch;
    }}

}}

</style>

</head>

<body>

<div class="layout">

{navigation}

<main class="main">

{body}

</main>

</div>

</body>

</html>
"""


# ------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------

def dashboard_guild_from_id(
    guild_id
):

    try:

        return bot.get_guild(
            int(guild_id)
        )

    except (
        TypeError,
        ValueError
    ):

        return None


def dashboard_guild_select(
    selected=None
):

    options = []

    for guild in bot.guilds:

        selected_text = (
            " selected"
            if selected
            and int(selected) == guild.id
            else ""
        )

        options.append(
            f"""
<option value="{guild.id}"{selected_text}>
{html.escape(guild.name)}
</option>
"""
        )

    return "".join(options)


def dashboard_run(coro):

    future = asyncio.run_coroutine_threadsafe(
        coro,
        bot.loop
    )

    return future.result(
        timeout=30
    )


# ============================================================
# MODERATION COROUTINES
# ============================================================

async def dashboard_ban(
    guild,
    user_id,
    reason
):

    user = guild.get_member(
        int(user_id)
    )

    if user is None:
        return False, "Member not found."

    try:

        await safe_dm(
            user,
            discord.Embed(
                title="🔨 You were banned",
                description=(
                    f'You were banned from '
                    f'**{guild.name}** for "{reason}".'
                ),
                color=discord.Color.red()
            )
        )

        await user.ban(
            reason=reason
        )

        await send_log(
            guild,
            "🔨 Member Banned",
            (
                f"**Member:** {user.mention}\n"
                f"**Reason:** {reason}\n"
                "**Source:** Dashboard"
            ),
            discord.Color.red()
        )

        return True, "Member banned successfully."

    except discord.Forbidden:

        return False, "I do not have permission to ban that member."

    except discord.HTTPException as error:

        return False, str(error)


async def dashboard_kick(
    guild,
    user_id,
    reason
):

    user = guild.get_member(
        int(user_id)
    )

    if user is None:
        return False, "Member not found."

    try:

        await safe_dm(
            user,
            discord.Embed(
                title="👢 You were kicked",
                description=(
                    f'You were kicked from '
                    f'**{guild.name}** for "{reason}".'
                ),
                color=discord.Color.orange()
            )
        )

        await user.kick(
            reason=reason
        )

        await send_log(
            guild,
            "👢 Member Kicked",
            (
                f"**Member:** {user.mention}\n"
                f"**Reason:** {reason}\n"
                "**Source:** Dashboard"
            ),
            discord.Color.orange()
        )

        return True, "Member kicked successfully."

    except discord.Forbidden:

        return False, "I do not have permission to kick that member."

    except discord.HTTPException as error:

        return False, str(error)


async def dashboard_warn(
    guild,
    user_id,
    reason,
    moderator_id
):

    user = guild.get_member(
        int(user_id)
    )

    if user is None:
        return False, "Member not found."

    warnings = get_guild_data(
        "warnings",
        guild.id
    )

    uid = str(
        user.id
    )

    warnings.setdefault(
        uid,
        []
    )

    warnings[uid].append(
        {
            "reason": reason,
            "moderator": int(moderator_id),
            "timestamp": int(
                datetime.now(
                    timezone.utc
                ).timestamp()
            ),
        }
    )

    save_database()

    await safe_dm(
        user,
        discord.Embed(
            title="⚠️ You were warned",
            description=(
                f'You were warned in '
                f'**{guild.name}** for "{reason}".'
            ),
            color=discord.Color.orange()
        )
    )

    await send_log(
        guild,
        "⚠️ Member Warned",
        (
            f"**Member:** {user.mention}\n"
            f"**Reason:** {reason}\n"
            "**Source:** Dashboard"
        ),
        discord.Color.orange()
    )

    return True, "Member warned successfully."


# ============================================================
# GIVEAWAY COROUTINE
# ============================================================

async def dashboard_create_giveaway(
    guild,
    channel,
    prize,
    duration,
    winners,
    host
):

    seconds = duration_parser(
        duration
    )

    if (
        seconds is None
        or seconds < 60
        or seconds > 365 * 86400
    ):

        return False, "Invalid duration."

    if winners < 1 or winners > 100:

        return False, "Winners must be between 1 and 100."

    data = {

        "guild_id":
            guild.id,

        "channel_id":
            channel.id,

        "prize":
            prize,

        "winners":
            winners,

        "host_id":
            guild.me.id,

        "host_text":
            host,

        "description":
            None,

        "image":
            None,

        "thumbnail":
            None,

        "required_role_id":
            None,

        "blacklisted_role_id":
            None,

        "extra_role_id":
            None,

        "extra_entries":
            0,

        "min_account_age_days":
            0,

        "min_server_days":
            0,

        "winner_dm":
            None,

        "entry_confirmation":
            "You entered the giveaway! 🎉",

        "entries":
            [],

        "winners_selected":
            [],

        "end_time":
            datetime.now(
                timezone.utc
            ).timestamp() + seconds,

        "ended":
            False,

        "paused":
            False,

        "duration":
            duration,
    }

    message = await channel.send(
        embed=create_giveaway_embed(
            data
        ),
        view=GiveawayView()
    )

    db["giveaways"][
        str(message.id)
    ] = data

    save_database()

    start_giveaway_task(
        message.id
    )

    await send_log(
        guild,
        "🎉 Giveaway Started",
        (
            f"**Prize:** {prize}\n"
            f"**Winners:** {winners}\n"
            f"**Duration:** {duration}\n"
            "**Source:** Dashboard"
        ),
        discord.Color.gold()
    )

    return True, (
        f"Giveaway created in "
        f"{channel.mention}."
    )


# ============================================================
# DASHBOARD HANDLER
# ============================================================

class DashboardHandler(
    BaseHTTPRequestHandler
):

    def _auth_ok(self):

        query = parse_qs(
            urlparse(
                self.path
            ).query
        )

        return (
            query.get(
                "key",
                [None]
            )[0]
            == DASHBOARD_PASSWORD
        )

    def _send(
        self,
        status,
        body,
        content_type="text/html; charset=utf-8"
    ):

        data = body.encode(
            "utf-8"
        )

        self.send_response(
            status
        )

        self.send_header(
            "Content-Type",
            content_type
        )

        self.send_header(
            "Content-Length",
            str(len(data))
        )

        self.end_headers()

        self.wfile.write(
            data
        )

    def _redirect(
        self,
        location
    ):

        self.send_response(
            302
        )

        self.send_header(
            "Location",
            location
        )

        self.end_headers()

    # --------------------------------------------------------
    # GET
    # --------------------------------------------------------

    def do_GET(self):

        path = urlparse(
            self.path
        ).path

        # UptimeRobot
        if path == "/health":

            return self._send(
                200,
                "OK",
                "text/plain"
            )

        if not self._auth_ok():

            return self._send(
                401,
                "Unauthorized. Add ?key=YOUR_DASHBOARD_PASSWORD"
            )

        query = parse_qs(
            urlparse(
                self.path
            ).query
        )

        guild_id = (
            query.get(
                "guild_id",
                [None]
            )[0]
        )

        guild = (
            dashboard_guild_from_id(
                guild_id
            )
            if guild_id
            else (
                bot.guilds[0]
                if bot.guilds
                else None
            )
        )

        # ====================================================
        # OVERVIEW
        # ====================================================

        if path in (
            "/",
            "/dashboard"
        ):

            guild_count = len(
                bot.guilds
            )

            total_members = sum(
                guild.member_count or 0
                for guild in bot.guilds
            )

            total_giveaways = sum(
                len(data)
                for data in db.get(
                    "giveaways",
                    {}
                ).values()
                if isinstance(
                    data,
                    dict
                )
            )

            total_tickets = sum(
                len(data)
                for data in db.get(
                    "tickets",
                    {}
                ).values()
                if isinstance(
                    data,
                    dict
                )
            )

            total_responders = sum(
                len(data)
                for data in db.get(
                    "autoresponders",
                    {}
                ).values()
                if isinstance(
                    data,
                    dict
                )
            )

            selected = (
                guild.id
                if guild
                else ""
            )

            body = f"""

<div class="topbar">

<div>

<div class="page-title">
Dashboard
</div>

<div class="page-subtitle">
Manage Visto Bot from one place.
</div>

</div>

<select class="server-select"
onchange="location='/?key={html.escape(DASHBOARD_PASSWORD)}&guild_id='+this.value">

{dashboard_guild_select(selected)}

</select>

</div>

<div class="stats-grid">

<div class="stat-card">
<div class="stat-label">Servers</div>
<div class="stat-value">
{guild_count}
</div>
</div>

<div class="stat-card">
<div class="stat-label">Members</div>
<div class="stat-value">
{total_members:,}
</div>
</div>

<div class="stat-card">
<div class="stat-label">Giveaways</div>
<div class="stat-value">
{total_giveaways}
</div>
</div>

<div class="stat-card">
<div class="stat-label">Tickets</div>
<div class="stat-value">
{total_tickets}
</div>
</div>

<div class="stat-card">
<div class="stat-label">Autoresponders</div>
<div class="stat-value">
{total_responders}
</div>
</div>

</div>

<div class="grid">

<div class="card">

<h2>⚡ Quick Actions</h2>

<p class="muted">
Use the sections on the left to manage your server.
</p>

<a class="btn"
href="/giveaways?key={html.escape(DASHBOARD_PASSWORD)}&guild_id={selected}">
🎉 Create Giveaway
</a>

<a class="btn btn-secondary"
href="/moderation?key={html.escape(DASHBOARD_PASSWORD)}&guild_id={selected}">
🛡️ Moderation
</a>

</div>

<div class="card">

<h2>🟢 Bot Status</h2>

<p>
<strong>Online:</strong>
✅
</p>

<p>
<strong>Bot:</strong>
Visto Bot
</p>

<p>
<strong>Connected Guilds:</strong>
{guild_count}
</p>

<p>
<strong>Health:</strong>
<a href="/health">OK</a>
</p>

</div>

</div>
"""

            return self._send(
                200,
                dashboard_page(
                    "Dashboard",
                    body,
                    selected
                )
            )

        # ====================================================
        # SETTINGS
        # ====================================================

        if path == "/settings":

            if guild is None:

                return self._send(
                    400,
                    "No guild selected."
                )

            settings = get_guild_data(
                "settings",
                guild.id
            )

            body = f"""

<div class="topbar">

<div>

<div class="page-title">
Server Settings
</div>

<div class="page-subtitle">
Configure Visto Bot for {html.escape(guild.name)}
</div>

</div>

</div>

<div class="card">

<form method="POST"
action="/settings?key={html.escape(DASHBOARD_PASSWORD)}">

<input
type="hidden"
name="guild_id"
value="{guild.id}"
>

<label>
General Message Channel ID
</label>

<input
name="general_channel"
value="{settings.get('general_channel', GENERAL_CHANNEL_ID)}"
>

<label>
Log Channel ID
</label>

<input
name="log_channel"
value="{settings.get('log_channel', '')}"
>

<label>
Buy Ticket Category ID
</label>

<input
name="buy_category"
value="{settings.get('ticket_buy_category', BUY_CATEGORY_ID)}"
>

<label>
Claim Ticket Category ID
</label>

<input
name="claim_category"
value="{settings.get('ticket_claim_category', CLAIM_CATEGORY_ID)}"
>

<label>
Support Ticket Category ID
</label>

<input
name="support_category"
value="{settings.get('ticket_support_category', SUPPORT_CATEGORY_ID)}"
>

<label>
Ticket Staff Role ID
</label>

<input
name="staff_role"
value="{settings.get('ticket_staff_role', TICKET_STAFF_ROLE_ID)}"
>

<button
class="btn-success">
Save Settings
</button>

</form>

</div>

"""

            return self._send(
                200,
                dashboard_page(
                    "Server Settings",
                    body,
                    guild.id
                )
            )

        # ====================================================
        # MODERATION
        # ====================================================

        if path == "/moderation":

            if guild is None:

                return self._send(
                    400,
                    "No guild selected."
                )

            members = sorted(
                guild.members,
                key=lambda m:
                    m.display_name.lower()
            )

            member_options = "".join(
                f'<option value="{m.id}">'
                f'{html.escape(m.display_name)}'
                f' ({m.id})'
                f'</option>'
                for m in members
                if not m.bot
            )

            body = f"""

<div class="topbar">

<div>

<div class="page-title">
Moderation
</div>

<div class="page-subtitle">
Manage members without opening Discord.
</div>

</div>

</div>

<div class="grid">

<div class="card">

<h2>🔨 Ban</h2>

<form method="POST"
action="/moderation?key={html.escape(DASHBOARD_PASSWORD)}">

<input
type="hidden"
name="guild_id"
value="{guild.id}"
>

<input
type="hidden"
name="action"
value="ban"
>

<label>
Member
</label>

<select name="user_id">
{member_options}
</select>

<label>
Reason
</label>

<textarea
name="reason"
required
placeholder="Reason for ban..."
></textarea>

<button class="btn-danger">
Ban Member
</button>

</form>

</div>

<div class="card">

<h2>👢 Kick</h2>

<form method="POST"
action="/moderation?key={html.escape(DASHBOARD_PASSWORD)}">

<input
type="hidden"
name="guild_id"
value="{guild.id}"
>

<input
type="hidden"
name="action"
value="kick"
>

<label>
Member
</label>

<select name="user_id">
{member_options}
</select>

<label>
Reason
</label>

<textarea
name="reason"
required
placeholder="Reason for kick..."
></textarea>

<button class="btn-danger">
Kick Member
</button>

</form>

</div>

<div class="card">

<h2>⚠️ Warn</h2>

<form method="POST"
action="/moderation?key={html.escape(DASHBOARD_PASSWORD)}">

<input
type="hidden"
name="guild_id"
value="{guild.id}"
>

<input
type="hidden"
name="action"
value="warn"
>

<label>
Member
</label>

<select name="user_id">
{member_options}
</select>

<label>
Reason
</label>

<textarea
name="reason"
required
placeholder="Warning reason..."
></textarea>

<button>
Warn Member
</button>

</form>

</div>

</div>
"""

            return self._send(
                200,
                dashboard_page(
                    "Moderation",
                    body,
                    guild.id
                )
            )

        # ====================================================
        # GIVEAWAYS
        # ====================================================

        if path == "/giveaways":

            if guild is None:

                return self._send(
                    400,
                    "No guild selected."
                )

            channels = "".join(
                f'<option value="{c.id}">'
                f'#{html.escape(c.name)}'
                f'</option>'
                for c in guild.text_channels
            )

            giveaway_rows = []

            for message_id, data in db.get(
                "giveaways",
                {}
            ).items():

                if int(
                    data.get(
                        "guild_id",
                        0
                    )
                ) != guild.id:

                    continue

                status = (
                    "Ended"
                    if data.get("ended")
                    else "Active"
                )

                giveaway_rows.append(
                    f"""
<tr>

<td>
{html.escape(
    str(data.get("prize", "Unknown"))
)}
</td>

<td>
{data.get("winners", 0)}
</td>

<td>
{status}
</td>

<td>
<code>{message_id}</code>
</td>

</tr>
"""
                )

            body = f"""

<div class="topbar">

<div>

<div class="page-title">
Giveaways
</div>

<div class="page-subtitle">
Create and manage giveaways from Visto Dashboard.
</div>

</div>

</div>

<div class="card">

<h2>🎉 Create Giveaway</h2>

<form method="POST"
action="/giveaway?key={html.escape(DASHBOARD_PASSWORD)}">

<input
type="hidden"
name="guild_id"
value="{guild.id}"
>

<label>
Prize
</label>

<input
name="prize"
required
placeholder="Nitro, Robux, Money, etc."
>

<label>
Duration
</label>

<input
name="duration"
required
placeholder="2h 30m"
>

<label>
Winners
</label>

<input
type="number"
name="winners"
value="1"
min="1"
max="100"
required
>

<label>
Giveaway Channel
</label>

<select name="channel_id">
{channels}
</select>

<label>
Host
</label>

<input
name="host"
placeholder="Giveaway Creator"
>

<button
class="btn-success">
🎉 Start Giveaway
</button>

</form>

</div>

<div class="card">

<h2>📋 Existing Giveaways</h2>

<table>

<thead>

<tr>
<th>Prize</th>
<th>Winners</th>
<th>Status</th>
<th>Message ID</th>
</tr>

</thead>

<tbody>

{"".join(giveaway_rows) or
"<tr><td colspan='4'>No giveaways yet.</td></tr>"}

</tbody>

</table>

<hr>

<form method="POST"
action="/giveaway/manage?key={html.escape(DASHBOARD_PASSWORD)}">

<input
type="hidden"
name="guild_id"
value="{guild.id}"
>

<label>
Message ID
</label>

<input
name="message_id"
required
placeholder="Giveaway message ID"
>

<label>
Action
</label>

<select name="action">

<option value="end">
End Giveaway
</option>

<option value="reroll">
Reroll Winners
</option>

<option value="delete">
Delete Giveaway Data
</option>

</select>

<button>
Apply
</button>

</form>

</div>

"""

            return self._send(
                200,
                dashboard_page(
                    "Giveaways",
                    body,
                    guild.id
                )
            )

        # ====================================================
        # TICKETS
        # ====================================================

        if path == "/tickets":

            if guild is None:

                return self._send(
                    400,
                    "No guild selected."
                )

            tickets = db.get(
                "tickets",
                {}
            ).get(
                str(guild.id),
                {}
            )

            rows = []

            for channel_id, ticket in tickets.items():

                rows.append(
                    f"""
<tr>

<td>
<code>{channel_id}</code>
</td>

<td>
{html.escape(
    str(ticket.get("type", "support"))
)}
</td>

<td>
{
    "Closed"
    if ticket.get("closed")
    else "Open"
}
</td>

<td>
{ticket.get("owner_id", "Unknown")}
</td>

</tr>
"""
                )

            body = f"""

<div class="topbar">

<div>

<div class="page-title">
Tickets
</div>

<div class="page-subtitle">
Monitor your Buy, Claim and Support tickets.
</div>

</div>

</div>

<div class="card">

<table>

<thead>

<tr>
<th>Channel</th>
<th>Type</th>
<th>Status</th>
<th>Owner</th>
</tr>

</thead>

<tbody>

{"".join(rows) or
"<tr><td colspan='4'>No tickets recorded.</td></tr>"}

</tbody>

</table>

</div>

"""

            return self._send(
                200,
                dashboard_page(
                    "Tickets",
                    body,
                    guild.id
                )
            )

        # ====================================================
        # AUTORESPONDERS
        # ====================================================

        if path == "/autoresponders":

            if guild is None:

                return self._send(
                    400,
                    "No guild selected."
                )

            responders = get_guild_data(
                "autoresponders",
                guild.id
            )

            rows = "".join(
                f"""
<tr>
<td>{html.escape(trigger)}</td>
<td>{html.escape(response)}</td>
</tr>
"""
                for trigger, response
                in responders.items()
            )

            body = f"""

<div class="topbar">

<div>

<div class="page-title">
Autoresponders
</div>

<div class="page-subtitle">
Create automatic responses without commands.
</div>

</div>

</div>

<div class="card">

<form method="POST"
action="/autoresponder?key={html.escape(DASHBOARD_PASSWORD)}">

<input
type="hidden"
name="guild_id"
value="{guild.id}"
>

<label>
Trigger
</label>

<input
name="trigger"
required
placeholder="hello"
>

<label>
Response
</label>

<textarea
name="response"
required
placeholder="Hey! 👋"
></textarea>

<button class="btn-success">
Add Autoresponder
</button>

</form>

<hr>

<table>

<thead>
<tr>
<th>Trigger</th>
<th>Response</th>
</tr>
</thead>

<tbody>

{rows or
"<tr><td colspan='2'>None configured.</td></tr>"}

</tbody>

</table>

</div>

"""

            return self._send(
                200,
                dashboard_page(
                    "Autoresponders",
                    body,
                    guild.id
                )
            )

        # ====================================================
        # STATISTICS
        # ====================================================

        if path == "/statistics":

            if guild is None:

                return self._send(
                    400,
                    "No guild selected."
                )

            messages = get_guild_data(
                "messages",
                guild.id
            )

            invites = get_guild_data(
                "invites",
                guild.id
            )

            total_messages = sum(
                int(v)
                for v in messages.values()
            )

            total_invites = sum(
                int(
                    get_invite_stats(
                        guild.id,
                        uid
                    ).get(
                        "total",
                        0
                    )
                )
                for uid in invites
            )

            body = f"""

<div class="topbar">

<div>

<div class="page-title">
Statistics
</div>

<div class="page-subtitle">
Live statistics for {html.escape(guild.name)}
</div>

</div>

</div>

<div class="stats-grid">

<div class="stat-card">
<div class="stat-label">
Total Messages
</div>

<div class="stat-value">
{total_messages:,}
</div>
</div>

<div class="stat-card">
<div class="stat-label">
Active Invites
</div>

<div class="stat-value">
{total_invites:,}
</div>
</div>

<div class="stat-card">
<div class="stat-label">
Tracked Members
</div>

<div class="stat-value">
{len(messages):,}
</div>
</div>

</div>

<div class="card">

<h2>📌 Message Channel</h2>

<p class="muted">

Only the configured General channel should contribute
to message statistics.

</p>

<p>

<strong>ID:</strong>

<code>
{GENERAL_CHANNEL_ID}
</code>

</p>

</div>

"""

            return self._send(
                200,
                dashboard_page(
                    "Statistics",
                    body,
                    guild.id
                )
            )

        return self._send(
            404,
            "Page not found."
        )

    # --------------------------------------------------------
    # POST
    # --------------------------------------------------------

    def do_POST(self):

        if not self._auth_ok():

            return self._send(
                401,
                "Unauthorized"
            )

        length = int(
            self.headers.get(
                "Content-Length",
                "0"
            )
        )

        raw = self.rfile.read(
            length
        ).decode(
            "utf-8"
        )

        data = {
            key: values[0]
            for key, values
            in parse_qs(raw).items()
            if values
        }

        path = urlparse(
            self.path
        ).path

        try:

            guild = dashboard_guild_from_id(
                data.get(
                    "guild_id"
                )
            )

            if guild is None:

                return self._send(
                    400,
                    "Invalid guild."
                )

            # =================================================
            # SETTINGS
            # =================================================

            if path == "/settings":

                dashboard_backup_database()

                settings = get_guild_data(
                    "settings",
                    guild.id
                )

                mapping = {

                    "general_channel":
                        "general_channel",

                    "log_channel":
                        "log_channel",

                    "buy_category":
                        "ticket_buy_category",

                    "claim_category":
                        "ticket_claim_category",

                    "support_category":
                        "ticket_support_category",

                    "staff_role":
                        "ticket_staff_role",
                }

                for form_key, db_key in mapping.items():

                    value = (
                        data.get(
                            form_key,
                            ""
                        ).strip()
                    )

                    if not value:
                        continue

                    settings[db_key] = int(
                        value
                    )

                save_database()

                return self._send(
                    200,
                    dashboard_page(
                        "Saved",
                        """
<div class="card">

<h2>✅ Settings Saved</h2>

<p>
Your server settings were updated.
Your existing bot data was not reset.
</p>

<a class="btn"
href="?key=__KEY__">
Back to Dashboard
</a>

</div>
""".replace(
                            "__KEY__",
                            html.escape(
                                DASHBOARD_PASSWORD
                            )
                        )
                    )
                )

            # =================================================
            # MODERATION
            # =================================================

            if path == "/moderation":

                action = data.get(
                    "action"
                )

                user_id = data.get(
                    "user_id"
                )

                reason = (
                    data.get(
                        "reason"
                    )
                    or
                    "No reason provided"
                )

                if action == "ban":

                    success, message = dashboard_run(
                        dashboard_ban(
                            guild,
                            user_id,
                            reason
                        )
                    )

                elif action == "kick":

                    success, message = dashboard_run(
                        dashboard_kick(
                            guild,
                            user_id,
                            reason
                        )
                    )

                elif action == "warn":

                    success, message = dashboard_run(
                        dashboard_warn(
                            guild,
                            user_id,
                            reason,
                            guild.owner_id
                        )
                    )

                else:

                    return self._send(
                        400,
                        "Unknown moderation action."
                    )

                color = (
                    "✅"
                    if success
                    else
                    "❌"
                )

                return self._send(
                    200,
                    dashboard_page(
                        "Moderation Result",
                        f"""
<div class="card">

<h2>
{color} Moderation Result
</h2>

<p>
{html.escape(message)}
</p>

<a class="btn"
href="/moderation?key={html.escape(DASHBOARD_PASSWORD)}&guild_id={guild.id}">
Back to Moderation
</a>

</div>
"""
                    )
                )

            # =================================================
            # GIVEAWAY
            # =================================================

            if path == "/giveaway":

                channel = guild.get_channel(
                    int(
                        data.get(
                            "channel_id"
                        )
                    )
                )

                if not isinstance(
                    channel,
                    discord.TextChannel
                ):

                    return self._send(
                        400,
                        "Invalid giveaway channel."
                    )

                prize = data.get(
                    "prize",
                    ""
                ).strip()

                duration = data.get(
                    "duration",
                    ""
                ).strip()

                try:

                    winners = int(
                        data.get(
                            "winners",
                            "1"
                        )
                    )

                except ValueError:

                    return self._send(
                        400,
                        "Invalid winners amount."
                    )

                host = (
                    data.get(
                        "host"
                    )
                    or
                    "Giveaway Creator"
                )

                success, message = dashboard_run(

                    dashboard_create_giveaway(

                        guild,
                        channel,
                        prize,
                        duration,
                        winners,
                        host
                    )
                )

                return self._send(
                    200,
                    dashboard_page(
                        "Giveaway Result",
                        f"""
<div class="card">

<h2>
{"🎉" if success else "❌"}
Giveaway Result
</h2>

<p>
{html.escape(message)}
</p>

<a class="btn"
href="/giveaways?key={html.escape(DASHBOARD_PASSWORD)}&guild_id={guild.id}">
Back to Giveaways
</a>

</div>
"""
                    )
                )

            # =================================================
            # GIVEAWAY MANAGEMENT
            # =================================================

            if path == "/giveaway/manage":

                message_id = str(
                    data.get(
                        "message_id"
                    )
                )

                action = data.get(
                    "action"
                )

                giveaway = db[
                    "giveaways"
                ].get(
                    message_id
                )

                if not giveaway:

                    return self._send(
                        404,
                        "Giveaway not found."
                    )

                if action == "end":

                    dashboard_run(
                        finish_giveaway(
                            message_id
                        )
                    )

                elif action == "reroll":

                    entries = list(
                        dict.fromkeys(
                            giveaway.get(
                                "entries",
                                []
                            )
                        )
                    )

                    if entries:

                        winners = random.sample(

                            entries,

                            min(
                                int(
                                    giveaway[
                                        "winners"
                                    ]
                                ),
                                len(entries)
                            )
                        )

                        giveaway[
                            "winners_selected"
                        ] = winners

                        save_database()

                elif action == "delete":

                    db[
                        "giveaways"
                    ].pop(
                        message_id,
                        None
                    )

                    save_database()

                return self._redirect(
                    f"/giveaways?key="
                    f"{DASHBOARD_PASSWORD}"
                    f"&guild_id={guild.id}"
                )

            # =================================================
            # AUTORESPONDER
            # =================================================

            if path == "/autoresponder":

                dashboard_backup_database()

                trigger = (
                    data.get(
                        "trigger",
                        ""
                    )
                    .strip()
                    .lower()
                )

                response = (
                    data.get(
                        "response",
                        ""
                    )
                    .strip()
                )

                if not trigger or not response:

                    return self._send(
                        400,
                        "Trigger and response are required."
                    )

                responders = get_guild_data(
                    "autoresponders",
                    guild.id
                )

                responders[
                    trigger
                ] = response

                save_database()

                return self._redirect(
                    f"/autoresponders?key="
                    f"{DASHBOARD_PASSWORD}"
                    f"&guild_id={guild.id}"
                )

            return self._send(
                404,
                "Unknown dashboard action."
            )

        except Exception as error:

            print(
                f"Dashboard POST error: {error}"
            )

            return self._send(
                500,
                f"Dashboard error: {html.escape(str(error))}"
            )

    def log_message(
        self,
        format,
        *args
    ):
        return


# ============================================================
# START DASHBOARD
# ============================================================

def start_dashboard():

    global _dashboard_server

    if _dashboard_server is not None:
        return

    try:

        _dashboard_server = ThreadingHTTPServer(

            (
                DASHBOARD_HOST,
                DASHBOARD_PORT
            ),

            DashboardHandler
        )

        thread = threading.Thread(
            target=_dashboard_server.serve_forever,
            daemon=True
        )

        thread.start()

        print(
            f"Visto Dashboard running on "
            f"port {DASHBOARD_PORT}"
        )

    except Exception as error:

        print(
            f"Dashboard failed to start: {error}"
        )

# ============================================================
# COMMAND ERRORS
# ============================================================

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        return await ctx.send(embed=error_embed("Permission Denied", "You don't have permission to use this command."))
    if isinstance(error, commands.MissingRequiredArgument):
        return await ctx.send(embed=error_embed("Missing Argument", "You're missing a required argument."))
    print(f"Prefix command error: {error}")


@bot.tree.error
async def on_app_command_error(interaction, error):
    print(f"Slash command error: {error}")
    if isinstance(error, app_commands.errors.MissingPermissions):
        embed = error_embed("Permission Denied", "You don't have permission to use this command.")
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)


# ============================================================
# READY
# ============================================================

@bot.event
async def on_ready():
    print("=" * 60)
    print(f"Visto connected as {bot.user}")
    print(f"Guilds: {len(bot.guilds)}")
    print("=" * 60)
    try:
        # Sync to the configured guild immediately so new slash commands
        # like /delwarn and advanced /giveaway options appear without
        # waiting for a global-command propagation delay.
        if GUILD_ID:
            guild_obj = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild_obj)
            synced = await bot.tree.sync(guild=guild_obj)
            print(f"Synced {len(synced)} slash commands to guild {GUILD_ID}.")
        else:
            synced = await bot.tree.sync()
            print(f"Synced {len(synced)} global slash commands.")
    except Exception as error:
        print(f"Slash sync error: {error}")

    await cache_all_invites()
    bot.add_view(TicketPanelView())
    bot.add_view(TicketControlsView())
    bot.add_view(ClosedTicketView())
    bot.add_view(GiveawayView())
    start_dashboard()
    await bot.change_presence(activity=discord.Game(name=".help • Visto"))


# ============================================================
# START
# ============================================================

async def main():
    await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
