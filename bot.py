import os
import json
import io
import random
import asyncio
import time
import math
import subprocess
import discord
from discord.ext import commands
from dotenv import load_dotenv
from datetime import timedelta

# Load token and IDs from .env file
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
BOT_OWNER_ID = os.getenv("BOT_OWNER_ID")  # YOUR personal Discord User ID — works across every server
WEBHOOK_TRIGGER_CHANNEL_ID = os.getenv("WEBHOOK_TRIGGER_CHANNEL_ID")  # channel the website's webhook posts into
WEBSITE_REPO_PATH = os.getenv("WEBSITE_REPO_PATH")  # local path to your GitHub Pages repo, e.g. /home/girikchaos/girikchaos-lab.github.io

# Level tier role names — the bot auto-creates these in every server it joins
LEVEL_ROLE_NAMES = {
    10: "Rookie",
    20: "Novice",
    30: "Rising",
    40: "Skilled",
    50: "Veteran",
    60: "Expert",
    70: "Elite",
    80: "Master",
    90: "Grandmaster",
    100: "Legend",
}

GUILD_LEVEL_ROLES_FILE = "guild_level_roles.json"


def load_guild_level_roles():
    """Load per-server level-role IDs from the JSON file, or return an empty dict if it doesn't exist or is unreadable."""
    if os.path.exists(GUILD_LEVEL_ROLES_FILE):
        with open(GUILD_LEVEL_ROLES_FILE, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}


def save_guild_level_roles(data):
    """Save the per-server level-role IDs back to the JSON file."""
    with open(GUILD_LEVEL_ROLES_FILE, "w") as f:
        json.dump(data, f, indent=4)


async def get_or_create_level_role(guild, threshold):
    """Gets (or creates) just ONE level-tier role, right when a member actually first reaches it.
    Deliberately lazy — never creates more than one role at a time, so there's no burst of role
    creations for anti-nuke/security bots to flag (this is what got us kicked from a server before)."""
    name = LEVEL_ROLE_NAMES[threshold]
    data = load_guild_level_roles()
    guild_id = str(guild.id)
    guild_roles = data.setdefault(guild_id, {})
    key = str(threshold)

    existing_id = guild_roles.get(key)
    role = guild.get_role(int(existing_id)) if existing_id else None
    if role is not None:
        return role

    # Reuse a role with the matching name if one already exists (avoids duplicates)
    role = discord.utils.get(guild.roles, name=name)
    if role is None:
        try:
            role = await guild.create_role(name=name, reason="Auto-created leveling tier role (on first use)")
            print(f"✅ Created role '{name}' in {guild.name}")
        except discord.Forbidden:
            print(f"⚠️ Missing permission to create role '{name}' in {guild.name}")
            return None

    guild_roles[key] = str(role.id)
    save_guild_level_roles(data)
    return role

# Shop items — 100 cosmetic color roles across 6 tiers, purchasable with XP. Bot auto-creates them per-server, same pattern as level roles.
# Each tuple is (key, display name, hex color). Tier controls price and the emoji shown.
SHOP_TIERS = {
    "common":    {"emoji": "⚪", "price": 800,   "count": 15},
    "uncommon":  {"emoji": "🔵", "price": 2000,  "count": 20},
    "rare":      {"emoji": "🟣", "price": 5000,  "count": 25},
    "epic":      {"emoji": "🟠", "price": 10000, "count": 20},
    "legendary": {"emoji": "🟡", "price": 20000, "count": 15},
    "mythic":    {"emoji": "🔴", "price": 40000, "count": 5},
}
# Tier counts sum to 100 — deliberately small. Discord caps every server at 250 roles TOTAL
# (including @everyone, the bot's own role, your level-tier roles, etc.), so 1000 was never
# actually possible — this leaves ~150 roles of headroom for everything else in the server.
# Each tier also has fixed saturation/lightness so mythic reads as vivid/rich and common reads
# as muted, while the hue still varies item to item.
_TIER_COLOR_PARAMS = {
    "common":    {"s": 0.45, "l": 0.55},
    "uncommon":  {"s": 0.55, "l": 0.50},
    "rare":      {"s": 0.65, "l": 0.50},
    "epic":      {"s": 0.75, "l": 0.48},
    "legendary": {"s": 0.85, "l": 0.45},
    "mythic":    {"s": 0.95, "l": 0.42},
}

# 40 adjectives x 26 color nouns = 1040 guaranteed-unique combinations — plenty for 1000 items,
# generated in a fixed order so the same 1000 names/colors come out identical on every restart.
_SHOP_ADJECTIVES = [
    "Slate", "Steel", "Sea", "Dusky", "Faded", "Pale", "Bright", "Deep", "Dark", "Light",
    "Royal", "Hot", "Warm", "Cool", "Frosted", "Molten", "Burnt", "Rich", "Vivid", "Muted",
    "Dull", "Glowing", "Radiant", "Shining", "Electric", "Neon", "Cyber", "Toxic", "Blazing", "Storming",
    "Frozen", "Ancient", "Mystic", "Sacred", "Cursed", "Divine", "Infernal", "Celestial", "Twilight", "Dawn",
]
_SHOP_NOUNS = [
    "Gray", "Blue", "Green", "Pink", "Purple", "Red", "Orange", "Yellow", "White", "Black",
    "Violet", "Cyan", "Magenta", "Silver", "Gold", "Bronze", "Copper", "Teal", "Coral", "Indigo",
    "Scarlet", "Emerald", "Sapphire", "Amber", "Jade", "Onyx",
]

import colorsys as _colorsys

SHOP_ITEMS = {}
_name_pairs = [(a, n) for a in _SHOP_ADJECTIVES for n in _SHOP_NOUNS]
_pair_index = 0
_global_index = 0
for _tier, _tier_data in SHOP_TIERS.items():
    _params = _TIER_COLOR_PARAMS[_tier]
    for _ in range(_tier_data["count"]):
        _adj, _noun = _name_pairs[_pair_index]
        _pair_index += 1
        _display_name = f"{_adj} {_noun}"
        _key = _display_name.lower().replace(" ", "_")

        # Golden-angle hue step gives a well-spread, deterministic, non-repeating color sequence.
        _hue = (_global_index * 137.508) % 360
        _r, _g, _b = _colorsys.hls_to_rgb(_hue / 360, _params["l"], _params["s"])
        _hex_color = (int(_r * 255) << 16) + (int(_g * 255) << 8) + int(_b * 255)

        SHOP_ITEMS[_key] = {
            "name": f"{_tier_data['emoji']} {_display_name}",
            "price": _tier_data["price"],
            "color": discord.Color(_hex_color),
            "tier": _tier,
        }
        _global_index += 1

SHOP_PURCHASES_FILE = "shop_purchases.json"


def load_shop_purchases():
    """Load per-guild shop purchase records, or return an empty dict if it doesn't exist or is unreadable."""
    if os.path.exists(SHOP_PURCHASES_FILE):
        with open(SHOP_PURCHASES_FILE, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}


def save_shop_purchases(data):
    """Save the shop purchases dict back to the JSON file."""
    with open(SHOP_PURCHASES_FILE, "w") as f:
        json.dump(data, f, indent=4)


async def get_or_create_shop_role(guild, item_key):
    """Gets this shop item's role in the guild, creating it if it doesn't exist yet.
    Returns (role, error_reason). role is None on failure; error_reason explains why."""
    item = SHOP_ITEMS[item_key]
    role = discord.utils.get(guild.roles, name=item["name"])
    if role is not None:
        return role, None

    if len(guild.roles) >= 250:
        return None, "role_limit"  # Discord's hard server-wide cap — nothing we can do but free up roles

    try:
        role = await guild.create_role(name=item["name"], color=item["color"], reason="Auto-created shop item role")
        return role, None
    except discord.Forbidden:
        return None, "permissions"
    except discord.HTTPException:
        return None, "role_limit"


async def get_or_create_member_role(guild):
    """Gets this server's 'Member' role, creating it if it doesn't exist yet. No .env config needed —
    every server gets its own auto-managed Member role the first time someone joins."""
    role = discord.utils.get(guild.roles, name="Member")
    if role is not None:
        return role

    try:
        role = await guild.create_role(
            name="Member",
            color=discord.Color.light_grey(),
            reason="Auto-created default member role",
        )
        return role
    except discord.Forbidden:
        print(f"⚠️ No permission to create the 'Member' role in {guild.name}")
        return None
    except discord.HTTPException as e:
        print(f"⚠️ Couldn't create the 'Member' role in {guild.name}: {e}")
        return None


WELCOME_CHANNEL_FILE = "welcome_channels.json"


def load_welcome_channels():
    """Returns {guild_id_str: channel_id_str}, or {} if the file doesn't exist/is unreadable."""
    if os.path.exists(WELCOME_CHANNEL_FILE):
        with open(WELCOME_CHANNEL_FILE, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}


def save_welcome_channels(data):
    with open(WELCOME_CHANNEL_FILE, "w") as f:
        json.dump(data, f, indent=4)


WARNINGS_FILE = "warnings.json"


def load_warnings():
    """Load warnings from the JSON file, or return an empty dict if it doesn't exist or is unreadable."""
    if os.path.exists(WARNINGS_FILE):
        with open(WARNINGS_FILE, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}


def save_warnings(data):
    """Save the warnings dict back to the JSON file."""
    with open(WARNINGS_FILE, "w") as f:
        json.dump(data, f, indent=4)


LEVELS_FILE = "levels.json"
XP_COOLDOWN_SECONDS = 60  # how often a user can earn XP
_last_xp_time = {}  # in-memory cooldown tracker: (guild_id, user_id) -> last timestamp


def get_guild_bucket(data, guild_id):
    """Returns (creating if needed) the per-server sub-dictionary within a top-level JSON data dict.
    Used to keep levels/streaks/investments completely separate between servers."""
    return data.setdefault(str(guild_id), {})


def load_levels():
    """Load leveling data from the JSON file, or return an empty dict if it doesn't exist or is unreadable."""
    if os.path.exists(LEVELS_FILE):
        with open(LEVELS_FILE, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}


def save_levels(data):
    """Save the leveling dict back to the JSON file."""
    with open(LEVELS_FILE, "w") as f:
        json.dump(data, f, indent=4)


def xp_needed_for(level):
    """XP required to go from `level` to `level + 1`. Increases each level."""
    return 100 + (level * 50)


def cumulative_xp_for_level(level):
    """Total XP required to reach `level` starting from level 0. Closed-form — instant even for huge levels."""
    return 25 * level * (level + 3)


def level_from_total_xp(total_xp):
    """Converts a total cumulative XP amount into (level, remaining_xp). Uses math instead of looping,
    so it stays instant even for absurdly large numbers (no risk of freezing the bot)."""
    if total_xp < 0:
        total_xp = 0

    # Solve 25*L^2 + 75*L - total_xp = 0 for L using the quadratic formula,
    # with math.isqrt for exact integer precision (works fine even on huge numbers).
    discriminant = 75 * 75 + 100 * total_xp
    sqrt_disc = math.isqrt(discriminant)
    level = (sqrt_disc - 75) // 50
    if level < 0:
        level = 0

    # Tiny correction in case integer rounding put us off by one (only ever a couple of steps)
    while cumulative_xp_for_level(level + 1) <= total_xp:
        level += 1
    while level > 0 and cumulative_xp_for_level(level) > total_xp:
        level -= 1

    remaining = total_xp - cumulative_xp_for_level(level)
    return level, remaining


def apply_xp_change(levels, user_id, delta):
    """Applies a +/- XP change to a user's record using closed-form math. Returns (old_level, new_level).
    Safe for any size number — won't freeze the bot even with absurdly large amounts."""
    if user_id not in levels:
        levels[user_id] = {"xp": 0, "level": 0}

    old_level = levels[user_id]["level"]
    current_total = cumulative_xp_for_level(old_level) + levels[user_id]["xp"]
    new_total = current_total + delta
    if new_total < 0:
        new_total = 0

    new_level, new_xp = level_from_total_xp(new_total)
    levels[user_id]["level"] = new_level
    levels[user_id]["xp"] = new_xp

    return old_level, new_level


