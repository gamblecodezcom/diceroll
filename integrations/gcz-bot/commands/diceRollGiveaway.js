/**
 * Dice Roll Giveaway — Gamblecodez `bot/commands/diceRollGiveaway.js`
 *
 * ESM (matches bots-hub.js / bot.js `import` style).
 * Wire from `/var/www/html/gcz/bot/bot.js` only — bots-hub.js stays unchanged; it already
 * forwards `POST /webhook` → `bot.handleUpdate()` (KVM1_MODE=1, no polling).
 *
 * Env (/var/www/html/gcz/.env):
 *   TELEGRAM_BOT_TOKEN     — already used by your bot (Telegraf)
 *   TELEGRAM_BOT_USERNAME  — optional; fallback for t.me/ links if botInfo missing
 *   BOT_USERNAME           — optional alias for username
 *   BOT_ADMINS             — optional comma-separated Telegram user IDs
 *   RESTRICT_ROLL_COMMANDS / RESTRICT_HUNT_COMMANDS — "true"|"1"|"yes" → admin-only host cmds
 */

import crypto from "crypto";

const CALLBACK_ALERT_MAX = 200;
const GIVE_PREFIX = "give_";
const CB_REVEAL_PREFIX = "drr";
const DEFAULT_DURATION_SEC = 210;
const ABORT_WINDOW_SEC = 30;
const ROLL_MIN = 1;
const ROLL_MAX = 100;
const HALFWAY_FRACTION = 0.5;
const REVEAL_NONCE_HEX_BYTES = 8;

const DURATION_OPTIONS = [60, 120, 180, 210, 300, 600];
const MAX_PLAYER_OPTIONS = [0, 5, 10, 20, 50, 100];
const WINNER_COUNT_OPTIONS = [1, 2, 3, 4, 5];

function parseAdminIds() {
  const raw = (process.env.BOT_ADMINS || "").trim();
  if (!raw) return new Set();
  return new Set(
    raw
      .split(",")
      .map((s) => s.trim())
      .filter((s) => /^\d+$/.test(s))
      .map((s) => parseInt(s, 10))
  );
}

const BOT_ADMINS = parseAdminIds();

function restrictRollCommands() {
  const a = (process.env.RESTRICT_ROLL_COMMANDS || "").toLowerCase();
  const b = (process.env.RESTRICT_HUNT_COMMANDS || "").toLowerCase();
  return ["1", "true", "yes"].includes(a) || ["1", "true", "yes"].includes(b);
}

function encodeChatId(chatId) {
  let u = BigInt(chatId);
  if (u < 0n) u = u + (1n << 64n);
  return u.toString(16);
}

function decodeChatId(hex) {
  let u = BigInt("0x" + hex);
  if (u >= 1n << 63n) u = u - (1n << 64n);
  return Number(u);
}

function btnText(text, maxLen = 64) {
  if (text.length <= maxLen) return text;
  return text.slice(0, maxLen - 1) + "…";
}

function randomInt(min, max) {
  return min + Math.floor(Math.random() * (max - min + 1));
}

