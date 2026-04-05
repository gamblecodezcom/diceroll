# Dice Roll Telegram Bot

Group game: closest roll to a secret target wins. The host attaches a **Cwallet (or any) HTTPS claim URL** when starting a round. The URL is **not** shown in the group. After the round, only the winner can use **Reveal claim link** — implemented with **inline keyboards and callback queries** (private popup and/or DM), because Telegram cannot show different text in the same group message bubble per user.

## Whisper / winner-only reveal

- Everyone sees the same winner announcement message with an inline button.
- On tap, Telegram sends a **CallbackQuery** to the bot. The bot calls `answerCallbackQuery`:
  - **Winner:** alert confirms + **full URL sent in private chat** when possible; if the URL is short enough and DMs fail, it may appear only in the popup.
  - **Non-winner:** alert only: *"Only the round winner can reveal"* — no URL leaked.

## Features

- **Per-chat rounds** — many groups at once.
- **HTML** — [Telegram-supported tags](https://core.telegram.org/bots/api#html-style) only; line breaks are newline characters, not `<br>`.
- **Join flow** — deep link + `/join` so the bot can DM the winner if needed.
- **Aliases** — `/create_hunt`, `/abort_hunt` still work (same as `/create_roll`, `/abort_roll`).
- **Optional HTTP** — `GET /` and `/health` on `PORT` for uptime checks.

## Setup

1. Bot from [@BotFather](https://t.me/BotFather).
2. **Group privacy off** so `/roll` works in groups.
3. **Delete messages** optional — hides the host command that contains the URL.

### Environment

| Variable | Description |
|----------|-------------|
| `BOT_TOKEN` | Required |
| `PORT` | HTTP port (default `8080`) — **use another port** if your Node app uses `8080` |
| `BIND` | Default `0.0.0.0` |
| `RESTRICT_ROLL_COMMANDS` | `true` / `1` / `yes` → only admins (+ `BOT_ADMINS`) start/abort |
| `RESTRICT_HUNT_COMMANDS` | Legacy alias for the same flag |
| `BOT_ADMINS` | Comma-separated Telegram user IDs |
| `BOT_USERNAME` | Optional fallback |

## VPS + PM2 (alongside Node)

Use a **separate `BOT_TOKEN`** for each bot process, or only one process may poll.

```bash
cd /path/to/this/repo
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export BOT_TOKEN="..." PORT=8090
pm2 start ecosystem.config.example.cjs --only dice-roll-bot
# Or merge the `dice-roll-bot` entry into your existing `ecosystem.config.cjs`
```

## Commands (groups)

- `/create_roll <https://...>` — start round (URL removed from chat if delete allowed). Alias: `/create_hunt`.
- `/join` — join button.
- `/roll` — once per round (after join in groups).
- `/abort_roll` — first 30s. Alias: `/abort_hunt`.
- `/status`

## Commands (DM)

- `/start`, `/help`, `/rules`

## Merging into a monorepo

Copy `bot.py`, `keep_alive.py`, `requirements.txt`, and the PM2 snippet into your backend tree (e.g. `telegram/dice-roll/`). Install Python deps in that environment; run as a second PM2 app with its own `PORT` and token.

## License

Use and modify for your community.
