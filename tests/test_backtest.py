"""Historical replay verification with deterministic synthetic data, not returns claims."""
import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from clef_trader.api import APIError
from clef_trader.backtest import Backtest, History, Portfolio, events_for, run_backtest
from clef_trader.cli import main
from clef_trader.config import load_config
from clef_trader.decision import QuotaError
from clef_trader.market import ET, timestamp

ROOT = Path(__file__).resolve().parents[1]
START, END = '2026-06-15', '2026-06-26'


class Archive:
    def __init__(self):
        self.rows, self.execution_calls = {}, []
        self.fail_quote = False

    def calendar(self, start, end):
        sessions, day = [], dt.date.fromisoformat(start)
        while day <= dt.date.fromisoformat(end):
            if day.weekday() < 5 and day.isoformat() != '2026-06-19':
                sessions.append({'date': day.isoformat(), 'open': '09:30', 'close': '16:00'})
            day += dt.timedelta(days=1)
        return sessions

    def price(self, symbol, instant):
        if symbol == 'LOSS' and instant.date().isoformat() == '2026-06-16' and instant.hour >= 15:
            return 98
        if symbol == 'WIN':
            return 100 + len(self.calendar(START, instant.date().isoformat())) - 1 if instant.date().isoformat() >= START else 100
        return 100

    def series(self, symbol, frame):
        key = symbol, frame
        if key not in self.rows:
            sessions = self.calendar('2024-04-01' if frame == '1Day' else '2026-05-01', '2026-06-30')
            rows = []
            for session in sessions:
                opening = dt.datetime.fromisoformat(session['date'] + 'T09:30').replace(tzinfo=ET)
                for n in range(1 if frame == '1Day' else 13):
                    instant = opening + dt.timedelta(minutes=30 * n) if frame != '1Day' else opening.replace(hour=0, minute=0)
                    p = self.price(symbol, instant)
                    rows.append({'t': instant.isoformat(), 'o': p, 'h': p + 1,
                                 'l': p - (3 if frame == '1Day' else 1), 'c': p, 'v': 10000})
            self.rows[key] = rows
        return self.rows[key]

    def data(self, path, params):
        symbol = path.split('/')[2]
        if symbol == 'BAD':
            raise APIError(404, 'Unknown symbol')
        start, end = timestamp(params['start']), timestamp(params['end'])
        return {'bars': [r for r in self.series(symbol, params['timeframe']) if start <= timestamp(r['t']) <= end]}

    def reference_trade(self, symbol, instant, feed='sip'):
        self.execution_calls.append(('trade', symbol, instant, feed))
        return self.price(symbol, instant)

    def execution_quote(self, symbol, instant):
        if self.fail_quote:
            raise RuntimeError('Missing synthetic quote')
        self.execution_calls.append(('quote', symbol, instant))
        p = self.price(symbol, instant)
        return {'t': instant.isoformat(), 'bp': p, 'ap': p}


