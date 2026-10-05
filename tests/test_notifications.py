import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from clef_trader.notifications import ET, Reporter, PaperReader, env_file, pnl_text, portfolio_report


class NotificationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.r = Reporter(self.root / 'state', [self.root])
        self.addCleanup(self.r.db.close)
        self.now = dt.datetime(2026, 10, 5, 17, tzinfo=ET)

    def test_independent_credentials(self):
        other = self.root / 'other'
        other.mkdir()
        for root, key in [(self.root, 'fixed'), (other, 'intelligent')]:
            (root / '.env').write_text(f'APCA_API_KEY_ID={key}\nAPCA_API_SECRET_KEY=secret-{key}\n')
        self.assertEqual(PaperReader(self.root).headers['APCA-API-KEY-ID'], 'fixed')
        self.assertEqual(PaperReader(other).headers['APCA-API-KEY-ID'], 'intelligent')

    def test_durable_deduplication(self):
        self.r.queue('fill:1', 'a')
        self.r.queue('fill:1', 'a')
        second = Reporter(self.r.state, [self.root])
        try:
            second.queue('fill:1', 'a')
            self.assertEqual(second.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 1)
            self.assertEqual(second.activated, self.r.activated)
        finally:
            second.db.close()

    def test_retry_does_not_mark_sent(self):
        self.r.queue('one', 'test')
        with patch('clef_trader.notifications.send', side_effect=RuntimeError('offline')):
            self.r.deliver({'NOTIFICATION_PROVIDER': 'telegram'})
        row = self.r.db.execute('SELECT * FROM outbox').fetchone()
        self.assertEqual((row['sent'], row['attempts']), (0, 1))
        self.r.db.execute('UPDATE outbox SET retry_at=0')
        with patch('clef_trader.notifications.send') as send:
            self.r.deliver({'NOTIFICATION_PROVIDER': 'telegram'})
            self.r.deliver({'NOTIFICATION_PROVIDER': 'telegram'})
            send.assert_called_once()
        self.assertEqual(self.r.db.execute('SELECT sent FROM outbox').fetchone()[0], 1)

    def test_partial_fills_each_alert_once_manual_ignored(self):
        class Reader:
            def get(self, path): return {'id': 'account'}
            def activities(self, after, kinds):
                return [dict(id=k, order_id=oid, side='buy', symbol='AAPL', qty='2', price='100',
                             transaction_time='2026-10-05T15:00:00Z', type='partial_fill')
                        for k, oid in [('fill1', 'order'), ('fill2', 'order'), ('manual', 'unrelated')]]
        with patch('clef_trader.notifications.ledger', return_value=({'account_id': 'account'}, {'order': {'reason': 'entry', 'client_id': 'client'}}, None)):
            self.r.fills(self.root, Reader())
            self.r.fills(self.root, Reader())
        self.assertEqual(self.r.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 2)

    def test_account_mismatch_refuses_fill_alerts(self):
        class Reader:
            def get(self, path): return {'id': 'wrong'}
        with patch('clef_trader.notifications.ledger', return_value=({'account_id': 'original'}, {}, None)):
            with self.assertRaises(RuntimeError): self.r.fills(self.root, Reader())

    def test_cash_transfers_not_profit_dividends_are_profit(self):
        baseline = dt.datetime(2026, 10, 2, 16, tzinfo=ET)
        activities = [dict(activity_type='CSD', date='2026-10-05', net_amount='5000'),
                      dict(activity_type='DIV', date='2026-10-05', net_amount='100')]
        self.assertEqual(pnl_text(105100, 100000, activities, baseline), '$100.00 (+0.10%)')
        activities.append(dict(activity_type='ACATS', date='2026-10-05'))
        self.assertIn('unavailable', pnl_text(105100, 100000, activities, baseline))

    def test_holiday_no_nightly_report_and_daily_dedupe(self):
        class Reader:
            trading = False
            def __init__(self, root): pass
            def get(self, path, **params): return [{'date': '2026-10-05'}] if self.trading else []
        with patch('clef_trader.notifications.PaperReader', Reader), patch.object(self.r, 'fills'), patch.object(self.r, 'report', return_value='nightly') as report:
            self.r.tick(self.now)
            report.assert_not_called()
            Reader.trading = True
            self.r.tick(self.now.replace(hour=16))
            report.assert_not_called()
            self.r.tick(self.now)
            self.r.tick(self.now.replace(hour=18))
            report.assert_called_once()

    def test_report_failure_is_unavailable_not_zero(self):
        with patch('clef_trader.notifications.PaperReader', side_effect=KeyError('credentials')):
            result = self.r.report(self.now)
        self.assertIn('unavailable', result)
        self.assertNotIn('$0.00', result)

    def test_report_previous_friday_and_total_snapshot_baselines(self):
        class Reader:
            def get(self, path, **params):
                if path == '/account': return dict(id='a', equity='101000', last_equity='100000', cash='96000', buying_power='192000')
                if path == '/positions': return [dict(symbol='AAPL', qty='10', market_value='5000', unrealized_pl='1000', unrealized_plpc='.25')]
                if path == '/calendar': return [dict(date='2026-10-02', close='16:00'), dict(date='2026-10-05', close='16:00')]
                if path == '/account/portfolio/history': return dict(timestamp=[1790985600], equity=[100000])
                raise AssertionError(path)
            def activities(self, *args): return []
        first = dict(at='2026-10-05T11:25:00-04:00', equity=100000)
        with patch('clef_trader.notifications.ledger', return_value=({'account_id': 'a'}, {}, first)):
            text, _ = portfolio_report(self.root, Reader(), self.now)
        self.assertIn('Weekly P/L: $1,000.00 (+1.00%)', text)
        self.assertIn('Daily P/L: $1,000.00 (+1.00%)', text)
        self.assertIn('Total P/L since first strategy snapshot: $1,000.00 (+1.00%)', text)
        self.assertIn('Cash: $96,000.00', text)

    def test_telegram_acceptance_and_failure(self):
        from clef_trader.notifications import send
        config = dict(NOTIFICATION_PROVIDER='telegram', TELEGRAM_BOT_TOKEN='123:token', TELEGRAM_CHAT_ID='42')
        with patch('clef_trader.notifications.request_json', return_value={'ok': True}) as request:
            send(config, 'filled')
            self.assertEqual(request.call_args.args[2], {'chat_id': '42', 'text': 'filled'})
        with patch('clef_trader.notifications.request_json', return_value={'ok': False}):
            with self.assertRaises(RuntimeError): send(config, 'filled')

    def test_ambiguous_same_day_cash_transfer_is_not_misreported(self):
        after = dt.datetime(2026, 10, 5, 11, tzinfo=ET)
        self.assertIn('unavailable', pnl_text(105000, 100000, [dict(activity_type='CSD', date='2026-10-05', net_amount='5000')], after))

    def test_multi_message_retry_preserves_successful_parts(self):
        self.r.queue('night', 'x' * 4000)
        with patch('clef_trader.notifications.send', side_effect=[None, RuntimeError('offline')]) as send:
            self.r.deliver({'NOTIFICATION_PROVIDER': 'telegram'})
            self.assertEqual(send.call_count, 2)
        self.r.db.execute('UPDATE outbox SET retry_at=0')
        with patch('clef_trader.notifications.send') as send:
            self.r.deliver({'NOTIFICATION_PROVIDER': 'telegram'})
            self.assertEqual(send.call_count, 2)
        self.assertEqual(self.r.db.execute('SELECT SUM(sent) FROM outbox').fetchone()[0], 3)


if __name__ == '__main__': unittest.main()
