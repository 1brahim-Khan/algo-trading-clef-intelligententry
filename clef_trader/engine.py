import datetime as dt
from decimal import Decimal
import hashlib
import json
import logging
import math
from pathlib import Path

from .api import APIError
from .config import allocation
from .decision import make_request
from .market import ET, Market, monday, session_time, stop_candidates, timestamp
from .store import BusyLockError

LOG = logging.getLogger('clef_trader')


def entry_slot(now, session, delay_minutes):
    opening = session_time(session, 'open')
    first = opening + dt.timedelta(minutes=30 + delay_minutes, seconds=10)
    if now < first:
        return None
    slot = first + dt.timedelta(minutes=30 * int((now - first).total_seconds() // 1800))
    return slot if now - slot <= dt.timedelta(minutes=3) else None


def client_id(strategy, week, symbol, side, day=None):
    identity = '|'.join([strategy, week, symbol, side, day or ''])
    return 'clef-' + hashlib.sha256(identity.encode()).hexdigest()[:40]


class Engine:
    def __init__(self, api, store, clef, cfg, root):
        self.api, self.store, self.clef, self.cfg = api, store, clef, cfg
        self.root = Path(root)
        self.market = Market(api, cfg)
        self.session_cache = {}
        self.last_snapshot = None

    def log(self, level, message):
        getattr(LOG, level.lower())(message)
        self.store.event(level, message)

    def initialize(self):
        account = self.api.account()
        self.store.bind(account['id'], self.cfg['strategy'])
        self.clef.bind_account(self.cfg['strategy'], account['id'])
        if account.get('status') != 'ACTIVE':
            raise RuntimeError('Paper account is not ACTIVE.')
        return account

    def session(self, now):
        day = now.date().isoformat()
        if day not in self.session_cache:
            sessions = self.api.calendar(day, day)
            self.session_cache[day] = sessions[0] if sessions else None
        return self.session_cache[day]

    def clock(self):
        value = self.api.clock()
        return timestamp(value['timestamp']).astimezone(ET), value['is_open']

    def reconcile(self):
        for order in self.store.pending():
            remote = self.api.find_order(order['client_id'])
            if remote:
                self.store.update_order(order['client_id'], remote)

    def account_ready(self, account):
        return account.get('status') == 'ACTIVE' and not account.get('trading_blocked') and not account.get('account_blocked')

    def exposure(self, positions, open_orders, exclude=None):
        total = sum(abs(Decimal(str(p['market_value']))) for p in positions.values())
        seen = set()
        for order in open_orders:
            cid = order.get('client_order_id')
            seen.add(cid)
            if order['side'] == 'buy' and cid != exclude:
                # Notional orders reserve the full requested amount conservatively.
                if order.get('notional'):
                    total += Decimal(order['notional'])
                else:
                    raise RuntimeError('External open quantity buy; entry paused until resolved.')
        for order in self.store.pending():
            if order['side'] == 'buy' and order['client_id'] not in seen and order['client_id'] != exclude:
                total += Decimal(json.loads(order['payload'])['notional'])
        return total

    def allowed_buy(self, row):
        now, is_open = self.clock()
        session = self.session(now)
        meta = json.loads(row['reason'])
        origin = timestamp(meta['slot'])
        if not is_open or not session or now < origin or now - origin > dt.timedelta(minutes=3):
            return False
        if now >= session_time(session, 'close') - dt.timedelta(minutes=self.cfg['entry_cutoff_minutes']):
            return False
        account = self.api.account()
        if not self.account_ready(account):
            return False
        positions = {p['symbol']: p for p in self.api.positions()}
        if row['symbol'] in positions or row['symbol'] in self.store.owned():
            return False
        orders = self.api.open_orders()
        if any(o['symbol'] == row['symbol'] for o in orders):
            return False
        amount = Decimal(json.loads(row['payload'])['notional'])
        if min(Decimal(account['cash']), Decimal(account['buying_power'])) < amount:
            return False
        if self.exposure(positions, orders, row['client_id']) + amount > Decimal(str(self.cfg['capital_limit'])):
            return False
        trade = self.api.latest_trade(row['symbol'])
        age = (now - timestamp(trade['t'])).total_seconds()
        price = float(trade['p'])
        if not math.isfinite(price) or price <= 0 or not -5 <= age <= 120:
            return False
        stop = float(row['stop_price'])
        reference = meta['reference_price']
        if abs(price / reference - 1) * 100 > self.cfg['max_entry_deviation_pct']:
            return False
        if not 0 < stop < price or (1 - stop / price) * 100 > self.cfg['max_stop_distance_pct']:
            return False
        return True

    def transmit(self, row):
        cid = row['client_id']
        found = self.api.find_order(cid)
        if found:
            self.store.update_order(cid, found)
            return
        if row['side'] == 'buy':
            if not self.allowed_buy(row):
                return
        else:
            now, is_open = self.clock()
            if not is_open or not self.account_ready(self.api.account()):
                return
            positions = {p['symbol']: p for p in self.api.positions()}
            position = positions.get(row['symbol'])
            qty = Decimal(json.loads(row['payload'])['qty'])
            if not position or Decimal(position['qty']) != qty or self.store.owned().get(row['symbol']) != qty:
                return
            if any(o['symbol'] == row['symbol'] for o in self.api.open_orders()):
                return
        try:
            result = self.api.submit(json.loads(row['payload']))
        except APIError as error:
            if error.status in {400, 403, 422}:
                found = self.api.find_order(cid)
                if found:
                    self.store.update_order(cid, found)
                    return
                self.store.write('UPDATE orders SET status=?,detail=? WHERE client_id=?', ('failed', str(error), cid))
            # Timeout, 5xx and 429 retain the intent. Reconcile before any retry.
            raise
        self.store.update_order(cid, result)
        self.log('INFO', f"{row['side'].upper()} {row['symbol']}: broker status {result['status']}")

    def resume(self, entries=True):
        for row in self.store.rows('SELECT * FROM orders WHERE status=?', ('intent',)):
            if row['side'] == 'buy':
                now, _ = self.clock()
                origin = timestamp(json.loads(row['reason'])['slot'])
                if now - origin > dt.timedelta(minutes=3):
                    # Preserve ambiguous submissions for future reconciliation. A 404
                    # must not discard an order that might have been accepted.
                    self.store.write('UPDATE orders SET status=? WHERE client_id=?', ('uncertain', row['client_id']))
                    continue
                if not entries:
                    continue
            self.transmit(row)

    def order(self, week, symbol, side, amount, now, reason, stop=None, decision_id=None):
        cid = client_id(self.cfg['strategy'], week, symbol, side, now.date().isoformat() if side == 'sell' else None)
        if self.store.rows('SELECT 1 FROM orders WHERE client_id=?', (cid,)):
            return
        payload = {'symbol': symbol, 'side': side, 'type': 'market', 'time_in_force': 'day',
                   'extended_hours': False, 'client_order_id': cid,
                   'notional' if side == 'buy' else 'qty': str(amount)}
        if side == 'buy' and not self.allowed_buy({'client_id': cid, 'symbol': symbol,
                'payload': json.dumps(payload), 'reason': json.dumps(reason), 'stop_price': str(stop)}):
            self.log('WARNING', f'{symbol}: entry execution guard blocked this decision; reconsider next interval.')
            return
        self.store.write('INSERT INTO orders(client_id,week,symbol,side,payload,status,stop_price,reason,decision_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                         (cid, week, symbol, side, json.dumps(payload), 'intent', str(stop) if stop else None,
                          json.dumps(reason), decision_id, now.isoformat()))
        row = self.store.rows('SELECT * FROM orders WHERE client_id=?', (cid,))[0]
        self.transmit(row)

    def exits(self):
        now, is_open = self.clock()
        session = self.session(now)
        if not is_open or not session:
            return
        near_close = now >= session_time(session, 'close') - dt.timedelta(minutes=self.cfg['close_exit_minutes'])
        positions = {p['symbol']: p for p in self.api.positions()}
        orders = self.api.open_orders()
        for symbol, qty in self.store.owned().items():
            position = positions.get(symbol)
            if not position or Decimal(position['qty']) != qty or qty <= 0:
                # Avoid repeated event spam; the mismatch remains visible in status.
                LOG.error('Managed quantity mismatch for %s; automatic sale blocked.', symbol)
                continue
            price = Decimal(position['current_price'])
            basis = Decimal(position['avg_entry_price'])
            stop = self.store.stop(symbol)
            reason = 'technical_stop' if stop and price <= stop else 'daily_red' if near_close and price < basis else None
            if reason:
                pending = [o for o in orders if o['symbol'] == symbol]
                if pending:
                    for order in pending:
                        if order['side'] == 'buy' and self.store.rows('SELECT 1 FROM orders WHERE client_id=?', (order.get('client_order_id'),)):
                            self.api.cancel(order['id'])
                            self.log('WARNING', f'{symbol}: canceling remainder of partial entry before exit; awaiting broker confirmation.')
                    continue
                self.order(monday(now.date()).isoformat(), symbol, 'sell', qty, now,
                           {'rule': reason, 'price': str(price), 'entry_price': str(basis), 'stop': str(stop)})

    def record_decision(self, week, symbol, slot, result, amount, detail):
        return self.store.write('INSERT INTO decisions(week,symbol,slot,action,probability,confidence,stop_price,allocation,detail) VALUES(?,?,?,?,?,?,?,?,?)',
                                (week, symbol, slot.isoformat(), result['action'], result['probability'],
                                 result['confidence'], result['stop_price'], amount, json.dumps(detail)))

    def analyze(self, week, item, slot):
        symbol = item['symbol']
        bundle, cutoff = self.market.bundle(symbol, slot)
        stops = stop_candidates(bundle)
        if not stops:
            raise RuntimeError('No valid numerical stop candidates.')
        payload, images = make_request(symbol, item['annotation'], bundle, cutoff, stops, self.cfg['model'])
        result, key = self.clef.evaluate(payload, stops)
        amount = allocation(self.cfg, result['probability'])
        artifact_dir = self.root / 'artifacts' / week / symbol / slot.strftime('%Y%m%d-%H%M')
        artifact_dir.mkdir(parents=True, exist_ok=True)
        for frame, png in images.items():
            (artifact_dir / (frame + '.png')).write_bytes(png)
        # Persist exact input/state and raw model output for audit/replay without credentials.
        (artifact_dir / 'analysis.json').write_text(json.dumps({'state': payload['state'], 'questions': payload['questions'], 'response': result['raw'], 'cache_key': key}, indent=2))
        detail = {'cache_key': key, 'evidence': result['evidence'], 'stop_choice': result['stop_choice'],
                  'cutoff': cutoff.isoformat(), 'artifact_dir': str(artifact_dir), 'model_response': result['raw']}
        ident = self.record_decision(week, symbol, slot, result, amount, detail)
        self.log('INFO', f"{symbol} {result['action']} p(enter)={result['probability']:.3f}, model confidence={result['confidence']:.3f}, allocation=${amount:,.0f}")
        if result['action'] != 'enter' or result['probability'] < self.cfg['entry_probability'] or result['confidence'] < self.cfg['entry_confidence'] or not result['stop_price'] or not amount:
            return
        now, is_open = self.clock()
        if not is_open:
            return
        self.order(week, symbol, 'buy', Decimal(str(amount)), now,
                   {'slot': slot.isoformat(), 'reference_price': bundle['30Min'][-1]['c'], 'cache_key': key},
                   result['stop_price'], ident)

    def snapshot(self, now):
        if self.last_snapshot and now - self.last_snapshot < dt.timedelta(minutes=30):
            return
        account = self.api.account()
        positions = self.api.positions()
        self.store.write('INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?)',
                         (now.isoformat(), float(account['equity']), float(account['cash']),
                          sum(abs(float(p['market_value'])) for p in positions)))
        self.last_snapshot = now

    def tick(self, entries=True):
        self.reconcile()
        self.exits()
        self.resume(entries)
        now, is_open = self.clock()
        session = self.session(now)
        if not is_open or not session:
            return
        self.snapshot(now)
        if not entries:
            return
        if now >= session_time(session, 'close') - dt.timedelta(minutes=self.cfg['entry_cutoff_minutes']):
            return
        slot = entry_slot(now, session, self.cfg['data_delay_minutes'])
        if not slot:
            return
        week = monday(now.date()).isoformat()
        lists = self.store.rows('SELECT items FROM watchlists WHERE week=?', (week,))
        if not lists:
            return
        for item in json.loads(lists[0]['items']):
            current, still_open = self.clock()
            if not still_open or current - slot > dt.timedelta(minutes=3):
                break
            symbol = item['symbol']
            if self.store.rows('SELECT 1 FROM decisions WHERE week=? AND symbol=? AND slot=?', (week, symbol, slot.isoformat())):
                continue
            if self.store.rows('SELECT 1 FROM decisions WHERE week=? AND symbol=? AND action=?', (week, symbol, 'skip')):
                continue
            if self.store.rows('SELECT 1 FROM orders WHERE week=? AND symbol=? AND side=?', (week, symbol, 'buy')):
                continue
            if symbol in self.store.owned() or any(p['symbol'] == symbol for p in self.api.positions()):
                continue
            if any(o['symbol'] == symbol for o in self.api.open_orders()):
                continue
            try:
                asset = self.api.asset(symbol)
                if asset.get('class') != 'us_equity' or asset.get('status') != 'active' or not asset.get('tradable') or not asset.get('fractionable'):
                    raise RuntimeError('Not an active, tradable fractional US equity.')
                self.analyze(week, item, slot)
            except BusyLockError:
                LOG.info('%s: shared Clef evaluation in progress; retrying this slot on the next poll.', symbol)
            except Exception as error:
                self.log('ERROR', f'{symbol}: analysis/order failed: {type(error).__name__}: {error}')
                if not self.store.rows('SELECT 1 FROM decisions WHERE week=? AND symbol=? AND slot=?', (week, symbol, slot.isoformat())):
                    self.record_decision(week, symbol, slot, {'action': 'error', 'probability': 0, 'confidence': 0, 'stop_price': None}, 0, {'error': str(error)})
            # Exit checks have priority even when analysis takes longer than a poll.
            self.reconcile()
            self.exits()
