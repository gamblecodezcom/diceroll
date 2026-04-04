"""
Telegram Dice Hunt bot — per-group games, HTML messages, deep-link join for DMs.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden
from telegram.ext import Application, CommandHandler, ContextTypes

from keep_alive import keep_alive

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("dice_hunt")

# --- Config ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
JOIN_PAYLOAD_PREFIX = "join_"
HUNT_DURATION_SEC = 210
HALFWAY_REMINDER_SEC = 105
ABORT_WINDOW_SEC = 30
ROLL_MIN, ROLL_MAX = 1, 100


def _prize_link_html(url: str) -> str:
    """Clickable anchor + visible escaped URL for Telegram HTML."""
    href = html.escape(url, quote=True)
    visible = html.escape(url, quote=False)
    return f'<a href="{href}">Open prize link</a>\n{visible}'


def _parse_admin_ids() -> set[int]:
    raw = os.environ.get("BOT_ADMINS", "").strip()
    if not raw:
        return set()
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


BOT_ADMINS = _parse_admin_ids()
# If true, only Telegram chat admins (or BOT_ADMINS) may /create_hunt and /abort_hunt in groups
RESTRICT_HUNT_COMMANDS = os.environ.get("RESTRICT_HUNT_COMMANDS", "").lower() in (
    "1",
    "true",
    "yes",
)


@dataclass
class HuntGame:
    is_active: bool = False
    target: int = 0
    reward_link: str = ""
    players: dict[int, dict[str, Any]] = field(default_factory=dict)
    winner_id: int | None = None
    start_time: float = 0.0
    joined_private: set[int] = field(default_factory=set)
    timer_task: asyncio.Task | None = None


# One active hunt per chat (group/supergroup/private)
games: dict[int, HuntGame] = {}


def _game(chat_id: int) -> HuntGame:
    if chat_id not in games:
        games[chat_id] = HuntGame()
    return games[chat_id]


def _bot_username(context: ContextTypes.DEFAULT_TYPE) -> str:
    u = context.bot.username
    if u:
        return u
    return os.environ.get("BOT_USERNAME", "YOUR_BOT_USERNAME")


def join_keyboard(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> InlineKeyboardMarkup:
    uname = _bot_username(context)
    url = f"https://t.me/{uname}?start={JOIN_PAYLOAD_PREFIX}{chat_id}"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🎲 Open bot & join this hunt (for prize DM)",
                    url=url,
                )
            ]
        ]
    )


async def _is_privileged_in_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    if not RESTRICT_HUNT_COMMANDS:
        return True
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return False
    if user.id in BOT_ADMINS:
        return True
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in ("creator", "administrator")
    except (BadRequest, Forbidden):
        return False


def _cancel_timer(game: HuntGame) -> None:
    t = game.timer_task
    if t and not t.done():
        t.cancel()
    game.timer_task = None


async def hunt_timer(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    game = _game(chat_id)
    try:
        await asyncio.sleep(HALFWAY_REMINDER_SEC)
        if game.is_active:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "⏳ <b>Halfway through the hunt.</b>\n"
                    "Roll now with <code>/roll</code> before time runs out."
                ),
                parse_mode="HTML",
                reply_markup=join_keyboard(context, chat_id),
            )
        await asyncio.sleep(HUNT_DURATION_SEC - HALFWAY_REMINDER_SEC)
        if game.is_active:
            game.is_active = False
            await announce_winner(chat_id, context)
    except asyncio.CancelledError:
        log.debug("Timer cancelled for chat %s", chat_id)
        raise


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat = update.effective_chat
    user = update.effective_user
    args = context.args or []

    # Deep link from group: /start join_<chat_id>
    if args and args[0].startswith(JOIN_PAYLOAD_PREFIX):
        rest = args[0][len(JOIN_PAYLOAD_PREFIX) :]
        if rest.lstrip("-").isdigit():
            target_chat_id = int(rest)
            g = _game(target_chat_id)
            if not g.is_active:
                await update.message.reply_html(
                    "There is no active hunt in that chat right now.\n"
                    "Wait for the next round or ask a mod to start one."
                )
                return
            g.joined_private.add(user.id)
            await update.message.reply_html(
                "<b>You are registered for the current hunt.</b>\n\n"
                "Go back to the group and send <code>/roll</code> before the timer ends.\n"
                "If you win, the bot will DM you the prize link — "
                "keep this chat open and tap <b>Start</b> if Telegram asks."
            )
            return

    text = (
        "<b>Deep Dickin Degen Den — Dice Hunt</b>\n\n"
        "One round, one hidden target number. Closest roll wins; "
        "the prize link is sent in <b>private</b> to the winner.\n\n"
        "<b>In a group</b>\n"
        "• Tap <b>Join</b> on the hunt message (or use the link) and press "
        "<b>Start</b> here — then <code>/roll</code> in the group.\n"
        "• <code>/create_hunt &lt;prize_url&gt;</code> — start a "
        f"{HUNT_DURATION_SEC // 60}m {HUNT_DURATION_SEC % 60}s round (link is deleted if allowed).\n"
        "• <code>/roll</code> — roll once per hunt.\n"
        f"• <code>/abort_hunt</code> — cancel in the first {ABORT_WINDOW_SEC}s.\n\n"
        "<b>Commands</b>\n"
        "<code>/help</code> — full help\n"
        "<code>/rules</code> — rules\n"
        "<code>/status</code> — hunt status in this chat\n"
        "<code>/join</code> — in a group: button to open the bot and register\n\n"
        "<i>Tip: If prize DMs fail, the winner must open this bot and press Start.</i>"
    )
    await update.message.reply_html(text)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    restrict = "On" if RESTRICT_HUNT_COMMANDS else "Off"
    admins_note = (
        f"<code>BOT_ADMINS</code> and chat admins can always run hunt commands when "
        f"restriction is on.\n"
        if RESTRICT_HUNT_COMMANDS
        else ""
    )
    await update.message.reply_html(
        "<b>Commands</b>\n"
        f"<code>/start</code> — intro &amp; register via join link\n"
        f"<code>/create_hunt &lt;url&gt;</code> — new hunt ({HUNT_DURATION_SEC}s)\n"
        f"<code>/roll</code> — one roll ({ROLL_MIN}–{ROLL_MAX})\n"
        f"<code>/abort_hunt</code> — abort within {ABORT_WINDOW_SEC}s\n"
        f"<code>/status</code> — players &amp; time left\n"
        f"<code>/join</code> — group: join link for DMs\n"
        f"<code>/rules</code> — how scoring works\n\n"
        f"<b>Host settings</b>\n"
        f"Command restriction (env): <b>{restrict}</b>\n"
        f"{admins_note}"
        f"<code>BOT_TOKEN</code> required. Optional: <code>PORT</code>, "
        f"<code>BIND</code>, <code>BOT_USERNAME</code> (if discovery fails).\n\n"
        "<b>Deploy</b>\n"
        "Replit / fps.ms: set env vars, run <code>python bot.py</code>. "
        "Use the web port from <code>PORT</code> for keep-alive pings."
    )


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_html(
        "<b>Rules</b>\n"
        f"• The bot picks a secret integer from {ROLL_MIN} to {ROLL_MAX}.\n"
        "• Each player may roll once per hunt.\n"
        "• Closest roll to the target wins (absolute difference).\n"
        "• Ties: earliest roll wins (first registered roll in our log).\n"
        "• Prize URL is DM’d to the winner; others never see it in chat.\n"
        "• In groups you should tap <b>Join</b> first so the bot can DM you."
    )


async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Post an inline button that opens the bot with a deep link to register for this chat's hunt."""
    if not update.message:
        return
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_html(
            "Use <code>/join</code> in a <b>group</b> where a hunt is running."
        )
        return
    g = _game(chat.id)
    if not g.is_active:
        await update.message.reply_html("No active hunt in this group right now.")
        return
    await update.message.reply_html(
        "<b>Join this hunt</b>\n"
        "Tap the button, press <b>Start</b> in private chat with the bot, "
        "then return here and send <code>/roll</code>.",
        reply_markup=join_keyboard(context, chat.id),
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    cid = update.effective_chat.id
    g = _game(cid)
    if not g.is_active:
        await update.message.reply_html("No hunt is running in this chat.")
        return
    elapsed = time.time() - g.start_time
    left = max(0, int(HUNT_DURATION_SEC - elapsed))
    lines = [
        "<b>Hunt status</b>",
        f"Target is hidden (range {ROLL_MIN}–{ROLL_MAX}).",
        f"Time left: ~{left}s",
        f"Players rolled: {len(g.players)}",
        f"Registered for DM: {len(g.joined_private)}",
    ]
    if g.players:
        lines.append("")
        for uid, data in g.players.items():
            name = html.escape(str(data.get("name", "?")))
            lines.append(f"• {name}: <code>{data.get('roll')}</code>")
    await update.message.reply_html("\n".join(lines))


async def create_hunt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat = update.effective_chat
    chat_id = chat.id

    if chat.type not in ("group", "supergroup"):
        await update.message.reply_html(
            "Start hunts in a <b>group</b> or <b>supergroup</b> so everyone can play."
        )
        return

    if not await _is_privileged_in_chat(update, context):
        await update.message.reply_html(
            "Only chat admins can start or abort hunts in this group "
            "(host set <code>RESTRICT_HUNT_COMMANDS</code>)."
        )
        return

    try:
        await update.message.delete()
    except BadRequest:
        log.warning("Could not delete /create_hunt message; grant Delete Messages to hide the link.")

    g = _game(chat_id)
    if g.is_active:
        await context.bot.send_message(
            chat_id=chat_id,
            text="A hunt is already running. ⏳",
            parse_mode="HTML",
        )
        return

    if not context.args:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Usage: <code>/create_hunt &lt;prize_url&gt;</code>",
            parse_mode="HTML",
        )
        return

    reward = context.args[0].strip()
    if not re.match(r"https?://", reward, re.I):
        await context.bot.send_message(
            chat_id=chat_id,
            text="Please use an <code>http://</code> or <code>https://</code> prize link.",
            parse_mode="HTML",
        )
        return

    _cancel_timer(g)
    g.is_active = True
    g.target = random.randint(ROLL_MIN, ROLL_MAX)
    g.reward_link = reward
    g.players.clear()
    g.winner_id = None
    g.joined_private.clear()
    g.start_time = time.time()

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "🔔 <b>The hunt begins!</b>\n"
            f"A secret target from {ROLL_MIN}–{ROLL_MAX} was chosen.\n"
            f"You have <b>{HUNT_DURATION_SEC}s</b> to roll.\n"
            f"You may <code>/abort_hunt</code> within the first <b>{ABORT_WINDOW_SEC}s</b>.\n\n"
            "<b>Important:</b> Tap the button below and press <b>Start</b> in private chat "
            "so the bot can DM the prize if you win. Then send <code>/roll</code> here."
        ),
        parse_mode="HTML",
        reply_markup=join_keyboard(context, chat_id),
    )

    g.timer_task = asyncio.create_task(hunt_timer(chat_id, context))