class Model:
    def __init__(self):
        self.calls = []
        self.fail_after = None

    def evaluate(self, payload, stops):
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise QuotaError('Synthetic daily limit')
        state = payload['state']
        self.calls.append(state)
        symbol = state['symbol']
        action = 'skip' if symbol == 'SKIP' else 'wait' if symbol == 'WAIT' or (symbol == 'LATER' and state['as_of'][:10] < '2026-06-17') else 'enter'
        return {'action': action, 'probability': .92 if action == 'enter' else .05,
                'confidence': .85, 'stop_price': stops['daily_support'], 'raw': {}}, symbol + state['as_of']


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = load_config(ROOT)
        self.history, self.model = Archive(), Model()
        self.items = [{'symbol': s, 'annotation': 'Synthetic support test.'} for s in ['WIN', 'LOSS', 'LATER', 'WAIT', 'SKIP']]
        render = patch('clef_trader.charts.render', return_value=b'synthetic-png')
        render.start()
        self.addCleanup(render.stop)

    def replay(self, **kwargs):
        return Backtest(self.history, self.model, self.cfg, self.items, START, START, END,
                        self.root / 'replay', slippage_bps=0, **kwargs)

    def test_full_ab_replay_original_week_and_later_daily_loss_exit(self):
        report = self.replay().run()
        self.assertEqual(report['status'], 'complete')
        fixed, intelligent = report['results']['fixed'], report['results']['intelligent']
        self.assertEqual(fixed['entries'], 3)
        self.assertEqual(fixed['closed_trades'], 1)
        self.assertEqual(fixed['trades'][0]['reason'], 'daily_red')
        self.assertTrue(fixed['trades'][0]['exit_at'].startswith('2026-06-16T15:55'))
        self.assertEqual(fixed['realized_pnl'], -100)
        self.assertEqual(fixed['final_equity'], 100300)
        self.assertEqual(intelligent['final_equity'], 100450)
        self.assertEqual(set(fixed['open_holdings']), {'WIN', 'LATER'})
        self.assertTrue(fixed['open_holdings']['LATER']['entry_at'].startswith('2026-06-17'))
        self.assertTrue(all(s['as_of'][:10] <= '2026-06-18' for s in self.model.calls))
        self.assertEqual(sum(s['symbol'] == 'SKIP' for s in self.model.calls), 1)
        self.assertEqual(fixed['open_holdings']['WIN']['decision_key'], intelligent['open_holdings']['WIN']['decision_key'])
        self.assertEqual(fixed['open_holdings']['WIN']['notional'], 5000)
        self.assertEqual(intelligent['open_holdings']['WIN']['notional'], 7500)
        self.assertTrue((self.root / 'replay' / 'report.html').exists())

    def test_inputs_exclude_future_intraday_daily_and_weekly_bars(self):
        replay = self.replay()
        replay.process_entries(dt.datetime(2026, 6, 15, 10, 15, 10, tzinfo=ET))
        for state in self.model.calls:
            cutoff = timestamp(state['as_of'])
            for frame in ('30Min', '1Hour'):
                minutes = 30 if frame == '30Min' else 60
                for row in state['ohlcv'][frame]:
                    self.assertLessEqual(timestamp(row[0]) + dt.timedelta(minutes=minutes), cutoff)
            self.assertTrue(all(timestamp(r[0]).date() < cutoff.date() for r in state['ohlcv']['1Day']))
            self.assertTrue(all(timestamp(r[0]).date() < dt.date(2026, 6, 15) for r in state['ohlcv']['1Week']))

    def test_quota_resume_preserves_decision_and_single_entry(self):
        self.model.fail_after = 1
        with self.assertRaises(QuotaError):
            self.replay().run()
        saved = json.loads((self.root / 'replay' / 'report.json').read_text())
        self.assertEqual(saved['status'], 'paused_quota')
        self.assertFalse(saved['completed'])
        self.assertEqual(saved['results']['fixed']['entries'], 1)
        self.model.fail_after = None
        completed = self.replay().run()
        self.assertEqual(completed['results']['fixed']['entries'], 3)
        self.assertEqual(sum(s['symbol'] == 'WIN' for s in self.model.calls), 1)
        calls = len(self.model.calls)
        self.assertEqual(self.replay().run()['results'], completed['results'])
        self.assertEqual(len(self.model.calls), calls)

    def test_entry_candle_not_used_as_a_stop_then_gap_is_adverse(self):
        replay = self.replay()
        entry = dt.datetime(2026, 6, 15, 10, 15, 10, tzinfo=ET)
        portfolio = replay.portfolios['fixed']
        portfolio.buy('WIN', entry, 5000, 100, 95, START, 'key', .9)
        rows = self.history.series('WIN', '30Min')
        for row in rows:
            if row['t'] == '2026-06-15T10:00:00-04:00':
                row['l'] = 90  # Could have happened before the 10:15 fill.
            if row['t'] == '2026-06-15T10:30:00-04:00':
                row.update(o=92, h=94, l=91, c=93)
        replay.process_bar(dt.datetime(2026, 6, 15, 10, 30, tzinfo=ET))
        self.assertIn('WIN', portfolio.state['holdings'])
        replay.process_bar(dt.datetime(2026, 6, 15, 11, 0, tzinfo=ET))
        self.assertNotIn('WIN', portfolio.state['holdings'])
        self.assertEqual(portfolio.state['trades'][0]['exit_price'], 92)

    def test_missing_held_data_pauses_without_partial_portfolio_mutation(self):
        replay = self.replay()
        entry = dt.datetime(2026, 6, 15, 10, 15, 10, tzinfo=ET)
        for p in replay.portfolios.values():
            p.buy('WIN', entry, 5000, 100, 95, START, 'key', .9)
        self.history.rows['WIN', '30Min'] = []
        replay.events = [(dt.datetime(2026, 6, 15, 10, 30, tzinfo=ET), 'bar', {})]
        with self.assertRaisesRegex(RuntimeError, 'Missing held-position'):
            replay.run()
        report = json.loads((self.root / 'replay' / 'report.json').read_text())
        self.assertEqual(report['status'], 'paused_error')
        self.assertEqual(report['processed_events'], 0)
        self.assertEqual(report['results']['fixed']['open_holdings']['WIN']['mark'], 100)

    def test_data_authorization_error_is_not_silently_excluded(self):
        replay = self.replay()
        replay.market.bundle = lambda *args: (_ for _ in ()).throw(APIError(403, 'SIP denied'))
        with self.assertRaises(APIError):
            replay.run()
        self.assertFalse(replay.exclusions)
        self.assertFalse(json.loads((self.root / 'replay' / 'report.json').read_text())['completed'])

    def test_missing_symbol_is_disclosed_and_other_symbols_continue(self):
        self.items.append({'symbol': 'BAD', 'annotation': 'Preserve the original spelling.'})
        report = self.replay().run()
        self.assertEqual(report['status'], 'complete_with_coverage_gaps')
        self.assertIn('BAD', report['exclusions'])
        self.assertEqual(report['results']['fixed']['entries'], 3)

    def test_missing_quote_prevents_fill_and_reports_gap(self):
        replay = self.replay()
        self.history.fail_quote = True
        replay.process_entries(dt.datetime(2026, 6, 15, 10, 15, 10, tzinfo=ET))
        self.assertFalse(replay.portfolios['fixed'].state['holdings'])
        self.assertTrue(any(g['stage'] == 'execution' for g in replay.coverage))

    def test_week_two_has_no_entries_unless_explicitly_repeated(self):
        slot = dt.datetime(2026, 6, 22, 10, 15, 10, tzinfo=ET)
        self.replay().process_entries(slot)
        self.assertEqual(len(self.model.calls), 0)
        self.replay(repeat_list=True).process_entries(slot)
        self.assertGreater(len(self.model.calls), 0)

    def test_holidays_and_early_close_schedule(self):
        sessions = self.history.calendar(START, END)
        self.assertEqual(len(sessions), 9)
        self.assertNotIn('2026-06-19', [s['date'] for s in sessions])
        events = events_for([{'date': START, 'open': '09:30', 'close': '13:00'}], self.cfg)
        self.assertEqual(next(e[0].strftime('%H:%M:%S') for e in events if e[1] == 'entry'), '10:15:10')
        self.assertEqual(next(e[0].strftime('%H:%M') for e in events if e[1] == 'daily_exit'), '12:55')
        self.assertTrue(all(e[0].hour < 13 for e in events if e[1] == 'entry'))

    def test_cap_and_no_reentry_even_after_exit(self):
        p = Portfolio('fixed', 10000)
        instant = dt.datetime(2026, 6, 15, 10, 15, tzinfo=ET)
        self.assertTrue(p.buy('ONE', instant, 7500, 100, 95, START, 'one', .95))
        self.assertFalse(p.buy('TWO', instant, 5000, 100, 95, START, 'two', .95))
        p.sell('ONE', instant, 100, 'test')
        self.assertFalse(p.buy('ONE', instant, 5000, 100, 95, START, 'one', .95))

    def test_backtest_cli_does_not_open_paper_store_or_engine(self):
        (self.root / 'config.json').write_text(json.dumps(self.cfg))
        env = {name: 'test-value' for name in ['APCA_API_KEY_ID', 'APCA_API_SECRET_KEY', 'CLOUDFLARE_ACCOUNT_ID', 'CLOUDFLARE_API_TOKEN']}
        with patch.dict(os.environ, env), patch('sys.argv', ['cli', '--project', str(self.root), 'backtest', 'list.txt', '--start', START, '--end', END]), patch('clef_trader.cli.Store') as store, patch('clef_trader.cli.Engine') as engine, patch('clef_trader.cli.Alpaca'), patch('clef_trader.cli.Clef'), patch('clef_trader.cli.logging.basicConfig'), patch('clef_trader.backtest.run_backtest', return_value=(self.root, {'status': 'complete', 'results': {}, 'exclusions': {}, 'coverage_gaps': []})), contextlib.redirect_stdout(io.StringIO()):
            main()
        store.assert_not_called()
        engine.assert_not_called()
        self.assertFalse((self.root / 'state').exists())

    def test_invalid_range_and_slippage_rejected_before_api(self):
        file = self.root / 'list.txt'
        file.write_text('Week: 2026-06-15\nWIN: Synthetic test.')
        for start, end, slip in [('2026-06-22', END, 10), (END, START, 10), (START, END, float('nan')), (START, END, -1)]:
            with self.subTest(start=start, end=end, slippage=slip), self.assertRaises(ValueError):
                run_backtest(None, None, self.cfg, self.root, file, start, end, slippage_bps=slip)


