"""
Dice Roll giveaway bot — webhook (primary) or polling, multi-winner, callback whisper.

Winner claim URLs: answerCallbackQuery(show_alert=True) only — no DMs.
Telegram alert text max ~200 chars per URL; use short links.

HTML: Telegram-supported tags only; newlines are literal.
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
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from keep_alive import keep_alive

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("dice_roll")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DEFAULT_DURATION_SEC = 210
HALFWAY_FRACTION = 0.5
ABORT_WINDOW_SEC = 30
ROLL_MIN, ROLL_MAX = 1, 100
REVEAL_NONCE_HEX_BYTES = 8
CALLBACK_ALERT_MAX = 200

# --- Deep link: give_<hex_unsigned_chat_id> ---
GIVE_PREFIX = "give_"

DURATION_OPTIONS = (60, 120, 180, 210, 300, 600)
MAX_PLAYER_OPTIONS = (0, 5, 10, 20, 50, 100)
WINNER_COUNT_OPTIONS = (1, 2, 3, 4, 5)


def _parse_admin_ids() -> set[int]:
    raw = os.environ.get("BOT_ADMINS", "").strip()
    if not raw:
        return set()
    return {int(p.strip()) for p in raw.split(",") if p.strip().isdigit()}


BOT_ADMINS = _parse_admin_ids()
RESTRICT_ROLL_COMMANDS = os.environ.get("RESTRICT_ROLL_COMMANDS", "").lower() in (
    "1",
    "true",
    "yes",
)
if os.environ.get("RESTRICT_HUNT_COMMANDS", "").lower() in ("1", "true", "yes"):
    RESTRICT_ROLL_COMMANDS = True

# Namespace reveal callbacks (matches integrations/gcz-bot Node module: drr + hex)
CB_REVEAL_PREFIX = "drr"
_reveal_nonce_to_info: dict[str, tuple[int, int]] = {}  # nonce -> (chat_id, winner_index)


def encode_chat_id(chat_id: int) -> str:
    u = chat_id % (1 << 64)
    return format(u, "x")


def decode_chat_id(payload: str) -> int:
    u = int(payload, 16)
    if u >= 1 << 63:
        return u - (1 << 64)
    return u


def _webhook_config() -> tuple[str | None, str | None]:
    """Returns (full_webhook_url, secret_token) or (None, None) for polling."""
    url = os.environ.get("WEBHOOK_URL", "").strip()
    if not url:
        base = os.environ.get("WEBHOOK_BASE_URL", "").strip().rstrip("/")
        path = os.environ.get("WEBHOOK_PATH", "telegram/webhook").strip().lstrip("/")
        if base:
            url = f"{base}/{path}"
    if not url:
        return None, None
    secret = os.environ.get("WEBHOOK_SECRET", "").strip() or None
    return url, secret


def _webhook_url_and_path(webhook_url: str) -> tuple[str, str]:
    """
    Telegram POSTs to webhook_url; local server listens on PORT at url_path (no leading /).
    """
    parsed = urlparse(webhook_url)
    if not parsed.scheme or not parsed.netloc:
        raise SystemExit("WEBHOOK_URL must be a full URL, e.g. https://your.domain/telegram/webhook")
    path = parsed.path.strip("/")
    if not path:
        path = os.environ.get("WEBHOOK_PATH", "telegram/webhook").strip().lstrip("/")
        webhook_url = f"{parsed.scheme}://{parsed.netloc}/{path}"
    scheme_l = parsed.scheme.lower()
    if scheme_l != "https" and not parsed.netloc.startswith("127.") and parsed.netloc not in ("localhost", "[::1]"):
        log.warning("Telegram requires HTTPS for webhooks on public hosts; got %s", webhook_url)
    return webhook_url.rstrip("/"), path


def _btn_text(text: str, max_len: int = 64) -> str:
    """Telegram inline button text limit."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _force_polling() -> bool:
    return os.environ.get("USE_POLLING", "").lower() in ("1", "true", "yes", "force")