def get_total_xp(levels, user_id):
    """Returns a user's total cumulative XP (level + progress combined) as one number."""
    data = levels.get(user_id, {"xp": 0, "level": 0})
    return cumulative_xp_for_level(data["level"]) + data["xp"]


def _update_stats_file_and_push(server_count):
    """Runs in a background thread — writes stats.json and pushes it to GitHub automatically.
    Blocking (subprocess/file I/O), so this must never be called directly from bot event handlers —
    always go through update_website_stats() instead, which offloads this to a thread."""
    if not WEBSITE_REPO_PATH:
        return

    stats_path = os.path.join(WEBSITE_REPO_PATH, "stats.json")
    try:
        with open(stats_path, "w") as f:
            json.dump({"servers": server_count}, f, indent=2)

        subprocess.run(["git", "add", "stats.json"], cwd=WEBSITE_REPO_PATH, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", f"Auto-update server count to {server_count}"],
            cwd=WEBSITE_REPO_PATH, capture_output=True, text=True,
        )  # if there's nothing new to commit, this "fails" harmlessly — that's fine, just skip pushing
        subprocess.run(["git", "push"], cwd=WEBSITE_REPO_PATH, check=True, capture_output=True)
        print(f"✅ Website stats updated to {server_count} servers and pushed live")
    except subprocess.CalledProcessError as e:
        print(f"⚠️ Failed to auto-update website stats: {e}")


async def update_website_stats():
    """Updates the website's live server count — runs in a background thread so it never blocks the bot."""
    await asyncio.to_thread(_update_stats_file_and_push, len(bot.guilds))


BOT_SETTINGS_FILE = "bot_settings.json"


def load_bot_settings():
    """Load bot settings (like invite pause state) from the JSON file, defaulting invites to enabled."""
    if os.path.exists(BOT_SETTINGS_FILE):
        with open(BOT_SETTINGS_FILE, "r") as f:
            try:
                data = json.load(f)
                data.setdefault("invite_enabled", True)
                return data
            except json.JSONDecodeError:
                pass
    return {"invite_enabled": True}


def save_bot_settings(data):
    """Save bot settings back to the JSON file."""
    with open(BOT_SETTINGS_FILE, "w") as f:
        json.dump(data, f, indent=2)


STREAKS_FILE = "streaks.json"


def load_streaks():
    """Load win-streak data from the JSON file, or return an empty dict if it doesn't exist or is unreadable."""
    if os.path.exists(STREAKS_FILE):
        with open(STREAKS_FILE, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}


def save_streaks(data):
    """Save the streaks dict back to the JSON file."""
    with open(STREAKS_FILE, "w") as f:
        json.dump(data, f, indent=4)


async def award_win(ctx, member):
    """Call this when a player wins a game. Increases their streak and gives bonus XP for it."""
    if ctx.guild is None:
        return  # streaks/XP are per-server — nothing to track in a DM

    guild_id = str(ctx.guild.id)
    user_id = str(member.id)

    streaks = load_streaks()
    guild_streaks = get_guild_bucket(streaks, guild_id)
    guild_streaks[user_id] = guild_streaks.get(user_id, 0) + 1
    streak = guild_streaks[user_id]
    save_streaks(streaks)

    xp_reward = min(50 * streak, 500)  # 50 XP per streak level, capped at 500

    levels = load_levels()
    guild_levels = get_guild_bucket(levels, guild_id)
    old_level, new_level = apply_xp_change(guild_levels, user_id, xp_reward)
    save_levels(levels)

    await ctx.send(
        f"🔥 **Win streak: {streak}!** {member.mention} earned **+{xp_reward} XP**!"
    )

    if new_level > old_level:
        await ctx.send(f"🎉 {member.mention} leveled up to **Level {new_level}**!")
        await update_level_role(member, new_level)


async def reset_streak(ctx, member):
    """Call this when a player loses or ties a game. Resets their win streak back to 0 and announces it."""
    if ctx.guild is None:
        return

    streaks_all = load_streaks()
    get_guild_bucket(streaks_all, str(ctx.guild.id))[str(member.id)] = 0
    save_streaks(streaks_all)
    await ctx.send(f"💔 {member.mention}'s win streak has been reset to **0**.")


async def update_level_role(member, level):
    """Gives the member the correct tier role for their level, removing older tier roles.
    Creates that ONE role on the spot if it doesn't exist yet — nothing is pre-created in bulk."""
    # Find the highest threshold this level qualifies for, out of ALL possible tiers —
    # not just ones that happen to already exist in this server yet.
    eligible_threshold = None
    for threshold in sorted(LEVEL_ROLE_NAMES.keys()):
        if level >= threshold:
            eligible_threshold = threshold

    if eligible_threshold is None:
        return  # this level doesn't qualify for any tier yet

    target_role = await get_or_create_level_role(member.guild, eligible_threshold)
    if not target_role:
        return  # couldn't create it (missing permissions) — already logged inside the helper

    data = load_guild_level_roles()
    guild_roles = data.get(str(member.guild.id), {})

    # Remove any other tier roles the member currently holds
    roles_to_remove = []
    for threshold_str, role_id in guild_roles.items():
        if int(threshold_str) != eligible_threshold:
            other_role = member.guild.get_role(int(role_id))
            if other_role and other_role in member.roles:
                roles_to_remove.append(other_role)

    try:
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove)
        if target_role not in member.roles:
            await member.add_roles(target_role)
            print(f"✅ Gave {member} the '{target_role.name}' role (level {level})")
    except discord.Forbidden:
        print("⚠️ Bot doesn't have permission to manage level roles — check role position/permissions")

# Set up intents (permissions the bot needs to see certain events)
intents = discord.Intents.default()
intents.message_content = True  # Required to read message text for commands
intents.members = True  # Required to detect when members join/leave

# Create the bot with a command prefix (e.g. !hello)
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")
    # Re-register persistent ticket views so existing panel/buttons keep working after a restart
    bot.add_view(TicketPanelView())
    bot.add_view(TicketActionsView())
    # Sync the website's "trusted by X servers" count in case it drifted while offline
    await update_website_stats()
    # Register/refresh all slash ("/") versions of every command with Discord
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash commands")
    except discord.HTTPException as e:
        print(f"⚠️ Slash command sync failed: {e}")


@bot.event
async def on_guild_remove(guild):
    """Runs whenever the bot is removed from a server — keeps the website's server count accurate."""
    print(f"📤 Left a server: {guild.name}")
    await update_website_stats()


@bot.event
async def on_guild_join(guild):
    """Runs whenever the bot is added to a new server — level-tier roles are created lazily as
    members actually earn them (see get_or_create_level_role), never all at once on join.
    Unless invites are currently paused or this specific server is banned, in which case the bot leaves instead."""
    banned = load_banned_servers()
    if str(guild.id) in banned:
        print(f"🚫 {guild.name} is on the ban list — leaving immediately.")
        await guild.leave()
        return

    settings = load_bot_settings()

    if not settings.get("invite_enabled", True):
        print(f"🚫 Invites are paused — leaving {guild.name} immediately.")
        try:
            if guild.owner:
                await guild.owner.send(
                    "👋 Hey! Girik Chaos is temporarily paused for moderation right now, "
                    "so I can't join new servers at the moment. Try again a bit later!"
                )
        except discord.Forbidden:
            pass
        await guild.leave()
        return

    print(f"📥 Joined a new server: {guild.name}")
    await update_website_stats()


@bot.event
async def on_message(message):
    """Handles the website's auto-DM trigger, awards XP for chatting (with a cooldown), then still processes commands normally."""

    # Website "Owner" button triggers: a webhook posts a special message into a designated
    # channel once it verifies the visitor's real Discord ID matches the owner's. We only
    # react to genuine webhook messages (not anything a regular member could type) in that
    # specific channel, so this can't be triggered by anyone else.
    if message.webhook_id:
        if WEBHOOK_TRIGGER_CHANNEL_ID and str(message.channel.id) == str(WEBHOOK_TRIGGER_CHANNEL_ID) and BOT_OWNER_ID:
            content = message.content.strip()
            try:
                owner_user = await bot.fetch_user(int(BOT_OWNER_ID))
            except discord.NotFound:
                owner_user = None

            if owner_user:
                if content == "OWNER_WEB_ACCESS_REQUEST":
                    await send_server_list_dm(owner_user)

                elif content == "OWNER_REMOVE_SERVER_MENU_REQUEST":
                    await send_remove_server_menu_dm(owner_user)

                elif content == "OWNER_BAN_SERVER_MENU_REQUEST":
                    await send_ban_server_menu_dm(owner_user)

                elif content == "OWNER_UNBAN_SERVER_MENU_REQUEST":
                    await send_unban_server_menu_dm(owner_user)

                elif content == "PAUSE_INVITES_REQUEST":
                    settings = load_bot_settings()
                    settings["invite_enabled"] = False
                    save_bot_settings(settings)
                    await owner_user.send("🚫 **Invites paused.** I'll auto-leave any server someone tries to add me to until you resume.")

                elif content == "RESUME_INVITES_REQUEST":
                    settings = load_bot_settings()
                    settings["invite_enabled"] = True
                    save_bot_settings(settings)
                    await owner_user.send("✅ **Invites resumed.** I'll join new servers normally again.")

                elif content.startswith("REMOVE_SERVER_REQUEST:"):
                    guild_id_str = content.split(":", 1)[1].strip()
                    try:
                        guild_id = int(guild_id_str)
                    except ValueError:
                        await owner_user.send(f"⚠️ `{guild_id_str}` isn't a valid server ID.")
                    else:
                        target_guild = bot.get_guild(guild_id)
                        if target_guild is None:
                            await owner_user.send(f"⚠️ I'm not in a server with ID `{guild_id}` (or I've already left it).")
                        else:
                            guild_name = target_guild.name
                            await target_guild.leave()
                            await owner_user.send(f"🚪 Left **{guild_name}** (`{guild_id}`) as requested from the website.")

        return  # never treat webhook messages as chat/commands

    if message.author.bot:
        return

    if message.guild is None:
        return  # XP is per-server now — no leveling from DMs

    user_id = str(message.author.id)
    guild_id = str(message.guild.id)
    now = time.time()
    cooldown_key = (guild_id, user_id)
    last_time = _last_xp_time.get(cooldown_key, 0)

    if now - last_time >= XP_COOLDOWN_SECONDS:
        _last_xp_time[cooldown_key] = now

        levels = load_levels()
        guild_levels = get_guild_bucket(levels, guild_id)
        old_level, new_level = apply_xp_change(guild_levels, user_id, random.randint(15, 25))
        save_levels(levels)

        if new_level > old_level:
            await message.channel.send(
                f"🎉 {message.author.mention} leveled up to **Level {new_level}**!"
            )
            await update_level_role(message.author, new_level)

    # NOTE: bot.process_commands() is intentionally NOT called here anymore —
    # this disables the ! prefix entirely. Only /slash commands work now.
    # (Slash commands don't go through on_message at all — Discord delivers them
    # via a separate interaction system, so removing this line doesn't affect them.)


@bot.event
async def on_member_join(member):
    """Runs whenever a new member joins the server."""
    # Use the channel configured via /setup, if one's been set for this server
    welcome_channels = load_welcome_channels()
    configured_id = welcome_channels.get(str(member.guild.id))
    channel = member.guild.get_channel(int(configured_id)) if configured_id else None

    # Fall back to auto-detect — the server's system channel, or the first channel the bot can post in
    if channel is None:
        channel = member.guild.system_channel
        if channel is None:
            for text_channel in member.guild.text_channels:
                if text_channel.permissions_for(member.guild.me).send_messages:
                    channel = text_channel
                    break

    if channel:
        member_count = member.guild.member_count

        embed = discord.Embed(
            title=f"Yoo!!! Welcome to {member.guild.name} 💀!!!",
            description=(
                f"{member.mention} Glad to see you spawn here 🔥. "
                f"We hope you get the BEST EXPERIENCE with us 😎\n\n"
                f"📜 Don't forget to check out our channels and vibe with the squad\n"
                f"👥 You're member **#{member_count}** to join the chaos!\n"
                f"⚡ Type `!hello` to say what's up to the bot!"
            ),
            color=discord.Color.dark_red(),
        )
        embed.set_footer(text="Welcome to the chaos")
        embed.timestamp = discord.utils.utcnow()

        # Attach and display the GIF as the embed's main image (only if the file exists)
        if os.path.exists("Girik Chaos.gif"):
            gif_file = discord.File("Girik Chaos.gif", filename="Girik Chaos.gif")
            embed.set_image(url="attachment://Girik Chaos.gif")
            await channel.send(embed=embed, file=gif_file)
        else:
            await channel.send(embed=embed)
    else:
        print("⚠️ No available channel found to send the welcome message in this server.")

    # Assign the auto-role — creates a "Member" role in this server if it doesn't exist yet
    role = await get_or_create_member_role(member.guild)
    if role:
        try:
            await member.add_roles(role)
            print(f"✅ Gave {member} the '{role.name}' role")
        except discord.Forbidden:
            print("⚠️ Bot doesn't have permission to assign this role — check role position/permissions")


