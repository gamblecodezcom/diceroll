# Merge Dice Roll Giveaway into `bot/bot.js` (Gamblecodez VPS)

Path on VPS: `/var/www/html/gcz/bot`  
Env: `/var/www/html/gcz/.env` (same `BOT_TOKEN` as the rest of the bot — **one** process must own polling/webhook).

## 1. Copy the command file

Copy `commands/diceRollGiveaway.js` into your repo:

```
/var/www/html/gcz/bot/commands/diceRollGiveaway.js
```

## 2. Wire `bot.js`

Use **Telegraf** (matches this module). At the top:

```js
const { registerDiceRollGiveaway, tryHandleGiveawayStart } = require("./commands/diceRollGiveaway");
```

**Important:** your existing `bot.start(...)` must run the giveaway deep-link **first**, then your normal `/start` logic:

```js
bot.start(async (ctx, next) => {
  if (await tryHandleGiveawayStart(ctx)) return;
  // ... your existing private /start (welcome, menus, etc.)
  return next?.();
});
```

After other middleware (session, etc.), register handlers:

```js
registerDiceRollGiveaway(bot);
```

Register **once**. Order relative to other `text` handlers: if another `bot.on("text")` runs first and never calls `next()`, move `registerDiceRollGiveaway` **above** it or chain `next()` correctly.

## 3. BotFather commands (optional)

Suggested entries:

```
dice_roll_giveaway - Start giveaway (tap button — do not press Enter only)
dice_roll - Same as dice_roll_giveaway
create_roll - Quick single-URL round (admin)
roll - Roll once
abort_roll - Abort in first 30s (admin)
status - Round status
```

## 4. `.env` additions (optional)

```env
BOT_USERNAME=YourBotName
BOT_ADMINS=123456789
RESTRICT_ROLL_COMMANDS=true
```

If `ctx.botInfo.username` is missing on first update, set `BOT_USERNAME` so the “open bot” button URL works.

## 5. Python bot on the same VPS

If you still run `bot.py` with the **same** `BOT_TOKEN`, **stop it** — only one client may poll or set webhook. Either:

- **Node only** (this integration), or  
- **Python only** (standalone `bot.py`), or  
- **different tokens** for two bots.

## 6. Behaviour summary

- Group: `/dice_roll_giveaway` → loud message + URL button → DM setup → toggles → launch.
- Whisper: `answerCallbackQuery` + `show_alert` only (~200 char URL max).
- Multi-winner: one `https://` URL per line in DM; winner count toggle 1–5.

## 7. If you do not use Telegraf

Port the same state machine (`games`, `giveawaySetups`, `revealNonceToInfo`) and Telegram methods (`sendMessage`, `answerCallbackQuery`, etc.) from this file into your framework, or add Telegraf alongside your stack only for this command (not recommended).