async def abort_hunt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = update.effective_chat.id
    g = _game(chat_id)

    if chat_id < 0 and not await _is_privileged_in_chat(update, context):
        await update.message.reply_html(
            "Only chat admins can abort when restriction is enabled."
        )
        return

    if not g.is_active:
        await update.message.reply_html("No hunt is active.")
        return

    elapsed = time.time() - g.start_time
    if elapsed <= ABORT_WINDOW_SEC:
        g.is_active = False
        _cancel_timer(g)
        await update.message.reply_html("Hunt cancelled. 🏁")
    else:
        await update.message.reply_html(
            f"Too late — abort is only allowed in the first {ABORT_WINDOW_SEC}s."
        )


async def roll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat = update.effective_chat
    chat_id = chat.id
    g = _game(chat_id)

    if not g.is_active:
        return

    user = update.effective_user
    user_id = user.id
    user_name = user.first_name or user.username or str(user_id)

    if chat.type in ("group", "supergroup") and user_id not in g.joined_private:
        await update.message.reply_html(
            "<b>Register first</b> so we can DM your prize.\n"
            "Tap the button below, press <b>Start</b>, then roll again here.",
            reply_markup=join_keyboard(context, chat_id),
        )
        return

    if user_id in g.players:
        await update.message.reply_html("You already rolled this hunt. 🛑")
        return

    val = random.randint(ROLL_MIN, ROLL_MAX)
    g.players[user_id] = {"name": user_name, "roll": val, "ts": time.time()}
    safe = html.escape(user_name)
    await update.message.reply_html(
        f"{safe} rolled <b>{val}</b>. 🎲",
    )