function tokenHex(bytes) {
  return crypto.randomBytes(bytes).toString("hex");
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

/** @type {Map<number, object>} */
const games = new Map();
/** @type {Map<string, { chatId: number, idx: number }>} */
const revealNonceToInfo = new Map();
/** userId -> setup wizard state */
const giveawaySetups = new Map();

function getGame(chatId) {
  if (!games.has(chatId)) {
    games.set(chatId, {
      isActive: false,
      target: 0,
      claimUrls: [],
      players: new Map(),
      winnerIds: [],
      revealNonces: [],
      startTime: 0,
      durationSec: DEFAULT_DURATION_SEC,
      maxPlayers: 0,
      timers: [],
      hostUserId: null,
    });
  }
  return games.get(chatId);
}

function clearTimers(g) {
  if (g.timers?.length) {
    g.timers.forEach((t) => clearTimeout(t));
    g.timers = [];
  }
}

function clearRevealRound(g) {
  for (const n of g.revealNonces) revealNonceToInfo.delete(n);
  g.revealNonces = [];
  g.winnerIds = [];
}

async function isPrivilegedInGroup(ctx) {
  if (!restrictRollCommands()) return true;
  const chat = ctx.chat;
  const user = ctx.from;
  if (!chat || !user) return false;
  if (BOT_ADMINS.has(user.id)) return true;
  try {
    const m = await ctx.telegram.getChatMember(chat.id, user.id);
    return m.status === "creator" || m.status === "administrator";
  } catch {
    return false;
  }
}

async function isUserAdminOfChat(ctx, userId, chatId) {
  if (BOT_ADMINS.has(userId)) return true;
  try {
    const m = await ctx.telegram.getChatMember(chatId, userId);
    return m.status === "creator" || m.status === "administrator";
  } catch {
    return false;
  }
}

function botUsernameFromCtx(ctx) {
  return (
    ctx.botInfo?.username ||
    process.env.TELEGRAM_BOT_USERNAME ||
    process.env.BOT_USERNAME ||
    null
  );
}

function openBotSetupUrl(ctx, groupChatId) {
  const me = botUsernameFromCtx(ctx);
  if (!me) return null;
  const payload = `${GIVE_PREFIX}${encodeChatId(groupChatId)}`;
  if (payload.length > 64) return null;
  return `https://t.me/${me}?start=${payload}`;
}

function defaultSetup() {
  return {
    targetChatId: null,
    claimUrls: [],
    durationSec: DEFAULT_DURATION_SEC,
    maxPlayers: 0,
    numWinners: 1,
  };
}

function setupKeyboard(ud) {
  const d = ud.durationSec;
  const mp = ud.maxPlayers;
  const nw = ud.numWinners;
  const maxLabel = mp === 0 ? "∞" : String(mp);
  return {
    inline_keyboard: [
      [{ text: btnText(`⏱ Countdown: ${d}s`), callback_data: "dr:su:dur" }],
      [{ text: btnText(`👥 Max players: ${maxLabel}`), callback_data: "dr:su:max" }],
      [{ text: btnText(`🏆 Winners: ${nw}`), callback_data: "dr:su:nwin" }],
      [{ text: btnText("🚀 LAUNCH IN GROUP"), callback_data: "dr:su:go" }],
    ],
  };
}

function cycle(cur, options) {
  const i = Math.max(0, options.indexOf(cur));
  return options[(i + 1) % options.length];
}

function pickWinners(g, k) {
  const ranked = [];
  for (const [uid, data] of g.players) {
    const rollV = data.roll;
    const diff = Math.abs(g.target - rollV);
    const ts = data.ts || 0;
    ranked.push({ diff, ts, uid, name: data.name, rollV });
  }
  ranked.sort((a, b) => a.diff - b.diff || a.ts - b.ts);
  const out = [];
  const seen = new Set();
  for (const r of ranked) {
    if (seen.has(r.uid)) continue;
    seen.add(r.uid);
    out.push([r.uid, r.name, r.rollV, r.diff]);
    if (out.length >= k) break;
  }
  return out;
}

/**
 * Call first inside your private-chat /start handler.
 * @param {import('telegraf').Context} ctx
 * @returns {Promise<boolean>}
 */
export async function tryHandleGiveawayStart(ctx) {
  if (ctx.chat?.type !== "private") return false;
  const payload = ctx.startPayload || "";
  if (!payload.startsWith(GIVE_PREFIX)) return false;

  const hexPart = payload.slice(GIVE_PREFIX.length);
  if (!/^[0-9a-fA-F]+$/.test(hexPart)) {
    await ctx.replyWithHTML("Invalid setup link. Post <code>/dice_roll_giveaway</code> in the group again.");
    return true;
  }
  let gid;
  try {
    gid = decodeChatId(hexPart);
  } catch {
    await ctx.replyWithHTML("Invalid setup link.");
    return true;
  }
  const uid = ctx.from.id;
  if (!(await isUserAdminOfChat(ctx, uid, gid))) {
    await ctx.replyWithHTML(
      "You must be an <b>administrator</b> in that group to run giveaway setup."
    );
    return true;
  }

  const ud = defaultSetup();
  ud.targetChatId = gid;
  giveawaySetups.set(uid, ud);

  await ctx.replyWithHTML(
    "<b>Step 1 — Claim link(s)</b>\n\n" +
      "Send <b>one HTTPS URL</b> per line. Line 1 = 1st place, line 2 = 2nd, …\n\n" +
      "<b>Step 2 — Settings</b>\n" +
      "Use the buttons, then <b>LAUNCH IN GROUP</b>.",
    { reply_markup: setupKeyboard(ud) }
  );
  return true;
}

function scheduleRoundEnd(telegram, chatId) {
  const g = getGame(chatId);
  clearTimers(g);
  const half = Math.max(1, Math.floor(g.durationSec * HALFWAY_FRACTION));
  const halfMs = half * 1000;
  const totalMs = g.durationSec * 1000;

  const tHalf = setTimeout(async () => {
    try {
      const gg = getGame(chatId);
      if (!gg.isActive) return;
      await telegram.sendMessage(
        chatId,
        "⏳ <b>Halfway.</b> Send <code>/roll</code> if you have not yet.",
        { parse_mode: "HTML" }
      );
    } catch (_) {}
  }, halfMs);

  const tEnd = setTimeout(async () => {
    try {
      const gg = getGame(chatId);
      if (!gg.isActive) return;
      gg.isActive = false;
      clearTimers(gg);
      await announceWinners(telegram, chatId);
    } catch (e) {
      console.error("[diceRollGiveaway] round end", e);
    }
  }, totalMs);

  g.timers = [tHalf, tEnd];
}

async function announceWinners(telegram, chatId) {
  const g = getGame(chatId);
  if (!g.claimUrls.length) {
    clearRevealRound(g);
    return;
  }
  if (!g.players.size) {
    await telegram.sendMessage(chatId, "No rolls — no winners. 🌑", { parse_mode: "HTML" });
    clearRevealRound(g);
    return;
  }

  const kWanted = g.claimUrls.length;
  let winners = pickWinners(g, kWanted);
  if (winners.length < kWanted) {
    g.claimUrls = g.claimUrls.slice(0, winners.length);
  }
  if (!winners.length) {
    await telegram.sendMessage(chatId, "No winners ranked. 🌑", { parse_mode: "HTML" });
    clearRevealRound(g);
    return;
  }

  g.winnerIds = winners.map((w) => w[0]);
  clearRevealRound(g);
  g.revealNonces = winners.map(() => tokenHex(REVEAL_NONCE_HEX_BYTES));
  g.revealNonces.forEach((n, i) => revealNonceToInfo.set(n, { chatId, idx: i }));

  const lines = ["⚔️ <b>ROUND OVER!</b>\n", `Target was <code>${g.target}</code>.\n`];
  const buttons = [];
  winners.forEach((w, i) => {
    const rank = i + 1;
    const [, name, rollV, diff] = w;
    const medal = rank === 1 ? "👑" : `#${rank}`;
    lines.push(`${medal} <b>${escapeHtml(name)}</b> — <code>${rollV}</code> (off <code>${diff}</code>)`);
    buttons.push([
      {
        text: btnText(`🔒 ${medal} Reveal #${rank} (winner only)`),
        callback_data: `${CB_REVEAL_PREFIX}${g.revealNonces[i]}`,
      },
    ]);
  });
  lines.push("", "<b>Winners:</b> tap <b>your</b> button — private popup only.");

  const caption = lines.join("\n");
  const markup = { inline_keyboard: buttons };
  const primaryUid = winners[0][0];

  try {
    const photos = await telegram.getUserProfilePhotos(primaryUid, { limit: 1 });
    if (photos.total_count > 0) {
      const fileId = photos.photos[0][photos.photos[0].length - 1].file_id;
      await telegram.sendPhoto(chatId, fileId, {
        caption,
        parse_mode: "HTML",
        reply_markup: markup,
      });
    } else {
      await telegram.sendMessage(chatId, caption, { parse_mode: "HTML", reply_markup: markup });
    }
  } catch {
    await telegram.sendMessage(chatId, caption, { parse_mode: "HTML", reply_markup: markup });
  }
}

async function launchGiveaway(ctx, ud) {
  const uid = ctx.from.id;
  const gid = ud.targetChatId;
  const urls = ud.claimUrls || [];
  const nw = ud.numWinners;

  if (urls.length < nw) {
    return ctx.telegram.sendMessage(
      uid,
      `You need at least ${nw} claim URL line(s). Send URLs, then tap LAUNCH again.`,
      { parse_mode: "HTML" }
    );
  }
  if (!(await isUserAdminOfChat(ctx, uid, gid))) {
    return ctx.telegram.sendMessage(uid, "You are not an admin in that group anymore.");
  }

  const g = getGame(gid);
  if (g.isActive) {
    return ctx.telegram.sendMessage(uid, "That group already has an active round.");
  }

  const useUrls = urls.slice(0, nw).map((u) => u.trim());
  for (const u of useUrls) {
    if (!/^https?:\/\//i.test(u)) {
      return ctx.telegram.sendMessage(uid, "Every line must be an http(s) URL.");
    }
  }

  clearRevealRound(g);
  clearTimers(g);
  g.isActive = true;
  g.target = randomInt(ROLL_MIN, ROLL_MAX);
  g.claimUrls = useUrls;
  g.players = new Map();
  g.startTime = Date.now() / 1000;
  g.durationSec = ud.durationSec;
  g.maxPlayers = ud.maxPlayers;
  g.hostUserId = uid;

  let warn = "";
  useUrls.forEach((u, i) => {
    if (u.length > CALLBACK_ALERT_MAX) {
      warn += `\n⚠️ Prize ${i + 1} URL is long; use a shortener (~${CALLBACK_ALERT_MAX} popup limit).`;
    }
  });

  await ctx.telegram.sendMessage(
    gid,
    "🎲 <b>GIVEAWAY LIVE</b> 🎲\n" +
      `Closest to secret target — <b>${nw}</b> winner(s).\n` +
      `⏱ <b>${g.durationSec}s</b>. <code>/abort_roll</code> first <b>${ABORT_WINDOW_SEC}s</b> (admins).\n\n` +
      "<code>/roll</code> once each. Winners: <b>Reveal</b> = private popup only." +
      warn,
    { parse_mode: "HTML" }
  );

  scheduleRoundEnd(ctx.telegram, gid);

  try {
    await ctx.editMessageText("<b>Giveaway started</b> in the group.", {
      parse_mode: "HTML",
      reply_markup: { inline_keyboard: [] },
    });
  } catch {
    await ctx.telegram.sendMessage(uid, "Giveaway started in the group.", { parse_mode: "HTML" });
  }
  giveawaySetups.delete(uid);
}

/**
 * @param {import('telegraf').Telegraf} bot
 */
export function registerDiceRollGiveaway(bot) {
  bot.command(["dice_roll_giveaway", "dice_roll"], async (ctx) => {
    if (ctx.chat?.type !== "group" && ctx.chat?.type !== "supergroup") {
      return ctx.replyWithHTML("<b>Use this command in a group.</b>");
    }
    const url = openBotSetupUrl(ctx, ctx.chat.id);
    if (!url) {
      return ctx.replyWithHTML(
        "Set <code>TELEGRAM_BOT_USERNAME</code> or <code>BOT_USERNAME</code> in .env, or ensure the bot received one update so username is known."
      );
    }
    const text =
      "🚨🎲 <b>DICE ROLL GIVEAWAY</b> 🎲🚨\n\n" +
      "⚠️ <b>STOP — READ THIS</b> ⚠️\n\n" +
      "<b>DO NOT PRESS ENTER</b> on the command alone.\n" +
      "<b>TAP THE BUTTON BELOW</b> — open the bot in private chat to paste claim link(s) and launch.\n\n" +
      "Multiple winners: one <code>https://</code> URL per line in DM.\n" +
      `Whisper: private popup only (~${CALLBACK_ALERT_MAX} chars — shorten links).`;

    await ctx.replyWithHTML(text, {
      reply_markup: {
        inline_keyboard: [
          [
            {
              text: btnText("👉 TAP HERE — OPEN BOT (do NOT press Enter only)"),
              url,
            },
          ],
        ],
      },
    });
  });

  bot.action(/^dr:su:(dur|max|nwin|go)$/, async (ctx) => {
    await ctx.answerCbQuery();
    const action = ctx.match[1];
    const uid = ctx.from.id;
    const ud = giveawaySetups.get(uid);
    if (!ud || ud.targetChatId == null) {
      try {
        await ctx.editMessageReplyMarkup({ inline_keyboard: [] });
      } catch (_) {}
      return;
    }

    if (action === "dur") ud.durationSec = cycle(ud.durationSec, DURATION_OPTIONS);
    else if (action === "max") ud.maxPlayers = cycle(ud.maxPlayers, MAX_PLAYER_OPTIONS);
    else if (action === "nwin") ud.numWinners = cycle(ud.numWinners, WINNER_COUNT_OPTIONS);
    else if (action === "go") return launchGiveaway(ctx, ud);

    try {
      await ctx.editMessageReplyMarkup(setupKeyboard(ud));
    } catch (_) {}
  });

  bot.on("text", async (ctx, next) => {
    if (ctx.chat?.type !== "private") return next();
    const ud = giveawaySetups.get(ctx.from.id);
    if (!ud || ud.targetChatId == null) return next();

    const text = (ctx.message.text || "").trim();
    const lines = text.split("\n").map((l) => l.trim()).filter(Boolean);
    if (!lines.length) return next();

    const urls = lines.filter((l) => /^https?:\/\//i.test(l));
    if (!urls.length) {
      return ctx.replyWithHTML("Send lines starting with <code>http://</code> or <code>https://</code>.");
    }
    ud.claimUrls = urls;
    return ctx.replyWithHTML(
      `Saved <b>${urls.length}</b> URL(s). Adjust toggles, then <b>LAUNCH IN GROUP</b>.`,
      { reply_markup: setupKeyboard(ud) }
    );
  });

  bot.command(["create_roll", "create_hunt"], async (ctx) => {
    if (ctx.chat?.type !== "group" && ctx.chat?.type !== "supergroup") {
      return ctx.replyWithHTML("Use in a <b>group</b>.");
    }
    if (!(await isPrivilegedInGroup(ctx))) {
      return ctx.replyWithHTML("Only admins can start rounds here.");
    }
    try {
      await ctx.deleteMessage();
    } catch (_) {}

    const g = getGame(ctx.chat.id);
    if (g.isActive) {
      return ctx.replyWithHTML("A round is already running. ⏳");
    }
    const arg = (ctx.args || []).join(" ").trim();
    if (!arg) {
      const url = openBotSetupUrl(ctx, ctx.chat.id);
      return ctx.replyWithHTML(
        "Usage: <code>/create_roll &lt;url&gt;</code> or <code>/dice_roll_giveaway</code> + button.",
        url
          ? {
              reply_markup: {
                inline_keyboard: [[{ text: btnText("👉 Open bot setup"), url }]],
              },
            }
          : {}
      );
    }
    if (!/^https?:\/\//i.test(arg)) {
      return ctx.replyWithHTML("URL must start with http:// or https://");
    }

    clearRevealRound(g);
    clearTimers(g);
    g.isActive = true;
    g.target = randomInt(ROLL_MIN, ROLL_MAX);
    g.claimUrls = [arg];
    g.players = new Map();
    g.durationSec = DEFAULT_DURATION_SEC;
    g.maxPlayers = 0;
    g.startTime = Date.now() / 1000;

    let warn = "";
    if (arg.length > CALLBACK_ALERT_MAX) {
      warn = `\n⚠️ URL &gt; ~${CALLBACK_ALERT_MAX} chars — shorten for popup.`;
    }
    await ctx.replyWithHTML(
      "🎲 <b>Dice Roll started!</b>\n" +
        `<code>/roll</code> once — <b>${g.durationSec}s</b>. <code>/abort_roll</code> first <b>${ABORT_WINDOW_SEC}s</b>.` +
        warn
    );
    scheduleRoundEnd(ctx.telegram, ctx.chat.id);
  });

  bot.command(["abort_roll", "abort_hunt"], async (ctx) => {
    const chatId = ctx.chat.id;
    const g = getGame(chatId);
    if (ctx.chat?.type !== "private" && !(await isPrivilegedInGroup(ctx))) {
      return ctx.replyWithHTML("Only admins can abort when restricted.");
    }
    if (!g.isActive) return ctx.replyWithHTML("No active round.");
    if (Date.now() / 1000 - g.startTime > ABORT_WINDOW_SEC) {
      return ctx.replyWithHTML(`Abort only in the first ${ABORT_WINDOW_SEC}s.`);
    }
    g.isActive = false;
    clearTimers(g);
    clearRevealRound(g);
    return ctx.replyWithHTML("Round cancelled. 🏁");
  });

  bot.command("roll", async (ctx) => {
    const g = getGame(ctx.chat.id);
    if (!g.isActive) return;

    const uid = ctx.from.id;
    const name = ctx.from.first_name || ctx.from.username || String(uid);
    if (g.maxPlayers && g.players.size >= g.maxPlayers && !g.players.has(uid)) {
      return ctx.replyWithHTML("Round is <b>full</b>.");
    }
    if (g.players.has(uid)) {
      return ctx.replyWithHTML("You already rolled. 🛑");
    }
    const val = randomInt(ROLL_MIN, ROLL_MAX);
    g.players.set(uid, { name, roll: val, ts: Date.now() / 1000 });
    return ctx.replyWithHTML(`${escapeHtml(name)} rolled <b>${val}</b>. 🎲`);
  });

  bot.command("status", async (ctx) => {
    const g = getGame(ctx.chat.id);
    if (!g.isActive) return ctx.replyWithHTML("No active round.");
    const elapsed = Date.now() / 1000 - g.startTime;
    const left = Math.max(0, Math.floor(g.durationSec - elapsed));
    const cap = g.maxPlayers ? ` / ${g.maxPlayers}` : "";
    const lines = [
      "<b>Round status</b>",
      `Timer: ~${left}s (${g.durationSec}s)`,
      `Players: ${g.players.size}${cap}`,
      `Winner slots: ${g.claimUrls.length}`,
    ];
    for (const [, data] of g.players) {
      lines.push(`• ${escapeHtml(data.name)}: <code>${data.roll}</code>`);
    }
    return ctx.replyWithHTML(lines.join("\n"));
  });

  bot.action(new RegExp(`^${CB_REVEAL_PREFIX}([0-9a-fA-F]+)$`), async (ctx) => {
    const nonce = ctx.match[1];
    const info = revealNonceToInfo.get(nonce);
    if (!info) {
      return ctx.answerCbQuery({ text: "Invalid or expired.", show_alert: true });
    }
    const { chatId, idx } = info;
    const g = getGame(chatId);

    const deny = (t) => ctx.answerCbQuery({ text: t.slice(0, CALLBACK_ALERT_MAX), show_alert: true });

    if (!g.revealNonces[idx] || g.revealNonces[idx] !== nonce) {
      return deny("This button is no longer valid.");
    }
    if (!g.winnerIds[idx] || ctx.from.id !== g.winnerIds[idx]) {
      return deny("Only the assigned winner can reveal this prize.");
    }
    const url = g.claimUrls[idx];
    if (!url) return deny("No prize for this slot.");
    if (url.length > CALLBACK_ALERT_MAX) {
      return deny(`URL too long for popup (max ${CALLBACK_ALERT_MAX}). Use a shortener.`);
    }
    return ctx.answerCbQuery({ text: url, show_alert: true });
  });
}