# Manual grouping for /help — add new command names here when you add new commands.
COMMAND_CATEGORIES = {
    "🎉 Fun & Games": ["hello", "ping", "handcricket", "numberguess", "duel", "slots", "tictactoe", "blackjack", "bossfight"],
    "📊 Leveling": ["rank", "leaderboard", "streak"],
    "💰 Economy & Shop": ["invest", "portfolio", "cashout", "shop", "buy"],
    "🛡️ Moderation — staff only": ["kick", "ban", "mute", "unmute", "warn", "warnings"],
    "⚙️ Server Setup — admin only": ["setup", "ticketsetup", "ticketpanel"],
    "👑 Owner only": ["addxp", "removexp", "servers"],
}


@bot.hybrid_command()
async def help(ctx):
    """Shows every command and what it does, grouped by category."""
    embed = discord.Embed(
        title="📖 Girik Chaos — Command List",
        description="Type `/` in the message box to see live autocomplete for all of these.",
        color=discord.Color.blurple(),
    )

    listed_names = set()
    for category, cmd_names in COMMAND_CATEGORIES.items():
        lines = []
        for name in cmd_names:
            cmd = bot.get_command(name)
            if cmd is None:
                continue
            listed_names.add(name)
            description = cmd.help or "No description set."
            lines.append(f"**/{cmd.name}** — {description}")
        if lines:
            embed.add_field(name=category, value="\n".join(lines), inline=False)

    # Catch-all for anything added later that hasn't been sorted into COMMAND_CATEGORIES yet
    uncategorized = [c for c in bot.commands if c.name not in listed_names and c.name != "help"]
    if uncategorized:
        lines = [f"**/{c.name}** — {c.help or 'No description set.'}" for c in uncategorized]
        embed.add_field(name="📦 Other", value="\n".join(lines), inline=False)

    await ctx.send(embed=embed)


@bot.hybrid_command()
async def hello(ctx):
    """Responds with a greeting."""
    await ctx.send(f"Hey {ctx.author.mention}! 👋")


@bot.hybrid_command()
async def ping(ctx):
    """Responds with the bot's latency."""
    latency = round(bot.latency * 1000)
    await ctx.send(f"Pong! 🏓 ({latency}ms)")


@bot.hybrid_command()
async def rank(ctx, member: discord.Member = None):
    """Shows your (or someone else's) level and XP."""
    if ctx.guild is None:
        await ctx.send("Levels are per-server — run this in a server, not in DMs.")
        return

    member = member or ctx.author
    guild_levels = get_guild_bucket(load_levels(), str(ctx.guild.id))
    user_id = str(member.id)

    if user_id not in guild_levels:
        await ctx.send(f"{member.mention} hasn't earned any XP yet — start chatting!")
        return

    data = guild_levels[user_id]
    needed = xp_needed_for(data["level"])

    embed = discord.Embed(title=f"📊 Rank — {member.display_name}", color=discord.Color.blue())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Level", value=str(data["level"]), inline=True)
    embed.add_field(name="XP", value=f"{data['xp']} / {needed}", inline=True)
    await ctx.send(embed=embed)


@bot.hybrid_command()
async def leaderboard(ctx):
    """Shows the top 10 members by level and XP."""
    if ctx.guild is None:
        await ctx.send("Leaderboards are per-server — run this in a server, not in DMs.")
        return

    guild_levels = get_guild_bucket(load_levels(), str(ctx.guild.id))
    if not guild_levels:
        await ctx.send("No one has earned XP yet — get chatting!")
        return

    sorted_users = sorted(
        guild_levels.items(),
        key=lambda item: (item[1]["level"], item[1]["xp"]),
        reverse=True,
    )[:10]

    embed = discord.Embed(title="🏆 Leaderboard", color=discord.Color.gold())
    for i, (user_id, data) in enumerate(sorted_users, start=1):
        member = ctx.guild.get_member(int(user_id))
        name = member.display_name if member else f"User {user_id}"
        embed.add_field(
            name=f"#{i} {name}",
            value=f"Level {data['level']} — {data['xp']} XP",
            inline=False,
        )
    await ctx.send(embed=embed)


@bot.hybrid_command()
async def streak(ctx, member: discord.Member = None):
    """Shows your (or someone else's) current game win streak."""
    if ctx.guild is None:
        await ctx.send("Win streaks are per-server — run this in a server, not in DMs.")
        return

    member = member or ctx.author
    guild_streaks = get_guild_bucket(load_streaks(), str(ctx.guild.id))
    current = guild_streaks.get(str(member.id), 0)
    await ctx.send(f"🔥 {member.mention}'s current win streak: **{current}**")


def is_staff():
    """Check that lets users with a real moderation permission use a command — works on any server."""
    async def predicate(ctx):
        perms = ctx.author.guild_permissions
        if perms.administrator or perms.kick_members or perms.ban_members or perms.moderate_members:
            return True
        await ctx.send("🚫 You need a moderation permission (Kick/Ban/Timeout Members or Administrator) to use this command.")
        return False
    return commands.check(predicate)


def is_admin_or_owner():
    """Check that lets Administrators (or the bot owner) use a command — for server-wide config commands."""
    async def predicate(ctx):
        if BOT_OWNER_ID and ctx.author.id == int(BOT_OWNER_ID):
            return True
        if ctx.guild and ctx.author.guild_permissions.administrator:
            return True
        await ctx.send("🚫 You need the **Administrator** permission to use this command.")
        return False
    return commands.check(predicate)



def is_bot_owner():
    """Check that ONLY lets your personal Discord account use a command — on any server, forever."""
    async def predicate(ctx):
        if not BOT_OWNER_ID:
            await ctx.send("⚠️ BOT_OWNER_ID isn't set in .env — this command is disabled.")
            return False
        if ctx.author.id == int(BOT_OWNER_ID):
            return True
        await ctx.send("🚫 Only the bot's owner can use this command.")
        return False
    return commands.check(predicate)


def is_protected_target(ctx, member):
    """Returns True if `member` is the real server owner (or the bot owner) and ctx.author is neither."""
    is_server_owner = member.id == ctx.guild.owner_id
    is_bot_owner_target = BOT_OWNER_ID and member.id == int(BOT_OWNER_ID)

    invoker_is_server_owner = ctx.author.id == ctx.guild.owner_id
    invoker_is_bot_owner = BOT_OWNER_ID and ctx.author.id == int(BOT_OWNER_ID)

    if (is_server_owner or is_bot_owner_target) and not (invoker_is_server_owner or invoker_is_bot_owner):
        return True
    return False


@bot.hybrid_command()
@is_bot_owner()
async def addxp(ctx, member: discord.Member, amount: int):
    """Adds XP to a member (Owner only). Safe for any size number — uses instant math, no loop."""
    levels = load_levels()
    guild_levels = get_guild_bucket(levels, str(ctx.guild.id))
    user_id = str(member.id)

    old_level, new_level = apply_xp_change(guild_levels, user_id, amount)
    save_levels(levels)

    await ctx.send(
        f"✅ Gave **{amount} XP** to {member.mention}. "
        f"Now Level **{new_level}** ({guild_levels[user_id]['xp']} XP)"
    )

    if new_level > old_level:
        await update_level_role(member, new_level)


@bot.hybrid_command()
@is_bot_owner()
async def removexp(ctx, member: discord.Member, amount: int):
    """Removes XP from a member (Owner only). Safe for any size number."""
    levels = load_levels()
    guild_levels = get_guild_bucket(levels, str(ctx.guild.id))
    user_id = str(member.id)

    old_level, new_level = apply_xp_change(guild_levels, user_id, -amount)
    save_levels(levels)

    await ctx.send(
        f"🗑️ Removed **{amount} XP** from {member.mention}. "
        f"Now Level **{new_level}** ({guild_levels[user_id]['xp']} XP)"
    )

    if new_level < old_level:
        await update_level_role(member, new_level)


@bot.hybrid_command()
@is_staff()
async def kick(ctx, member: discord.Member, *, reason="No reason provided"):
    """Kicks a member from the server."""
    if is_protected_target(ctx, member):
        await ctx.send("🚫 You cannot take action against an Owner.")
        return
    await member.kick(reason=reason)
    await ctx.send(f"👢 **{member}** was kicked. Reason: {reason}")


@bot.hybrid_command()
@is_staff()
async def ban(ctx, member: discord.Member, *, reason="No reason provided"):
    """Bans a member from the server."""
    if is_protected_target(ctx, member):
        await ctx.send("🚫 You cannot take action against an Owner.")
        return
    await member.ban(reason=reason)
    await ctx.send(f"🔨 **{member}** was banned. Reason: {reason}")


@bot.hybrid_command()
@is_staff()
async def mute(ctx, member: discord.Member, minutes: int, *, reason="No reason provided"):
    """Times out (mutes) a member for a set number of minutes."""
    if is_protected_target(ctx, member):
        await ctx.send("🚫 You cannot take action against an Owner.")
        return
    duration = timedelta(minutes=minutes)
    await member.timeout(duration, reason=reason)
    await ctx.send(f"🔇 **{member}** was muted for {minutes} minute(s). Reason: {reason}")


@bot.hybrid_command()
@is_staff()
async def unmute(ctx, member: discord.Member):
    """Removes a timeout from a member."""
    await member.timeout(None)
    await ctx.send(f"🔊 **{member}** has been unmuted.")


@bot.hybrid_command()
@is_staff()
async def warn(ctx, member: discord.Member, *, reason="No reason provided"):
    """Warns a member and logs it."""
    if is_protected_target(ctx, member):
        await ctx.send("🚫 You cannot take action against an Owner.")
        return
    warnings = load_warnings()
    user_id = str(member.id)

    if user_id not in warnings:
        warnings[user_id] = []

    warnings[user_id].append({
        "reason": reason,
        "moderator": str(ctx.author),
        "timestamp": discord.utils.utcnow().isoformat(),
    })
    save_warnings(warnings)

    count = len(warnings[user_id])
    await ctx.send(f"⚠️ **{member}** was warned. Reason: {reason}\nTotal warnings: **{count}**")


@bot.hybrid_command()
async def warnings(ctx, member: discord.Member):
    """Shows all warnings for a member."""
    data = load_warnings()
    user_id = str(member.id)

    if user_id not in data or len(data[user_id]) == 0:
        await ctx.send(f"✅ **{member}** has no warnings.")
        return

    embed = discord.Embed(
        title=f"Warnings for {member}",
        color=discord.Color.orange(),
    )
    for i, w in enumerate(data[user_id], start=1):
        embed.add_field(
            name=f"Warning #{i}",
            value=f"**Reason:** {w['reason']}\n**By:** {w['moderator']}\n**When:** {w['timestamp']}",
            inline=False,
        )
    await ctx.send(embed=embed)


