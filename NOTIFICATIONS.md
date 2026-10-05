# Telegram portfolio notifications

One independent local reporter covers both paper portfolios. It reads broker fill activities every 30 seconds and sends an alert for each confirmed execution (including partial fills). It never submits orders or calls Clef. Only executions belonging to orders in these strategy ledgers are alerted. Alerts begin when the reporter is first activated; old trades are not replayed.

At 5 p.m. America/New_York on market trading days, it queues one combined report: daily, weekly, and total P/L with percentages; equity, cash, buying power, gross exposure, holdings and unrealized P/L; entry pause and broker account status. Failed authentication is reported as unavailable. Weekly P/L starts at the prior week's closing equity; total P/L starts at the first recorded strategy snapshot. Cash transfers are subtracted from equity change, dividends/fees remain in P/L. Returns are simple baseline returns, not time-weighted returns. External securities transfers invalidate P/L. Account-wide values include any outside account activity. Marks reflect report time and can include after-hours movements.

## Connect your Telegram bot

1. Open https://t.me/BotFather in Telegram. Send `/newbot` and follow its prompts. Copy the bot token into the private configuration file below. Never commit it or paste it into a chat.
2. Open your new bot and send it a private message, e.g. `start`.
3. Edit `/Users/ibrahimkhan/Documents/Codex/2026-10-04/algo-trading-clef-fixedentry/state/notifications/notifications.env`:

```
NOTIFICATION_PROVIDER=none
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=
REPORT_HOUR_ET=17
```

4. Run this once. It discovers your private chat ID, enables Telegram in the configuration, and sends a connection test:

```sh
cd /Users/ibrahimkhan/Documents/Codex/2026-10-04/algo-trading-clef-fixedentry
./.venv/bin/python -m clef_trader.notifications --projects "$PWD" ../algo-trading-clef-intelligententry --state "$PWD/state/notifications" --setup-telegram
```

If your bot has received messages from several private chats, setup refuses to guess: enter your own `TELEGRAM_CHAT_ID` and `NOTIFICATION_PROVIDER=telegram` manually, then run the same command with `--test` instead of `--setup-telegram`.

The running reporter reloads its configuration each cycle. A failed test does not imply delivery is connected. Use a dedicated bot; an existing bot webhook can prevent `getUpdates` chat discovery. Sending personal financial information to a group requires explicitly entering that group's chat ID.

## Run or preview

```sh
# Fresh combined report, printed locally; no Telegram delivery
./.venv/bin/python -m clef_trader.notifications --projects "$PWD" ../algo-trading-clef-intelligententry --state "$PWD/state/notifications" --preview
# Reporter service (foreground). Run one instance only.
./.venv/bin/python -m clef_trader.notifications --projects "$PWD" ../algo-trading-clef-intelligententry --state "$PWD/state/notifications"
```

The background instance's PID, logs, nightly text reports, heartbeat and durable queue are under `fixedentry/state/notifications/`. Delivery is disabled until configured; messages queue locally. After connecting, queued messages are delivered. Successful messages are not resent on normal restarts. Temporary failures retry with backoff; a provider accepting a message before a network timeout can cause a duplicate on retry. Long reports split into several messages. Do not delete the SQLite queue to restart. Broker keys are read separately from each project's `.env` on each cycle, so tomorrow's intelligent account credential fix requires no reporter restart.

The Mac and reporter must remain running. Background processes do not automatically restart after reboot/logout, and missed past-night reports are not reconstructed. Notifications do not consume Cloudflare allowance. Telegram Bot API delivery has no per-message charge for this personal bot.
