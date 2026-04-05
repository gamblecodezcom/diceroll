# Dice Roll Giveaway → Gamblecodez (`bot.js` + `bots-hub.js`)

Paths on VPS:

- Bot code: `/var/www/html/gcz/bot/bot.js`
- Hub (PM2): `/var/www/html/gcz/bot/bots-hub.js`
- Env: `/var/www/html/gcz/.env`

## How your stack works (from your hub)

- **`bots-hub.js`** exposes `POST /webhook` and calls **`bot.handleUpdate(payload)`** — all Telegraf handlers (including giveaway) run there.
- **`KVM1_MODE=1`**: no `bot.launch()` / no polling — **webhook only**. Dice Roll timers use `setTimeout` + `telegram.sendMessage`; they work as long as the hub process stays up (PM2).
- **Do not change `bots-hub.js`** for this feature unless you intentionally add routes; wiring is **`bot.js` only**.

## 1. Copy the command module (ESM)

```bash
cp commands/diceRollGiveaway.js /var/www/html/gcz/bot/commands/diceRollGiveaway.js
```

This file uses **`import` / `export`** to match your `import("./bot.js")` style.

## 2. Merge into `bot.js`

```js
import { registerDiceRollGiveaway, tryHandleGiveawayStart } from "./commands/diceRollGiveaway.js";
```

**`/start` — run giveaway deep link first** (private chat, `start=give_<hex>`):

```js
bot.start(async (ctx, next) => {
  if (await tryHandleGiveawayStart(ctx)) return;
  // ... your existing private /start
  return next?.();
});
```

**Register handlers once** (after `bot` exists, before or after other commands — avoid duplicate `bot.on("text")` that swallows messages without `next()`):

```js
registerDiceRollGiveaway(bot);
```

See `bot.merge.example.js` for a short template.

## 3. `.env` (alongside existing vars)

Your bot token is already **`TELEGRAM_BOT_TOKEN`** — the giveaway module does not read `BOT_TOKEN`; Telegraf uses whatever you passed when constructing `bot`.

Optional:

```env
# For t.me/<bot>?start=... buttons if username not yet on ctx.botInfo
TELEGRAM_BOT_USERNAME=YourBotName
# or
BOT_USERNAME=YourBotName

BOT_ADMINS=123456789
RESTRICT_ROLL_COMMANDS=true
```

## 4. Webhook URL

Telegram must deliver **all** update types you need (messages, **callback_query** for inline buttons). Your existing `setWebhook` URL (e.g. `TELEGRAM_WEBHOOK_URL` / `https://bot.gamblecodez.com/tg-webhook`) should stay pointed at the hub. No second webhook for dice roll.

If reveal buttons never fire, confirm nginx forwards **`POST`** with body to `/webhook` and Telegram’s webhook isn’t filtering `allowed_updates` incorrectly.

## 5. BotFather

Register commands, e.g.:

```
dice_roll_giveaway - Giveaway (tap button in group)
dice_roll - Same
dice_help - Full walkthrough + Telegram HTML rules
create_roll - Quick round with URL (admin)
roll - Roll
abort_roll - Abort first 30s
status - Status
```

## 6. Behaviour

- Group: `/dice_roll_giveaway` → ASCII banner + **tap to open bot** + optional “what next” callback.
- Live round: inline **How do I play?** / **prizes** / **status** (callbacks answered so Telegram never spins).
- DM: paste URLs, toggles, **LAUNCH** or **Cancel setup** (clears stuck wizard).
- `/dice_help` — full start-to-finish + allowed HTML tags note.
- Winners: **Reveal** → **`answerCallbackQuery` popup only** (~200 char URL max). Errors use `safeAnswerCb` so old queries do not crash the handler.

## 7. Legacy CommonJS

If your `bot` package is still CJS (`require`), rename or duplicate this file to `.cjs` and use `module.exports` — or finish migrating `bot.js` to ESM (`"type": "module"`).
