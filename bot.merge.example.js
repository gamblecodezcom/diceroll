/**
 * EXAMPLE: merge into /var/www/html/gcz/bot/bot.js
 *
 * bots-hub.js already does: await bot.handleUpdate(payload) — do NOT duplicate webhook here.
 * Only register Telegraf handlers + wrap /start for giveaway deep links.
 *
 * Add near your other imports:
 */

// import { Telegraf } from "telegraf";
// import { registerDiceRollGiveaway, tryHandleGiveawayStart } from "./commands/diceRollGiveaway.js";

/*
 * In KVM1_MODE=1 your bot likely does NOT call bot.launch(). Still register handlers once
 * after creating `bot`:

bot.start(async (ctx, next) => {
  if (await tryHandleGiveawayStart(ctx)) return;
  // ... existing private /start (login widget follow-up, menus, etc.)
  return next?.();
});

registerDiceRollGiveaway(bot);

 * If you export `bot` for bots-hub: export { bot };  // unchanged
 */
