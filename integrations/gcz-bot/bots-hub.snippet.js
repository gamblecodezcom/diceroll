/**
 * PM2 runs bots-hub.js — wire the giveaway there (or in a shared factory both hub + bot use).
 *
 * PASTE / ADAPT into your real bots-hub.js after the Telegraf (or bot) instance exists.
 */

// const { Telegraf } = require("telegraf");
// const bot = new Telegraf(process.env.BOT_TOKEN);

const {
  registerDiceRollGiveaway,
  tryHandleGiveawayStart,
} = require("./commands/diceRollGiveaway");

// --- Option A: start handler lives in bots-hub.js ---
bot.start(async (ctx, next) => {
  if (await tryHandleGiveawayStart(ctx)) return;
  // existing welcome / menus for private chat
  // ...
});

registerDiceRollGiveaway(bot);

// --- Option B: bot.js exports a function that builds the bot; call from hub ---
// const { createMainBot } = require("./bot");
// const bot = createMainBot();
// (ensure createMainBot wraps /start with tryHandleGiveawayStart and calls registerDiceRollGiveaway)

// bot.launch() or your existing launch / webhook code
