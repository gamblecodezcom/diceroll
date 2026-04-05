# Dice Roll Giveaway (Telegram)

This repo ships **two runnable forms** of the same product:

| Piece | Use case |
|-------|-----------|
| **`bot.py`** + `keep_alive.py` | Standalone Python on VPS, Replit, or PM2 (`python bot.py`) — **webhook-first** or **polling** |
| **`integrations/gcz-bot/`** | Drop into **Node/Telegraf** (`commands/diceRollGiveaway.js`) and wire **`bots-hub.js`** or `bot.js` — see `README-INTEGRATION.md` |

Dice giveaway with **Cwallet-style HTTPS claim links**, **multi-winner** (one URL per winner line), **countdown**, **max players**, and **winner-only whisper** via **callback popup** (no DMs). Reveal callbacks use prefix **`drr`** so Python and Node builds stay aligned.

## Loud group entry (avoid “Enter” accidents)

In the group, run:

- `/dice_roll_giveaway` or **`/dice_roll`**

The bot replies with a **big warning** and an inline button:

**Do not press Enter on the command — tap the button** to open the bot in private chat.

## Host flow (DM)

1. Tap the button from the group message.
2. `/start` runs with a deep link; the bot asks for **claim URL(s)**.
   - One URL = one winner.
   - **Multiple lines** = multiple URLs (1st line = 1st place, …).
3. Use inline toggles:
   - **Countdown** (60 … 600s)
   - **Max players** (0 = unlimited, or cap)
   - **Winners** (1–5)
4. Tap **LAUNCH IN GROUP**.

Only **group admins** (or `BOT_ADMINS`) can complete setup for that group.

## Players

- `/roll` once per round in the group.
- After the round, each winner taps **their** reveal button → **private popup** with the URL (Telegram ~200 character limit — shorten long links).

## Quick host command (group)

`/create_roll <https://url>` — single winner, default 210s, no max cap (same as before).

## Webhook (primary) vs polling

| Mode | When |
|------|------|
| **Webhook** | `WEBHOOK_URL` or `WEBHOOK_BASE_URL` set, and `USE_POLLING` not `true` |
| **Polling** | No webhook URL, or `USE_POLLING=true` |

**Webhook:** Put **HTTPS** on nginx/Caddy in front of the bot. Example:

```nginx
location /telegram/webhook {
    proxy_pass http://127.0.0.1:8080/telegram/webhook;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

Set `WEBHOOK_URL=https://your.domain/telegram/webhook` to match.

**Polling:** `keep_alive` still serves `/health` on `PORT` for PM2/uptime.

## Env vars

See `.env.example`.

## PM2

Use `ecosystem.config.example.cjs`. **One bot token per polling/webhook process.** If Node uses the same token, do not run two pollers.

## Commands

| Command | Where |
|---------|--------|
| `/dice_roll_giveaway`, `/dice_roll` | Group — attention + open-bot button |
| `/start` | DM — setup wizard (via button link) |
| `/roll` | Group |
| `/create_roll`, `/create_hunt` | Group — quick single-URL round |
| `/abort_roll`, `/abort_hunt` | Group |
| `/status`, `/help`, `/rules` | Both |

## License

Use and modify for your community.
