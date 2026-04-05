# Dice Roll Telegram Bot

Closest roll wins. The host passes a **Cwallet (or any) HTTPS claim URL** with `/create_roll`; it is **not** posted in the group (command deleted when the bot can delete it).

## Whisper only (no winner DM)

Telegram cannot show different message text per user in the same group bubble. The bot uses an **inline keyboard** and **callback query**:

- After the round, the winner announcement includes **Reveal claim link (winner only — private)**.
- On tap, the bot calls **`answerCallbackQuery`** with **`show_alert=True`**.
  - **Winner:** private popup contains the full claim URL (whisper).
  - **Anyone else:** popup says they are not the winner — **no URL**.

**No DMs** are sent for the prize.

**Limit:** Telegram allows about **200 characters** for callback alert text. If the claim URL is longer, the winner sees an error asking the host to use a **URL shortener** next time. The bot warns the group when a round starts if the URL is too long.

## Features

- Per-chat rounds, HTML in messages (Telegram-supported tags only).
- Aliases: `/create_hunt`, `/abort_hunt`.
- Optional `RESTRICT_ROLL_COMMANDS` (legacy `RESTRICT_HUNT_COMMANDS`).
- Optional HTTP keep-alive on `PORT` (`/` and `/health`).

## Setup

1. Bot from [@BotFather](https://t.me/BotFather).
2. **Group privacy off** so `/roll` works in groups.
3. **Delete messages** optional — hides the host command with the URL.

## VPS + PM2

See `ecosystem.config.example.cjs`. Use a **different `PORT`** than your Node app. **One `BOT_TOKEN` per** long-polling process.

## Commands (groups)

- `/create_roll <url>` — start round. Alias: `/create_hunt`.
- `/roll` — once per round.
- `/abort_roll` — first 30s. Alias: `/abort_hunt`.
- `/status`

## Commands (DM)

- `/start`, `/help`, `/rules`

## License

Use and modify for your community.
