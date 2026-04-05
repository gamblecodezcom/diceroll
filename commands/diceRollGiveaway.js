/**
 * Dice Roll Giveaway — wire from `bot.js` (Node / Telegraf, ESM).
 * bots-hub.js → bot.handleUpdate() only; no Python.
 *
 * Telegram HTML: use only <b>, <i>, <u>, <s>, <code>, <pre>, <a href="...">.
 * No raw <br> — line breaks are \n in the string.
 * https://core.telegram.org/bots/api#html-style
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

const DICE_FACES = ["⚀", "⚁", "⚂", "⚃", "⚄", "⚅"];

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

function diceEmojiForRoll(n) {
  return DICE_FACES[(Math.abs(n - 1) % 6 + 6) % 6];
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

async function safeAnswerCb(ctx, opts) {
  try {
    await ctx.answerCbQuery(opts);
  } catch (_) {
    /* query too old / already answered — don’t break the bot */
  }
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
      [{ text: btnText("❌ Cancel setup"), callback_data: "dr:su:cancel" }],
    ],
  };
}

/** Player helpers on live round messages (callbacks always answered). */
function liveRoundKeyboard() {
  return {
    inline_keyboard: [
      [
        { text: btnText("🎲 How do I play?"), callback_data: "dr:hint:play" },
        { text: btnText("🏆 How do prizes work?"), callback_data: "dr:hint:prize" },
      ],
      [{ text: btnText("📊 Refresh status (/status)"), callback_data: "dr:hint:status" }],
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

const MSG_HTML_GUIDE =
  "<b>Telegram HTML (what this bot uses)</b>\n" +
  "Allowed tags: <code>&lt;b&gt;</code>, <code>&lt;i&gt;</code>, <code>&lt;u&gt;</code>, " +
  "<code>&lt;s&gt;</code>, <code>&lt;code&gt;</code>, <code>&lt;pre&gt;</code>, " +
  "<code>&lt;a href=\"…\"&gt;</code>.\n" +
  "Line breaks = new lines in the message, <u>not</u> <code>&lt;br&gt;</code>.\n" +
  "Prize links in popups: max ~200 characters — shorten with a link shortener.";

const MSG_FULL_WALKTHROUGH =
  "<b>🎰 Dice Roll Giveaway — start to finish</b>\n\n" +
  "<b>1 — Host (group admin)</b>\n" +
  "Send <code>/dice_roll_giveaway</code> or <code>/dice_roll</code>.\n" +
  "<b>Do not only press Enter.</b> Tap <b>OPEN BOT</b> on the card.\n\n" +
  "<b>2 — Host (private chat with bot)</b>\n" +
  "Paste <b>one https:// claim URL per line</b> (line 1 = 1st place).\n" +
  "Tap toggles: countdown, max players, number of winners.\n" +
  "Tap <b>LAUNCH IN GROUP</b>.\n\n" +
  "<b>3 — Everyone (group)</b>\n" +
  "Send <code>/roll</code> once before time runs out.\n" +
  "Use the <b>How do I play?</b> button if you forget.\n\n" +
  "<b>4 — After the round</b>\n" +
  "Winners tap <b>only their</b> Reveal button — link shows in a <b>private popup</b>.\n\n" +
  "<b>Stuck?</b> Tap <b>Cancel setup</b> in DM, or run the group command again.\n" +
  "<code>/dice_help</code> shows this anytime.";

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
    await ctx.replyWithHTML(
      "Invalid setup link. Ask the host to run <code>/dice_roll_giveaway</code> in the group again."
    );
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
    "<b>🎯 Host setup — step 1 of 2</b>\n\n" +
      "Paste your <b>Cwallet / claim</b> links:\n" +
      "• <b>One winner</b> → one line starting with <code>https://</code>\n" +
      "• <b>Multiple winners</b> → one URL per line (top = 1st place)\n\n" +
      "<b>Step 2</b> — use the buttons to tune the game, then <b>LAUNCH IN GROUP</b>.\n\n" +
      MSG_HTML_GUIDE,
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
        "⏳ <b>Halfway there!</b>\n" +
          "Still in? Send <code>/roll</code> in <u>this</u> chat if you have not yet.\n" +
          "Tap <b>How do I play?</b> on the giveaway message if you need a hint.",
        { parse_mode: "HTML", reply_markup: liveRoundKeyboard() }
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
    await telegram.sendMessage(
      chatId,
      "🌑 <b>No dice on the table</b>\nNobody rolled — no winners this round.",
      { parse_mode: "HTML" }
    );
    clearRevealRound(g);
    return;
  }

  const kWanted = g.claimUrls.length;
  let winners = pickWinners(g, kWanted);
  if (winners.length < kWanted) {
    g.claimUrls = g.claimUrls.slice(0, winners.length);
  }
  if (!winners.length) {
    await telegram.sendMessage(chatId, "🌑 <b>Could not pick winners.</b>", { parse_mode: "HTML" });
    clearRevealRound(g);
    return;
  }

  g.winnerIds = winners.map((w) => w[0]);
  clearRevealRound(g);
  g.revealNonces = winners.map(() => tokenHex(REVEAL_NONCE_HEX_BYTES));
  g.revealNonces.forEach((n, i) => revealNonceToInfo.set(n, { chatId, idx: i }));

  const banner =
    "<pre>╔══════════════════════╗\n" +
    "║  🎲  ROUND COMPLETE  🎲  ║\n" +
    "╚══════════════════════╝</pre>\n";

  const lines = [
    banner,
    "<b>🎊 The vault number was…</b> <code>" + g.target + "</code>\n",
    "<b>🏆 Winners</b>",
  ];
  const buttons = [];
  winners.forEach((w, i) => {
    const rank = i + 1;
    const [, name, rollV, diff] = w;
    const medal = rank === 1 ? "👑" : "🥈";
    const label = rank === 1 ? "1st" : rank === 2 ? "2nd" : rank === 3 ? "3rd" : `${rank}th`;
    lines.push(
      `${medal} <b>${label}:</b> ${escapeHtml(name)} → <code>${rollV}</code> <i>(off ${diff})</i>`
    );
    buttons.push([
      {
        text: btnText(`🔐 ${label}: tap YOUR prize`),
        callback_data: `${CB_REVEAL_PREFIX}${g.revealNonces[i]}`,
      },
    ]);
  });
  lines.push(
    "",
    "<b>Winners:</b> tap <u>your</u> button only — <b>private popup</b>, not visible to others.",
    "<i>Not a winner?</i> Buttons will say so — no spoilers in chat."
  );

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
  } catch (e) {
    console.error("[diceRollGiveaway] announce", e);
    try {
      await telegram.sendMessage(chatId, caption, { parse_mode: "HTML", reply_markup: markup });
    } catch (e2) {
      console.error("[diceRollGiveaway] announce fallback", e2);
    }
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
      `<b>Need more links</b>\nYou chose <b>${nw}</b> winner(s) but only have <b>${urls.length}</b> URL line(s).\nPaste more lines, then tap <b>LAUNCH</b> again.`,
      { parse_mode: "HTML" }
    );
  }
  if (!(await isUserAdminOfChat(ctx, uid, gid))) {
    return ctx.telegram.sendMessage(
      uid,
      "<b>Permission changed</b>\nYou are not an admin in that group anymore.",
      { parse_mode: "HTML" }
    );
  }

  const g = getGame(gid);
  if (g.isActive) {
    return ctx.telegram.sendMessage(
      uid,
      "<b>Already live</b>\nThat group already has a round running. Wait for it to finish.",
      { parse_mode: "HTML" }
    );
  }

  const useUrls = urls.slice(0, nw).map((u) => u.trim());
  for (const u of useUrls) {
    if (!/^https?:\/\//i.test(u)) {
      return ctx.telegram.sendMessage(
        uid,
        "<b>Invalid line</b>\nEvery line must start with <code>http://</code> or <code>https://</code>.",
        { parse_mode: "HTML" }
      );
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
      warn += `\n⚠️ Prize <b>${i + 1}</b> URL is long — shorten for the ~${CALLBACK_ALERT_MAX}-char popup.`;
    }
  });

  const openUrl = openBotSetupUrl(ctx, gid);
  const hostRow = openUrl
    ? [[{ text: btnText("🔁 Host: open setup again"), url: openUrl }]]
    : [];

  const liveBanner =
    "<pre>╔══════════════════════════╗\n" +
    "║ 🎰  DICE ROLL LIVE  🎰  ║\n" +
    "╚══════════════════════════╝</pre>\n";

  await ctx.telegram.sendMessage(
    gid,
    liveBanner +
      "<b>Giveaway is ON!</b> ✨\n\n" +
      `<b>Goal:</b> roll closest to the secret number (${ROLL_MIN}–${ROLL_MAX}).\n` +
      `<b>Winners:</b> <b>${nw}</b>\n` +
      `<b>Time:</b> <code>${g.durationSec}s</code>\n` +
      (g.maxPlayers ? `<b>Cap:</b> <code>${g.maxPlayers}</code> players\n` : "<b>Cap:</b> open to all\n") +
      "\n<b>Your move:</b> send <code>/roll</code> here <u>once</u>.\n" +
      `<b>Admins:</b> <code>/abort_roll</code> works in the first <b>${ABORT_WINDOW_SEC}s</b>.` +
      warn,
    {
      parse_mode: "HTML",
      reply_markup: {
        inline_keyboard: [...hostRow, ...liveRoundKeyboard().inline_keyboard],
      },
    }
  );

  scheduleRoundEnd(ctx.telegram, gid);

  try {
    await ctx.editMessageText(
      "<b>✅ Launched!</b>\nThe giveaway is running in the group. You can close this chat.",
      { parse_mode: "HTML", reply_markup: { inline_keyboard: [] } }
    );
  } catch {
    await ctx.telegram.sendMessage(
      uid,
      "<b>✅ Launched!</b>\nCheck the group — the round is live.",
      { parse_mode: "HTML" }
    );
  }
  giveawaySetups.delete(uid);
}