@bot.hybrid_command()
async def handcricket(ctx, overs: int = 1):
    """Play full hand cricket vs the bot — toss, bat/bowl, innings, and a run chase."""
    total_balls = overs * 6

    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel

    async def get_choice(prompt, valid_options):
        await ctx.send(prompt)
        while True:
            try:
                msg = await bot.wait_for("message", check=check, timeout=30.0)
            except asyncio.TimeoutError:
                await ctx.send("⏰ Timed out. Game cancelled.")
                return None
            content = msg.content.strip().lower()
            if content in valid_options:
                return content
            await ctx.send(f"Please type one of: {', '.join(valid_options)}")

    async def play_innings(batting_side, innings_num, target=None):
        score = 0
        balls = 0
        while balls < total_balls:
            remaining = f" (need **{target - score}** more to win)" if target else ""
            await ctx.send(f"🎾 Ball {balls + 1}/{total_balls} — type a number **1-6**{remaining}")
            try:
                msg = await bot.wait_for("message", check=check, timeout=30.0)
            except asyncio.TimeoutError:
                await ctx.send("⏰ Timed out. Game cancelled.")
                return None
            content = msg.content.strip().lower()
            if not content.isdigit() or not (1 <= int(content) <= 6):
                await ctx.send("Please type a number between 1 and 6.")
                continue

            player_num = int(content)
            bot_num = random.randint(1, 6)
            await ctx.send(f"You: **{player_num}** | Bot: **{bot_num}**")

            if player_num == bot_num:
                who = "You are" if batting_side == "player" else "I am"
                await ctx.send(f"💥 **WICKET!** {who} out. Innings {innings_num} score: **{score}**")
                return score

            runs = player_num if batting_side == "player" else bot_num
            score += runs
            balls += 1
            await ctx.send(f"Runs: +{runs} → Score: **{score}**")

            if target and score >= target:
                await ctx.send(f"🎯 Target reached! Innings {innings_num} score: **{score}**")
                return score

        await ctx.send(f"🏁 Overs complete! Innings {innings_num} final score: **{score}**")
        return score

    # --- Toss ---
    call = await get_choice("🪙 Call the toss! Type `heads` or `tails`.", ["heads", "tails"])
    if call is None:
        return
    flip = random.choice(["heads", "tails"])
    await ctx.send(f"The coin lands on **{flip}**!")

    if call == flip:
        await ctx.send("🎉 You won the toss!")
        choice = await get_choice("Type `bat` or `bowl` to choose.", ["bat", "bowl"])
        if choice is None:
            return
        player_bats_first = (choice == "bat")
    else:
        await ctx.send("🤖 I won the toss! I choose to **bat** first.")
        player_bats_first = False

    first_side = "player" if player_bats_first else "bot"
    second_side = "bot" if player_bats_first else "player"

    # --- Innings 1 ---
    await ctx.send(f"\n**Innings 1: {'You are' if first_side == 'player' else 'I am'} batting!**")
    score1 = await play_innings(first_side, 1)
    if score1 is None:
        return

    target = score1 + 1

    # --- Innings 2 ---
    await ctx.send(f"\n**Innings 2: {'You are' if second_side == 'player' else 'I am'} batting! Target: {target}**")
    score2 = await play_innings(second_side, 2, target=target)
    if score2 is None:
        return

    player_score = score1 if first_side == "player" else score2
    bot_score = score1 if first_side == "bot" else score2

    await ctx.send(f"\n📊 **Final Score** — You: **{player_score}**, Bot: **{bot_score}**")
    if player_score > bot_score:
        await ctx.send("🎉 **YOU WIN!** GG 🏏")
        await award_win(ctx, ctx.author)
    elif bot_score > player_score:
        await ctx.send("🤖 **I WIN!** Better luck next time 🏏")
        await reset_streak(ctx, ctx.author)
    else:
        await ctx.send("🤝 **It's a TIE!**")
        await reset_streak(ctx, ctx.author)


@bot.hybrid_command()
async def numberguess(ctx):
    """Number guessing duel — both pick a secret 1-100 number and try to crack each other's."""

    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel

    async def get_number(prompt, valid_range=(1, 100)):
        await ctx.send(prompt)
        while True:
            try:
                msg = await bot.wait_for("message", check=check, timeout=30.0)
            except asyncio.TimeoutError:
                await ctx.send("⏰ Timed out. Game cancelled.")
                return None
            content = msg.content.strip()
            if content.isdigit() and valid_range[0] <= int(content) <= valid_range[1]:
                return int(content)
            await ctx.send(f"Please type a number between {valid_range[0]} and {valid_range[1]}.")

    fun_higher = [
        "📈 Higher! Reach for the sky!",
        "⬆️ Nope, go bigger!",
        "🔥 Getting warmer... but higher!",
    ]
    fun_lower = [
        "📉 Lower! Come back down!",
        "⬇️ Too high, try lower!",
        "❄️ Cooling off... go lower!",
    ]
    fun_correct = [
        "🎯 BOOM! Got it exactly!",
        "🏆 Nailed it!",
        "💥 Spot on!",
    ]

    bot_secret = random.randint(1, 100)

    player_secret = await get_number(
        "🔢 Pick your secret number between **1-100** and type it in chat (I promise not to peek 👀)"
    )
    if player_secret is None:
        return

    await ctx.send("Alright, secrets locked in! Let's duel 🔥")

    bot_low, bot_high = 1, 100  # bot's search range for guessing the player's number
    round_num = 1

    while True:
        await ctx.send(f"\n**Round {round_num}**")

        # Player guesses the bot's number
        player_guess = await get_number("🔢 Your guess for **my** number (1-100)?")
        if player_guess is None:
            return

        if player_guess == bot_secret:
            await ctx.send(
                f"{random.choice(fun_correct)} You guessed my number (**{bot_secret}**) first! 🎉 **YOU WIN!**"
            )
            await award_win(ctx, ctx.author)
            return
        elif player_guess < bot_secret:
            await ctx.send(random.choice(fun_higher))
        else:
            await ctx.send(random.choice(fun_lower))

        # Bot guesses the player's number (smart binary search — auto-checked, no self-reporting)
        bot_guess = (bot_low + bot_high) // 2
        await ctx.send(f"🤖 My guess for **your** number: **{bot_guess}**!")

        if bot_guess == player_secret:
            await ctx.send(f"🤖 **I WIN!** Your number was **{bot_guess}**! GG 😎")
            await reset_streak(ctx, ctx.author)
            return
        elif bot_guess < player_secret:
            await ctx.send("🤖 Hmm, I'll need to go higher next round!")
            bot_low = bot_guess + 1
        else:
            await ctx.send("🤖 Hmm, I'll need to go lower next round!")
            bot_high = bot_guess - 1

        round_num += 1


RPS_BEATS = {"rock": "scissors", "scissors": "paper", "paper": "rock"}
RPS_EMOJI = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}


class RPSView(discord.ui.View):
    """Rock/Paper/Scissors buttons for a duel. Choices are only ever confirmed to the
    person who clicked (via ephemeral replies) — nobody else, including the opponent,
    can see what was picked until both have chosen and the bot reveals both at once."""

    def __init__(self, challenger, opponent, timeout=60):
        super().__init__(timeout=timeout)
        self.challenger = challenger
        self.opponent = opponent
        self.choices = {}
        self.done = asyncio.Event()

    async def handle_choice(self, interaction: discord.Interaction, choice: str):
        if interaction.user.id not in (self.challenger.id, self.opponent.id):
            await interaction.response.send_message("This isn't your duel! 👀", ephemeral=True)
            return
        if interaction.user.id in self.choices:
            await interaction.response.send_message("You already locked in your move — waiting on your opponent!", ephemeral=True)
            return

        self.choices[interaction.user.id] = choice
        await interaction.response.send_message(
            f"✅ Locked in {RPS_EMOJI[choice]} **{choice}**! Waiting for your opponent...", ephemeral=True
        )

        if len(self.choices) == 2:
            self.done.set()
            self.stop()

    @discord.ui.button(label="Rock", emoji="🪨", style=discord.ButtonStyle.secondary)
    async def rock_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_choice(interaction, "rock")

    @discord.ui.button(label="Paper", emoji="📄", style=discord.ButtonStyle.secondary)
    async def paper_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_choice(interaction, "paper")

    @discord.ui.button(label="Scissors", emoji="✂️", style=discord.ButtonStyle.secondary)
    async def scissors_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_choice(interaction, "scissors")

    async def on_timeout(self):
        self.done.set()


@bot.hybrid_command()
async def duel(ctx, opponent: discord.Member, wager: int):
    """Challenge another member to a Chaos Duel — wager XP, winner takes it all."""

    if wager <= 0:
        await ctx.send("You need to wager at least 1 XP.")
        return

    if opponent.bot:
        await ctx.send("You can't duel a bot 🤖")
        return

    if opponent.id == ctx.author.id:
        await ctx.send("You can't duel yourself 💀")
        return

    levels = load_levels()
    guild_levels = get_guild_bucket(levels, str(ctx.guild.id))
    challenger_id = str(ctx.author.id)
    opponent_id = str(opponent.id)

    def total_xp(user_id):
        data = guild_levels.get(user_id, {"xp": 0, "level": 0})
        return cumulative_xp_for_level(data["level"]) + data["xp"]

    if total_xp(challenger_id) < wager:
        await ctx.send(f"{ctx.author.mention}, you don't have {wager} XP to wager!")
        return

    # --- Challenge + accept step ---
    await ctx.send(
        f"⚔️ {ctx.author.mention} has challenged {opponent.mention} to a **Chaos Duel** for **{wager} XP**!\n"
        f"{opponent.mention}, type `!accept` within 60 seconds to fight, or ignore to decline."
    )

    def accept_check(m):
        return (
            m.author.id == opponent.id
            and m.channel == ctx.channel
            and m.content.strip().lower() == "!accept"
        )

    try:
        await bot.wait_for("message", check=accept_check, timeout=60.0)
    except asyncio.TimeoutError:
        await ctx.send(f"⏰ {opponent.mention} didn't accept in time. Duel cancelled.")
        return

    if total_xp(opponent_id) < wager:
        await ctx.send(f"{opponent.mention} doesn't have {wager} XP to wager! Duel cancelled.")
        return

    # --- Both players pick secretly via private buttons, right here in the channel ---
    view = RPSView(ctx.author, opponent)
    await ctx.send(
        f"⚔️ {ctx.author.mention} vs {opponent.mention} — click your move below!\n"
        f"Only you can see what you picked. 👀🔒",
        view=view,
    )

    await view.done.wait()

    if len(view.choices) < 2:
        await ctx.send("⏰ Duel cancelled — not everyone locked in a move in time.")
        return

    challenger_move = view.choices[ctx.author.id]
    opponent_move = view.choices[opponent.id]

    await ctx.send(
        f"⚔️ **Reveal!** {ctx.author.mention} chose {RPS_EMOJI[challenger_move]} **{challenger_move}**, "
        f"{opponent.mention} chose {RPS_EMOJI[opponent_move]} **{opponent_move}**!"
    )

    if challenger_move == opponent_move:
        await ctx.send("🤝 It's a **tie**! No XP changes hands. Run it back anytime.")
        return

    if RPS_BEATS[challenger_move] == opponent_move:
        winner, loser = ctx.author, opponent
    else:
        winner, loser = opponent, ctx.author

    levels = load_levels()  # reload in case anything else changed it mid-duel
    guild_levels = get_guild_bucket(levels, str(ctx.guild.id))
    _, winner_new_level = apply_xp_change(guild_levels, str(winner.id), wager)
    _, loser_new_level = apply_xp_change(guild_levels, str(loser.id), -wager)
    save_levels(levels)

    await ctx.send(f"🏆 {winner.mention} wins the duel and takes **{wager} XP** from {loser.mention}!")

    await update_level_role(winner, winner_new_level)
    await update_level_role(loser, loser_new_level)


INVESTMENTS_FILE = "investments.json"

# ===================== SLOTS =====================
SLOT_SYMBOLS = [
    {"emoji": "🍒", "weight": 40, "multiplier": 2},
    {"emoji": "🍋", "weight": 30, "multiplier": 3},
    {"emoji": "🔔", "weight": 15, "multiplier": 5},
    {"emoji": "⭐", "weight": 10, "multiplier": 10},
    {"emoji": "💎", "weight": 5, "multiplier": 25},
]


def spin_slot_reel():
    return random.choices(SLOT_SYMBOLS, weights=[s["weight"] for s in SLOT_SYMBOLS], k=1)[0]


