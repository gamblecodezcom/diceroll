# Dice Roll Giveaway — Node / Telegraf

Telegram **dice giveaway** module for **`bot.js`**: Cwallet-style HTTPS prizes, **multi-winner** (one URL per line), **countdown**, **max players**, **winner-only whisper** via callback popup (no DMs).

**Runtime:** Node.js + [Telegraf](https://github.com/telegraf/telegraf). Your **`bots-hub.js`** (or equivalent) should forward updates with `bot.handleUpdate(payload)` — see `README-INTEGRATION.md`.

## Quick copy

```bash
cp commands/diceRollGiveaway.js /var/www/html/gcz/bot/commands/diceRollGiveaway.js
```

In **`bot.js`**:

```js
import { registerDiceRollGiveaway, tryHandleGiveawayStart } from "./commands/diceRollGiveaway.js";

bot.start(async (ctx, next) => {
  if (await tryHandleGiveawayStart(ctx)) return;
  // ... your existing /start
  return next?.();
});

registerDiceRollGiveaway(bot);
```

See **`bot.merge.example.js`** for a short template.

## Features

- **`/dice_roll_giveaway`** / **`/dice_roll`** — loud group card + **tap to open bot** (not Enter-only)
- **`/dice_help`** — full walkthrough + Telegram HTML rules
- DM wizard: URLs, toggles, **LAUNCH** or **Cancel setup**
- Live round: hint buttons (play / prizes / status)
- **`/create_roll <url>`** — quick single-winner round
- **`/roll`**, **`/status`**, **`/abort_roll`** (+ hunt aliases)
- Reveal callbacks prefixed **`drr`** + hex

## Env

See **`.env.example`**. Uses **`TELEGRAM_BOT_TOKEN`** (and optional **`TELEGRAM_BOT_USERNAME`**) like your GCZ stack.

## Files

| File | Purpose |
|------|---------|
| `commands/diceRollGiveaway.js` | Module: `registerDiceRollGiveaway`, `tryHandleGiveawayStart` |
| `README-INTEGRATION.md` | GCZ `bots-hub.js` + `bot.js` + BotFather |
| `bot.merge.example.js` | Paste pattern for `bot.js` |
| `bots-hub.snippet.js` | Note: hub usually needs no change |

## License

Use and modify for your community.
