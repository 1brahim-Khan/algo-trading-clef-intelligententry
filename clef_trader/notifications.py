"""Independent paper-account fill alerts and nightly reports; no AI or order submissions."""
import argparse
import datetime as dt
import json
import logging
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .api import APIError, request_json
from .store import exclusive

ET = ZoneInfo('America/New_York')
UTC = dt.timezone.utc
TRANSFER_TYPES = {'CSD', 'CSW', 'ACATC', 'JNLC'}
SECURITY_TRANSFERS = {'ACATS', 'JNLS'}


def env_file(path):
    """Read each project's keys independently; never reuse the other account's env."""
    result = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            key, sep, value = line.partition('=')
            if not sep or not key.strip().isidentifier():
                raise ValueError('Invalid environment file entry')
            result[key.strip()] = value.strip().strip('\"\'')
    return result


class PaperReader:
    def __init__(self, root):
        env = env_file(root / '.env')
        self.headers = {'APCA-API-KEY-ID': env['APCA_API_KEY_ID'],
                        'APCA-API-SECRET-KEY': env['APCA_API_SECRET_KEY']}
        if not all(self.headers.values()):
            raise ValueError('Missing paper credentials')

    def get(self, path, **params):
        suffix = '?' + urlencode(params) if params else ''
        return request_json('https://paper-api.alpaca.markets/v2' + path + suffix,
                            self.headers, timeout=15)

    def activities(self, after, kinds):
        result, token = [], None
        for _ in range(100):
            params = dict(after=after, activity_types=','.join(kinds), direction='asc', page_size=100)
            if token:
                params['page_token'] = token
            page = self.get('/account/activities', **params)
            result.extend(page)
            if len(page) < 100:
                return result
            next_token = page[-1]['id']
            if next_token == token:
                raise RuntimeError('Activity pagination did not advance')
            token = next_token
        raise RuntimeError('Activity history too large; report unavailable')


def ledger(root):
    path = root / 'state' / 'paper.sqlite3'
    if not path.exists():
        return {}, {}, None
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        meta = dict(db.execute('SELECT key,value FROM metadata'))
        orders = {}
        for row in db.execute('SELECT * FROM orders'):
            detail = json.loads(row['detail'] or '{}')
            if detail.get('id'):
                orders[detail['id']] = dict(row)
        first = db.execute('SELECT * FROM snapshots ORDER BY at LIMIT 1').fetchone()
        return meta, orders, dict(first) if first else None
    finally:
        db.close()


def money(value):
    return f'${float(value):,.2f}'


def pnl_text(equity, baseline, activities, after):
    if baseline is None or baseline <= 0:
        return 'unavailable (no valid baseline)'
    flows = 0.0
    for item in activities:
        stamp = item.get('transaction_time') or item.get('date', '')
        if not stamp:
            return 'unavailable (undated account activity)'
        # Cash activity dates without times apply to the whole settlement day.
        moment = dt.datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=ET)
        if 'T' not in stamp and moment.date() == after.astimezone(ET).date():
            return 'unavailable (cash activity time overlaps baseline)'
        if moment <= after:
            continue
        if item['activity_type'] in SECURITY_TRANSFERS:
            return 'unavailable (external securities transfer)'
        if item['activity_type'] in TRANSFER_TYPES:
            flows += float(item['net_amount'])
    profit = equity - baseline - flows
    return f'{money(profit)} ({profit / baseline:+.2%})'


