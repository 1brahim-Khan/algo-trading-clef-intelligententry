import contextlib
import datetime as dt
from decimal import Decimal
import fcntl
import json
import sqlite3
import time

TERMINAL = ('filled', 'canceled', 'expired', 'rejected', 'replaced', 'failed', 'abandoned')


class BusyLockError(RuntimeError):
    pass


@contextlib.contextmanager
def exclusive(path, wait_seconds=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise BusyLockError(f'Another process holds {path.name}.') from None
                time.sleep(.1)
        yield


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=20)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
          CREATE TABLE IF NOT EXISTS watchlists (week TEXT PRIMARY KEY, items TEXT);
          CREATE TABLE IF NOT EXISTS orders (
            client_id TEXT PRIMARY KEY, week TEXT, symbol TEXT, side TEXT,
            payload TEXT, status TEXT, filled_qty TEXT DEFAULT '0',
            filled_avg_price TEXT DEFAULT '0', stop_price TEXT, reason TEXT,
            decision_id INTEGER, created_at TEXT, detail TEXT);
          CREATE TABLE IF NOT EXISTS decisions (
            id INTEGER PRIMARY KEY, week TEXT, symbol TEXT, slot TEXT,
            action TEXT, probability REAL, confidence REAL, stop_price REAL,
            allocation REAL, detail TEXT, UNIQUE(week,symbol,slot));
          CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, at TEXT, level TEXT, message TEXT);
          CREATE TABLE IF NOT EXISTS snapshots (
            at TEXT PRIMARY KEY, equity REAL, cash REAL, exposure REAL);
        ''')

    def rows(self, sql, args=()):
        return self.db.execute(sql, args).fetchall()

    def write(self, sql, args=()):
        with self.db:
            return self.db.execute(sql, args).lastrowid

    def bind(self, account_id, strategy):
        for key, value in [('account_id', account_id), ('strategy', strategy)]:
            old = self.rows('SELECT value FROM metadata WHERE key=?', (key,))
            if old and old[0]['value'] != value:
                raise RuntimeError(f'Ledger {key} mismatch; use the original account and strategy.')
            self.write('INSERT OR IGNORE INTO metadata VALUES (?,?)', (key, value))

    def event(self, level, message):
        self.write('INSERT INTO events(at,level,message) VALUES(?,?,?)',
                   (dt.datetime.now(dt.timezone.utc).isoformat(), level, message))

    def owned(self):
        qty = {}
        for row in self.rows('SELECT symbol,side,filled_qty FROM orders'):
            qty[row['symbol']] = qty.get(row['symbol'], Decimal(0)) + Decimal(row['filled_qty']) * (1 if row['side'] == 'buy' else -1)
        return {symbol: q for symbol, q in qty.items() if q > 0}

    def update_order(self, cid, order):
        self.write('UPDATE orders SET status=?,filled_qty=?,filled_avg_price=?,detail=? WHERE client_id=?',
                   (order['status'], str(order.get('filled_qty') or '0'),
                    str(order.get('filled_avg_price') or '0'), json.dumps(order), cid))

    def pending(self):
        placeholders = ','.join('?' for _ in TERMINAL)
        return self.rows(f'SELECT * FROM orders WHERE status NOT IN ({placeholders})', TERMINAL)

    def stop(self, symbol):
        rows = self.rows('SELECT stop_price FROM orders WHERE symbol=? AND side=? AND CAST(filled_qty AS REAL)>0 ORDER BY created_at DESC LIMIT 1', (symbol, 'buy'))
        return Decimal(rows[0]['stop_price']) if rows and rows[0]['stop_price'] else None

    def summary(self):
        return {name: [dict(r) for r in self.rows(query)] for name, query in {
            'watchlists': 'SELECT * FROM watchlists ORDER BY week DESC',
            'orders': 'SELECT * FROM orders ORDER BY created_at DESC',
            'decisions': 'SELECT * FROM decisions ORDER BY id DESC LIMIT 100',
            'events': 'SELECT * FROM events ORDER BY id DESC LIMIT 30',
            'snapshots': 'SELECT * FROM snapshots ORDER BY at DESC LIMIT 100'
        }.items()} | {'owned': {s: str(q) for s, q in self.owned().items()}}