@bot.hybrid_command()
async def slots(ctx, bet: int):
    """Spin the slot machine — bet XP, match symbols to win big."""
    if ctx.guild is None:
        await ctx.send("XP is per-server — run this in a server, not in DMs.")
        return

    if bet <= 0:
        await ctx.send("Bet at least 1 XP.")
        return

    levels = load_levels()
    guild_levels = get_guild_bucket(levels, str(ctx.guild.id))
    user_id = str(ctx.author.id)
    if get_total_xp(guild_levels, user_id) < bet:
        await ctx.send(f"You don't have {bet} XP to bet!")
        return

    reels = [spin_slot_reel() for _ in range(3)]
    emojis = [r["emoji"] for r in reels]

    if emojis[0] == emojis[1] == emojis[2]:
        payout = bet * reels[0]["multiplier"]
        result_text = f"🎉 **JACKPOT!** Triple {emojis[0]} — you win **{payout} XP**!"
        net = payout - bet
    elif emojis[0] == emojis[1] or emojis[1] == emojis[2] or emojis[0] == emojis[2]:
        payout = bet // 2
        result_text = f"🙂 Two match — you get **{payout} XP** back."
        net = payout - bet
    else:
        result_text = f"💥 No match — you lose **{bet} XP**."
        net = -bet

    old_level, new_level = apply_xp_change(guild_levels, user_id, net)
    save_levels(levels)

    embed = discord.Embed(
        title="🎰 Slot Machine",
        description=f"**[ {'  |  '.join(emojis)} ]**\n\n{result_text}",
        color=discord.Color.gold(),
    )
    await ctx.send(embed=embed)

    if new_level != old_level:
        await update_level_role(ctx.author, new_level)


# ===================== TIC-TAC-TOE =====================
TTT_WIN_LINES = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]


class TicTacToeButton(discord.ui.Button):
    def __init__(self, index):
        super().__init__(style=discord.ButtonStyle.secondary, label="\u200b", row=index // 3)
        self.index = index

    async def callback(self, interaction: discord.Interaction):
        await self.view.handle_move(interaction, self.index)


class TicTacToeView(discord.ui.View):
    """Live Tic-Tac-Toe board. No wager — just bragging rights."""

    def __init__(self, player_x, player_o):
        super().__init__(timeout=180)
        self.player_x = player_x
        self.player_o = player_o
        self.board = [None] * 9
        self.turn = player_x
        for i in range(9):
            self.add_item(TicTacToeButton(i))

    def current_symbol(self):
        return "X" if self.turn.id == self.player_x.id else "O"

    def check_winner(self):
        for line in TTT_WIN_LINES:
            a, b, c = line
            if self.board[a] and self.board[a] == self.board[b] == self.board[c]:
                return line
        return None

    async def handle_move(self, interaction, index):
        if interaction.user.id not in (self.player_x.id, self.player_o.id):
            await interaction.response.send_message("This isn't your game!", ephemeral=True)
            return
        if interaction.user.id != self.turn.id:
            await interaction.response.send_message("It's not your turn!", ephemeral=True)
            return
        if self.board[index] is not None:
            await interaction.response.send_message("That cell's taken!", ephemeral=True)
            return

        symbol = self.current_symbol()
        self.board[index] = symbol
        button = self.children[index]
        button.label = symbol
        button.style = discord.ButtonStyle.danger if symbol == "X" else discord.ButtonStyle.primary
        button.disabled = True

        winner_line = self.check_winner()
        if winner_line:
            for i in winner_line:
                self.children[i].style = discord.ButtonStyle.success
            for child in self.children:
                child.disabled = True
            winner = self.player_x if symbol == "X" else self.player_o
            await interaction.response.edit_message(content=f"🎉 {winner.mention} wins Tic-Tac-Toe!", view=self)
            self.stop()
            return

        if all(cell is not None for cell in self.board):
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(content="🤝 It's a draw!", view=self)
            self.stop()
            return

        self.turn = self.player_o if self.turn.id == self.player_x.id else self.player_x
        await interaction.response.edit_message(
            content=f"❌⭕ Tic-Tac-Toe — {self.turn.mention}'s turn ({self.current_symbol()})", view=self
        )


@bot.hybrid_command()
async def tictactoe(ctx, opponent: discord.Member):
    """Play Tic-Tac-Toe against another member — no wager, just bragging rights."""
    if opponent.bot:
        await ctx.send("You can't play against a bot 🤖")
        return
    if opponent.id == ctx.author.id:
        await ctx.send("You can't play against yourself 💀")
        return

    view = TicTacToeView(ctx.author, opponent)
    await ctx.send(
        f"❌⭕ Tic-Tac-Toe — {ctx.author.mention} (X) vs {opponent.mention} (O)\n{ctx.author.mention}'s turn (X)",
        view=view,
    )


# ===================== BLACKJACK =====================
CARD_RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
CARD_SUITS = ["♠", "♥", "♦", "♣"]


def new_deck():
    deck = [(rank, suit) for rank in CARD_RANKS for suit in CARD_SUITS]
    random.shuffle(deck)
    return deck


def card_label(card):
    rank, suit = card
    return f"{rank}{suit}"


def hand_value(hand):
    value, aces = 0, 0
    for rank, _ in hand:
        if rank == "A":
            value += 11
            aces += 1
        elif rank in ("J", "Q", "K"):
            value += 10
        else:
            value += int(rank)
    while value > 21 and aces:
        value -= 10
        aces -= 1
    return value


def hand_display(hand):
    return " ".join(card_label(c) for c in hand)


class BlackjackView(discord.ui.View):
    """Live Blackjack hand vs the dealer. Bet is only actually moved at the end (win/lose/push)."""

    def __init__(self, player, bet, deck, player_hand, dealer_hand):
        super().__init__(timeout=120)
        self.player = player
        self.bet = bet
        self.deck = deck
        self.player_hand = player_hand
        self.dealer_hand = dealer_hand

    def render(self, reveal_dealer=False):
        dealer_display = hand_display(self.dealer_hand) if reveal_dealer else f"{card_label(self.dealer_hand[0])} 🂠"
        return (
            f"**Dealer:** {dealer_display}\n"
            f"**{self.player.display_name}:** {hand_display(self.player_hand)} (**{hand_value(self.player_hand)}**)"
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.player.id:
            await interaction.response.send_message("This isn't your game!", ephemeral=True)
            return False
        return True

    async def finish(self, interaction, outcome_text, net_xp):
        for child in self.children:
            child.disabled = True

        levels = load_levels()
        guild_levels = get_guild_bucket(levels, str(self.player.guild.id))
        old_level, new_level = apply_xp_change(guild_levels, str(self.player.id), net_xp)
        save_levels(levels)

        content = (
            f"{self.render(reveal_dealer=True)}\n**Dealer total: {hand_value(self.dealer_hand)}**\n\n{outcome_text}"
        )
        await interaction.response.edit_message(content=content, view=self)
        self.stop()

        if new_level != old_level:
            await update_level_role(self.player, new_level)

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary)
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.player_hand.append(self.deck.pop())
        if hand_value(self.player_hand) > 21:
            await self.finish(interaction, f"💥 Bust! You lose **{self.bet} XP**.", -self.bet)
            return
        await interaction.response.edit_message(content=self.render(), view=self)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.secondary)
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        while hand_value(self.dealer_hand) < 17:
            self.dealer_hand.append(self.deck.pop())

        player_total = hand_value(self.player_hand)
        dealer_total = hand_value(self.dealer_hand)

        if dealer_total > 21 or player_total > dealer_total:
            await self.finish(interaction, f"🎉 You win **{self.bet} XP**!", self.bet)
        elif player_total == dealer_total:
            await self.finish(interaction, "🤝 Push — bet returned.", 0)
        else:
            await self.finish(interaction, f"😔 Dealer wins. You lose **{self.bet} XP**.", -self.bet)


@bot.hybrid_command()
async def blackjack(ctx, bet: int):
    """Play Blackjack vs the dealer — bet XP, get closer to 21 without busting."""
    if ctx.guild is None:
        await ctx.send("XP is per-server — run this in a server, not in DMs.")
        return

    if bet <= 0:
        await ctx.send("Bet at least 1 XP.")
        return

    levels = load_levels()
    guild_levels = get_guild_bucket(levels, str(ctx.guild.id))
    user_id = str(ctx.author.id)
    if get_total_xp(guild_levels, user_id) < bet:
        await ctx.send(f"You don't have {bet} XP to bet!")
        return

    deck = new_deck()
    player_hand = [deck.pop(), deck.pop()]
    dealer_hand = [deck.pop(), deck.pop()]
    view = BlackjackView(ctx.author, bet, deck, player_hand, dealer_hand)

    if hand_value(player_hand) == 21:
        payout = int(bet * 1.5)
        old_level, new_level = apply_xp_change(guild_levels, user_id, payout)
        save_levels(levels)
        await ctx.send(f"{view.render(reveal_dealer=True)}\n\n🃏 **Blackjack!** You win **{payout} XP**!")
        if new_level != old_level:
            await update_level_role(ctx.author, new_level)
        return

    await ctx.send(view.render(), view=view)


# ===================== BOSS FIGHT =====================
# In-memory only (per channel) — an active fight won't survive a bot restart, but that's a fair
# tradeoff for keeping this simple; restarts mid-fight should be rare.
active_boss_fights = {}
BOSS_NAMES = ["Chaos Wyrm", "Void Reaper", "Inferno Titan", "Shadow Colossus", "Storm Leviathan"]


