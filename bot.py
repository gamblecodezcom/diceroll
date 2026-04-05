"""
Telegram Dice Roll bot — Cwallet claim URL revealed only to the winner via callback
whisper (answerCallbackQuery + show_alert). No DMs for the prize.

Telegram caps alert text at ~200 characters; longer claim URLs cannot be shown in full
in a popup — use a shortener or the host must remit the link another way.

HTML in chat messages: only supported tags. Newlines are literal, not <br>.
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

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ROUND_DURATION_SEC = 210
HALFWAY_REMINDER_SEC = 105
ABORT_WINDOW_SEC = 30
ROLL_MIN, ROLL_MAX = 1, 100
REVEAL_NONCE_HEX_BYTES = 8
# Telegram: answerCallbackQuery text max 200 characters
CALLBACK_ALERT_MAX = 200


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
if os.environ.get("RESTRICT_HUNT_COMMANDS", "").lower() in ("1", "true", "yes"):
    RESTRICT_ROLL_COMMANDS = True

_reveal_nonce_to_chat: dict[str, int] = {}


@dataclass
class DiceRound:
    is_active: bool = False
    target: int = 0
    claim_url: str = ""
    players: dict[int, dict[str, Any]] = field(default_factory=dict)
    winner_id: int | None = None
    start_time: float = 0.0
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


def winner_reveal_keyboard(nonce: str) -> InlineKeyboardMarkup:
    data = f"r{nonce}"
    if len(data.encode("utf-8")) > 64:
        log.error("callback_data too long: %s", len(data))
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔒 Reveal claim link (winner only — private)",
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
    text = (
        "<b>Dice Roll</b> — closest roll wins the Cwallet claim.\n\n"
        "The claim URL is never posted in the group. When the round ends, "
        "<b>only the winner</b> can tap <b>Reveal claim link</b>; Telegram shows it "
        f"as a <b>private popup</b> (whisper), not in the chat. Max ~{CALLBACK_ALERT_MAX} "
        "characters — use a short link if your claim URL is longer.\n\n"
        "<b>In a group</b>\n"
        "• <code>/create_roll &lt;claim_url&gt;</code> — start "
        f"({ROUND_DURATION_SEC // 60}m {ROUND_DURATION_SEC % 60}s). "
        "Alias: <code>/create_hunt</code>.\n"
        "• <code>/roll</code> — once per round.\n"
        f"• <code>/abort_roll</code> — first {ABORT_WINDOW_SEC}s (alias <code>/abort_hunt</code>).\n\n"
        "<code>/help</code> <code>/rules</code> <code>/status</code>"
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
        f"<code>/start</code> — overview\n"
        f"<code>/create_roll &lt;url&gt;</code> — new round ({ROUND_DURATION_SEC}s)\n"
        f"<code>/roll</code> — one roll ({ROLL_MIN}–{ROLL_MAX})\n"
        f"<code>/abort_roll</code> — abort within {ABORT_WINDOW_SEC}s\n"
        f"<code>/status</code> — time left &amp; rolls\n"
        f"<code>/rules</code> — rules\n\n"
        "<b>Winner — whisper only</b>\n"
        "Tap <b>Reveal claim link</b> on the result message. Only you see the popup; "
        f"others get an error. No DMs. Telegram allows up to ~{CALLBACK_ALERT_MAX} "
        "characters in that popup — shorten long Cwallet links.\n\n"
        f"<b>Env</b>\n"
        f"<code>RESTRICT_ROLL_COMMANDS</code> / <code>RESTRICT_HUNT_COMMANDS</code>: <b>{restrict}</b>\n"
        f"{admins_note}"
        "<code>BOT_TOKEN</code> required. <code>PORT</code> for optional HTTP.\n\n"
        "<b>PM2 + Node</b>\n"
        "Separate <code>PORT</code> per process; one <code>BOT_TOKEN</code> per poller."
    )


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_html(
        "<b>Rules</b>\n"
        f"• Secret target {ROLL_MIN}–{ROLL_MAX}.\n"
        "• One roll per player per round.\n"
        "• Closest to the target wins; tie-break: earliest roll.\n"
        "• Claim URL is not shown in the group.\n"
        f"• Winner gets the link in a private callback popup only (~{CALLBACK_ALERT_MAX} chars max)."
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
    g.start_time = time.time()

    warn = ""
    if len(claim) > CALLBACK_ALERT_MAX:
        warn = (
            f"\n\n⚠️ This URL is longer than ~{CALLBACK_ALERT_MAX} characters. "
            "The winner may not see the full link in the whisper popup — use a URL shortener."
        )

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "🔔 <b>Dice Roll started!</b>\n"
            f"Secret target {ROLL_MIN}–{ROLL_MAX}. You have <b>{ROUND_DURATION_SEC}s</b>.\n"
            f"<code>/abort_roll</code> in the first <b>{ABORT_WINDOW_SEC}s</b>.\n\n"
            "Send <code>/roll</code> in this chat. The winner reveals the claim via "
            "<b>private popup</b> only (no DM)."
            f"{warn}"
        ),
        parse_mode="HTML",
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
    chat_id = update.effective_chat.id
    g = _game(chat_id)

    if not g.is_active:
        return

    user = update.effective_user
    user_id = user.id
    user_name = user.first_name or user.username or str(user_id)

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
        "🔒 <b>Winner:</b> tap the button. You will see the claim link in a "
        "<b>private popup</b> only. Everyone else sees a denial — nothing is DMed."
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
        await q.answer(text=msg[:CALLBACK_ALERT_MAX], show_alert=True)

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

    if len(url) > CALLBACK_ALERT_MAX:
        await deny(
            "Claim URL is too long for Telegram's private popup (200 char max). "
            "Ask the host for a shortened link next round."
        )
        return

    await q.answer(text=url, show_alert=True)


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