@dataclass
class DiceRound:
    is_active: bool = False
    target: int = 0
    claim_urls: list[str] = field(default_factory=list)
    players: dict[int, dict[str, Any]] = field(default_factory=dict)
    winner_ids: list[int] = field(default_factory=list)
    reveal_nonces: list[str] = field(default_factory=list)
    start_time: float = 0.0
    duration_sec: int = DEFAULT_DURATION_SEC
    max_players: int = 0
    timer_task: asyncio.Task | None = None
    host_user_id: int | None = None


games: dict[int, DiceRound] = {}


def _game(chat_id: int) -> DiceRound:
    if chat_id not in games:
        games[chat_id] = DiceRound()
    return games[chat_id]


def _clear_reveal_round(g: DiceRound) -> None:
    for n in g.reveal_nonces:
        _reveal_nonce_to_info.pop(n, None)
    g.reveal_nonces.clear()
    g.winner_ids.clear()


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


async def _is_user_admin_of_chat(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int
) -> bool:
    if user_id in BOT_ADMINS:
        return True
    try:
        m = await context.bot.get_chat_member(chat_id, user_id)
        return m.status in ("creator", "administrator")
    except (BadRequest, Forbidden):
        return False


def _cancel_timer(g: DiceRound) -> None:
    t = g.timer_task
    if t and not t.done():
        t.cancel()
    g.timer_task = None


def _bot_username(context: ContextTypes.DEFAULT_TYPE) -> str:
    if context.bot.username:
        return context.bot.username
    return os.environ.get("BOT_USERNAME", "YOUR_BOT_USERNAME")


def open_bot_setup_keyboard(context: ContextTypes.DEFAULT_TYPE, group_chat_id: int) -> InlineKeyboardMarkup:
    payload = f"{GIVE_PREFIX}{encode_chat_id(group_chat_id)}"
    if len(payload) > 64:
        log.error("start parameter too long: %s", len(payload))
    url = f"https://t.me/{_bot_username(context)}?start={payload}"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    _btn_text(
                        "👉 TAP HERE — OPEN BOT (do NOT press Enter on command)"
                    ),
                    url=url,
                )
            ]
        ]
    )


def _default_setup() -> dict[str, Any]:
    return {
        "target_chat_id": None,
        "claim_urls": [],
        "duration_sec": DEFAULT_DURATION_SEC,
        "max_players": 0,
        "num_winners": 1,
        "awaiting_urls": True,
    }


