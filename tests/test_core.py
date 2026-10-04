import datetime as dt
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from clef_trader.config import allocation, load_config
from clef_trader.decision import Clef, QuotaError, validate_response
from clef_trader.engine import Engine, entry_slot
from clef_trader.market import ET, Market, clean_bars, hourly, weekly
from clef_trader.store import BusyLockError, Store
from clef_trader.watchlist import parse_watchlist

ROOT = Path(__file__).resolve().parents[1]


def response(p=.92, confidence=.85, action='enter'):
    return {'model': 'clef', 'usage': {'input_tokens': 8000, 'output_tokens': 0}, 'answers': {
        'action': {'type': 'choice', 'choice': action,
                   'probabilities': {'enter': p, 'wait': 1 - p, 'skip': 0}, 'confidence': confidence},
        'stop': {'type': 'choice', 'choice': 'volatility', 'probabilities': {'volatility': .9, 'none': .1}, 'confidence': .8},
        'evidence': {'type': 'choice', 'choice': 'pullback', 'probabilities': {'breakout': .1, 'pullback': .8, 'reversal': .05, 'unclear': .05}, 'confidence': .8}
    }}


class FakeAPI:
    def __init__(self):
        self.now = dt.datetime(2026, 10, 5, 10, 15, 20, tzinfo=ET)
        self.is_open = True
        self.cash = Decimal('100000')
        self.order_map, self.held, self.submissions = {}, {}, []
        self.timeout_after_accept = False
        self.trade_price = 100
        self.account_id = 'paper-fixed'
        self.close = '16:00'

    def clock(self):
        return {'timestamp': self.now.isoformat(), 'is_open': self.is_open}

    def calendar(self, start, end):
        days, day = [], dt.date.fromisoformat(start)
        while day <= dt.date.fromisoformat(end):
            if day.weekday() < 5:
                days.append({'date': day.isoformat(), 'open': '09:30', 'close': self.close})
            day += dt.timedelta(days=1)
        return days

    def account(self):
        return {'id': self.account_id, 'status': 'ACTIVE', 'cash': str(self.cash), 'buying_power': str(self.cash),
                'equity': str(self.cash + sum(Decimal(p['market_value']) for p in self.held.values()))}

    def positions(self):
        return list(self.held.values())

    def open_orders(self):
        return [v | {'client_order_id': k} for k, v in self.order_map.items() if v['status'] not in {'filled', 'canceled', 'expired', 'rejected'}]

    def find_order(self, cid):
        return self.order_map.get(cid)

    def latest_trade(self, symbol):
        return {'t': self.now.isoformat(), 'p': self.trade_price}

    def asset(self, symbol):
        return {'class': 'us_equity', 'status': 'active', 'tradable': True, 'fractionable': True}

    def submit(self, payload):
        self.submissions.append(payload)
        symbol = payload['symbol']
        if payload['side'] == 'buy':
            notional = Decimal(payload['notional'])
            qty = notional / Decimal(str(self.trade_price))
            self.cash -= notional
            self.held[symbol] = {'symbol': symbol, 'qty': str(qty), 'current_price': str(self.trade_price),
                                 'avg_entry_price': str(self.trade_price), 'market_value': str(notional)}
        else:
            qty = Decimal(payload['qty'])
            self.cash += qty * Decimal(self.held[symbol]['current_price'])
            del self.held[symbol]
        result = payload | {'status': 'filled', 'filled_qty': str(qty), 'filled_avg_price': str(self.trade_price)}
        self.order_map[payload['client_order_id']] = result
        if self.timeout_after_accept:
            self.timeout_after_accept = False
            raise TimeoutError('Response lost after broker acceptance')
        return result

    def cancel(self, order_id):
        self.order_map[order_id]['status'] = 'canceled'


