import os
import json
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
MEMBER_ROLE_ID = os.getenv("MEMBER_ROLE_ID")
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


async def ensure_level_roles(guild):
    """Creates the 10 tier roles in this server if they don't already exist, and remembers their IDs."""
    data = load_guild_level_roles()
    guild_id = str(guild.id)
    if guild_id not in data:
        data[guild_id] = {}

    changed = False
    for threshold, name in LEVEL_ROLE_NAMES.items():
        key = str(threshold)
        existing_id = data[guild_id].get(key)
        role = guild.get_role(int(existing_id)) if existing_id else None

        if role is None:
            # Reuse a role with the matching name if one already exists (avoids duplicates)
            role = discord.utils.get(guild.roles, name=name)
            if role is None:
                try:
                    role = await guild.create_role(name=name, reason="Auto-created leveling tier role")
                    print(f"✅ Created role '{name}' in {guild.name}")
                except discord.Forbidden:
                    print(f"⚠️ Missing permission to create role '{name}' in {guild.name}")
                    continue
            data[guild_id][key] = str(role.id)
            changed = True

    if changed:
        save_guild_level_roles(data)

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
_last_xp_time = {}  # in-memory cooldown tracker: user_id -> last timestamp


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
    streaks = load_streaks()
    user_id = str(member.id)

    streaks[user_id] = streaks.get(user_id, 0) + 1
    streak = streaks[user_id]
    save_streaks(streaks)

    xp_reward = min(50 * streak, 500)  # 50 XP per streak level, capped at 500

    levels = load_levels()
    old_level, new_level = apply_xp_change(levels, user_id, xp_reward)
    save_levels(levels)

    await ctx.send(
        f"🔥 **Win streak: {streak}!** {member.mention} earned **+{xp_reward} XP**!"
    )

    if new_level > old_level:
        await ctx.send(f"🎉 {member.mention} leveled up to **Level {new_level}**!")
        await update_level_role(member, new_level)


async def reset_streak(ctx, member):
    """Call this when a player loses or ties a game. Resets their win streak back to 0 and announces it."""
    streaks = load_streaks()
    user_id = str(member.id)
    streaks[user_id] = 0
    save_streaks(streaks)
    await ctx.send(f"💔 {member.mention}'s win streak has been reset to **0**.")


async def update_level_role(member, level):
    """Gives the member the correct tier role for their level, removing older tier roles."""
    data = load_guild_level_roles()
    guild_roles = data.get(str(member.guild.id), {})

    # Find the highest threshold this level qualifies for
    eligible_threshold = None
    for threshold in sorted(LEVEL_ROLE_NAMES.keys()):
        key = str(threshold)
        if key in guild_roles and level >= threshold:
            eligible_threshold = threshold

    if eligible_threshold is None:
        return  # This server's roles aren't set up yet, or level doesn't qualify for any tier

    target_role = member.guild.get_role(int(guild_roles[str(eligible_threshold)]))
    if not target_role:
        print(f"⚠️ Level role for threshold {eligible_threshold} not found in {member.guild.name}")
        return

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
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")
    # Make sure every server the bot is already in has its level-tier roles set up
    for guild in bot.guilds:
        await ensure_level_roles(guild)
    # Sync the website's "trusted by X servers" count in case it drifted while offline
    await update_website_stats()


@bot.event
async def on_guild_remove(guild):
    """Runs whenever the bot is removed from a server — keeps the website's server count accurate."""
    print(f"📤 Left a server: {guild.name}")
    await update_website_stats()


@bot.event
async def on_guild_join(guild):
    """Runs whenever the bot is added to a new server — auto-creates the level tier roles,
    unless invites are currently paused, in which case the bot immediately leaves instead."""
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
    await ensure_level_roles(guild)
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

        return  # never treat webhook messages as chat/commands

    if message.author.bot:
        await bot.process_commands(message)
        return

    user_id = str(message.author.id)
    now = time.time()
    last_time = _last_xp_time.get(user_id, 0)

    if now - last_time >= XP_COOLDOWN_SECONDS:
        _last_xp_time[user_id] = now

        levels = load_levels()
        old_level, new_level = apply_xp_change(levels, user_id, random.randint(15, 25))
        save_levels(levels)

        if new_level > old_level:
            await message.channel.send(
                f"🎉 {message.author.mention} leveled up to **Level {new_level}**!"
            )
            await update_level_role(message.author, new_level)

    # IMPORTANT: without this line, none of the !commands would work anymore
    await bot.process_commands(message)