def portfolio_report(root, reader, now):
    account = reader.get('/account')
    meta, orders, first = ledger(root)
    if meta.get('account_id') and meta['account_id'] != account['id']:
        raise RuntimeError('Account differs from strategy ledger')
    positions = reader.get('/positions')
    equity = float(account['equity'])
    monday = now.date() - dt.timedelta(days=now.weekday())
    begin = min(monday - dt.timedelta(days=10),
                dt.datetime.fromisoformat(first['at']).date() if first else monday)
    history = reader.get('/account/portfolio/history', start=dt.datetime.combine(begin, dt.time(), ET).isoformat(), timeframe='1D')
    sessions = reader.get('/calendar', start=(now.date() - dt.timedelta(days=14)).isoformat(),
                          end=now.date().isoformat())
    previous = [s for s in sessions if s['date'] < now.date().isoformat()]
    daily_after = dt.datetime.fromisoformat(previous[-1]['date'] + 'T' + previous[-1]['close']).replace(tzinfo=ET) if previous else None
    points = [(dt.datetime.fromtimestamp(t, ET), float(e))
              for t, e in zip(history['timestamp'], history['equity']) if e and e > 0]
    before_week = [p for p in points if p[0].date() < monday]
    weekly = before_week[-1] if before_week else None
    # Daily history timestamps are midnight UTC; use the corresponding session's close.
    if weekly:
        closing = next((s['close'] for s in sessions if s['date'] == weekly[0].date().isoformat()), None)
        weekly = (dt.datetime.combine(weekly[0].date(), dt.time.fromisoformat(closing), ET), weekly[1]) if closing else None
    activities = reader.activities(begin.isoformat(), sorted(TRANSFER_TYPES | SECURITY_TRANSFERS))
    prior_day = [p for p in points if daily_after and p[0].date() == daily_after.date()]
    daily_base = prior_day[-1][1] if prior_day else float(account['last_equity'])
    daily = pnl_text(equity, daily_base, activities, daily_after) if daily_after else 'unavailable'
    weekly_pnl = pnl_text(equity, weekly[1], activities, weekly[0]) if weekly else 'unavailable (no prior-week baseline)'
    total = pnl_text(equity, first['equity'], activities, dt.datetime.fromisoformat(first['at'])) if first else 'unavailable (trader has not started)'
    exposure = sum(abs(float(p['market_value'])) for p in positions)
    lines = [f"{root.name.removeprefix('algo-trading-clef-')} — PAPER",
             f'Daily P/L: {daily}', f'Weekly P/L: {weekly_pnl}',
             f'Total P/L since first strategy snapshot: {total}',
             f"Equity: {money(equity)} | Cash: {money(account['cash'])}",
             f"Buying power: {money(account['buying_power'])} (strategy uses cash only)",
             f'Gross exposure: {money(exposure)} ({exposure / equity:.1%})' if equity else f'Gross exposure: {money(exposure)}',
             f"Open positions: {len(positions)} | Unrealized P/L: {money(sum(float(p['unrealized_pl']) for p in positions))}"]
    for p in sorted(positions, key=lambda p: abs(float(p['market_value'])), reverse=True):
        lines.append(f"{p['symbol']}: {p['qty']} shares, value {money(p['market_value'])}, P/L {money(p['unrealized_pl'])} ({float(p['unrealized_plpc']):+.1%})")
    lines.append('Entries paused' if (root / 'state' / 'pause').exists() else 'Entries enabled')
    lines.append('Account blocked' if account.get('trading_blocked') or account.get('account_blocked') else 'Account trading status: active')
    return '\n'.join(lines), account['id']