class BossFightView(discord.ui.View):
    def __init__(self, channel_id, guild_id, boss_name, max_hp):
        super().__init__(timeout=600)
        self.channel_id = channel_id
        self.guild_id = guild_id
        self.boss_name = boss_name
        self.max_hp = max_hp
        self.hp = max_hp
        self.damage_dealt = {}
        self.participants = {}
        self.defeated = False

    def hp_bar(self):
        pct = max(self.hp, 0) / self.max_hp
        filled = round(pct * 20)
        return "🟥" * filled + "⬛" * (20 - filled)

    def render(self):
        top = sorted(self.damage_dealt.items(), key=lambda kv: kv[1], reverse=True)[:5]
        lines = [f"<@{uid}> — {dmg} dmg" for uid, dmg in top] or ["No hits yet — click Attack!"]
        return (
            f"👹 **{self.boss_name}**\n"
            f"{self.hp_bar()}  **{max(self.hp, 0)}/{self.max_hp} HP**\n\n"
            f"**Top damage:**\n" + "\n".join(lines)
        )

    @discord.ui.button(label="⚔️ Attack", style=discord.ButtonStyle.danger)
    async def attack(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.defeated:
            await interaction.response.send_message("This boss is already defeated!", ephemeral=True)
            return

        dmg = random.randint(8, 25)
        crit = random.random() < 0.12
        if crit:
            dmg *= 2

        self.hp -= dmg
        self.damage_dealt[interaction.user.id] = self.damage_dealt.get(interaction.user.id, 0) + dmg
        self.participants[interaction.user.id] = interaction.user

        crit_text = " 💥 **CRIT!**" if crit else ""
        hit_msg = f"{interaction.user.mention} hits **{self.boss_name}** for **{dmg}** damage!{crit_text}"

        if self.hp <= 0:
            self.defeated = True
            for child in self.children:
                child.disabled = True

            levels = load_levels()
            guild_levels = get_guild_bucket(levels, str(self.guild_id))
            reward_lines = []
            for uid, dmg_dealt in self.damage_dealt.items():
                reward = max(10, dmg_dealt * 3)
                old_level, new_level = apply_xp_change(guild_levels, str(uid), reward)
                reward_lines.append(f"<@{uid}> +{reward} XP")
                member = self.participants.get(uid)
                if member and new_level != old_level:
                    await update_level_role(member, new_level)
            save_levels(levels)

            active_boss_fights.pop(self.channel_id, None)

            content = (
                f"{hit_msg}\n\n💀 **{self.boss_name} has been defeated!**\n\n"
                f"**Rewards:**\n" + "\n".join(reward_lines)
            )
            await interaction.response.edit_message(content=content, view=self)
            self.stop()
            return

        await interaction.response.edit_message(content=f"{hit_msg}\n\n{self.render()}", view=self)


@bot.hybrid_command()
async def bossfight(ctx):
    """Spawn a boss for the server to fight together — attackers earn XP on the kill."""
    if ctx.guild is None:
        await ctx.send("Boss fights only work in a server, not DMs.")
        return

    if ctx.channel.id in active_boss_fights:
        await ctx.send("There's already an active boss fight in this channel!")
        return

    boss_name = random.choice(BOSS_NAMES)
    max_hp = random.randint(300, 600)
    view = BossFightView(ctx.channel.id, ctx.guild.id, boss_name, max_hp)
    active_boss_fights[ctx.channel.id] = view

    await ctx.send(f"👹 A wild **{boss_name}** appeared with **{max_hp} HP**! Click Attack to fight!\n\n{view.render()}", view=view)



def load_investments():
    """Load investment data from the JSON file, or return an empty dict if it doesn't exist or is unreadable."""
    if os.path.exists(INVESTMENTS_FILE):
        with open(INVESTMENTS_FILE, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}


def save_investments(data):
    """Save the investments dict back to the JSON file."""
    with open(INVESTMENTS_FILE, "w") as f:
        json.dump(data, f, indent=4)


@bot.hybrid_command()
async def invest(ctx, target: discord.Member, amount: int):
    """Invest XP in another member — its value rises and falls with their XP growth (or decline)."""
    if amount <= 0:
        await ctx.send("Invest at least 1 XP.")
        return

    if target.id == ctx.author.id:
        await ctx.send("You can't invest in yourself 💀 — diversify into someone else!")
        return

    if target.bot:
        await ctx.send("You can't invest in a bot 🤖")
        return

    if BOT_OWNER_ID and target.id == int(BOT_OWNER_ID):
        await ctx.send("🚫 You can't invest in the bot owner — nice try 😏")
        return

    guild_id = str(ctx.guild.id)
    levels = load_levels()
    guild_levels = get_guild_bucket(levels, guild_id)
    investor_id = str(ctx.author.id)
    target_id = str(target.id)

    investor_xp = get_total_xp(guild_levels, investor_id)
    if investor_xp < amount:
        await ctx.send(f"You don't have {amount} XP to invest!")
        return

    target_xp = get_total_xp(guild_levels, target_id)
    if target_xp <= 0:
        await ctx.send(f"{target.mention} hasn't earned any XP yet — nothing to invest in!")
        return

    investments = load_investments()
    guild_investments = get_guild_bucket(investments, guild_id)
    if investor_id not in guild_investments:
        guild_investments[investor_id] = {}

    if target_id in guild_investments[investor_id]:
        await ctx.send(
            f"You already have an active investment in {target.mention}. "
            f"Cash out first with `!cashout @{target.display_name}` before investing again."
        )
        return

    apply_xp_change(guild_levels, investor_id, -amount)
    save_levels(levels)

    guild_investments[investor_id][target_id] = {
        "amount": amount,
        "base_xp": target_xp,
    }
    save_investments(investments)

    await ctx.send(
        f"📈 {ctx.author.mention} invested **{amount} XP** in {target.mention}! Watch their growth closely..."
    )


@bot.hybrid_command()
async def portfolio(ctx, member: discord.Member = None):
    """Shows your (or someone else's) active investments and their current value."""
    if ctx.guild is None:
        await ctx.send("Investments are per-server — run this in a server, not in DMs.")
        return

    member = member or ctx.author
    guild_id = str(ctx.guild.id)
    investments = load_investments()
    guild_investments = get_guild_bucket(investments, guild_id)
    investor_id = str(member.id)

    if investor_id not in guild_investments or not guild_investments[investor_id]:
        await ctx.send(f"{member.mention} has no active investments.")
        return

    guild_levels = get_guild_bucket(load_levels(), guild_id)
    embed = discord.Embed(title=f"📊 {member.display_name}'s Portfolio", color=discord.Color.green())

    for target_id, inv in guild_investments[investor_id].items():
        target_member = ctx.guild.get_member(int(target_id))
        name = target_member.display_name if target_member else f"User {target_id}"

        current_target_xp = get_total_xp(guild_levels, target_id)
        base_xp = max(inv["base_xp"], 1)
        current_value = int(inv["amount"] * (current_target_xp / base_xp))
        change_pct = ((current_value - inv["amount"]) / inv["amount"]) * 100 if inv["amount"] else 0
        arrow = "📈" if change_pct >= 0 else "📉"

        embed.add_field(
            name=f"{arrow} {name}",
            value=f"Invested: {inv['amount']} XP → Now worth: **{current_value} XP** ({change_pct:+.1f}%)",
            inline=False,
        )

    await ctx.send(embed=embed)


@bot.hybrid_command()
async def cashout(ctx, target: discord.Member):
    """Cash out your investment in a member, converting its current value back to your own XP."""
    guild_id = str(ctx.guild.id)
    investments = load_investments()
    guild_investments = get_guild_bucket(investments, guild_id)
    investor_id = str(ctx.author.id)
    target_id = str(target.id)

    if investor_id not in guild_investments or target_id not in guild_investments[investor_id]:
        await ctx.send(f"You don't have an active investment in {target.mention}.")
        return

    levels = load_levels()
    guild_levels = get_guild_bucket(levels, guild_id)
    inv = guild_investments[investor_id][target_id]

    current_target_xp = get_total_xp(guild_levels, target_id)
    base_xp = max(inv["base_xp"], 1)
    current_value = max(int(inv["amount"] * (current_target_xp / base_xp)), 0)

    old_level, new_level = apply_xp_change(guild_levels, investor_id, current_value)
    save_levels(levels)

    del guild_investments[investor_id][target_id]
    save_investments(investments)

    change = current_value - inv["amount"]
    emoji = "🤑" if change >= 0 else "💸"
    await ctx.send(
        f"{emoji} {ctx.author.mention} cashed out their investment in {target.mention}: "
        f"**{inv['amount']} XP** → **{current_value} XP** ({'+' if change >= 0 else ''}{change} XP)"
    )

    if new_level > old_level:
        await update_level_role(ctx.author, new_level)


SHOP_ITEMS_PER_TIER_PAGE = 10  # every tier count (150/200/250/200/150/50) divides evenly by this


def _build_shop_pages(owned):
    """Builds combined shop pages — each page holds a slice from every tier, labeled by tier title.
    Smaller tiers stop appearing once they run out of items; Rare (250 items) drives the total page count."""
    tier_chunks = {}
    for tier in SHOP_TIERS:
        tier_items = [(k, v) for k, v in SHOP_ITEMS.items() if v["tier"] == tier]
        tier_chunks[tier] = [
            tier_items[i:i + SHOP_ITEMS_PER_TIER_PAGE]
            for i in range(0, len(tier_items), SHOP_ITEMS_PER_TIER_PAGE)
        ]

    total_pages = max(len(chunks) for chunks in tier_chunks.values())
    pages = []
    for page_index in range(total_pages):
        lines = []
        for tier, data in SHOP_TIERS.items():
            chunks = tier_chunks[tier]
            if page_index >= len(chunks):
                continue  # this tier's items are all shown on earlier pages
            lines.append(f"**{data['emoji']} {tier.upper()} — {data['price']} XP** (tier page {page_index + 1}/{len(chunks)})")
            for key, item in chunks[page_index]:
                display_name = item["name"].split(" ", 1)[1]  # strip the leading tier emoji, header already shows it
                owned_tag = " ✅" if key in owned else ""
                lines.append(f"• {display_name} — `{key}`{owned_tag}")
            lines.append("")
        pages.append("\n".join(lines).strip())
    return pages


class ShopView(discord.ui.View):
    """Interactive paginated shop menu. Locked to whoever ran !shop — no one else can drive their buttons."""

    def __init__(self, pages, owner_id):
        super().__init__(timeout=180)
        self.pages = pages  # list of pre-built description strings, one per page
        self.owner_id = owner_id
        self.index = 0
        self.message = None
        self._sync_buttons()

    def _sync_buttons(self):
        self.previous_button.disabled = self.index == 0
        self.next_button.disabled = self.index == len(self.pages) - 1

    def build_embed(self):
        embed = discord.Embed(
            title="🛒 Chaos Shop — 100 cosmetic roles",
            description=f"{self.pages[self.index]}\n\nBuy with `!buy <item>` • Page {self.index + 1}/{len(self.pages)}",
            color=discord.Color.dark_red(),
        )
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This isn't your shop menu — run `!shop` to get your own.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.blurple)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index -= 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.blurple)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index += 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


@bot.hybrid_command()
async def shop(ctx):
    """Browse the shop — 100 cosmetic roles across tiers. Buy with !buy <item>."""
    if ctx.guild is None:
        await ctx.send("The shop is per-server — run this in a server text channel, not in DMs.")
        return

    purchases = load_shop_purchases()
    guild_id = str(ctx.guild.id)
    user_id = str(ctx.author.id)
    owned = purchases.get(guild_id, {}).get(user_id, [])

    pages = _build_shop_pages(owned)
    view = ShopView(pages, ctx.author.id)
    view.message = await ctx.send(embed=view.build_embed(), view=view)


@bot.hybrid_command()
async def buy(ctx, item_key: str):
    """Buy a cosmetic role from the shop using your XP. Owner only: `buy all` grants everything free."""
    if ctx.guild is None:
        await ctx.send("The shop is per-server — run this in a server text channel, not in DMs.")
        return

    item_key = item_key.lower()

    if item_key == "all":
        if not BOT_OWNER_ID or ctx.author.id != int(BOT_OWNER_ID):
            await ctx.send("🚫 Only the bot's owner can use `!buy all`.")
            return

        purchases = load_shop_purchases()
        guild_id = str(ctx.guild.id)
        user_id = str(ctx.author.id)
        owned = purchases.setdefault(guild_id, {}).setdefault(user_id, [])

        unowned = {k: v for k, v in SHOP_ITEMS.items() if k not in owned}
        if not unowned:
            await ctx.send("👑 You already own every item in the shop.")
            return

        total_cost = sum(item["price"] for item in unowned.values())
        levels = load_levels()
        guild_levels = get_guild_bucket(levels, guild_id)
        user_xp = get_total_xp(guild_levels, user_id)

        if user_xp < total_cost:
            await ctx.send(
                f"👑 Buying all {len(unowned)} remaining items costs **{total_cost} XP** — "
                f"you have **{user_xp} XP** (short by **{total_cost - user_xp} XP**)."
            )
            return

        granted, failed = [], []
        hit_role_limit = False
        msg = await ctx.send(f"👑 Buying all {len(unowned)} remaining shop items for **{total_cost} XP**, hang tight...")

        for key, item in unowned.items():
            if hit_role_limit:
                break
            role, error_reason = await get_or_create_shop_role(ctx.guild, key)
            if role is None:
                failed.append(item["name"])
                if error_reason == "role_limit":
                    hit_role_limit = True  # no point trying more — the server's out of role slots
                continue
            try:
                await ctx.author.add_roles(role)
            except discord.Forbidden:
                failed.append(item["name"])
                continue
            owned.append(key)
            granted.append(item["name"])

        granted_cost = sum(unowned[k]["price"] for k in owned if k in unowned)
        old_level, new_level = apply_xp_change(guild_levels, user_id, -granted_cost)
        save_levels(levels)
        save_shop_purchases(purchases)

        summary = f"👑 {ctx.author.mention} bought **{len(granted)} items** for **{granted_cost} XP**."
        if hit_role_limit:
            summary += f"\n🚫 Stopped early — this server hit Discord's 250-role limit. {len(failed)} items weren't created. Delete some unused roles to free up space."
        elif failed:
            summary += f"\n⚠️ Failed on {len(failed)} (permissions issue — check role hierarchy, not charged for these): {', '.join(failed[:10])}{'...' if len(failed) > 10 else ''}"
        await msg.edit(content=summary)

        if new_level < old_level:
            await update_level_role(ctx.author, new_level)
        return

    if item_key not in SHOP_ITEMS:
        await ctx.send(f"That's not in the shop. Use `!shop` to see what's available.")
        return

    purchases = load_shop_purchases()
    guild_id = str(ctx.guild.id)
    user_id = str(ctx.author.id)
    purchases.setdefault(guild_id, {}).setdefault(user_id, [])

    if item_key in purchases[guild_id][user_id]:
        await ctx.send(f"You already own **{SHOP_ITEMS[item_key]['name']}**!")
        return

    item = SHOP_ITEMS[item_key]
    levels = load_levels()
    guild_levels = get_guild_bucket(levels, guild_id)
    user_xp = get_total_xp(guild_levels, user_id)

    if user_xp < item["price"]:
        await ctx.send(f"You need **{item['price']} XP** for {item['name']} — you have **{user_xp} XP**.")
        return

    role, error_reason = await get_or_create_shop_role(ctx.guild, item_key)
    if role is None:
        if error_reason == "role_limit":
            await ctx.send("🚫 This server has hit Discord's 250-role limit, so I can't create any new roles. Delete some unused roles first.")
        else:
            await ctx.send("⚠️ I don't have permission to create/assign that role here — ask a server admin to give me the **Manage Roles** permission.")
        return

    old_level, new_level = apply_xp_change(guild_levels, user_id, -item["price"])
    save_levels(levels)

    try:
        await ctx.author.add_roles(role)
    except discord.Forbidden:
        # Refund if the role couldn't actually be assigned
        apply_xp_change(guild_levels, user_id, item["price"])
        save_levels(levels)
        await ctx.send("⚠️ Couldn't assign that role (permissions issue) — you've been refunded.")
        return

    purchases[guild_id][user_id].append(item_key)
    save_shop_purchases(purchases)

    await ctx.send(f"🛍️ {ctx.author.mention} bought **{item['name']}** for **{item['price']} XP**!")

    if new_level < old_level:
        await update_level_role(ctx.author, new_level)


def build_server_embeds():
    """Builds the list of dark-red styled embeds showing every server + its owner. Returns a list of embeds."""
    guilds = bot.guilds
    if not guilds:
        return []

    embeds = []
    chunk_size = 8
    total_pages = ((len(guilds) - 1) // chunk_size) + 1

    for page, i in enumerate(range(0, len(guilds), chunk_size), start=1):
        chunk = guilds[i:i + chunk_size]

        embed = discord.Embed(
            title=f"👑 Servers I'm In ({len(guilds)} total)",
            color=discord.Color.dark_red(),
        )

        if page == 1 and guilds[0].icon:
            embed.set_thumbnail(url=guilds[0].icon.url)

        for guild in chunk:
            owner = guild.owner
            owner_text = f"{owner} (`{owner.id}`)" if owner else "Unknown"
            embed.add_field(
                name=f"🏰 {guild.name}",
                value=f"**Owner:** {owner_text}\n**Members:** {guild.member_count}\n**Server ID:** `{guild.id}`",
                inline=False,
            )

        embed.set_footer(text=f"Page {page} of {total_pages} • Girik Chaos Official")
        embed.timestamp = discord.utils.utcnow()
        embeds.append(embed)

    return embeds


async def send_server_list_dm(user):
    """DMs the given user the full server list (used by both !servers and the website's auto-trigger)."""
    embeds = build_server_embeds()
    if not embeds:
        await user.send("I'm not in any servers right now.")
        return
    for embed in embeds:
        await user.send(embed=embed)


@bot.hybrid_command()
@is_bot_owner()
async def servers(ctx):
    """Owner-only: DMs you a styled list of every server the bot is in, with each server's owner."""
    await send_server_list_dm(ctx.author)
    if ctx.guild:  # only try to confirm in-channel if this wasn't already a DM
        await ctx.send("📩 Sent you the full list in DMs!")


class WelcomeChannelSelect(discord.ui.ChannelSelect):
    """Native Discord channel picker — only shows real text channels the user can actually pick from."""

    def __init__(self):
        super().__init__(
            placeholder="Choose a channel for welcome messages...",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        # ChannelSelect.values gives a lightweight partial channel — resolve the real one from
        # cache so we can actually check permissions on it.
        selected_channel = guild.get_channel(self.values[0].id)

        if selected_channel is None:
            await interaction.response.edit_message(content="⚠️ Couldn't find that channel — try again.", view=None)
            return

        perms = selected_channel.permissions_for(guild.me)
        if not perms.send_messages:
            await interaction.response.edit_message(
                content=f"⚠️ I don't have permission to send messages in {selected_channel.mention} — pick a different channel or fix my permissions there.",
                view=None,
            )
            return

        welcome_channels = load_welcome_channels()
        welcome_channels[str(guild.id)] = str(selected_channel.id)
        save_welcome_channels(welcome_channels)

        await interaction.response.edit_message(
            content=f"✅ Welcome messages will now be sent in {selected_channel.mention}.",
            view=None,
        )


class SetupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(WelcomeChannelSelect())

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        print(f"⚠️ Error in /setup view: {error}")
        try:
            if interaction.response.is_done():
                await interaction.followup.send("⚠️ Something went wrong — try `/setup` again.", ephemeral=True)
            else:
                await interaction.response.send_message("⚠️ Something went wrong — try `/setup` again.", ephemeral=True)
        except discord.HTTPException:
            pass


@bot.hybrid_command()
@is_admin_or_owner()
async def setup(ctx):
    """Admin only: pick which channel new-member welcome messages get sent to."""
    if ctx.guild is None:
        await ctx.send("Run this in a server, not in DMs — it configures that server's welcome channel.")
        return

    await ctx.send("⚙️ **Server Setup** — pick a channel for welcome messages:", view=SetupView())


class RemoveServerSelect(discord.ui.Select):
    """One dropdown of up to 25 servers. RemoveServerView chains several of these together if the bot is in more than 25."""

    def __init__(self, guilds_chunk, label_suffix=""):
        options = [
            discord.SelectOption(
                label=guild.name[:100],
                description=f"{guild.member_count} members • ID: {guild.id}",
                value=str(guild.id),
            )
            for guild in guilds_chunk
        ]
        super().__init__(
            placeholder=f"Choose a server to remove me from{label_suffix}...",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        guild_id = int(self.values[0])
        target_guild = bot.get_guild(guild_id)

        if target_guild is None:
            await interaction.response.edit_message(content=f"⚠️ I'm not in that server anymore (or already left it).", view=None)
            return

        guild_name = target_guild.name
        await target_guild.leave()
        await interaction.response.edit_message(content=f"🚪 Left **{guild_name}** (`{guild_id}`) as requested.", view=None)


class RemoveServerView(discord.ui.View):
    """DMed to the owner when they hit Remove Server on the website. Splits into multiple dropdowns if needed (25 servers per menu, 5 menus max = 125 servers)."""

    def __init__(self, guilds):
        super().__init__(timeout=300)
        chunk_size = 25
        chunks = [guilds[i:i + chunk_size] for i in range(0, len(guilds), chunk_size)]
        for idx, chunk in enumerate(chunks[:5]):  # Discord caps a view at 5 action rows
            suffix = f" (menu {idx + 1})" if len(chunks) > 1 else ""
            self.add_item(RemoveServerSelect(chunk, suffix))


async def send_remove_server_menu_dm(user):
    """DMs the owner an interactive dropdown to pick which server to leave. Used by the website's Moderation > Remove Server button."""
    guilds = bot.guilds
    if not guilds:
        await user.send("I'm not in any servers right now.")
        return
    view = RemoveServerView(guilds)
    await user.send(f"🛡️ **Moderation — Remove Server**\nPick a server below and I'll leave it immediately.", view=view)


# ===================== Ban / Unban servers =====================
BANNED_SERVERS_FILE = "banned_servers.json"


def load_banned_servers():
    """Returns {guild_id_str: {"name": ..., "banned_at": ...}}, or {} if the file doesn't exist/is unreadable."""
    if os.path.exists(BANNED_SERVERS_FILE):
        with open(BANNED_SERVERS_FILE, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}


def save_banned_servers(data):
    with open(BANNED_SERVERS_FILE, "w") as f:
        json.dump(data, f, indent=4)


class BanServerSelect(discord.ui.Select):
    """Pick a currently-joined server to ban — bans it AND leaves it immediately."""

    def __init__(self, guilds_chunk, label_suffix=""):
        options = [
            discord.SelectOption(
                label=guild.name[:100],
                description=f"{guild.member_count} members • ID: {guild.id}",
                value=str(guild.id),
            )
            for guild in guilds_chunk
        ]
        super().__init__(placeholder=f"Choose a server to ban{label_suffix}...", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        guild_id = int(self.values[0])
        target_guild = bot.get_guild(guild_id)
        if target_guild is None:
            await interaction.response.edit_message(content="⚠️ I'm not in that server anymore.", view=None)
            return

        guild_name = target_guild.name
        banned = load_banned_servers()
        banned[str(guild_id)] = {"name": guild_name, "banned_at": time.time()}
        save_banned_servers(banned)
        await target_guild.leave()

        await interaction.response.edit_message(
            content=f"🚫 Banned **{guild_name}** (`{guild_id}`) and left it. I won't rejoin unless you unban it.",
            view=None,
        )


class BanServerView(discord.ui.View):
    def __init__(self, guilds):
        super().__init__(timeout=300)
        chunk_size = 25
        chunks = [guilds[i:i + chunk_size] for i in range(0, len(guilds), chunk_size)]
        for idx, chunk in enumerate(chunks[:5]):
            suffix = f" (menu {idx + 1})" if len(chunks) > 1 else ""
            self.add_item(BanServerSelect(chunk, suffix))


async def send_ban_server_menu_dm(user):
    """DMs the owner a dropdown to pick which currently-joined server to ban + leave."""
    guilds = bot.guilds
    if not guilds:
        await user.send("I'm not in any servers right now.")
        return
    view = BanServerView(guilds)
    await user.send("🛡️ **Moderation — Ban Server**\nPick a server below — I'll ban it and leave immediately.", view=view)


class UnbanServerSelect(discord.ui.Select):
    """Pick a banned server (built from stored records, since the bot isn't in these anymore) to unban."""

    def __init__(self, entries_chunk, label_suffix=""):
        options = [
            discord.SelectOption(label=name[:100], description=f"ID: {guild_id}", value=guild_id)
            for guild_id, name in entries_chunk
        ]
        super().__init__(placeholder=f"Choose a server to unban{label_suffix}...", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        guild_id = self.values[0]
        banned = load_banned_servers()
        entry = banned.pop(guild_id, None)
        save_banned_servers(banned)

        if entry is None:
            await interaction.response.edit_message(content="⚠️ That server's already unbanned.", view=None)
            return

        await interaction.response.edit_message(
            content=f"✅ Unbanned **{entry['name']}** (`{guild_id}`). I can be invited back there now.",
            view=None,
        )


class UnbanServerView(discord.ui.View):
    def __init__(self, entries):
        super().__init__(timeout=300)
        chunk_size = 25
        chunks = [entries[i:i + chunk_size] for i in range(0, len(entries), chunk_size)]
        for idx, chunk in enumerate(chunks[:5]):
            suffix = f" (menu {idx + 1})" if len(chunks) > 1 else ""
            self.add_item(UnbanServerSelect(chunk, suffix))


async def send_unban_server_menu_dm(user):
    """DMs the owner a dropdown of currently-banned servers to pick one to unban."""
    banned = load_banned_servers()
    if not banned:
        await user.send("You don't have any banned servers right now.")
        return
    entries = [(guild_id, data["name"]) for guild_id, data in banned.items()]
    view = UnbanServerView(entries)
    await user.send("🛡️ **Moderation — Unban Server**\nPick a server below to lift its ban.", view=view)


# ===================== CHAOS TICKET SYSTEM =====================
TICKET_CONFIG_FILE = "ticket_config.json"
TICKETS_FILE = "tickets.json"

TICKET_CATEGORIES = {
    "bug": {"emoji": "🐛", "label": "Bug Report", "desc": "Something's broken and it's chaos"},
    "shop": {"emoji": "💰", "label": "Shop / Purchase Issue", "desc": "XP, roles, or a buy gone wrong"},
    "report": {"emoji": "🚨", "label": "Report a Member", "desc": "Someone's causing real chaos"},
    "question": {"emoji": "❓", "label": "General Question", "desc": "Just need to ask something"},
    "emergency": {"emoji": "🔥", "label": "Chaos Emergency", "desc": "Everything is on fire, send help"},
}

TICKET_OPEN_LINES = [
    "Alright, buckle up — a staff member will be here shortly.",
    "Ticket secured. Chaos will be handled accordingly.",
    "Message received loud and clear. Someone's on the way.",
    "This ticket is now officially Your Problem™ (staff's, not yours).",
]


def load_ticket_config():
    if os.path.exists(TICKET_CONFIG_FILE):
        with open(TICKET_CONFIG_FILE, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}


def save_ticket_config(data):
    with open(TICKET_CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=4)


def load_tickets():
    if os.path.exists(TICKETS_FILE):
        with open(TICKETS_FILE, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}


def save_tickets(data):
    with open(TICKETS_FILE, "w") as f:
        json.dump(data, f, indent=4)


async def get_or_create_ticket_category(guild, staff_role):
    """Lazily creates (or reuses) a 'Chaos Tickets' category, private by default."""
    category = discord.utils.get(guild.categories, name="🎫 Chaos Tickets")
    if category is not None:
        return category

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    try:
        return await guild.create_category("🎫 Chaos Tickets", overwrites=overwrites, reason="Ticket system setup")
    except discord.Forbidden:
        return None


class TicketCategorySelect(discord.ui.Select):
    """Persistent select — survives bot restarts via a fixed custom_id, no per-instance state needed."""

    def __init__(self):
        options = [
            discord.SelectOption(label=data["label"], description=data["desc"], emoji=data["emoji"], value=key)
            for key, data in TICKET_CATEGORIES.items()
        ]
        super().__init__(
            placeholder="🎫 Open a ticket — pick a category...",
            options=options,
            min_values=1,
            max_values=1,
            custom_id="ticket_open_category_select",
        )

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        category_key = self.values[0]
        category_data = TICKET_CATEGORIES[category_key]
        opener = interaction.user

        tickets = load_tickets()
        guild_tickets = tickets.setdefault(str(guild.id), {})

        for existing_channel_id, info in guild_tickets.items():
            if info["opener_id"] == str(opener.id):
                existing_channel = guild.get_channel(int(existing_channel_id))
                if existing_channel:
                    await interaction.response.send_message(
                        f"You already have an open ticket: {existing_channel.mention}", ephemeral=True
                    )
                    return

        config = load_ticket_config().get(str(guild.id), {})
        staff_role = guild.get_role(int(config["staff_role_id"])) if config.get("staff_role_id") else None

        ticket_category = await get_or_create_ticket_category(guild, staff_role)
        if ticket_category is None:
            await interaction.response.send_message(
                "⚠️ I don't have permission to create ticket channels here — ask an admin to check my permissions.",
                ephemeral=True,
            )
            return

        config["ticket_counter"] = config.get("ticket_counter", 0) + 1
        ticket_number = config["ticket_counter"]
        full_config = load_ticket_config()
        full_config[str(guild.id)] = config
        save_ticket_config(full_config)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            opener: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        channel_name = f"ticket-{ticket_number:04d}-{opener.name}"[:100]
        try:
            ticket_channel = await guild.create_text_channel(
                channel_name, category=ticket_category, overwrites=overwrites,
                reason=f"Ticket opened by {opener}",
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "⚠️ I don't have permission to create ticket channels here.", ephemeral=True
            )
            return

        guild_tickets[str(ticket_channel.id)] = {
            "opener_id": str(opener.id),
            "category": category_key,
            "claimed_by": None,
            "ticket_number": ticket_number,
            "created_at": time.time(),
        }
        save_tickets(tickets)

        embed = discord.Embed(
            title=f"{category_data['emoji']} Ticket #{ticket_number:04d} — {category_data['label']}",
            description=(
                f"{random.choice(TICKET_OPEN_LINES)}\n\n"
                f"**Opened by:** {opener.mention}\n"
                f"**Category:** {category_data['label']}"
            ),
            color=discord.Color.dark_red(),
        )
        embed.set_footer(text="Use the buttons below to claim or close this ticket.")

        await ticket_channel.send(
            content=f"{opener.mention}" + (f" {staff_role.mention}" if staff_role else ""),
            embed=embed,
            view=TicketActionsView(),
        )
        await interaction.response.send_message(f"🎫 Ticket created: {ticket_channel.mention}", ephemeral=True)


class TicketPanelView(discord.ui.View):
    """The public panel members click to open a ticket. Persistent — timeout=None + fixed custom_id."""

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketCategorySelect())


async def get_or_create_ticket_log_channel(guild, staff_role):
    """Lazily creates (or reuses) a private #ticket-logs channel — closed transcripts post here
    instead of DMing anyone, so closing tickets never floods a person's DMs."""
    channel = discord.utils.get(guild.text_channels, name="ticket-logs")
    if channel is not None:
        return channel

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    try:
        return await guild.create_text_channel("ticket-logs", overwrites=overwrites, reason="Ticket log channel")
    except discord.Forbidden:
        return None


class TicketCloseReasonModal(discord.ui.Modal, title="Close Ticket"):
    reason_input = discord.ui.TextInput(
        label="Reason for closing (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
        placeholder="e.g. Issue resolved, duplicate ticket, spam...",
    )

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        tickets = load_tickets()
        guild_tickets = tickets.setdefault(str(guild.id), {})
        ticket = guild_tickets.get(str(interaction.channel.id), {
            "opener_id": str(interaction.user.id), "category": "question", "ticket_number": 0,
        })

        reason_text = self.reason_input.value.strip() or "No reason provided"
        await interaction.response.send_message(f"🔒 Closing this ticket in 5 seconds...\n**Reason:** {reason_text}")

        # Build a simple text transcript before the channel disappears
        lines = []
        async for msg in interaction.channel.history(limit=500, oldest_first=True):
            timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M")
            lines.append(f"[{timestamp}] {msg.author}: {msg.content}")
        transcript_text = "\n".join(lines) or "(no messages)"

        config = load_ticket_config().get(str(guild.id), {})
        staff_role = guild.get_role(int(config["staff_role_id"])) if config.get("staff_role_id") else None
        log_channel = await get_or_create_ticket_log_channel(guild, staff_role)

        if log_channel:
            opener = guild.get_member(int(ticket["opener_id"]))
            category_label = TICKET_CATEGORIES.get(ticket.get("category"), {}).get("label", "Unknown")

            embed = discord.Embed(
                title=f"🔒 Ticket #{ticket.get('ticket_number', 0):04d} Closed",
                color=discord.Color.dark_grey(),
            )
            embed.add_field(name="Opened by", value=opener.mention if opener else f"User {ticket['opener_id']}", inline=True)
            embed.add_field(name="Closed by", value=interaction.user.mention, inline=True)
            embed.add_field(name="Category", value=category_label, inline=True)
            embed.add_field(name="Reason", value=reason_text, inline=False)

            transcript_file = discord.File(
                io.StringIO(transcript_text), filename=f"ticket-{ticket.get('ticket_number', 0):04d}-transcript.txt"
            )
            await log_channel.send(embed=embed, file=transcript_file)

        if str(interaction.channel.id) in guild_tickets:
            del guild_tickets[str(interaction.channel.id)]
            save_tickets(tickets)

        await asyncio.sleep(5)
        try:
            await interaction.channel.delete(reason=f"Ticket closed: {reason_text}")
        except discord.Forbidden:
            pass



class TicketActionsView(discord.ui.View):
    """Claim/Close buttons inside an open ticket channel. Persistent — works after restarts since
    all needed info is looked up fresh from tickets.json using the channel ID, not stored on the view."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🙋 Claim", style=discord.ButtonStyle.primary, custom_id="ticket_claim_btn")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        config = load_ticket_config().get(str(guild.id), {})
        staff_role = guild.get_role(int(config["staff_role_id"])) if config.get("staff_role_id") else None

        is_staff_member = (
            (staff_role and staff_role in interaction.user.roles)
            or interaction.user.guild_permissions.administrator
            or (BOT_OWNER_ID and interaction.user.id == int(BOT_OWNER_ID))
        )
        if not is_staff_member:
            await interaction.response.send_message("🚫 Only staff can claim tickets.", ephemeral=True)
            return

        tickets = load_tickets()
        guild_tickets = tickets.setdefault(str(guild.id), {})
        ticket = guild_tickets.get(str(interaction.channel.id))
        if ticket is None:
            await interaction.response.send_message("⚠️ This doesn't look like an active ticket.", ephemeral=True)
            return

        ticket["claimed_by"] = str(interaction.user.id)
        save_tickets(tickets)

        button.label = f"Claimed by {interaction.user.display_name}"
        button.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(f"🙋 {interaction.user.mention} claimed this ticket — the chaos is contained.")

    @discord.ui.button(label="🔒 Close Ticket", style=discord.ButtonStyle.danger, custom_id="ticket_close_btn")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        tickets = load_tickets()
        guild_tickets = tickets.setdefault(str(guild.id), {})
        ticket = guild_tickets.get(str(interaction.channel.id))
        if ticket is None:
            await interaction.response.send_message("⚠️ This doesn't look like an active ticket.", ephemeral=True)
            return

        config = load_ticket_config().get(str(guild.id), {})
        staff_role = guild.get_role(int(config["staff_role_id"])) if config.get("staff_role_id") else None
        is_staff_member = (
            (staff_role and staff_role in interaction.user.roles)
            or interaction.user.guild_permissions.administrator
            or (BOT_OWNER_ID and interaction.user.id == int(BOT_OWNER_ID))
        )
        is_opener = str(interaction.user.id) == ticket["opener_id"]
        if not (is_staff_member or is_opener):
            await interaction.response.send_message("🚫 Only the ticket opener or staff can close this.", ephemeral=True)
            return

        await interaction.response.send_modal(TicketCloseReasonModal())


class TicketSetupChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(
            placeholder="Choose a channel for the ticket panel...",
            channel_types=[discord.ChannelType.text],
            min_values=1, max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.panel_channel_id = self.values[0].id
        await interaction.response.defer()


class TicketSetupRoleSelect(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(placeholder="Choose the staff role for tickets...", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        self.view.staff_role_id = self.values[0].id
        await interaction.response.defer()


class TicketSetupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.panel_channel_id = None
        self.staff_role_id = None
        self.add_item(TicketSetupChannelSelect())
        self.add_item(TicketSetupRoleSelect())

    @discord.ui.button(label="✅ Post Ticket Panel", style=discord.ButtonStyle.success, row=2)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.panel_channel_id is None or self.staff_role_id is None:
            await interaction.response.send_message("Pick both a channel and a staff role first.", ephemeral=True)
            return

        guild = interaction.guild
        channel = guild.get_channel(self.panel_channel_id)
        if channel is None:
            await interaction.response.send_message("⚠️ Couldn't find that channel — try again.", ephemeral=True)
            return

        config = load_ticket_config()
        config[str(guild.id)] = {
            **config.get(str(guild.id), {}),
            "panel_channel_id": str(self.panel_channel_id),
            "staff_role_id": str(self.staff_role_id),
        }
        save_ticket_config(config)

        embed = discord.Embed(
            title="🎫 Need Help? Open a Ticket",
            description="Pick a category from the dropdown below and a private channel will be created just for you.",
            color=discord.Color.dark_red(),
        )
        await channel.send(embed=embed, view=TicketPanelView())

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"✅ Ticket panel posted in {channel.mention}, staff role set.", view=self
        )


@bot.hybrid_command()
@is_admin_or_owner()
async def ticketsetup(ctx):
    """Admin only: set up the chaos ticket system — pick a panel channel and staff role."""
    if ctx.guild is None:
        await ctx.send("Run this in a server, not in DMs.")
        return

    await ctx.send("⚙️ **Ticket Setup** — configure both, then hit Post Ticket Panel:", view=TicketSetupView())


@bot.hybrid_command()
@is_admin_or_owner()
async def ticketpanel(ctx):
    """Admin only: resend the ticket panel to its configured channel — handy if it's buried."""
    if ctx.guild is None:
        await ctx.send("Run this in a server, not in DMs.")
        return

    config = load_ticket_config().get(str(ctx.guild.id), {})
    channel_id = config.get("panel_channel_id")
    if not channel_id:
        await ctx.send("No ticket panel has been set up yet — run `/ticketsetup` first.")
        return

    channel = ctx.guild.get_channel(int(channel_id))
    if channel is None:
        await ctx.send("⚠️ The configured panel channel doesn't exist anymore — run `/ticketsetup` again to pick a new one.")
        return

    embed = discord.Embed(
        title="🎫 Need Help? Open a Ticket",
        description="Pick a category from the dropdown below and a private channel will be created just for you.",
        color=discord.Color.dark_red(),
    )
    await channel.send(embed=embed, view=TicketPanelView())
    await ctx.send(f"✅ Ticket panel resent in {channel.mention}.")