@bot.event
async def on_member_join(member):
    """Runs whenever a new member joins the server."""
    # Auto-detect a channel to welcome in — uses the server's configured system channel,
    # or falls back to the first text channel the bot can post in. No manual ID setup needed.
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

    # Assign the auto-role
    if MEMBER_ROLE_ID:
        role = member.guild.get_role(int(MEMBER_ROLE_ID))
        if role:
            try:
                await member.add_roles(role)
                print(f"✅ Gave {member} the '{role.name}' role")
            except discord.Forbidden:
                print("⚠️ Bot doesn't have permission to assign this role — check role position/permissions")
        else:
            print("⚠️ Member role not found — check MEMBER_ROLE_ID in .env")


@bot.command()
async def hello(ctx):
    """Responds with a greeting."""
    await ctx.send(f"Hey {ctx.author.mention}! 👋")


@bot.command()
async def ping(ctx):
    """Responds with the bot's latency."""
    latency = round(bot.latency * 1000)
    await ctx.send(f"Pong! 🏓 ({latency}ms)")


@bot.command()
async def rank(ctx, member: discord.Member = None):
    """Shows your (or someone else's) level and XP."""
    member = member or ctx.author
    levels = load_levels()
    user_id = str(member.id)

    if user_id not in levels:
        await ctx.send(f"{member.mention} hasn't earned any XP yet — start chatting!")
        return

    data = levels[user_id]
    needed = xp_needed_for(data["level"])

    embed = discord.Embed(title=f"📊 Rank — {member.display_name}", color=discord.Color.blue())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Level", value=str(data["level"]), inline=True)
    embed.add_field(name="XP", value=f"{data['xp']} / {needed}", inline=True)
    await ctx.send(embed=embed)