class HistoryTests(unittest.TestCase):
    def test_prefetched_future_is_not_exposed_to_chart_queries(self):
        with tempfile.TemporaryDirectory() as directory:
            history = History(None, Path(directory), START, END)
            rows = Archive().series('WIN', '30Min')
            history.loaded['WIN', '30Min'] = rows
            cutoff = '2026-06-15T10:00:00-04:00'
            data = history.data('/stocks/WIN/bars', {'timeframe': '30Min', 'start': '2026-06-01T00:00:00-04:00', 'end': cutoff})
            self.assertTrue(data['bars'])
            self.assertTrue(all(timestamp(r['t']) <= timestamp(cutoff) for r in data['bars']))

    def test_historical_execution_uses_past_trade_and_next_quote_with_cache(self):
        class API:
            def __init__(self):
                self.calls = []

            def data(self, path, params):
                self.calls.append((path, params.copy()))
                if path.endswith('/trades'):
                    return {'trades': [{'t': params['end'], 'p': 100}]}
                return {'quotes': [{'t': params['start'], 'bp': 99.9, 'ap': 100.1, 'bs': 10, 'as': 10}]}

        with tempfile.TemporaryDirectory() as directory, patch('clef_trader.backtest.time.sleep'):
            api = API()
            history = History(api, Path(directory), START, END)
            instant = dt.datetime(2026, 6, 15, 10, 15, 10, tzinfo=ET)
            self.assertEqual(history.reference_trade('WIN', instant, feed='iex'), 100)
            self.assertEqual(history.execution_quote('WIN', instant)['ap'], 100.1)
            history.execution_quote('WIN', instant)
            self.assertEqual(len(api.calls), 2)
            trade_params, quote_params = api.calls[0][1], api.calls[1][1]
            self.assertEqual(timestamp(trade_params['end']), instant)
            self.assertEqual(timestamp(quote_params['end']), instant + dt.timedelta(seconds=10))
            self.assertEqual(quote_params['feed'], 'sip')
            self.assertEqual(quote_params['asof'], START)

    def test_stale_trade_and_nonfinite_quote_are_rejected(self):
        class API:
            def data(self, path, params):
                if path.endswith('/trades'):
                    return {'trades': [{'t': '2026-06-15T09:00:00-04:00', 'p': 100}]}
                return {'quotes': [{'t': params['start'], 'bp': 100, 'ap': float('inf')}]}

        with tempfile.TemporaryDirectory() as directory, patch('clef_trader.backtest.time.sleep'):
            history = History(API(), Path(directory), START, END)
            instant = dt.datetime(2026, 6, 15, 10, 15, 10, tzinfo=ET)
            with self.assertRaises(RuntimeError):
                history.reference_trade('WIN', instant)
            with self.assertRaises(RuntimeError):
                history.execution_quote('WIN', instant)


if __name__ == '__main__':
    unittest.main()
