# Merge Dice Roll Giveaway (PM2 → `bots-hub.js`)

Path on VPS: `/var/www/html/gcz/bot`  
Env: `/var/www/html/gcz/.env` (same `BOT_TOKEN` as the rest of the bot — **one** process must own polling/webhook).

**PM2** starts **`bots-hub.js`**, not `bot.js`. Put the giveaway wiring where your **Telegraf instance is created and launched** (often `bots-hub.js`, or a module it imports from `bot.js`).

See `bots-hub.snippet.js` for a minimal paste pattern.

## 1. Copy the command file

Copy `commands/diceRollGiveaway.js` into your repo:

```
/var/www/html/gcz/bot/commands/diceRollGiveaway.js
```

## 2. Wire `bots-hub.js` (or shared bot factory)

Use **Telegraf** (matches this module). Where you have the live `bot` instance:

```js
const { registerDiceRollGiveaway, tryHandleGiveawayStart } = require("./commands/diceRollGiveaway");
```

**Important:** `/start` must run the giveaway deep-link **first**, then your normal private `/start`:

```js
bot.start(async (ctx, next) => {
  if (await tryHandleGiveawayStart(ctx)) return;
  // ... your existing private /start (welcome, menus, etc.)
  return next?.();
});
```

After session/middleware (if any), register handlers **once**:

```js
registerDiceRollGiveaway(bot);
```

If `bot.start` is defined inside **`bot.js`** and `bots-hub.js` only `require("./bot")` / `launch()`, edit **`bot.js`** for `tryHandleGiveawayStart` + `registerDiceRollGiveaway` instead — the rule is: whichever file attaches handlers to the Telegraf instance PM2 runs.

**Handler order:** if another `bot.on("text")` runs first and never calls `next()`, move `registerDiceRollGiveaway` **above** it or fix `next()` chaining.

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