@bot.command()
async def leaderboard(ctx):
    """Shows the top 10 members by level and XP."""
    levels = load_levels()
    if not levels:
        await ctx.send("No one has earned XP yet — get chatting!")
        return

    sorted_users = sorted(
        levels.items(),
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


@bot.command()
async def streak(ctx, member: discord.Member = None):
    """Shows your (or someone else's) current game win streak."""
    member = member or ctx.author
    streaks = load_streaks()
    current = streaks.get(str(member.id), 0)
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


@bot.command()
@is_bot_owner()
async def addxp(ctx, member: discord.Member, amount: int):
    """Adds XP to a member (Owner only). Safe for any size number — uses instant math, no loop."""
    levels = load_levels()
    user_id = str(member.id)

    old_level, new_level = apply_xp_change(levels, user_id, amount)
    save_levels(levels)

    await ctx.send(
        f"✅ Gave **{amount} XP** to {member.mention}. "
        f"Now Level **{new_level}** ({levels[user_id]['xp']} XP)"
    )

    if new_level > old_level:
        await update_level_role(member, new_level)


@bot.command()
@is_bot_owner()
async def removexp(ctx, member: discord.Member, amount: int):
    """Removes XP from a member (Owner only). The XP just vanishes — for cheaters. Safe for any size number."""
    levels = load_levels()
    user_id = str(member.id)

    old_level, new_level = apply_xp_change(levels, user_id, -amount)
    save_levels(levels)

    await ctx.send(
        f"🗑️ Removed **{amount} XP** from {member.mention}. "
        f"Now Level **{new_level}** ({levels[user_id]['xp']} XP)"
    )

    if new_level < old_level:
        await update_level_role(member, new_level)


@bot.command()
@is_staff()
async def kick(ctx, member: discord.Member, *, reason="No reason provided"):
    """Kicks a member from the server."""
    if is_protected_target(ctx, member):
        await ctx.send("🚫 You cannot take action against an Owner.")
        return
    await member.kick(reason=reason)
    await ctx.send(f"👢 **{member}** was kicked. Reason: {reason}")


@bot.command()
@is_staff()
async def ban(ctx, member: discord.Member, *, reason="No reason provided"):
    """Bans a member from the server."""
    if is_protected_target(ctx, member):
        await ctx.send("🚫 You cannot take action against an Owner.")
        return
    await member.ban(reason=reason)
    await ctx.send(f"🔨 **{member}** was banned. Reason: {reason}")


@bot.command()
@is_staff()
async def mute(ctx, member: discord.Member, minutes: int, *, reason="No reason provided"):
    """Times out (mutes) a member for a set number of minutes."""
    if is_protected_target(ctx, member):
        await ctx.send("🚫 You cannot take action against an Owner.")
        return
    duration = timedelta(minutes=minutes)
    await member.timeout(duration, reason=reason)
    await ctx.send(f"🔇 **{member}** was muted for {minutes} minute(s). Reason: {reason}")


@bot.command()
@is_staff()
async def unmute(ctx, member: discord.Member):
    """Removes a timeout from a member."""
    await member.timeout(None)
    await ctx.send(f"🔊 **{member}** has been unmuted.")


@bot.command()
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


@bot.command()
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


@bot.command()
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


@bot.command()
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


@bot.command()
async def duel(ctx, opponent: discord.Member, wager: int):
    """Challenge another member to a Chaos Duel — wager XP, winner takes it all. Choices are
    made via private buttons right in the channel, so nobody needs to leave and check DMs."""

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
    challenger_id = str(ctx.author.id)
    opponent_id = str(opponent.id)

    def total_xp(user_id):
        data = levels.get(user_id, {"xp": 0, "level": 0})
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
    _, winner_new_level = apply_xp_change(levels, str(winner.id), wager)
    _, loser_new_level = apply_xp_change(levels, str(loser.id), -wager)
    save_levels(levels)

    await ctx.send(f"🏆 {winner.mention} wins the duel and takes **{wager} XP** from {loser.mention}!")

    await update_level_role(winner, winner_new_level)
    await update_level_role(loser, loser_new_level)


INVESTMENTS_FILE = "investments.json"


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


@bot.command()
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

    levels = load_levels()
    investor_id = str(ctx.author.id)
    target_id = str(target.id)

    investor_xp = get_total_xp(levels, investor_id)
    if investor_xp < amount:
        await ctx.send(f"You don't have {amount} XP to invest!")
        return

    target_xp = get_total_xp(levels, target_id)
    if target_xp <= 0:
        await ctx.send(f"{target.mention} hasn't earned any XP yet — nothing to invest in!")
        return

    investments = load_investments()
    if investor_id not in investments:
        investments[investor_id] = {}

    if target_id in investments[investor_id]:
        await ctx.send(
            f"You already have an active investment in {target.mention}. "
            f"Cash out first with `!cashout @{target.display_name}` before investing again."
        )
        return

    apply_xp_change(levels, investor_id, -amount)
    save_levels(levels)

    investments[investor_id][target_id] = {
        "amount": amount,
        "base_xp": target_xp,
    }
    save_investments(investments)

    await ctx.send(
        f"📈 {ctx.author.mention} invested **{amount} XP** in {target.mention}! Watch their growth closely..."
    )


@bot.command()
async def portfolio(ctx, member: discord.Member = None):
    """Shows your (or someone else's) active investments and their current value."""
    member = member or ctx.author
    investments = load_investments()
    investor_id = str(member.id)

    if investor_id not in investments or not investments[investor_id]:
        await ctx.send(f"{member.mention} has no active investments.")
        return

    levels = load_levels()
    embed = discord.Embed(title=f"📊 {member.display_name}'s Portfolio", color=discord.Color.green())

    for target_id, inv in investments[investor_id].items():
        target_member = ctx.guild.get_member(int(target_id))
        name = target_member.display_name if target_member else f"User {target_id}"

        current_target_xp = get_total_xp(levels, target_id)
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


@bot.command()
async def cashout(ctx, target: discord.Member):
    """Cash out your investment in a member, converting its current value back to your own XP."""
    investments = load_investments()
    investor_id = str(ctx.author.id)
    target_id = str(target.id)

    if investor_id not in investments or target_id not in investments[investor_id]:
        await ctx.send(f"You don't have an active investment in {target.mention}.")
        return

    levels = load_levels()
    inv = investments[investor_id][target_id]

    current_target_xp = get_total_xp(levels, target_id)
    base_xp = max(inv["base_xp"], 1)
    current_value = max(int(inv["amount"] * (current_target_xp / base_xp)), 0)

    old_level, new_level = apply_xp_change(levels, investor_id, current_value)
    save_levels(levels)

    del investments[investor_id][target_id]
    save_investments(investments)

    change = current_value - inv["amount"]
    emoji = "🤑" if change >= 0 else "💸"
    await ctx.send(
        f"{emoji} {ctx.author.mention} cashed out their investment in {target.mention}: "
        f"**{inv['amount']} XP** → **{current_value} XP** ({'+' if change >= 0 else ''}{change} XP)"
    )

    if new_level > old_level:
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


@bot.command()
@is_bot_owner()
async def servers(ctx):
    """Owner-only: DMs you a styled list of every server the bot is in, with each server's owner."""
    await send_server_list_dm(ctx.author)
    if ctx.guild:  # only try to confirm in-channel if this wasn't already a DM
        await ctx.send("📩 Sent you the full list in DMs!")

    if ctx.guild:  # only try to confirm in-channel if this wasn't already a DM
        await ctx.send("📩 Sent you the full list in DMs!")


bot.run(TOKEN)