class Reporter:
    def __init__(self, state, roots):
        state.mkdir(parents=True, exist_ok=True)
        self.state, self.roots = state, roots
        self.db = sqlite3.connect(state / 'notifications.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,body TEXT,sent INTEGER DEFAULT 0,
            attempts INTEGER DEFAULT 0,retry_at REAL DEFAULT 0);
        ''')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('activated', dt.datetime.now(UTC).isoformat()))
        self.activated = self.db.execute("SELECT value FROM meta WHERE key='activated'").fetchone()[0]

    def queue(self, key, text):
        # Small stable chunks also support Discord's 2,000-character limit.
        with self.db:
            for n, start in enumerate(range(0, len(text), 1800)):
                self.db.execute('INSERT OR IGNORE INTO outbox(id,body) VALUES (?,?)',
                                (f'{key}:{n}', text[start:start+1800]))

    def fills(self, root, reader):
        account = reader.get('/account')
        meta, orders, _ = ledger(root)
        if not meta.get('account_id'):
            return
        if meta['account_id'] != account['id']:
            raise RuntimeError('Account differs from strategy ledger')
        for fill in reader.activities(self.activated, ['FILL']):
            order = orders.get(fill['order_id'])
            if not order:
                continue
            text = (f"PAPER trade — {root.name.removeprefix('algo-trading-clef-')}\n"
                    f"{fill['side'].upper()} {fill['symbol']}: {fill['qty']} shares @ {money(fill['price'])}\n"
                    f"Value: {money(float(fill['qty']) * float(fill['price']))}\n"
                    f"Time: {fill['transaction_time']} | {fill['type']}\n"
                    f"Reason: {order['reason']}\nOrder: {order['client_id']}")
            self.queue(f"fill:{account['id']}:{fill['id']}", text)

    def report(self, now):
        pieces, account_ids = [], set()
        for root in self.roots:
            try:
                text, account_id = portfolio_report(root, PaperReader(root), now)
                if account_id in account_ids:
                    raise RuntimeError('Both strategies point to the same paper account')
                account_ids.add(account_id)
                pieces.append(text)
            except Exception as error:
                reason = f'HTTP {error.status}' if isinstance(error, APIError) else type(error).__name__
                pieces.append(f'{root.name}: unavailable ({reason}); values are not reported as zero.')
        return f'Portfolio update — {now:%Y-%m-%d %H:%M %Z}\n\n' + '\n\n'.join(pieces) + '\n\nAccount-wide P/L, adjusted for recorded cash transfers. Weekly: since prior week close. Total: since first strategy snapshot. Marks are as of report time, including any after-hours changes.'

    def tick(self, now):
        trading_day = False
        for root in self.roots:
            try:
                reader = PaperReader(root)
                self.fills(root, reader)
                if not trading_day:
                    trading_day = bool(reader.get('/calendar', start=now.date().isoformat(), end=now.date().isoformat()))
            except Exception as error:
                logging.warning('%s unavailable: %s', root.name,
                                f'HTTP {error.status}' if isinstance(error, APIError) else type(error).__name__)
        config = env_file(self.state / 'notifications.env')
        hour = int(config.get('REPORT_HOUR_ET', '17'))
        if not 0 <= hour <= 23:
            raise ValueError('REPORT_HOUR_ET must be 0–23')
        key = f'nightly:{now.date()}'
        if trading_day and now.hour >= hour and not self.db.execute('SELECT 1 FROM outbox WHERE id=?', (key + ':0',)).fetchone():
            body = self.report(now)
            (self.state / f'report-{now.date()}.txt').write_text(body + '\n')
            self.queue(key, body)
        self.deliver(config)
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('heartbeat', now.isoformat()))

    def deliver(self, config):
        if config.get('NOTIFICATION_PROVIDER', 'none') == 'none':
            return
        for row in self.db.execute('SELECT * FROM outbox WHERE sent=0 AND retry_at<=? ORDER BY rowid LIMIT 30', (time.time(),)).fetchall():
            try:
                send(config, row['body'])
            except Exception as error:
                delay = min(1800, 30 * 2 ** min(row['attempts'], 6))
                with self.db:
                    self.db.execute('UPDATE outbox SET attempts=attempts+1,retry_at=? WHERE id=?', (time.time()+delay, row['id']))
                logging.warning('Notification delivery failed: %s (retry queued)', type(error).__name__)
                break
            else:
                with self.db:
                    self.db.execute('UPDATE outbox SET sent=1 WHERE id=?', (row['id'],))


def send(config, text):
    provider = config['NOTIFICATION_PROVIDER']
    if provider == 'telegram':
        token, chat = config['TELEGRAM_BOT_TOKEN'], config['TELEGRAM_CHAT_ID']
        if not token or not chat or '/' in token or '?' in token:
            raise ValueError('Configure Telegram bot token and chat ID')
        result = request_json(f'https://api.telegram.org/bot{token}/sendMessage', {},
                              {'chat_id': chat, 'text': text}, timeout=15)
        if not result.get('ok'):
            raise RuntimeError('Telegram did not accept notification')
    else:
        raise ValueError('NOTIFICATION_PROVIDER must be none or telegram')


def setup_telegram(state):
    path = state / 'notifications.env'
    config = env_file(path)
    token = config.get('TELEGRAM_BOT_TOKEN', '')
    if not token or '/' in token or '?' in token:
        raise ValueError('First save TELEGRAM_BOT_TOKEN in notifications.env')
    result = request_json(f'https://api.telegram.org/bot{token}/getUpdates', {}, timeout=15)
    if not result.get('ok'):
        raise RuntimeError('Telegram could not retrieve chats')
    chats = {str(u['message']['chat']['id']) for u in result.get('result', [])
             if u.get('message', {}).get('chat', {}).get('type') == 'private'}
    if len(chats) != 1:
        raise ValueError('Send a private message to your bot first. If several chats exist, enter your own TELEGRAM_CHAT_ID manually.')
    chat = chats.pop()
    lines = path.read_text().splitlines()
    keys = {'NOTIFICATION_PROVIDER': 'telegram', 'TELEGRAM_CHAT_ID': chat}
    lines = [line for line in lines if line.partition('=')[0].strip() not in keys]
    path.write_text('\n'.join(lines + [f'{k}={v}' for k, v in keys.items()]) + '\n')
    path.chmod(0o600)
    send(env_file(path), 'Paper portfolio notifications connected. Trade fills and nightly reports will arrive here. No trade was placed by setup.')
    print('Telegram configured; test accepted. The running reporter reloads this file automatically.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--projects', nargs='+', type=Path, required=True)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--preview', action='store_true', help='Print a fresh report without sending')
    parser.add_argument('--setup-telegram', action='store_true', help='Discover the chat ID after you send your bot a private message')
    parser.add_argument('--test', action='store_true', help='Send a test notification without placing a trade')
    args = parser.parse_args()
    roots = [p.expanduser().resolve() for p in args.projects]
    if len(set(roots)) != len(roots):
        raise ValueError('Duplicate project')
    reporter = Reporter(args.state.expanduser().resolve(), roots)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if args.setup_telegram:
        setup_telegram(reporter.state)
        return
    if args.preview:
        print(reporter.report(dt.datetime.now(ET)))
        return
    if args.test:
        send(env_file(reporter.state / 'notifications.env'), 'Paper portfolio notifications connected. This is a delivery test; no trade was placed.')
        print('Test notification accepted by provider.')
        return
    with exclusive(reporter.state / 'notifications.lock'):
        while True:
            try:
                reporter.tick(dt.datetime.now(ET))
            except Exception as error:
                logging.error('Reporter cycle failed: %s', type(error).__name__)
            time.sleep(30)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as error:
        # HTTP errors can contain provider tokens in their URL; never print them.
        detail = f'HTTP {error.status}' if isinstance(error, APIError) else type(error).__name__
        raise SystemExit(f'Notifications failed: {detail}. Check local configuration and setup instructions.') from None