/**
 * @param {import('telegraf').Telegraf} bot
 */
export function registerDiceRollGiveaway(bot) {
  bot.command("dice_help", async (ctx) => {
    try {
      await ctx.replyWithHTML(MSG_FULL_WALKTHROUGH + "\n\n" + MSG_HTML_GUIDE);
    } catch (e) {
      console.error("[diceRollGiveaway] dice_help", e);
    }
  });

  bot.command(["dice_roll_giveaway", "dice_roll"], async (ctx) => {
    try {
      if (ctx.chat?.type !== "group" && ctx.chat?.type !== "supergroup") {
        return ctx.replyWithHTML("<b>Run this in a group</b> where you want the giveaway.");
      }
      const url = openBotSetupUrl(ctx, ctx.chat.id);
      if (!url) {
        return ctx.replyWithHTML(
          "<b>Bot username missing</b>\nSet <code>TELEGRAM_BOT_USERNAME</code> or <code>BOT_USERNAME</code> in <code>.env</code>, or let the bot receive any update first."
        );
      }
      const banner =
        "<pre>╔════════════════════════════╗\n" +
        "║  🎲   GIVEAWAY STARTER   🎲  ║\n" +
        "╚════════════════════════════╝</pre>\n";

      const text =
        banner +
        "<b>⚠️ Read before you tap ⚠️</b>\n\n" +
        "<b>Do NOT</b> only press <b>Enter</b> on the command.\n" +
        "<b>DO</b> tap the <b>big button</b> below — it opens this bot in <b>private</b> chat.\n\n" +
        "<b>There you will:</b>\n" +
        "1. Paste <code>https://</code> prize link(s) — <b>one line per winner</b>\n" +
        "2. Tap toggles (time / max players / winners)\n" +
        "3. Tap <b>LAUNCH IN GROUP</b>\n\n" +
        "<b>Players</b> then use <code>/roll</code> here.\n" +
        "<b>Prizes</b> = private popup for winners only (~200 char links — shorten!).\n\n" +
        "<code>/dice_help</code> — full walkthrough + HTML rules";

      await ctx.replyWithHTML(text, {
        reply_markup: {
          inline_keyboard: [
            [{ text: btnText("👉 OPEN BOT — setup giveaway (tap me)"), url }],
            [{ text: btnText("📖 What happens next?"), callback_data: "dr:hint:host" }],
          ],
        },
      });
    } catch (e) {
      console.error("[diceRollGiveaway] dice_roll_giveaway", e);
      try {
        await ctx.reply("⚠️ Something went wrong. Try again or /dice_help");
      } catch (_) {}
    }
  });

  bot.action(/^dr:hint:(play|prize|status|host)$/, async (ctx) => {
    const kind = ctx.match[1];
    let text = "";
    if (kind === "play") {
      text =
        "In THIS group, type /roll and send. One roll per person. Wait for the timer to end.";
    } else if (kind === "prize") {
      text =
        "If you won, tap ONLY your Reveal button. Your link opens in a private Telegram popup — others cannot see it.";
    } else if (kind === "status") {
      text = "Send /status in the group to see time left and who rolled.";
    } else {
      text =
        "After you tap OPEN BOT: paste prize URLs in DM, adjust buttons, tap LAUNCH. Group sees the live round.";
    }
    await safeAnswerCb(ctx, { text: text.slice(0, CALLBACK_ALERT_MAX), show_alert: true });
  });

  bot.action(/^dr:su:(dur|max|nwin|go|cancel)$/, async (ctx) => {
    await safeAnswerCb(ctx, {});
    const action = ctx.match[1];
    const uid = ctx.from.id;

    if (action === "cancel") {
      if (giveawaySetups.has(uid)) {
        giveawaySetups.delete(uid);
        try {
          await ctx.editMessageText("<b>Setup cancelled.</b>\nRun <code>/dice_roll_giveaway</code> in the group to start again.", {
            parse_mode: "HTML",
            reply_markup: { inline_keyboard: [] },
          });
        } catch {
          await ctx.telegram.sendMessage(uid, "<b>Setup cancelled.</b>", { parse_mode: "HTML" });
        }
      }
      return;
    }

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
    try {
      if (ctx.chat?.type !== "private") return next();
      const ud = giveawaySetups.get(ctx.from.id);
      if (!ud || ud.targetChatId == null) return next();

      const text = (ctx.message.text || "").trim();
      const lines = text.split("\n").map((l) => l.trim()).filter(Boolean);
      if (!lines.length) return next();

      const urls = lines.filter((l) => /^https?:\/\//i.test(l));
      if (!urls.length) {
        return ctx.replyWithHTML(
          "<b>Those lines are not links</b>\n" +
            "Each prize line must start with <code>http://</code> or <code>https://</code>.\n\n" +
            "<b>Or</b> tap <b>Cancel setup</b> if you want to stop.\n" +
            "<code>/dice_help</code> for the full guide.",
          { reply_markup: setupKeyboard(ud) }
        );
      }
      ud.claimUrls = urls;
      return ctx.replyWithHTML(
        `<b>✅ Saved ${urls.length} link(s)</b>\n` +
          `<b>Winners selected:</b> <code>${ud.numWinners}</code> — you need at least that many lines.\n` +
          "Adjust toggles if needed, then <b>LAUNCH IN GROUP</b>.",
        { reply_markup: setupKeyboard(ud) }
      );
    } catch (e) {
      console.error("[diceRollGiveaway] private text", e);
      try {
        await ctx.reply("⚠️ Could not read that. Try again or /dice_help");
      } catch (_) {}
    }
  });

  bot.command(["create_roll", "create_hunt"], async (ctx) => {
    try {
      if (ctx.chat?.type !== "group" && ctx.chat?.type !== "supergroup") {
        return ctx.replyWithHTML("Use in a <b>group</b>.");
      }
      if (!(await isPrivilegedInGroup(ctx))) {
        return ctx.replyWithHTML("Only <b>admins</b> can start a quick round here.");
      }
      try {
        await ctx.deleteMessage();
      } catch (_) {}

      const g = getGame(ctx.chat.id);
      if (g.isActive) {
        return ctx.replyWithHTML("<b>Already rolling</b> — a round is live. ⏳");
      }
      const arg = (ctx.args || []).join(" ").trim();
      if (!arg) {
        const url = openBotSetupUrl(ctx, ctx.chat.id);
        return ctx.replyWithHTML(
          "<b>Quick start</b>\n<code>/create_roll https://your-prize-link</code>\n\n" +
            "<b>Or</b> use <code>/dice_roll_giveaway</code> for the full wizard.",
          url
            ? {
                reply_markup: {
                  inline_keyboard: [[{ text: btnText("🎯 Full giveaway wizard"), url }]],
                },
              }
            : {}
        );
      }
      if (!/^https?:\/\//i.test(arg)) {
        return ctx.replyWithHTML("URL must start with <code>http://</code> or <code>https://</code>");
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
        warn = `\n⚠️ URL is long — shorten for the <code>${CALLBACK_ALERT_MAX}</code>-char popup.`;
      }
      await ctx.replyWithHTML(
        "<pre>🎲 ─── GO! ─── 🎲</pre>\n" +
          "<b>Round started!</b>\n" +
          `<code>/roll</code> once each · <b>${g.durationSec}s</b> on the clock\n` +
          `<code>/abort_roll</code> · first <b>${ABORT_WINDOW_SEC}s</b> (admins)` +
          warn,
        { reply_markup: liveRoundKeyboard() }
      );
      scheduleRoundEnd(ctx.telegram, ctx.chat.id);
    } catch (e) {
      console.error("[diceRollGiveaway] create_roll", e);
      try {
        await ctx.reply("⚠️ Could not start round. Try /dice_help");
      } catch (_) {}
    }
  });

  bot.command(["abort_roll", "abort_hunt"], async (ctx) => {
    try {
      const chatId = ctx.chat.id;
      const g = getGame(chatId);
      if (ctx.chat?.type !== "private" && !(await isPrivilegedInGroup(ctx))) {
        return ctx.replyWithHTML("Only <b>admins</b> can abort when restrictions are on.");
      }
      if (!g.isActive) return ctx.replyWithHTML("No active round.");
      if (Date.now() / 1000 - g.startTime > ABORT_WINDOW_SEC) {
        return ctx.replyWithHTML(`Abort only in the first <b>${ABORT_WINDOW_SEC}s</b>.`);
      }
      g.isActive = false;
      clearTimers(g);
      clearRevealRound(g);
      return ctx.replyWithHTML("<b>Round cancelled.</b> 🏁");
    } catch (e) {
      console.error("[diceRollGiveaway] abort", e);
    }
  });

  bot.command("roll", async (ctx) => {
    try {
      const g = getGame(ctx.chat.id);
      if (!g.isActive) return;

      const uid = ctx.from.id;
      const name = ctx.from.first_name || ctx.from.username || String(uid);
      if (g.maxPlayers && g.players.size >= g.maxPlayers && !g.players.has(uid)) {
        return ctx.replyWithHTML("<b>Table full</b> — max players reached for this round.");
      }
      if (g.players.has(uid)) {
        return ctx.replyWithHTML("<b>Already rolled</b> — one shot per round! 🛑");
      }
      const val = randomInt(ROLL_MIN, ROLL_MAX);
      g.players.set(uid, { name, roll: val, ts: Date.now() / 1000 });
      const face = diceEmojiForRoll(val);
      return ctx.replyWithHTML(
        `${face} <b>${escapeHtml(name)}</b> throws the dice… <b>${val}</b>! 🎲✨`
      );
    } catch (e) {
      console.error("[diceRollGiveaway] roll", e);
      try {
        await ctx.reply("⚠️ Roll failed — try /roll again");
      } catch (_) {}
    }
  });

  bot.command("status", async (ctx) => {
    try {
      const g = getGame(ctx.chat.id);
      if (!g.isActive) return ctx.replyWithHTML("<b>No active round</b> in this chat.");
      const elapsed = Date.now() / 1000 - g.startTime;
      const left = Math.max(0, Math.floor(g.durationSec - elapsed));
      const cap = g.maxPlayers ? ` / ${g.maxPlayers}` : "";
      const lines = [
        "<b>📊 Live status</b>",
        `⏱ <b>~${left}s</b> left · total <code>${g.durationSec}s</code>`,
        `👥 <b>${g.players.size}</b> rolled${cap}`,
        `🏆 <b>${g.claimUrls.length}</b> prize slot(s)`,
        "",
        "<b>Rolled so far:</b>",
      ];
      for (const [, data] of g.players) {
        lines.push(`• ${escapeHtml(data.name)} → <code>${data.roll}</code>`);
      }
      return ctx.replyWithHTML(lines.join("\n"));
    } catch (e) {
      console.error("[diceRollGiveaway] status", e);
    }
  });

  bot.action(new RegExp(`^${CB_REVEAL_PREFIX}([0-9a-fA-F]+)$`), async (ctx) => {
    try {
      const nonce = ctx.match[1];
      const info = revealNonceToInfo.get(nonce);
      if (!info) {
        return safeAnswerCb(ctx, { text: "Expired or invalid button.", show_alert: true });
      }
      const { chatId, idx } = info;
      const g = getGame(chatId);

      const deny = (t) => safeAnswerCb(ctx, { text: t.slice(0, CALLBACK_ALERT_MAX), show_alert: true });

      if (!g.revealNonces[idx] || g.revealNonces[idx] !== nonce) {
        return deny("This button is no longer valid.");
      }
      if (!g.winnerIds[idx] || ctx.from.id !== g.winnerIds[idx]) {
        return deny("Not your prize — only the listed winner can open this.");
      }
      const url = g.claimUrls[idx];
      if (!url) return deny("No prize for this slot.");
      if (url.length > CALLBACK_ALERT_MAX) {
        return deny(`Link too long for Telegram popup (max ${CALLBACK_ALERT_MAX}). Ask host to shorten.`);
      }
      return safeAnswerCb(ctx, { text: url, show_alert: true });
    } catch (e) {
      console.error("[diceRollGiveaway] reveal", e);
      await safeAnswerCb(ctx, { text: "Try again in a moment.", show_alert: true });
    }
  });
}
