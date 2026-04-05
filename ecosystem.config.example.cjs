/**
 * PM2 example: run this Python bot next to your Node Telegram backend.
 * Copy to ecosystem.config.cjs (or merge into your existing PM2 file).
 *
 * 1. Use a BOT_TOKEN that is NOT already used by another polling bot process.
 * 2. Set PORT to a port your Node app does not bind (e.g. 8090).
 * 3. From this directory: pm2 start ecosystem.config.example.cjs --only dice-roll-bot
 */
module.exports = {
  apps: [
    {
      name: "dice-roll-bot",
      script: "bot.py",
      interpreter: "python3",
      cwd: __dirname,
      env: {
        BOT_TOKEN: "REPLACE_ME",
        PORT: "8090",
        BIND: "0.0.0.0",
      },
      autorestart: true,
      max_restarts: 10,
      restart_delay: 3000,
    },
  ],
};