class FakeClef:
    def __init__(self, p=.92):
        self.calls, self.p = 0, p

    def bind_account(self, strategy, account_id):
        pass

    def evaluate(self, payload, stops):
        self.calls += 1
        return {'action': 'enter', 'probability': self.p, 'confidence': .85,
                'stop_price': 97, 'stop_choice': 'volatility', 'evidence': 'pullback', 'raw': {}}, 'shared-key'


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = load_config(ROOT) | {'strategy': 'fixed'}
        self.api, self.store, self.clef = FakeAPI(), Store(':memory:'), FakeClef()
        self.addCleanup(self.store.db.close)
        self.engine = Engine(self.api, self.store, self.clef, self.cfg, self.root)
        self.engine.initialize()
        self.store.write('INSERT INTO watchlists VALUES (?,?)', ('2026-10-05', json.dumps([{'symbol': 'AAPL', 'annotation': 'Pullback and reclaim daily support.'}])))
        self.bundle = {frame: [{'t': '2026-10-05T09:30:00-04:00', 'o': 99, 'h': 101, 'l': 98, 'c': 100, 'v': 10000}] * 30 for frame in ['30Min', '1Hour', '1Day', '1Week']}
        self.engine.market.bundle = lambda symbol, slot: (self.bundle, slot - dt.timedelta(minutes=15, seconds=10))
        self.render = patch('clef_trader.charts.render', return_value=b'png')
        self.render.start()

    def tearDown(self):
        self.render.stop()
        self.temp.cleanup()

    def test_fixed_entry_and_no_duplicates(self):
        self.engine.tick()
        self.engine.tick()
        self.assertEqual(len(self.api.submissions), 1)
        self.assertEqual(Decimal(self.api.submissions[0]['notional']), 5000)
        self.assertEqual(self.clef.calls, 1)

    def test_intelligent_uses_probability_tier(self):
        self.cfg['strategy'] = 'intelligent'
        self.engine.tick()
        self.assertEqual(Decimal(self.api.submissions[0]['notional']), 7500)

    def test_timeout_after_acceptance_reconciles(self):
        self.api.timeout_after_accept = True
        self.engine.tick()
        self.engine.tick()
        self.assertEqual(len(self.api.submissions), 1)
        self.assertEqual(self.store.owned()['AAPL'], 50)

    def test_daily_red_exits_on_later_day(self):
        self.engine.tick()
        self.api.now = dt.datetime(2026, 10, 8, 15, 55, 10, tzinfo=ET)
        self.api.held['AAPL']['current_price'] = '99'
        self.engine.tick()
        self.assertEqual(self.api.submissions[-1]['side'], 'sell')
        self.assertEqual(self.store.owned(), {})

    def test_winner_and_flat_are_held_friday(self):
        self.engine.tick()
        self.api.now = dt.datetime(2026, 10, 9, 15, 55, 10, tzinfo=ET)
        self.engine.tick()
        self.api.held['AAPL']['current_price'] = '110'
        self.engine.tick()
        self.assertEqual(len(self.api.submissions), 1)

    def test_intraday_technical_stop(self):
        self.engine.tick()
        self.api.now = dt.datetime(2026, 10, 6, 11, tzinfo=ET)
        self.api.held['AAPL']['current_price'] = '96'
        self.engine.tick()
        self.assertEqual(self.api.submissions[-1]['side'], 'sell')
        reason = json.loads(self.store.rows("SELECT reason FROM orders WHERE side='sell'")[0]['reason'])
        self.assertEqual(reason['rule'], 'technical_stop')

    def test_no_reentry_after_stop_same_week(self):
        self.engine.tick()
        self.api.held['AAPL']['current_price'] = '96'
        self.engine.tick()
        self.api.now += dt.timedelta(minutes=30)
        self.engine.tick()
        self.assertEqual(len(self.api.submissions), 2)

    def test_no_add_to_existing_holding_next_week(self):
        self.engine.tick()
        self.store.write('INSERT INTO watchlists VALUES (?,?)', ('2026-10-12', json.dumps([{'symbol': 'AAPL', 'annotation': 'More support.'}])))
        self.api.now = dt.datetime(2026, 10, 12, 10, 15, 20, tzinfo=ET)
        self.engine.tick()
        self.assertEqual(len(self.api.submissions), 1)

    def test_unmanaged_position_untouched(self):
        self.api.held['AAPL'] = {'symbol': 'AAPL', 'qty': '1', 'current_price': '1', 'avg_entry_price': '100', 'market_value': '1'}
        self.api.now = dt.datetime(2026, 10, 5, 15, 55, 10, tzinfo=ET)
        self.engine.tick()
        self.assertEqual(self.api.submissions, [])

    def test_quantity_mismatch_blocks_sale(self):
        self.engine.tick()
        self.api.held['AAPL']['qty'] = '51'
        self.api.held['AAPL']['current_price'] = '90'
        self.engine.tick()
        self.assertEqual(len(self.api.submissions), 1)

    def test_cash_cap_and_no_margin(self):
        self.api.cash = Decimal(4999)
        self.engine.tick()
        self.assertEqual(self.api.submissions, [])

    def test_total_cap_counts_external_positions(self):
        self.api.held['MSFT'] = {'symbol': 'MSFT', 'qty': '990', 'market_value': '99000', 'current_price': '100', 'avg_entry_price': '100'}
        self.engine.tick()
        self.assertEqual(self.api.submissions, [])

    def test_price_gap_blocks_entry(self):
        self.api.trade_price = 110
        self.engine.tick()
        self.assertEqual(self.api.submissions, [])

    def test_low_conviction_does_not_enter(self):
        self.clef.p = .65
        self.engine.tick()
        self.assertEqual(self.api.submissions, [])

    def test_paused_entries_still_exit(self):
        self.engine.tick()
        self.api.now = dt.datetime(2026, 10, 6, 15, 55, 10, tzinfo=ET)
        self.api.held['AAPL']['current_price'] = '99'
        self.engine.tick(entries=False)
        self.assertEqual(self.api.submissions[-1]['side'], 'sell')

    def test_closed_market_no_order(self):
        self.api.is_open = False
        self.engine.tick()
        self.assertEqual(self.api.submissions, [])

    def test_early_close_daily_exit(self):
        self.engine.tick()
        self.api.close = '13:00'
        self.api.now = dt.datetime(2026, 10, 6, 12, 55, 10, tzinfo=ET)
        self.api.held['AAPL']['current_price'] = '99'
        self.engine.tick()
        self.assertEqual(self.api.submissions[-1]['side'], 'sell')

    def test_slot_alignment_accounts_for_delay(self):
        session = {'date': '2026-10-05', 'open': '09:30', 'close': '16:00'}
        self.assertIsNone(entry_slot(dt.datetime(2026, 10, 5, 10, tzinfo=ET), session, 15))
        slot = entry_slot(self.api.now, session, 15)
        self.assertEqual(slot.strftime('%H:%M:%S'), '10:15:10')
        self.assertIsNone(entry_slot(self.api.now + dt.timedelta(minutes=10), session, 15))

    def test_response_validation_rejects_nan(self):
        bad = response()
        bad['answers']['action']['confidence'] = float('nan')
        with self.assertRaises(ValueError):
            validate_response(bad, {'volatility': 97}, 'clef')

    def test_valid_response(self):
        parsed = validate_response(response(), {'volatility': 97}, 'clef')
        self.assertEqual(parsed['stop_price'], 97)

    def test_response_validation_rejects_missing_probabilities(self):
        bad = response()
        del bad['answers']['action']['probabilities']['wait']
        with self.assertRaises(ValueError):
            validate_response(bad, {'volatility': 97}, 'clef')

    def test_shared_cache_reuses_call_and_accounts_distinct(self):
        clef = Clef(self.root / 'cache', self.cfg)
        self.addCleanup(clef.db.close)
        clef.bind_account('fixed', 'account-a')
        with self.assertRaises(RuntimeError):
            clef.bind_account('intelligent', 'account-a')
        clef.bind_account('intelligent', 'account-b')
        payload = {'model': 'clef', 'state': 'identical input'}
        with patch.dict('os.environ', {'CLOUDFLARE_ACCOUNT_ID': 'abc', 'CLOUDFLARE_API_TOKEN': 'secret'}), patch('clef_trader.decision.request_json', return_value={'success': True, 'result': response()}) as call:
            clef.evaluate(payload, {'volatility': 97})
            clef.evaluate(payload, {'volatility': 97})
            self.assertEqual(call.call_count, 1)

    def test_quota_does_not_call_provider(self):
        clef = Clef(self.root / 'cache', self.cfg)
        self.addCleanup(clef.db.close)
        day = dt.datetime.now(dt.timezone.utc).date().isoformat()
        clef.db.execute('INSERT INTO calls VALUES (?,?)', (day, self.cfg['max_ai_calls_per_utc_day']))
        clef.db.commit()
        with patch('clef_trader.decision.request_json') as call, self.assertRaises(QuotaError):
            clef.evaluate({'model': 'clef', 'state': 'test'}, {'volatility': 97})
        self.assertFalse(call.called)

    def test_sizing_monotonic(self):
        cfg = self.cfg | {'strategy': 'intelligent'}
        self.assertEqual([allocation(cfg, p) for p in [.69, .72, .85, .92, .97]], [0, 2500, 5000, 7500, 10000])

    def test_watchlist_annotations_and_duplicates(self):
        path = self.root / 'watchlist.txt'
        path.write_text('Week: 2026-10-05\naapl: Reclaim the daily support\nWait for confirmation.\n')
        week, items = parse_watchlist(path, dt.date(2026, 10, 4))
        self.assertEqual(week, '2026-10-05')
        self.assertEqual(items[0]['symbol'], 'AAPL')
        self.assertIn('Wait for confirmation', items[0]['annotation'])
        path.write_text('Week: 2026-10-05\nAAPL: Daily support\nAAPL: Daily support\n')
        with self.assertRaises(ValueError):
            parse_watchlist(path, dt.date(2026, 10, 4))

    def test_hourly_uses_session_alignment(self):
        rows = [{'t': f'2026-10-05T{time}:00-04:00', 'o': 99, 'h': 101, 'l': 98, 'c': 100, 'v': 10} for time in ['09:30', '10:00', '10:30']]
        result = hourly(rows, {'2026-10-05': {'date': '2026-10-05', 'open': '09:30', 'close': '16:00'}})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['v'], 20)

    def test_invalid_market_data_rejected(self):
        with self.assertRaises(ValueError):
            clean_bars([{'t': '2026-10-05T09:30:00-04:00', 'o': 100, 'h': 99, 'l': 98, 'c': 100, 'v': 1}])

    def test_pending_buy_reserves_cash_exposure(self):
        positions = {'MSFT': {'market_value': '90000'}}
        orders = [{'client_order_id': 'external', 'side': 'buy', 'notional': '6000'}]
        self.assertEqual(self.engine.exposure(positions, orders), 96000)

    def test_external_quantity_buy_blocks_new_entries(self):
        with self.assertRaises(RuntimeError):
            self.engine.exposure({}, [{'client_order_id': 'external', 'side': 'buy', 'qty': '100'}])

    def test_partial_fill_cancels_remainder_then_exits(self):
        self.engine.tick()
        cid = self.api.submissions[0]['client_order_id']
        self.api.order_map[cid].update(status='partially_filled', filled_qty='20', id=cid)
        self.store.update_order(cid, self.api.order_map[cid])
        self.api.held['AAPL']['qty'] = '20'
        self.api.held['AAPL']['current_price'] = '96'
        self.engine.tick()
        self.assertEqual(self.api.order_map[cid]['status'], 'canceled')
        self.engine.tick()
        self.assertEqual(self.api.submissions[-1]['side'], 'sell')
        self.assertEqual(Decimal(self.api.submissions[-1]['qty']), 20)

    def test_intent_crash_retries_same_id(self):
        original = self.api.submit
        with patch.object(self.api, 'submit', side_effect=TimeoutError('Request never reached broker')):
            self.engine.tick()
        self.assertEqual(len(self.store.pending()), 1)
        self.engine.tick()
        self.assertEqual(len(self.api.submissions), 1)
        self.assertEqual(len(self.store.rows('SELECT * FROM orders')), 1)

    def test_stale_uncertain_intent_stays_reconcilable(self):
        with patch.object(self.api, 'submit', side_effect=TimeoutError('Unknown submission')):
            self.engine.tick()
        self.api.now += dt.timedelta(minutes=10)
        self.engine.tick()
        row = self.store.pending()[0]
        self.assertEqual(row['status'], 'uncertain')
        self.api.order_map[row['client_id']] = {'status': 'filled', 'filled_qty': '50', 'filled_avg_price': '100'}
        self.engine.reconcile()
        self.assertEqual(self.store.owned()['AAPL'], 50)

    def test_invalid_model_does_not_change_account(self):
        self.clef.evaluate = lambda payload, stops: (_ for _ in ()).throw(ValueError('Invalid schema'))
        self.engine.tick()
        self.assertEqual(self.api.submissions, [])
        self.assertEqual(self.store.rows('SELECT action FROM decisions')[0]['action'], 'error')

    def test_shared_cache_busy_retries_without_losing_slot(self):
        original = self.clef.evaluate
        self.clef.evaluate = lambda payload, stops: (_ for _ in ()).throw(BusyLockError('Other experiment is evaluating'))
        self.engine.tick()
        self.assertEqual(self.store.rows('SELECT * FROM decisions'), [])
        self.clef.evaluate = original
        self.engine.tick()
        self.assertEqual(len(self.api.submissions), 1)

    def test_neuron_budget_blocks_uncached_but_allows_cached(self):
        clef = Clef(self.root / 'cache', self.cfg)
        self.addCleanup(clef.db.close)
        day = dt.datetime.now(dt.timezone.utc).date().isoformat()
        clef.db.execute('INSERT INTO usage VALUES (?,?,?)', (day, 8900, 300))
        clef.db.commit()
        with self.assertRaises(QuotaError):
            clef.evaluate({'model': 'clef', 'state': 'test'}, {'volatility': 97})


class MarketTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.cfg = load_config(ROOT)
        self.market = Market(self.api, self.cfg)

    def test_partial_and_extended_hours_are_excluded(self):
        day = dt.date(2026, 10, 1)
        intraday = []
        for i in range(4):
            if day.weekday() < 5:
                for n in range(13):
                    instant = dt.datetime.combine(day, dt.time(9, 30), ET) + dt.timedelta(minutes=30*n)
                    intraday.append({'t': instant.isoformat(), 'o': 99, 'h': 101, 'l': 98, 'c': 100, 'v': 100})
            day += dt.timedelta(days=1)
        # Append the current completed, incomplete, and premarket bars.
        for clock in ['08:30', '09:30', '10:00']:
            intraday.append({'t': f'2026-10-05T{clock}:00-04:00', 'o': 99, 'h': 101, 'l': 98, 'c': 100, 'v': 100})
        daily = [{'t': (dt.datetime(2026, 8, 1, tzinfo=ET) + dt.timedelta(days=i)).isoformat(),
                  'o': 99, 'h': 101, 'l': 98, 'c': 100, 'v': 10000} for i in range(65)]
        self.market.bars = lambda symbol, frame, start, end: intraday if frame == '30Min' else daily
        result, cutoff = self.market.bundle('AAPL', dt.datetime(2026, 10, 5, 10, 15, 10, tzinfo=ET))
        self.assertEqual(result['30Min'][-1]['t'], '2026-10-05T09:30:00-04:00')
        self.assertFalse(any('T08:30' in r['t'] for r in result['30Min']))
        self.assertTrue(all(dt.datetime.fromisoformat(r['t']).date() < dt.date(2026, 10, 5) for r in result['1Day']))

    def test_pagination_and_free_sip_cutoff(self):
        calls = []
        def data(path, params):
            calls.append(dict(params))
            n = 2 if 'page_token' in params else 1
            return {'bars': [{'t': f'2026-10-02T{n+9}:00:00-04:00', 'o': 99, 'h': 101, 'l': 98, 'c': 100, 'v': 100}],
                    'next_page_token': 'second-page' if n == 1 else None}
        self.api.data = data
        end = dt.datetime(2026, 10, 5, 10, tzinfo=ET)
        result = self.market.bars('AAPL', '30Min', end - dt.timedelta(days=5), end)
        self.assertEqual(len(result), 2)
        self.assertEqual(calls[0]['feed'], 'sip')
        self.assertEqual(calls[1]['page_token'], 'second-page')
        self.assertEqual(calls[0]['timeframe'], '30Min')

    def test_weekly_current_period_excluded(self):
        rows = [{'t': f'2026-10-{day:02d}T00:00:00-04:00', 'o': 99, 'h': 101, 'l': 98, 'c': 100, 'v': 100} for day in [1, 2, 5, 6]]
        result = weekly(rows, dt.date(2026, 10, 5))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['v'], 200)


if __name__ == '__main__':
    unittest.main()