def setup_settings_keyboard(ud: dict[str, Any]) -> InlineKeyboardMarkup:
    d = ud["duration_sec"]
    mp = ud["max_players"]
    nw = ud["num_winners"]
    max_label = "∞" if mp == 0 else str(mp)
    rows = [
        [
            InlineKeyboardButton(_btn_text(f"⏱ Countdown: {d}s"), callback_data="su:dur"),
        ],
        [
            InlineKeyboardButton(_btn_text(f"👥 Max players: {max_label}"), callback_data="su:max"),
        ],
        [
            InlineKeyboardButton(_btn_text(f"🏆 Winners: {nw}"), callback_data="su:nwin"),
        ],
        [
            InlineKeyboardButton(_btn_text("🚀 LAUNCH IN GROUP"), callback_data="su:go"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def _cycle(cur: int, options: tuple[int, ...]) -> int:
    i = options.index(cur) if cur in options else 0
    return options[(i + 1) % len(options)]


async def cmd_dice_roll_giveaway(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Loud group entry: force tap URL button, never rely on command submit."""
    if not update.message:
        return
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_html(
            "<b>Use this command in your group</b> to post the giveaway starter."
        )
        return

    text = (
        "🚨🎲 <b>DICE ROLL GIVEAWAY</b> 🎲🚨\n\n"
        "⚠️ <b>STOP — READ THIS</b> ⚠️\n\n"
        "<b>DO NOT PRESS ENTER</b> on the command line above.\n"
        "<b>DO NOT SEND</b> the command again.\n\n"
        "✅ <b>TAP THE BIG BUTTON BELOW</b> — it opens this bot in private chat.\n"
        "There you will paste your <b>Cwallet / claim HTTPS link(s)</b> and set "
        "countdown, max players, and number of winners.\n\n"
        "<b>Multiple winners</b> = send <b>one claim URL per line</b> (first line = 1st place).\n\n"
        "Whisper prizes: only winners see their link in a <b>private popup</b> "
        f"(max ~{CALLBACK_ALERT_MAX} chars — shorten long URLs).\n\n"
        "— Hosts: you must be a <b>group admin</b> (or in BOT_ADMINS) to finish setup."
    )
    await update.message.reply_html(
        text,
        reply_markup=open_bot_setup_keyboard(context, chat.id),
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    args = context.args or []

    if args and args[0].startswith(GIVE_PREFIX):
        hex_part = args[0][len(GIVE_PREFIX) :]
        if re.fullmatch(r"[0-9a-fA-F]+", hex_part):
            try:
                gid = decode_chat_id(hex_part)
            except ValueError:
                await update.message.reply_html("Invalid setup link. Ask the host to post <code>/dice_roll_giveaway</code> again.")
                return
            uid = update.effective_user.id
            if not await _is_user_admin_of_chat(context, uid, gid):
                await update.message.reply_html(
                    "You must be an <b>administrator</b> in that group to run giveaway setup."
                )
                return
            context.user_data["giveaway_setup"] = _default_setup()
            context.user_data["giveaway_setup"]["target_chat_id"] = gid
            await update.message.reply_html(
                "<b>Step 1 — Claim link(s)</b>\n\n"
                "Send <b>one HTTPS URL</b> for a single winner.\n"
                "For <b>multiple winners</b>, send <b>one URL per line</b> "
                "(top = 1st place, next = 2nd, …).\n\n"
                "<b>Step 2 — Settings</b>\n"
                "Use the buttons below to change countdown, max players, and winner count. "
                "Then tap <b>LAUNCH GIVEAWAY IN GROUP</b>.\n\n"
                "Tip: shorten links so they fit Telegram’s private popup (~200 characters).",
                reply_markup=setup_settings_keyboard(context.user_data["giveaway_setup"]),
            )
            return

    text = (
        "<b>Dice Roll Giveaway</b>\n\n"
        "In your group, run:\n"
        "<code>/dice_roll_giveaway</code> or <code>/dice_roll</code>\n\n"
        "Then <b>tap the button</b> in that message (do not only press Enter).\n\n"
        "<code>/help</code> — full help"
    )
    await update.message.reply_html(text)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    wh, _ = _webhook_config()
    mode = "Webhook" if wh and not _force_polling() else "Polling"
    await update.message.reply_html(
        "<b>How to run a giveaway</b>\n"
        "1. In the group: <code>/dice_roll_giveaway</code>\n"
        "2. <b>Tap the button</b> (do not submit the command with Enter only).\n"
        "3. In DM: paste claim URL(s), adjust toggles, tap <b>LAUNCH</b>.\n\n"
        "<b>Players</b>: <code>/roll</code> once per round.\n"
        "<b>Winners</b>: tap <b>Reveal</b> on the result — private popup only.\n\n"
        "<b>Quick host command</b> (group): <code>/create_roll &lt;url&gt;</code> — single winner, default timer.\n"
        f"<code>/abort_roll</code> — first {ABORT_WINDOW_SEC}s\n"
        "<code>/status</code> — progress\n\n"
        f"<b>Transport</b>: {html.escape(mode)}\n"
        "Webhook: set <code>WEBHOOK_URL</code> (or <code>WEBHOOK_BASE_URL</code> + <code>WEBHOOK_PATH</code>). "
        "Put HTTPS / TLS on nginx; bot listens HTTP on <code>PORT</code>.\n"
        "Force polling: <code>USE_POLLING=true</code>."
    )


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_html(
        "<b>Rules</b>\n"
        f"• Secret target {ROLL_MIN}–{ROLL_MAX}.\n"
        "• One roll per player.\n"
        "• Closest rolls win (1st, 2nd, …); tie-break: earlier roll.\n"
        "• Each winner gets their own reveal button.\n"
        f"• Popup text limit ~{CALLBACK_ALERT_MAX} chars."
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    g = _game(update.effective_chat.id)
    if not g.is_active:
        await update.message.reply_html("No active round in this chat.")
        return
    elapsed = time.time() - g.start_time
    left = max(0, int(g.duration_sec - elapsed))
    cap = f" / {g.max_players}" if g.max_players else ""
    lines = [
        "<b>Round status</b>",
        f"Timer: ~{left}s left (duration {g.duration_sec}s)",
        f"Players: {len(g.players)}{cap}",
        f"Winners this round: {len(g.claim_urls)}",
    ]
    if g.players:
        lines.append("")
        for _uid, data in g.players.items():
            name = html.escape(str(data.get("name", "?")))
            lines.append(f"• {name}: <code>{data.get('roll')}</code>")
    await update.message.reply_html("\n".join(lines))


async def on_setup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.data or not q.message:
        return
    await q.answer()
    if not q.data.startswith("su:"):
        return
    action = q.data[3:]
    ud = context.user_data.get("giveaway_setup")
    if not ud or ud.get("target_chat_id") is None:
        await q.edit_message_reply_markup(reply_markup=None)
        return

    if action == "dur":
        ud["duration_sec"] = _cycle(ud["duration_sec"], DURATION_OPTIONS)
    elif action == "max":
        ud["max_players"] = _cycle(ud["max_players"], MAX_PLAYER_OPTIONS)
    elif action == "nwin":
        ud["num_winners"] = _cycle(ud["num_winners"], WINNER_COUNT_OPTIONS)
    elif action == "go":
        await _launch_giveaway_from_setup(update, context, ud, q.message)
        return

    try:
        await q.edit_message_reply_markup(reply_markup=setup_settings_keyboard(ud))
    except BadRequest:
        pass


async def _launch_giveaway_from_setup(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    ud: dict[str, Any],
    status_message: Any,
) -> None:
    uid = update.effective_user.id
    gid = ud["target_chat_id"]
    urls = ud.get("claim_urls") or []
    nw = int(ud["num_winners"])

    if len(urls) < nw:
        await context.bot.send_message(
            chat_id=uid,
            text=(
                f"You need at least <b>{nw}</b> claim URL(s). "
                "Send them in separate lines, then tap LAUNCH again."
            ),
            parse_mode="HTML",
        )
        return

    if not await _is_user_admin_of_chat(context, uid, gid):
        await context.bot.send_message(chat_id=uid, text="You are not an admin in that group anymore.")
        return

    g = _game(gid)
    if g.is_active:
        await context.bot.send_message(chat_id=uid, text="That group already has an active round.")
        return

    use_urls = urls[:nw]
    for u in use_urls:
        if not re.match(r"https?://", u.strip(), re.I):
            await context.bot.send_message(chat_id=uid, text="Every line must be an http(s) URL.")
            return

    _clear_reveal_round(g)
    _cancel_timer(g)
    g.is_active = True
    g.target = random.randint(ROLL_MIN, ROLL_MAX)
    g.claim_urls = [u.strip() for u in use_urls]
    g.players.clear()
    g.winner_ids = []
    g.start_time = time.time()
    g.duration_sec = int(ud["duration_sec"])
    g.max_players = int(ud["max_players"])
    g.host_user_id = uid

    warn = ""
    for i, u in enumerate(g.claim_urls):
        if len(u) > CALLBACK_ALERT_MAX:
            warn += f"\n⚠️ Prize {i+1} URL is long; use a shortener (~{CALLBACK_ALERT_MAX} char popup limit)."

    await context.bot.send_message(
        chat_id=gid,
        text=(
            "🎲 <b>GIVEAWAY LIVE</b> 🎲\n"
            f"Closest to the secret target wins — <b>{nw}</b> winner(s).\n"
            f"⏱ <b>{g.duration_sec}s</b> on the clock.\n"
            f"<code>/abort_roll</code> in the first <b>{ABORT_WINDOW_SEC}s</b> (admins).\n\n"
            "Reply with <code>/roll</code> in this chat (once each).\n"
            "Prizes: <b>Reveal</b> buttons — <b>private popup only</b>."
            f"{warn}"
        ),
        parse_mode="HTML",
    )

    g.timer_task = asyncio.create_task(round_timer(gid, context))

    try:
        await status_message.edit_text(
            "<b>Giveaway started</b> in the group. Good luck!",
            parse_mode="HTML",
            reply_markup=None,
        )
    except BadRequest:
        await context.bot.send_message(chat_id=uid, text="Giveaway started in the group.", parse_mode="HTML")

    context.user_data.pop("giveaway_setup", None)


async def on_private_claim_urls(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    ud = context.user_data.get("giveaway_setup")
    if not ud or ud.get("target_chat_id") is None:
        return
    if not ud.get("awaiting_urls", True):
        return

    text = (update.message.text or "").strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return
    urls = []
    for ln in lines:
        if re.match(r"https?://", ln, re.I):
            urls.append(ln)
    if not urls:
        await update.message.reply_html("Send at least one line starting with <code>http://</code> or <code>https://</code>.")
        return

    ud["claim_urls"] = urls
    await update.message.reply_html(
        f"Saved <b>{len(urls)}</b> URL(s). Check winner count in the buttons — "
        "you need at least that many lines. Tap <b>LAUNCH GIVEAWAY IN GROUP</b> when ready.",
        reply_markup=setup_settings_keyboard(ud),
    )


async def create_roll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat = update.effective_chat
    chat_id = chat.id

    if chat.type not in ("group", "supergroup"):
        await update.message.reply_html("Use this in a <b>group</b>.")
        return

    if not await _is_privileged_in_chat(update, context):
        await update.message.reply_html("Only admins can start rounds here.")
        return

    try:
        await update.message.delete()
    except BadRequest:
        log.warning("Could not delete create_roll message.")

    g = _game(chat_id)
    if g.is_active:
        await context.bot.send_message(chat_id=chat_id, text="A round is already running. ⏳", parse_mode="HTML")
        return

    if not context.args:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Usage: <code>/create_roll &lt;url&gt;</code> — or use <code>/dice_roll_giveaway</code> + bot setup.",
            parse_mode="HTML",
            reply_markup=open_bot_setup_keyboard(context, chat_id),
        )
        return

    claim = context.args[0].strip()
    if not re.match(r"https?://", claim, re.I):
        await context.bot.send_message(
            chat_id=chat_id,
            text="URL must start with http:// or https://",
            parse_mode="HTML",
        )
        return

    _clear_reveal_round(g)
    _cancel_timer(g)
    g.is_active = True
    g.target = random.randint(ROLL_MIN, ROLL_MAX)
    g.claim_urls = [claim]
    g.players.clear()
    g.winner_ids = []
    g.duration_sec = DEFAULT_DURATION_SEC
    g.max_players = 0
    g.start_time = time.time()
    g.host_user_id = update.effective_user.id

    warn = ""
    if len(claim) > CALLBACK_ALERT_MAX:
        warn = f"\n⚠️ URL &gt; ~{CALLBACK_ALERT_MAX} chars — use a shortener for the whisper popup."

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "🎲 <b>Dice Roll started!</b>\n"
            f"<code>/roll</code> once each — <b>{g.duration_sec}s</b>.\n"
            f"<code>/abort_roll</code> first <b>{ABORT_WINDOW_SEC}s</b>.\n"
            "Winner: <b>Reveal</b> = private popup only."
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
        await update.message.reply_html("Only admins can abort (when restricted).")
        return

    if not g.is_active:
        await update.message.reply_html("No active round.")
        return

    if time.time() - g.start_time <= ABORT_WINDOW_SEC:
        g.is_active = False
        _cancel_timer(g)
        _clear_reveal_round(g)
        await update.message.reply_html("Round cancelled. 🏁")
    else:
        await update.message.reply_html(f"Abort only in the first {ABORT_WINDOW_SEC}s.")


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

    if g.max_players and len(g.players) >= g.max_players and user_id not in g.players:
        await update.message.reply_html("This round is <b>full</b> (max players reached).")
        return

    if user_id in g.players:
        await update.message.reply_html("You already rolled. 🛑")
        return

    val = random.randint(ROLL_MIN, ROLL_MAX)
    g.players[user_id] = {"name": user_name, "roll": val, "ts": time.time()}
    safe = html.escape(user_name)
    await update.message.reply_html(f"{safe} rolled <b>{val}</b>. 🎲")


async def round_timer(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    g = _game(chat_id)
    try:
        half = max(1, int(g.duration_sec * HALFWAY_FRACTION))
        await asyncio.sleep(half)
        if g.is_active:
            await context.bot.send_message(
                chat_id=chat_id,
                text="⏳ <b>Halfway.</b> Send <code>/roll</code> if you have not yet.",
                parse_mode="HTML",
            )
        await asyncio.sleep(max(0, g.duration_sec - half))
        if g.is_active:
            g.is_active = False
            await announce_winners(chat_id, context)
    except asyncio.CancelledError:
        log.debug("Timer cancelled for chat %s", chat_id)
        raise


def _pick_winners(g: DiceRound, k: int) -> list[tuple[int, str, int, int]]:
    """Return up to k winners: (user_id, name, roll, off_by) sorted by rank."""
    if not g.players:
        return []
    ranked: list[tuple[int, float, int, str, int]] = []
    for uid, data in g.players.items():
        roll_v = int(data["roll"])
        diff = abs(g.target - roll_v)
        ts = float(data.get("ts", 0))
        name = str(data.get("name", "?"))
        ranked.append((diff, ts, uid, name, roll_v))
    ranked.sort(key=lambda x: (x[0], x[1]))
    out: list[tuple[int, str, int, int]] = []
    seen: set[int] = set()
    for diff, _ts, uid, name, roll_v in ranked:
        if uid in seen:
            continue
        seen.add(uid)
        out.append((uid, name, roll_v, diff))
        if len(out) >= k:
            break
    return out


def multi_reveal_keyboard(nonces: list[str], labels: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for nonce, label in zip(nonces, labels):
        data = f"{CB_REVEAL_PREFIX}{nonce}"
        if len(data.encode("utf-8")) > 64:
            log.error("reveal callback_data too long")
        rows.append([InlineKeyboardButton(label, callback_data=data)])
    return InlineKeyboardMarkup(rows)


async def announce_winners(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    g = _game(chat_id)
    if not g.claim_urls:
        g.is_active = False
        _cancel_timer(g)
        return

    if not g.players:
        await context.bot.send_message(
            chat_id=chat_id,
            text="No rolls — no winners. 🌑",
            parse_mode="HTML",
        )
        g.is_active = False
        _cancel_timer(g)
        _clear_reveal_round(g)
        return

    k_wanted = len(g.claim_urls)
    winners = _pick_winners(g, k_wanted)
    if len(winners) < k_wanted:
        g.claim_urls = g.claim_urls[: len(winners)]
    k = len(winners)
    if k == 0:
        await context.bot.send_message(
            chat_id=chat_id,
            text="No winners could be ranked. 🌑",
            parse_mode="HTML",
        )
        g.is_active = False
        _cancel_timer(g)
        _clear_reveal_round(g)
        return

    g.winner_ids = [w[0] for w in winners]
    _clear_reveal_round(g)
    g.reveal_nonces = [secrets.token_hex(REVEAL_NONCE_HEX_BYTES) for _ in winners]
    for i, nonce in enumerate(g.reveal_nonces):
        _reveal_nonce_to_info[nonce] = (chat_id, i)

    g.is_active = False
    _cancel_timer(g)

    lines = [
        "⚔️ <b>ROUND OVER!</b>\n",
        f"Target was <code>{g.target}</code>.\n",
    ]
    labels: list[str] = []
    for rank, (uid, name, roll_v, diff) in enumerate(winners, start=1):
        sn = html.escape(name)
        medal = "👑" if rank == 1 else f"#{rank}"
        lines.append(f"{medal} <b>{sn}</b> — <code>{roll_v}</code> (off <code>{diff}</code>)")
        labels.append(_btn_text(f"🔒 {medal} Reveal #{rank} (winner only)"))

    lines.append("")
    lines.append(
        "<b>Winners:</b> tap <b>only your</b> button. Private popup — no chat leak."
    )
    caption = "\n".join(lines)
    markup = multi_reveal_keyboard(g.reveal_nonces, labels)

    primary_uid = winners[0][0]
    try:
        photos = await context.bot.get_user_profile_photos(user_id=primary_uid, limit=1)
        if photos.photos:
            ph = photos.photos[0][-1]
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=ph.file_id,
                caption=caption,
                parse_mode="HTML",
                reply_markup=markup,
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=caption,
                parse_mode="HTML",
                reply_markup=markup,
            )
    except Exception:
        log.exception("Announce failed")
        await context.bot.send_message(
            chat_id=chat_id,
            text=caption,
            parse_mode="HTML",
            reply_markup=markup,
        )


async def on_reveal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.data or not q.from_user:
        return
    if not q.data.startswith(CB_REVEAL_PREFIX):
        return
    nonce = q.data[len(CB_REVEAL_PREFIX) :]
    info = _reveal_nonce_to_info.get(nonce)
    if not info:
        await q.answer(text="Invalid or expired button.", show_alert=True)
        return
    chat_id, idx = info
    g = _game(chat_id)

    async def deny(msg: str) -> None:
        await q.answer(text=msg[:CALLBACK_ALERT_MAX], show_alert=True)

    if idx >= len(g.reveal_nonces) or g.reveal_nonces[idx] != nonce:
        await deny("This button is no longer valid.")
        return
    if idx >= len(g.winner_ids) or q.from_user.id != g.winner_ids[idx]:
        await deny("Only the assigned winner can reveal this prize.")
        return
    if idx >= len(g.claim_urls):
        await deny("No prize URL for this slot.")
        return

    url = g.claim_urls[idx]
    if len(url) > CALLBACK_ALERT_MAX:
        await deny(
            f"Prize URL too long for popup (max {CALLBACK_ALERT_MAX}). Host must use a shortener."
        )
        return

    await q.answer(text=url, show_alert=True)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN in the environment.")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .build()
    )

    application.add_handler(CommandHandler("dice_roll_giveaway", cmd_dice_roll_giveaway))
    application.add_handler(CommandHandler("dice_roll", cmd_dice_roll_giveaway))
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("rules", cmd_rules))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("create_roll", create_roll))
    application.add_handler(CommandHandler("create_hunt", create_roll))
    application.add_handler(CommandHandler("abort_roll", abort_roll))
    application.add_handler(CommandHandler("abort_hunt", abort_roll))
    application.add_handler(CommandHandler("roll", roll))
    application.add_handler(CallbackQueryHandler(on_setup_callback, pattern=r"^su:"))
    application.add_handler(
        CallbackQueryHandler(on_reveal_callback, pattern=r"^drr[0-9a-fA-F]+$")
    )
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            on_private_claim_urls,
        )
    )

    webhook_url, webhook_secret = _webhook_config()
    use_webhook = bool(webhook_url) and not _force_polling()

    if use_webhook:
        assert webhook_url is not None
        public_url, path = _webhook_url_and_path(webhook_url)
        log.info("Webhook mode: public_url=%s url_path=%s port=%s", public_url, path, os.environ.get("PORT", "8080"))
        application.run_webhook(
            listen=os.environ.get("BIND", "0.0.0.0"),
            port=int(os.environ.get("PORT", "8080")),
            url_path=path,
            webhook_url=public_url,
            secret_token=webhook_secret,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
    else:
        if webhook_url and _force_polling():
            log.info("WEBHOOK_URL set but USE_POLLING=true — using polling + keep-alive")
        keep_alive()
        log.info("Starting polling…")
        application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
