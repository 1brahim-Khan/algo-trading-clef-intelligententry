# Clef chart trader — confidence-sized entries

Local, **paper-only** swing-trading experiment using annotated weekly watchlists, four chart timeframes, Cloudflare Clef, and Alpaca. Each qualified entry requests **$2,500–$10,000**, selected from Clef’s entry conviction. This project is separate from the original Monday-buy/Friday-sell tool.

The companion [fixed-entry project](https://github.com/1brahim-Khan/algo-trading-clef-fixedentry) uses the same entry decisions and exits, but requests $5,000 for each qualified entry. Give the two projects separate Alpaca paper accounts with equal starting cash, ideally $100,000 each.

## Behavior

1. Import your watchlist with one freeform technical/chart annotation per ticker.
2. Every 30 minutes during regular trading hours, evaluate tickers that have not entered this week and are not already held.
3. Fetch consolidated price and volume history; render completed 30-minute, hourly, daily, and weekly candlestick charts. Daily charts overlay the 21/50 EMAs and weekly charts overlay the 8 EMA. Numerical context includes daily/weekly relative strength versus QQQ, a 252-session price-high window, and the meaning/limits of common watchlist shorthand. No candle smaller than 30 minutes is analyzed or requested.
4. Send the charts, exact OHLCV values, annotation, and calculated stop candidates to **`@cf/cloudflare/clef`**. Clef selects `enter`, `wait`, or `skip`, a technical stop, and an evidence category. `skip` retires the ticker for this watchlist week; `wait` is reconsidered at the next interval.
5. Enter only when `enter` has probability ≥ 0.70, model-reported confidence ≥ 0.50, and a defensible stop. The fresh IEX execution reference must be within 2% of the last completed consolidated candle close. Stop distance must be within 12%. These thresholds are configurable experimental parameters, not validated trading performance claims.
6. Request a confidence-sized `market`/`day` notional order using the configured $2,500/$5,000/$7,500/$10,000 tiers. Keep the position across weeks. Never add to an existing holding or re-enter a ticker during the same week after an order attempt/exit.
7. Check held positions on each polling cycle. Sell if broker-reported current price crosses the selected technical stop. Also sell any held position below its actual average entry price during the **last five minutes of every trading day**, regardless of how long it has been held. Winners and flat positions stay open. No take profits and no blanket Friday sale.

Use `close_exit_minutes: 1` for a final-minute loss check. Checks repeat until the session closes; a position that becomes red after the first check can still exit. Holidays and early closes follow Alpaca's calendar. Near-close decisions use a current broker position snapshot, not the delayed chart feed. They do not use the unknown final closing price.

## Free data and free AI

**Charts:** Alpaca's historical SIP endpoint covers all US exchanges. Basic accounts can query it for free when `end` is at least 15 minutes old. The code explicitly uses `feed=sip` and ends requests 15 minutes plus a small scheduling buffer behind the clock; it never silently substitutes IEX volume for consolidated volume. If access fails, no entry is submitted.

On a normal day, the first check is **10:15:10 a.m. New York time**: the 9:30–10:00 candle has become available. Later checks occur at :15:10 and :45:10, until the entry cutoff 30 minutes before close. This intentionally gives up entries at the opening bell. Only completed bars are included; hourly bars align to the session's 9:30 open. Daily and weekly charts exclude their current, unfinished periods. Missing/stale data blocks new entries.

**Execution:** free real-time IEX last trades are used only as a pre-order reference with a 120-second freshness guard. IEX covers one exchange; the actual paper fill is determined by Alpaca. Market fills may differ from this reference. Exit comparisons use Alpaca's position valuation and average cost.

**AI:** Cloudflare documents a shared free allocation of 10,000 Neurons/day, reset at 00:00 UTC. This app estimates usage from reported input tokens and published model rates; default local caps are 9,000 estimated Neurons and 100 uncached calls/day. Estimates may differ from Cloudflare's accounting and do not include other apps using your account. Free quota is finite: a large watchlist may not get every planned evaluation. Quota/API failures are recorded and block that entry analysis; mechanical exits do not require AI and remain active.

Both sibling projects use the same shared cache at `../.clef-shared-cache` by default. Identical model inputs reuse one response, improving A/B consistency and reducing duplicate quota usage. If you put the projects in different parent folders, set the same absolute `CLEF_CACHE_DIR` in both `.env` files. Cached results contain your annotations and market context locally; protect that folder. Keep `model` and analysis settings identical in the two projects. You can explicitly choose `clef-flash` in both configs to reduce compute cost; the tool never switches models automatically.

Sources: [Alpaca data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq), [bars reference](https://docs.alpaca.markets/us/reference/stockbars), [Clef API and schemas](https://developers.cloudflare.com/workers-ai/models/clef/), [Cloudflare free allocation and model rates](https://developers.cloudflare.com/workers-ai/platform/pricing/).

## Install

Python 3.11+ on macOS/Linux; Pillow is the only third-party runtime dependency. From this repository:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
```

Edit `.env` locally:

```dotenv
APCA_API_KEY_ID=your-paper-account-key
APCA_API_SECRET_KEY=your-paper-account-secret
CLOUDFLARE_ACCOUNT_ID=your-cloudflare-account-id
CLOUDFLARE_API_TOKEN=your-workers-ai-token
```

Use a Cloudflare API token permitted to run Workers AI in the selected account. Use a distinct Alpaca paper account for each project. The broker hostname is hardcoded to `paper-api.alpaca.markets`; there is no live option. Environment variables override `.env` values, so avoid exporting one project's Alpaca keys globally into both terminal sessions. Secrets, ledgers, charts, and logs are excluded from Git.

Verify connectivity, delayed consolidated data, chart-image input, and the actual Clef response schema:

```sh
python -m clef_trader doctor
```

`doctor` performs read requests and one real model evaluation, **never a broker order**. It also binds the local ledger to the chosen account and checks that the A/B account IDs differ. A free Cloudflare account is intended; actual model authorization and quota availability are verified by this command. If the account requires a plan change or cannot access Clef, entries remain blocked rather than substituting an unrelated model.

## Watchlist and run

Create `my-watchlist.json`:

```json
{
  "week": "2026-10-05",
  "tickers": [
    {"symbol": "AAPL", "annotation": "Watching the daily consolidation. Enter only after a convincing resistance breakout with volume; avoid chasing."},
    {"symbol": "MSFT", "annotation": "Looking for a pullback to support, a higher low, and a reclaim on the 30-minute chart."}
  ]
}
```

Annotations can be vague; the model is allowed to wait or skip. These examples demonstrate syntax and are not an actual trading watchlist. Text format also works, including multiline notes:

```text
Week: 2026-10-05
AAPL: Watching the daily consolidation breakout; need volume confirmation.
MSFT: Pullback to support, then a 30-minute reclaim.
```

You can also paste your existing format into a `.txt` file without reformatting:

```text
October 5th 2026 Weekly Watchlist
$AAPL Daily flag near resistance. Wait for volume confirmation. Grade A-
$MSFT Holding weekly support, looking for a reclaim. B+
With market momentum improving, watch the $QQQ support reclaim.
```

The parser supports grades before or after the annotation and preserves author-supplied `RS 98`-style ratings separately from calculated ticker/QQQ relative strength. A heading without a year uses the import/preview date's year; use an explicit year for archives. Dollar price levels such as `$88` are not ticker symbols. Pasted emoji image links become their emoji labels. The concluding market paragraph is shared context for every ticker. A `$QQQ` reference inside that paragraph is not added as a trade target. Grades are the author's opinion and do not automatically change allocation; the chart decision still has to qualify. Symbol spelling and specified price levels are preserved exactly; unavailable symbols are rejected by the broker asset check rather than silently corrected.

Preview any list, including an archived one, without activating it:

```sh
python -m clef_trader preview-watchlist examples/pasted-format.txt
```

An old heading such as `June 15th 2026` can be previewed but cannot be imported for October trading. Your supplied historical sample is stored privately in the ignored `state/` folder and is not an active watchlist. For a real new week, provide its actual dated heading and annotations. JSON can also include a top-level `market_context` and per-ticker `grade` fields.

Import the **same file** into both project folders before starting them:

```sh
python -m clef_trader watchlist /absolute/path/to/my-watchlist.json
python -m clef_trader run
```

Start each project in its own terminal with its own virtual environment and `.env`. Each scheduler has separate positions, order ledger, and account snapshots. A completed week's watchlist never rolls forward automatically. Import a new list each Sunday. Once a week has been analyzed, its saved list is immutable to preserve the experiment's audit trail; future lists can be imported while running. Stop the scheduler before changing a current-week list that has not been analyzed yet.

On macOS, prevent idle sleep while running:

```sh
caffeinate -i python -m clef_trader run
```

Keep the computer powered on, connected, and logged in. Start the processes before Monday, October 5's market session after validating keys and importing your actual list. Nothing is automatically started by installation.

## Inspect and compare

```sh
python -m clef_trader status
python -m clef_trader compare ../algo-trading-clef-fixedentry
python -m clef_trader pause
python -m clef_trader resume
python -m clef_trader once
python -m clef_trader demo
```

- `status`: watchlists, positions managed by the ledger, orders, decisions, logs, snapshots, and estimated shared AI usage.
- `compare`: account equity changes, exposure, filled entries/exits, and managed holdings. Compare equal starting balances and matching decision cache keys; account equity also reflects any outside activity. A/B results have different exposure and may have different fills, so profit differences alone do not prove a sizing policy is better.
- `pause`/`resume`: pause or resume future entries; exit monitoring continues. Already accepted broker orders are not canceled by pausing.
- `once`: one time-gated scheduling cycle, not a force-buy command.
- `demo`: synthetic chart-rendering and sizing demonstration with no market request, no model call, and no order.

Every analysis saves four PNG charts plus exact input context, typed questions, raw Clef probabilities/confidence, and a shared cache hash under `artifacts/`. Model-selected evidence labels are audit categories; Clef does not generate a prose explanation. The allocation logged for `wait`/`skip` is hypothetical and does not mean an order was placed.

## Historical A/B backtest

Run the historical replay from **either** repository. One command simulates both sizing policies with separate $100,000 portfolios (or `capital_limit` if changed), the same Clef decisions, and the same execution assumptions. It reads historical data and calls Clef; it never submits broker orders, imports an active watchlist, or opens/changes the paper-trading ledger. Only one Alpaca data key pair is needed for this replay.

For the privately saved June 15 example, from the project directory:

```sh
.venv/bin/python -m clef_trader backtest state/historical-watchlist-2026-06-15.txt --start 2026-06-15 --end 2026-06-26
```

The private file exists in the local projects created in this session, not in GitHub. After a fresh clone, save your own archived watchlist file and substitute its path. Configure the Alpaca and Cloudflare keys in `.env` first. There are no real June performance results included in the repository; tests use synthetic market data and a simulated model.

By default, the June 15 list is eligible for new entries **only in its original week**. Positions remain monitored through June 26, including technical stops and a daily loss check five minutes before close. Alpaca's historical calendar determines holidays/early closes. End-of-period winners stay open and are marked at the final close. `--repeat-watchlist` is an explicit alternative for a different experiment; omit it for this June test.

`artifacts/backtests/<run-id>/report.html` shows both portfolios' equity, return, realized/unrealized P/L, entry/exit counts, sampled maximum drawdown, equity curves, trades, remaining holdings, and data coverage. `report.json` holds detailed results and the model decisions; each analysis also saves the historical charts and input context. Excluded/unavailable symbols retain their original spelling and appear in the report. Other missing analysis/execution samples are reported as coverage gaps; a missing candle for a held position pauses the replay rather than inventing a valuation.

The chart builder exposes only completed candles available at the simulated timestamp, reproducing the configured data delay. Future candles are excluded even though the archive is prefetched. A past IEX trade reproduces the entry-price guard. Entries use the first valid historical SIP ask within ten seconds after the decision; daily loss exits compare a recent past SIP trade with average entry price and fill at a historical bid. Both sides add 10 basis points of adverse slippage by default. Use `--slippage-bps 0` or another value from 0–100 for a sensitivity test. Slippage or settings changes create a separate run ID.

**This is an approximate historical simulation.** Technical stops use completed 30-minute OHLC bars wholly after entry, with a worse opening price on gaps. The first partial candle after entry is excluded because its low may precede the fill; this can miss a real stop and overstate returns. Stops are recorded at bar end, not at the true trigger time. Daily-red exits are sampled once before close, rather than reproducing every live polling cycle. The replay assumes immediate full fractional fills; it does not model order latency, partial fills, fees, or the broker's internal position valuation.

Historical execution prices remain raw. Chart OHLC and volume are adjusted using only stock splits effective by the simulated timestamp, so a past reverse split does not create a false breakout and later splits do not enter earlier inputs. Alpaca corporate actions are checked before running; a non-cash corporate action within the evaluation period refuses the whole range until explicit share/action modeling is added. Dividend cash distributions are disclosed but not modeled. Corporate-action records are included in the report. No current asset listing is used as a substitute for historical tradability. A revised replay version starts a separate checkpoint while still reusing identical Clef inputs from the shared cache.

Clef was [released October 1, 2026](https://blog.cloudflare.com/clef-decision-models/), after the June sample. Although the supplied charts contain no future candles, the current model's training knowledge may include later outcomes. This is retrospective analysis, not an unbiased test of what a June-deployed model would have done. Forward paper results remain necessary to evaluate the experiment.

Historical consolidated bars, trades, and quotes use Alpaca's [historical data access](https://docs.alpaca.markets/us/docs/market-data-faq); June data is older than the Basic plan's 15-minute restriction. Actual entitlement is checked by the requests; there is no fallback to fabricated data. Archive files are cached locally in `../.clef-backtest-data` and Clef uses the existing shared AI cache. Backtests count against the same real-day Cloudflare allowance as the paper schedulers. Many waiting tickers can require hundreds of evaluations; the free AI allowance may stretch a replay across multiple days. Quota exhaustion saves a partial report and checkpoint. Rerun **the identical command** after the quota resets to resume without duplicating decisions or entries. Partial reports are visibly labeled. API failures pause the replay, preserving its checkpoint for retry.

## Order and stop limits

The $100,000 cap is per strategy, also limited by actual available cash/buying power; the app never uses margin to spend more than cash. Existing positions and pending buys count toward the capital cap. A/B balances must be configured in Alpaca; this app does not fund/reset accounts. Only active fractional US equities are accepted for dollar-sized entries.

Orders use deterministic client IDs, persisted before submission. Network failures reconcile with the broker before retries. Partial fills are recorded, never automatically topped up. If a partial entry needs to exit, the remaining entry order is canceled and the app waits for broker confirmation before selling filled shares. Unresolved order intents remain visible and reserve capital; do not delete the ledger to resolve them. If an order status is uncertain, check the same client ID in Alpaca and reconcile it before manual intervention.

The protective stop is a **local software stop**, checked normally every 15 seconds and after chart evaluations. Network/model calls can delay a cycle. It is not a persistent broker-native stop and is inactive while the process or computer is off. No overnight/extended-hours sales are submitted. Market gaps can fill below the stop. A missed daily close check is not retrospectively executed next morning unless the technical stop is also breached; the next daily loss check occurs before that day's close.

The app sells only quantities attributable to this project's filled orders. Unrelated holdings are left alone. Manual trades, stock splits, or other corporate actions that create a quantity mismatch block automatic sales for that symbol; inspect logs and broker records. Avoid outside trading in these dedicated paper portfolios. Rejected/canceled/expired orders are logged and not automatically recreated under a new client ID. Keep your ledger and shared cache; back up SQLite files with the schedulers stopped.

Sizing in this project uses Clef's **`enter` option probability** as its conviction measure: 0.70–0.79 → $2,500; 0.80–0.89 → $5,000; 0.90–0.949 → $7,500; ≥0.95 → $10,000. Model-reported distribution confidence is an additional entry gate. These numbers are experimental classifier outputs, **not calibrated probabilities of investment profit**. The fixed project always requests $5,000 for the same qualified decision.

## Tests

```sh
python -m unittest discover -s tests -v
```

Tests simulate model and broker responses, including accepted orders with lost responses, ownership protection, sizing, cash/exposure limits, delayed-data scheduling, early closes, daily-red exits, and quota handling. Real market/model connectivity and paper fills require your keys and a successful `doctor` run. No broker order is submitted by tests or the demo.
