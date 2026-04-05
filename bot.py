"""
Telegram Dice Roll bot — Cwallet-style claim URL, winner-only reveal via callbacks.

Telegram has no per-user message bubbles in groups. The "whisper" is implemented with
CallbackQuery: answerCallbackQuery (popup) and/or a private DM with the full URL.

HTML: only supported tags (<b>, <code>, <a>, …). Newlines are literal, not <br>.
https://core.telegram.org/bots/api#html-style
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import random
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from keep_alive import keep_alive

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("dice_roll")

# --- Config ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
JOIN_PAYLOAD_PREFIX = "join_"
ROUND_DURATION_SEC = 210
HALFWAY_REMINDER_SEC = 105
ABORT_WINDOW_SEC = 30
ROLL_MIN, ROLL_MAX = 1, 100
# Callback data must stay within Telegram's 64-byte limit; "r" + 16 hex = 17 bytes
REVEAL_NONCE_HEX_BYTES = 8
# Popup text max ~200 chars; long URLs go to DM
CALLBACK_ALERT_MAX = 180


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
RESTRICT_ROLL_COMMANDS = os.environ.get("RESTRICT_ROLL_COMMANDS", "").lower() in (
    "1",
    "true",
    "yes",
)
# Legacy env name (hunt) still honored
if os.environ.get("RESTRICT_HUNT_COMMANDS", "").lower() in ("1", "true", "yes"):
    RESTRICT_ROLL_COMMANDS = True


# nonce (hex) -> chat_id for inline "reveal" buttons (cleared when a new round starts)
_reveal_nonce_to_chat: dict[str, int] = {}


def _prize_link_html(url: str) -> str:
    href = html.escape(url, quote=True)
    visible = html.escape(url, quote=False)
    return f'<a href="{href}">Open claim link</a>\n{visible}'


@dataclass
class DiceRound:
    is_active: bool = False
    target: int = 0
    claim_url: str = ""
    players: dict[int, dict[str, Any]] = field(default_factory=dict)
    winner_id: int | None = None
    start_time: float = 0.0
    joined_private: set[int] = field(default_factory=set)
    timer_task: asyncio.Task | None = None
    reveal_nonce: str | None = None


games: dict[int, DiceRound] = {}


def _game(chat_id: int) -> DiceRound:
    if chat_id not in games:
        games[chat_id] = DiceRound()
    return games[chat_id]


def _invalidate_reveal(round_state: DiceRound) -> None:
    if round_state.reveal_nonce:
        _reveal_nonce_to_chat.pop(round_state.reveal_nonce, None)
        round_state.reveal_nonce = None


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
                    "🎲 Open bot & join this round (for DMs)",
                    url=url,
                )
            ]
        ]
    )


def winner_reveal_keyboard(nonce: str) -> InlineKeyboardMarkup:
    """Callback payload r<nonce>; only the stored winner may receive the URL."""
    data = f"r{nonce}"
    if len(data.encode("utf-8")) > 64:
        log.error("callback_data too long: %s", len(data))
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔒 Reveal claim link (winner only)",
                    callback_data=data,
                )
            ]
        ]
    )


async def _is_privileged_in_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    if not RESTRICT_ROLL_COMMANDS:
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


def _cancel_timer(round_state: DiceRound) -> None:
    t = round_state.timer_task
    if t and not t.done():
        t.cancel()
    round_state.timer_task = None


async def round_timer(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    g = _game(chat_id)
    try:
        await asyncio.sleep(HALFWAY_REMINDER_SEC)
        if g.is_active:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "⏳ <b>Halfway through the round.</b>\n"
                    "Roll with <code>/roll</code> before time runs out."
                ),
                parse_mode="HTML",
                reply_markup=join_keyboard(context, chat_id),
            )
        await asyncio.sleep(ROUND_DURATION_SEC - HALFWAY_REMINDER_SEC)
        if g.is_active:
            g.is_active = False
            await announce_winner(chat_id, context)
    except asyncio.CancelledError:
        log.debug("Timer cancelled for chat %s", chat_id)
        raise


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    args = context.args or []

    if args and args[0].startswith(JOIN_PAYLOAD_PREFIX):
        rest = args[0][len(JOIN_PAYLOAD_PREFIX) :]
        if rest.lstrip("-").isdigit():
            target_chat_id = int(rest)
            g = _game(target_chat_id)
            if not g.is_active:
                await update.message.reply_html(
                    "No active dice round in that chat.\n"
                    "Wait for the next one or ask a mod to start it."
                )
                return
            g.joined_private.add(update.effective_user.id)
            await update.message.reply_html(
                "<b>You are registered for this round.</b>\n\n"
                "Return to the group and send <code>/roll</code> before the timer ends.\n"
                "If you win, use the <b>Reveal claim link</b> button — only you will see it."
            )
            return

    text = (
        "<b>Dice Roll</b> — closest roll wins the Cwallet claim.\n\n"
        "The claim URL never appears in the group chat. After the round, only the winner "
        "can use the inline button; others get a denial. The URL is shown as a private "
        "popup and/or sent to your DMs.\n\n"
        "<b>In a group</b>\n"
        "• Tap <b>Join</b> (or the link), press <b>Start</b> here, then <code>/roll</code> in the group.\n"
        "• <code>/create_roll &lt;claim_url&gt;</code> — host starts a round "
        f"({ROUND_DURATION_SEC // 60}m {ROUND_DURATION_SEC % 60}s). "
        "<code>/create_hunt</code> still works as an alias.\n"
        "• <code>/roll</code> — once per round.\n"
        f"• <code>/abort_roll</code> — cancel in the first {ABORT_WINDOW_SEC}s "
        "(<code>/abort_hunt</code> alias).\n\n"
        "<b>Commands</b>\n"
        "<code>/help</code> <code>/rules</code> <code>/status</code> <code>/join</code>"
    )
    await update.message.reply_html(text)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    restrict = "On" if RESTRICT_ROLL_COMMANDS else "Off"
    admins_note = (
        "<code>BOT_ADMINS</code> and chat admins may run host commands when restriction is on.\n"
        if RESTRICT_ROLL_COMMANDS
        else ""
    )
    await update.message.reply_html(
        "<b>Dice Roll — commands</b>\n"
        f"<code>/start</code> — intro &amp; join registration\n"
        f"<code>/create_roll &lt;url&gt;</code> — new round ({ROUND_DURATION_SEC}s)\n"
        f"<code>/roll</code> — one roll ({ROLL_MIN}–{ROLL_MAX})\n"
        f"<code>/abort_roll</code> — abort within {ABORT_WINDOW_SEC}s\n"
        f"<code>/status</code> — time left &amp; rolls\n"
        f"<code>/join</code> — post join button\n"
        f"<code>/rules</code> — rules\n\n"
        f"<b>Winner claim</b>\n"
        "After the round, tap <b>Reveal claim link (winner only)</b>. Telegram shows a "
        "private popup and/or sends the full URL in DM. Non-winners only see an error popup.\n\n"
        f"<b>Host env</b>\n"
        f"<code>RESTRICT_ROLL_COMMANDS</code> / legacy <code>RESTRICT_HUNT_COMMANDS</code>: <b>{restrict}</b>\n"
        f"{admins_note}"
        f"<code>BOT_TOKEN</code> required. <code>PORT</code> for optional HTTP keep-alive.\n\n"
        "<b>VPS + PM2</b>\n"
        "Run alongside Node: use a different <code>PORT</code> for this bot (see <code>ecosystem.config.example.cjs</code>)."
    )


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_html(
        "<b>Rules</b>\n"
        f"• Secret target integer {ROLL_MIN}–{ROLL_MAX}.\n"
        "• One roll per player per round.\n"
        "• Closest to the target wins.\n"
        "• Tie-break: earliest roll wins.\n"
        "• Claim URL: host passes it to the bot; it is not posted to the group.\n"
        "• Winner reveals via the inline button (callback) — private to them."
    )


async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_html(
            "Use <code>/join</code> in a <b>group</b> with an active round."
        )
        return
    g = _game(chat.id)
    if not g.is_active:
        await update.message.reply_html("No active round in this group.")
        return
    await update.message.reply_html(
        "<b>Join this round</b>\n"
        "Tap the button, press <b>Start</b> in private chat, then <code>/roll</code> here.",
        reply_markup=join_keyboard(context, chat.id),
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    cid = update.effective_chat.id
    g = _game(cid)
    if not g.is_active:
        await update.message.reply_html("No round is running in this chat.")
        return
    elapsed = time.time() - g.start_time
    left = max(0, int(ROUND_DURATION_SEC - elapsed))
    lines = [
        "<b>Round status</b>",
        f"Target hidden ({ROLL_MIN}–{ROLL_MAX}).",
        f"Time left: ~{left}s",
        f"Players rolled: {len(g.players)}",
        f"Registered (DM): {len(g.joined_private)}",
    ]
    if g.players:
        lines.append("")
        for _uid, data in g.players.items():
            name = html.escape(str(data.get("name", "?")))
            lines.append(f"• {name}: <code>{data.get('roll')}</code>")
    await update.message.reply_html("\n".join(lines))


async def create_roll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat = update.effective_chat
    chat_id = chat.id

    if chat.type not in ("group", "supergroup"):
        await update.message.reply_html(
            "Start rounds in a <b>group</b> or <b>supergroup</b>."
        )
        return

    if not await _is_privileged_in_chat(update, context):
        await update.message.reply_html(
            "Only chat admins can start or abort rounds here "
            "(host set <code>RESTRICT_ROLL_COMMANDS</code>)."
        )
        return

    try:
        await update.message.delete()
    except BadRequest:
        log.warning("Could not delete host command; grant Delete Messages to hide the URL.")

    g = _game(chat_id)
    if g.is_active:
        await context.bot.send_message(
            chat_id=chat_id,
            text="A round is already running. ⏳",
            parse_mode="HTML",
        )
        return

    if not context.args:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Usage: <code>/create_roll &lt;cwallet_claim_url&gt;</code>",
            parse_mode="HTML",
        )
        return

    claim = context.args[0].strip()
    if not re.match(r"https?://", claim, re.I):
        await context.bot.send_message(
            chat_id=chat_id,
            text="Please use an <code>http://</code> or <code>https://</code> claim URL.",
            parse_mode="HTML",
        )
        return

    _invalidate_reveal(g)
    _cancel_timer(g)
    g.is_active = True
    g.target = random.randint(ROLL_MIN, ROLL_MAX)
    g.claim_url = claim
    g.players.clear()
    g.winner_id = None
    g.joined_private.clear()
    g.start_time = time.time()

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "🔔 <b>Dice Roll started!</b>\n"
            f"Secret target {ROLL_MIN}–{ROLL_MAX}. You have <b>{ROUND_DURATION_SEC}s</b>.\n"
            f"<code>/abort_roll</code> works in the first <b>{ABORT_WINDOW_SEC}s</b>.\n\n"
            "<b>Join</b> via the button (and <b>Start</b> in DM), then <code>/roll</code>.\n"
            "The Cwallet claim is revealed only to the winner via a private button tap."
        ),
        parse_mode="HTML",
        reply_markup=join_keyboard(context, chat_id),
    )

    g.timer_task = asyncio.create_task(round_timer(chat_id, context))


async def abort_roll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        await update.message.reply_html("No active round.")
        return

    elapsed = time.time() - g.start_time
    if elapsed <= ABORT_WINDOW_SEC:
        g.is_active = False
        _cancel_timer(g)
        _invalidate_reveal(g)
        await update.message.reply_html("Round cancelled. 🏁")
    else:
        await update.message.reply_html(
            f"Abort only allowed in the first {ABORT_WINDOW_SEC}s."
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
            "<b>Register first</b> (button below + <b>Start</b> in DM), then roll.",
            reply_markup=join_keyboard(context, chat_id),
        )
        return

    if user_id in g.players:
        await update.message.reply_html("You already rolled this round. 🛑")
        return

    val = random.randint(ROLL_MIN, ROLL_MAX)
    g.players[user_id] = {"name": user_name, "roll": val, "ts": time.time()}
    safe = html.escape(user_name)
    await update.message.reply_html(f"{safe} rolled <b>{val}</b>. 🎲")


async def announce_winner(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    g = _game(chat_id)
    if not g.players:
        await context.bot.send_message(
            chat_id=chat_id,
            text="No rolls — no winner. Claim URL was not used. 🌑",
            parse_mode="HTML",
        )
        g.is_active = False
        _cancel_timer(g)
        _invalidate_reveal(g)
        return

    best: tuple[int, float, int] | None = None
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

    _invalidate_reveal(g)
    nonce = secrets.token_hex(REVEAL_NONCE_HEX_BYTES)
    g.reveal_nonce = nonce
    _reveal_nonce_to_chat[nonce] = chat_id

    g.is_active = False
    _cancel_timer(g)

    wn = html.escape(winner_name)
    caption = (
        "⚔️ <b>Round over!</b>\n"
        f"Target was <code>{g.target}</code>.\n\n"
        f"👑 Winner: {wn} — roll <code>{winning_roll}</code> "
        f"(off by <code>{closest_diff}</code>).\n\n"
        "🔒 <b>Winner:</b> tap the button below. Others will not see your claim link."
    )

    try:
        photos = await context.bot.get_user_profile_photos(user_id=g.winner_id, limit=1)
        if photos.photos:
            best_photo = photos.photos[0][-1]
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=best_photo.file_id,
                caption=caption,
                parse_mode="HTML",
                reply_markup=winner_reveal_keyboard(nonce),
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=caption,
                parse_mode="HTML",
                reply_markup=winner_reveal_keyboard(nonce),
            )
    except Exception:
        log.exception("Winner announce failed; sending text")
        await context.bot.send_message(
            chat_id=chat_id,
            text=caption,
            parse_mode="HTML",
            reply_markup=winner_reveal_keyboard(nonce),
        )


async def on_reveal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.data or not q.from_user:
        return

    data = q.data
    if not data.startswith("r"):
        return

    nonce = data[1:]
    chat_id = _reveal_nonce_to_chat.get(nonce)
    g = _game(chat_id) if chat_id is not None else None

    async def deny(msg: str) -> None:
        await q.answer(text=msg[:200], show_alert=True)

    if not g or g.reveal_nonce != nonce or g.winner_id is None:
        await deny("This reveal button is no longer valid.")
        return

    if q.from_user.id != g.winner_id:
        await deny("Only the round winner can reveal the claim link.")
        return

    url = g.claim_url
    if not url:
        await deny("No claim URL is stored for this round.")
        return

    dm_ok = False
    try:
        await context.bot.send_message(
            chat_id=g.winner_id,
            text="👑 <b>Your claim link</b>\n\n" + _prize_link_html(url),
            parse_mode="HTML",
            disable_web_page_preview=False,
        )
        dm_ok = True
    except Forbidden:
        dm_ok = False
    except BadRequest:
        dm_ok = False

    if dm_ok:
        await q.answer(
            text="Full claim link sent to your private chat with this bot.",
            show_alert=True,
        )
        return

    # No DM: try to fit URL in popup (Telegram ~200 char limit for alert text)
    if len(url) <= CALLBACK_ALERT_MAX:
        await q.answer(text=url, show_alert=True)
        return

    await q.answer(
        text=(
            "Open a private chat with this bot, press Start, then tap the button again "
            "to receive the full link."
        ),
        show_alert=True,
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
    application.add_handler(CommandHandler("create_roll", create_roll))
    application.add_handler(CommandHandler("create_hunt", create_roll))
    application.add_handler(CommandHandler("abort_roll", abort_roll))
    application.add_handler(CommandHandler("abort_hunt", abort_roll))
    application.add_handler(CommandHandler("roll", roll))
    application.add_handler(CallbackQueryHandler(on_reveal_callback, pattern=r"^r[0-9a-fA-F]+$"))

    log.info("Dice Roll bot polling…")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
