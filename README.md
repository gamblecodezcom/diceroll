# Dice Hunt Telegram Bot

Group dice game: closest roll to a secret target wins. The prize URL is removed from the group (when the bot can delete the command) and sent to the winner by **private message**.

## Features

- **Per-chat games** — multiple groups can run hunts at the same time.
- **HTML messages** — bold, code, and safe escaping for names and links.
- **Join flow** — inline button and `/join` open `t.me/YourBot?start=join_<group_id>` so players press **Start** in DM; then `/roll` works in the group and the bot can DM the prize.
- **Background timer** — non-blocking; reminder at 105s; winner announcement at 210s.
- **Keep-alive HTTP** — `GET /` and `GET /health` on `PORT` (default `8080`, bind `0.0.0.0`) for Replit, fps.ms, or other hosts that expect a web process.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather), copy the token.
2. **Disable Group Privacy** (BotFather → Bot Settings → Group Privacy → **Turn off**) so the bot receives `/roll` and other commands in groups.
3. In the group, add the bot and give it **Delete messages** if you want `/create_hunt` messages removed.

### Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `BOT_TOKEN` | Yes | Token from BotFather |
| `PORT` | No | HTTP port (default `8080`) |
| `BIND` | No | Bind address (default `0.0.0.0`) |
| `BOT_USERNAME` | No | Fallback if username discovery fails (no `@`) |
| `BOT_ADMINS` | No | Comma-separated Telegram user IDs (always allowed for hunt commands when restriction is on) |
| `RESTRICT_HUNT_COMMANDS` | No | If `true` / `1` / `yes`, only **chat admins** (and `BOT_ADMINS`) may `/create_hunt` and `/abort_hunt` in groups |

Copy `.env.example` to `.env` locally if you use a loader; on Replit/fps.ms set secrets in the dashboard.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export BOT_TOKEN="your_token"
python bot.py
```

Open `http://127.0.0.1:8080/health` to verify the keep-alive server.

## Replit

1. New Repl → import this repo or upload files.
2. Add secret `BOT_TOKEN`.
3. Replit sets `PORT` automatically; the bot listens on `0.0.0.0`.
4. Run `python bot.py` (or use the Run button with `.replit`).

## fps.ms / generic PaaS

- Set `BOT_TOKEN` and ensure the platform injects `PORT`.
- Start command: `python bot.py` (see `Procfile` as `web:` for process types that require an HTTP port).
- Point uptime pings at `https://your-app/health` if needed.

## Commands (groups)

- `/create_hunt <https://prize...>` — start a hunt (link message deleted if permitted).
- `/join` — posts the **Join** button (deep link to bot + Start).
- `/roll` — roll once (after joining via button in groups).
- `/abort_hunt` — cancel in the first 30 seconds.
- `/status` — time left and rolls.

## Commands (anywhere)

- `/start` — help and deep-link registration when opened from the join URL.
- `/help`, `/rules` — documentation.

## License

Use and modify for your community.