async def announce_winner(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    g = _game(chat_id)
    if not g.players:
        await context.bot.send_message(
            chat_id=chat_id,
            text="No rolls — no winner. The prize link stays hidden. 🌑",
            parse_mode="HTML",
        )
        g.is_active = False
        _cancel_timer(g)
        return

    # Closest wins; tie-break: earliest roll (lower ts)
    best: tuple[int, float, int] | None = None  # (diff, ts, uid)
    winner_name = ""
    winning_roll = 0

    for uid, data in g.players.items():
        diff = abs(g.target - int(data["roll"]))
        ts = float(data.get("ts", 0))
        key = (diff, ts, uid)
        if best is None or key < best:
            best = key
            g.winner_id = uid
            winner_name = str(data.get("name", "?"))
            winning_roll = int(data["roll"])

    assert g.winner_id is not None and best is not None
    closest_diff = best[0]

    dm_success = False
    try:
        await context.bot.send_message(
            chat_id=g.winner_id,
            text=(
                "👑 <b>You won the hunt!</b>\n\n"
                f"Your roll: <code>{winning_roll}</code>\n"
                f"Target: <code>{g.target}</code>\n"
                f"Off by: <code>{closest_diff}</code>\n\n"
                "🎁 <b>Prize link:</b>\n"
                f"{_prize_link_html(g.reward_link)}"
            ),
            parse_mode="HTML",
            disable_web_page_preview=False,
        )
        dm_success = True
    except Forbidden:
        dm_success = False
    except BadRequest:
        dm_success = False

    wn = html.escape(winner_name)
    caption = (
        "⚔️ <b>Hunt over!</b>\n"
        f"Target was <code>{g.target}</code>.\n\n"
        f"👑 Winner: {wn} with <code>{winning_roll}</code> "
        f"(off by <code>{closest_diff}</code>).\n\n"
    )
    if dm_success:
        caption += "🔒 Prize link sent to the winner in private chat."
    else:
        caption += (
            "⚠️ Could not DM the winner. They should open the bot and press "
            "<b>Start</b>, then ask an admin to remit the prize."
        )

    g.is_active = False
    _cancel_timer(g)

    try:
        photos = await context.bot.get_user_profile_photos(user_id=g.winner_id, limit=1)
        if photos.photos:
            best_photo = photos.photos[0][-1]
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=best_photo.file_id,
                caption=caption,
                parse_mode="HTML",
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=caption,
                parse_mode="HTML",
            )
    except Exception:
        log.exception("Photo announce failed; sending text")
        await context.bot.send_message(
            chat_id=chat_id,
            text=caption,
            parse_mode="HTML",
        )


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN in the environment.")

    keep_alive()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .build()
    )

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("rules", cmd_rules))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("join", cmd_join))
    application.add_handler(CommandHandler("create_hunt", create_hunt))
    application.add_handler(CommandHandler("abort_hunt", abort_hunt))
    application.add_handler(CommandHandler("roll", roll))

    log.info("Dice Hunt bot polling…")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
