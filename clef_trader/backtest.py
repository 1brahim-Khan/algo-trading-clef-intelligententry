"""Read-only historical replay. No order/account-mutating API is used."""
import datetime as dt
import hashlib
import json
import logging
import math
import time

from .api import APIError
from .config import allocation
from .decision import make_request, QuotaError
from .market import ET, Market, monday, session_time, stop_candidates, timestamp
from .store import exclusive


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class History:
    """Downloads immutable windows once, then presents time-bounded views."""
    def __init__(self, api, directory, start, end, symbols=None):
        self.api, self.directory, self.start, self.end = api, directory, start, end
        directory.mkdir(parents=True, exist_ok=True)
        self.loaded = {}
        self.symbols = sorted(set(symbols or []))
        self.action_audit = {}
        self.last_request = 0

    def cached(self, path, params):
        file = self.directory / (fingerprint([path, params]) + '.json')
        with exclusive(self.directory / 'data.lock', wait_seconds=30):
            if file.exists():
                return json.loads(file.read_text())
            # Stay below Basic's published historical REST rate during a replay.
            wait = .35 - (time.monotonic() - self.last_request)
            if wait > 0:
                time.sleep(wait)
            self.last_request = time.monotonic()
            result = self.api.corporate_actions(params) if path == '/corporate-actions' else self.api.data(path, params)
            temporary = file.with_suffix('.tmp')
            temporary.write_text(json.dumps(result))
            temporary.replace(file)
            return result

    def actions(self, symbol):
        key = ','.join(self.symbols) if self.symbols else symbol
        if key not in self.action_audit:
            params = {'symbols': key, 'start': str(dt.date.fromisoformat(self.start) - dt.timedelta(days=800)),
                      'end': self.end, 'limit': 1000}
            groups, pages = {}, set()
            while True:
                result = self.cached('/corporate-actions', params)
                for kind, rows in result.get('corporate_actions', {}).items():
                    groups.setdefault(kind, []).extend(rows)
                token = result.get('next_page_token')
                if not token:
                    break
                if token in pages or len(pages) >= 50:
                    raise RuntimeError('Unexpected corporate-action pagination.')
                pages.add(token)
                params['page_token'] = token
            self.action_audit[key] = groups
        return {kind: [a for a in rows if symbol in [a.get(k) for k in ('symbol', 'old_symbol', 'new_symbol', 'acquiree_symbol', 'acquirer_symbol')]]
                for kind, rows in self.action_audit[key].items()}

    def adjusted_view(self, symbol, rows, cutoff):
        actions = self.actions(symbol)
        splits = []
        for kind, events in actions.items():
            for event in events:
                effective = event.get('ex_date') or event.get('effective_date') or event.get('process_date')
                if not effective:
                    raise RuntimeError(f'Incomplete corporate-action date for {symbol}.')
                if kind in {'forward_splits', 'reverse_splits'} and effective <= cutoff.astimezone(ET).date().isoformat():
                    old, new = float(event['old_rate']), float(event['new_rate'])
                    if not all(math.isfinite(v) and v > 0 for v in [old, new]):
                        raise RuntimeError(f'Invalid split ratio for {symbol}.')
                    splits.append((effective, old / new))
        adjusted = []
        for row in rows:
            factor = math.prod(ratio for date, ratio in splits if timestamp(row['t']).astimezone(ET).date().isoformat() < date)
            # Keep integer/float representations unchanged when no adjustment
            # is needed, preserving the exact shared decision-cache payload.
            adjusted.append(row if factor == 1 else row | {k: row[k] * factor for k in ('o', 'h', 'l', 'c')} | {'v': row['v'] / factor})
        return adjusted

    def validate_actions(self):
        # Refuse the entire unsupported dataset up front rather than using a
        # future event to silently exclude a symbol from earlier decisions.
        for symbol in self.symbols:
            for kind, events in self.actions(symbol).items():
                for event in events:
                    effective = event.get('ex_date') or event.get('effective_date') or event.get('process_date')
                    if effective and self.start <= effective <= self.end and kind != 'cash_dividends':
                        raise RuntimeError(f'{symbol} has a {kind} event on {effective} during the replay; this range needs explicit corporate-action modeling.')

    def calendar(self, start, end):
        if not hasattr(self, 'all_sessions'):
            file = self.directory / ('calendar-' + fingerprint([self.start, self.end]) + '.json')
            if file.exists():
                self.all_sessions = json.loads(file.read_text())
            else:
                self.all_sessions = self.api.calendar(str(dt.date.fromisoformat(self.start) - dt.timedelta(days=800)), self.end)
                file.write_text(json.dumps(self.all_sessions))
        return [s for s in self.all_sessions if start <= s['date'] <= end]

    def series(self, symbol, frame):
        key = (symbol, frame)
        if key not in self.loaded:
            days = 800 if frame == '1Day' else 40
            beginning = dt.datetime.combine(dt.date.fromisoformat(self.start) - dt.timedelta(days=days), dt.time(), ET)
            ending = dt.datetime.combine(dt.date.fromisoformat(self.end) + dt.timedelta(days=1), dt.time(), ET)
            params = {'timeframe': frame, 'start': beginning.isoformat(), 'end': ending.isoformat(),
                      'feed': 'sip', 'adjustment': 'raw', 'asof': self.start, 'sort': 'asc', 'limit': 10000}
            rows, pages = [], set()
            while True:
                page = self.cached('/stocks/' + symbol + '/bars', params)
                rows.extend(page.get('bars') or [])
                token = page.get('next_page_token')
                if not token:
                    break
                if token in pages or len(pages) >= 50:
                    raise RuntimeError('Unexpected historical pagination.')
                pages.add(token)
                params['page_token'] = token
            from .market import clean_bars
            self.loaded[key] = clean_bars(rows)
        return self.loaded[key]

    def data(self, path, params):
        # Market.bundle applies completion checks; even the prefetched archive
        # exposes only rows at or before the requested historical cutoff.
        symbol = path.split('/')[2]
        start, end = timestamp(params['start']), timestamp(params['end'])
        rows = [r for r in self.series(symbol, params['timeframe']) if start <= timestamp(r['t']) <= end]
        return {'bars': self.adjusted_view(symbol, rows, end), 'next_page_token': None}

    def execution_quote(self, symbol, instant):
        params = {'start': instant.isoformat(), 'end': (instant + dt.timedelta(seconds=10)).isoformat(),
                  'feed': 'sip', 'sort': 'asc', 'limit': 100, 'asof': self.start}
        result = self.cached('/stocks/' + symbol + '/quotes', params)
        for quote in result.get('quotes') or []:
            if (instant <= timestamp(quote['t']) <= instant + dt.timedelta(seconds=10)
                    and all(math.isfinite(float(quote[k])) for k in ('bp', 'ap'))
                    and 0 < quote['bp'] <= quote['ap']
                    and quote.get('bs', 1) > 0 and quote.get('as', 1) > 0):
                return quote
        raise RuntimeError(f'No valid historical execution quote for {symbol} at {instant}.')

    def reference_trade(self, symbol, instant, feed='sip'):
        params = {'start': (instant - dt.timedelta(seconds=120)).isoformat(), 'end': instant.isoformat(),
                  'feed': feed, 'sort': 'desc', 'limit': 1, 'asof': self.start}
        result = self.cached('/stocks/' + symbol + '/trades', params)
        trades = result.get('trades') or []
        if not trades or not instant - dt.timedelta(seconds=120) <= timestamp(trades[0]['t']) <= instant:
            raise RuntimeError(f'No recent historical {feed} trade for {symbol} at {instant}.')
        price = float(trades[0]['p'])
        if not math.isfinite(price) or price <= 0:
            raise RuntimeError('Invalid historical execution reference.')
        return price


class Portfolio:
    def __init__(self, name, initial, saved=None):
        self.name, self.initial = name, initial
        self.state = saved or {'cash': initial, 'holdings': {}, 'trades': [], 'entries': [], 'equity': [], 'skipped': []}

    def equity(self):
        return self.state['cash'] + sum(p['qty'] * p['mark'] for p in self.state['holdings'].values())

    def buy(self, symbol, instant, amount, price, stop, week, decision_key, probability):
        if symbol in self.state['holdings'] or [week, symbol] in self.state['entries']:
            return False
        exposure = self.equity() - self.state['cash']
        if amount > self.state['cash'] + 1e-8 or exposure + amount > self.initial + 1e-8:
            self.state['skipped'].append({'symbol': symbol, 'at': instant.isoformat(), 'reason': 'cash_or_exposure_cap'})
            return False
        self.state['cash'] -= amount
        self.state['holdings'][symbol] = {'qty': amount / price, 'entry_price': price, 'entry_at': instant.isoformat(),
                                           'notional': amount, 'stop': stop, 'mark': price,
                                           'decision_key': decision_key, 'probability': probability}
        self.state['entries'].append([week, symbol])
        return True

    def sell(self, symbol, instant, price, reason):
        position = self.state['holdings'].pop(symbol)
        proceeds = position['qty'] * price
        self.state['cash'] += proceeds
        self.state['trades'].append({'symbol': symbol, **position, 'exit_at': instant.isoformat(),
                                     'exit_price': price, 'pnl': proceeds - position['notional'], 'reason': reason})

    def snapshot(self, instant):
        self.state['equity'].append({'at': instant.isoformat(), 'equity': self.equity(),
                                    'cash': self.state['cash'], 'exposure': self.equity() - self.state['cash']})

    def report(self):
        trades = self.state['trades']
        realized = sum(t['pnl'] for t in trades)
        unrealized = sum(p['qty'] * (p['mark'] - p['entry_price']) for p in self.state['holdings'].values())
        peak, drawdown = self.initial, 0
        for point in self.state['equity']:
            peak = max(peak, point['equity'])
            drawdown = max(drawdown, (peak - point['equity']) / peak)
        return {'initial_equity': self.initial, 'final_equity': self.equity(), 'return_pct': (self.equity() / self.initial - 1) * 100,
                'realized_pnl': realized, 'unrealized_pnl': unrealized,
                'closed_trades': len(trades), 'entries': len(self.state['entries']),
                'closed_trade_win_rate_pct': 100 * sum(t['pnl'] > 0 for t in trades) / len(trades) if trades else None,
                'sampled_max_drawdown_pct': drawdown * 100, 'open_holdings': self.state['holdings'],
                'trades': trades, 'skipped_allocations': self.state['skipped'], 'equity_curve': self.state['equity']}


def events_for(sessions, cfg):
    events = []
    for session in sessions:
        opening, closing = session_time(session, 'open'), session_time(session, 'close')
        instant = opening + dt.timedelta(minutes=30)
        while instant <= closing:
            events.append((instant, 'bar', session))
            instant += dt.timedelta(minutes=30)
        instant = opening + dt.timedelta(minutes=30 + cfg['data_delay_minutes'], seconds=10)
        while instant < closing - dt.timedelta(minutes=cfg['entry_cutoff_minutes']):
            events.append((instant, 'entry', session))
            instant += dt.timedelta(minutes=30)
        events.append((closing - dt.timedelta(minutes=cfg['close_exit_minutes']), 'daily_exit', session))
        events.append((closing, 'snapshot', session))
    return sorted(events, key=lambda e: (e[0], {'bar': 0, 'daily_exit': 1, 'entry': 2, 'snapshot': 3}[e[1]]))


def assumptions(cfg, slippage_bps):
    return [
        'Current Clef is applied retrospectively. Input candles are bounded by simulated time; pretraining knowledge of later events cannot be excluded.',
        f"Signal charts reproduce the configured {cfg['data_delay_minutes']}-minute delay and use completed candles of 30 minutes or longer.",
        'Execution bars use raw prices. Chart history is adjusted only for stock splits effective by the simulated cutoff, including volume. The run refuses ranges with non-cash corporate actions during the replay; dividends are disclosed but cash distributions are not modeled. Symbol mapping is as of the run start.',
        'Entries use a past IEX reference trade and first valid SIP ask within 10 seconds after the decision, with adverse slippage. These are simulated immediate fractional fills, not reconstructed Alpaca paper executions.',
        'Stops are approximated from completed 30-minute OHLC bars wholly after entry and recorded at bar end. The entry candle is excluded because its low may precede the fill; this can miss a real stop and overstate returns. Gaps fill at the worse of opening price and stop, plus adverse slippage. This is not the live 15-second software stop.',
        f"Daily-red exits are sampled once {cfg['close_exit_minutes']} minutes before close using a past SIP trade, then a historical bid. Live repeated polling during the final minutes is not reproduced.",
        f'Adverse slippage is {slippage_bps} basis points per side, in addition to historical bid/ask spread where quotes are used. Regulatory fees, dividends, borrowing, and latency are not modeled.',
        'Open winners are marked at the final session close, not force-sold. Maximum drawdown is sampled at 30-minute marks, not tick-by-tick.',
        'No current asset/tradability filter is used as a proxy for historical eligibility. Data coverage failures are disclosed, never replaced by invented bars or symbols.'
    ]


class Backtest:
    def __init__(self, history, clef, cfg, items, week, start, end, directory, repeat_list=False, slippage_bps=10):
        self.history, self.clef, self.cfg, self.items = history, clef, cfg, items
        self.week, self.start, self.end = week, start, end
        self.directory, self.repeat, self.slippage = directory, repeat_list, slippage_bps / 10000
        directory.mkdir(parents=True, exist_ok=True)
        self.checkpoint = directory / 'checkpoint.json'
        saved = json.loads(self.checkpoint.read_text()) if self.checkpoint.exists() else {}
        self.index, self.decisions, self.exclusions = saved.get('index', 0), saved.get('decisions', []), saved.get('exclusions', {})
        self.coverage = saved.get('coverage_gaps', [])
        self.error = saved.get('error')
        self.portfolios = {mode: Portfolio(mode, float(cfg['capital_limit']), saved.get('portfolios', {}).get(mode)) for mode in ['fixed', 'intelligent']}
        self.market = Market(history, cfg)
        self.events = events_for(history.calendar(start, end), cfg)
        if not self.events:
            raise ValueError('The requested range has no trading sessions.')

    def save(self, status):
        saved = {'index': self.index, 'status': status, 'decisions': self.decisions, 'exclusions': self.exclusions,
                 'coverage_gaps': self.coverage, 'error': self.error,
                 'portfolios': {mode: p.state for mode, p in self.portfolios.items()}}
        temporary = self.checkpoint.with_suffix('.tmp')
        temporary.write_text(json.dumps(saved, indent=2))
        temporary.replace(self.checkpoint)
        report = {'status': status, 'start': self.start, 'end': self.end, 'watchlist_week': self.week,
                  'replay_version': 2, 'configuration': self.cfg | {'strategy': 'ab'},
                  'slippage_bps': self.slippage * 10000, 'watchlist': self.items,
                  'repeat_watchlist': self.repeat, 'processed_events': self.index, 'total_events': len(self.events),
                  'completed': self.index == len(self.events), 'error': self.error,
                  'assumptions': assumptions(self.cfg, self.slippage * 10000), 'exclusions': self.exclusions,
                  'coverage_gaps': self.coverage, 'watchlist_symbols': [i['symbol'] for i in self.items],
                  'corporate_action_audit': getattr(self.history, 'action_audit', {}),
                  'decisions': self.decisions, 'results': {mode: p.report() for mode, p in self.portfolios.items()}}
        (self.directory / 'report.json').write_text(json.dumps(report, indent=2))
        write_html(self.directory / 'report.html', report)
        return report

    def process_bar(self, instant):
        for portfolio in self.portfolios.values():
            for symbol, position in list(portfolio.state['holdings'].items()):
                rows = [r for r in self.history.series(symbol, '30Min') if timestamp(r['t']) + dt.timedelta(minutes=30) == instant]
                if not rows:
                    raise RuntimeError(f'Missing held-position candle for {symbol} at {instant}; replay paused.')
                row = rows[0]
                position['mark'] = row['c']
                if timestamp(row['t']) >= timestamp(position['entry_at']) and row['l'] <= position['stop']:
                    fill = min(row['o'], position['stop']) * (1 - self.slippage)
                    portfolio.sell(symbol, instant, fill, 'technical_stop_ohlc_approximation')
            portfolio.snapshot(instant)

    def process_daily_exit(self, instant):
        symbols = {symbol for p in self.portfolios.values() for symbol in p.state['holdings']}
        for symbol in sorted(symbols):
            reference = self.history.reference_trade(symbol, instant)
            quote = None
            for portfolio in self.portfolios.values():
                position = portfolio.state['holdings'].get(symbol)
                if position:
                    position['mark'] = reference
                    if reference < position['entry_price']:
                        quote = quote or self.history.execution_quote(symbol, instant)
                        portfolio.sell(symbol, timestamp(quote['t']), quote['bp'] * (1 - self.slippage), 'daily_red')

    def process_entries(self, instant):
        week = monday(instant.date()).isoformat()
        if not self.repeat and week != self.week:
            return
        logging.info('Replaying entry check %s', instant.isoformat())
        for item in self.items:
            symbol = item['symbol']
            if symbol in self.exclusions:
                continue
            eligible = [p for p in self.portfolios.values() if symbol not in p.state['holdings'] and [week, symbol] not in p.state['entries']]
            if not eligible or any(d['week'] == week and d['symbol'] == symbol and d['action'] == 'skip' for d in self.decisions):
                continue
            old = next((d for d in self.decisions if d['symbol'] == symbol and d['slot'] == instant.isoformat()), None)
            if old:
                result, key, reference = old['result'], old['cache_key'], old['reference_price']
            else:
                try:
                    bundle, cutoff = self.market.bundle(symbol, instant)
                    stops = stop_candidates(bundle)
                    if not stops:
                        raise RuntimeError('No valid technical stops.')
                    payload, images = make_request(symbol, item['annotation'], bundle, cutoff, stops, self.cfg['model'],
                                                   self.market.technical_context(bundle, cutoff), item.get('grade'), item.get('market_context'), item.get('rs_rating'))
                except (ValueError, RuntimeError) as error:
                    if isinstance(error, APIError):
                        if error.status != 404:
                            raise
                        self.exclusions[symbol] = {'reason': str(error), 'first_slot': instant.isoformat()}
                    else:
                        self.coverage.append({'symbol': symbol, 'slot': instant.isoformat(), 'stage': 'analysis', 'reason': str(error)})
                    self.save('running')
                    continue
                result, key = self.clef.evaluate(payload, stops)
                reference = bundle['30Min'][-1]['c']
                self.decisions.append({'week': week, 'symbol': symbol, 'slot': instant.isoformat(), 'action': result['action'],
                                       'result': result, 'cache_key': key, 'reference_price': reference})
                artifact = self.directory / 'analyses' / symbol / instant.strftime('%Y%m%d-%H%M')
                artifact.mkdir(parents=True, exist_ok=True)
                for frame, image in images.items():
                    (artifact / (frame + '.png')).write_bytes(image)
                (artifact / 'analysis.json').write_text(json.dumps({'state': payload['state'], 'questions': payload['questions'], 'result': result}, indent=2))
                self.save('running')
            if result['action'] != 'enter' or result['probability'] < self.cfg['entry_probability'] or result['confidence'] < self.cfg['entry_confidence'] or not result['stop_price']:
                continue
            try:
                trade = self.history.reference_trade(symbol, instant, feed='iex')
                quote = self.history.execution_quote(symbol, instant)
            except RuntimeError as error:
                if isinstance(error, APIError):
                    raise
                self.coverage.append({'symbol': symbol, 'slot': instant.isoformat(), 'stage': 'execution', 'reason': str(error)})
                self.save('running')
                continue
            stop, fill = result['stop_price'], quote['ap'] * (1 + self.slippage)
            if abs(trade / reference - 1) * 100 > self.cfg['max_entry_deviation_pct'] or not 0 < stop < trade or (1 - stop / trade) * 100 > self.cfg['max_stop_distance_pct'] or fill <= stop:
                continue
            for portfolio in eligible:
                amount = allocation(self.cfg | {'strategy': portfolio.name}, result['probability'])
                if amount:
                    portfolio.buy(symbol, timestamp(quote['t']), amount, fill, stop, week, key, result['probability'])
            self.save('running')

    def run(self):
        self.error = None
        while self.index < len(self.events):
            instant, kind, session = self.events[self.index]
            before = {mode: json.loads(json.dumps(p.state)) for mode, p in self.portfolios.items()}
            try:
                if kind == 'bar':
                    self.process_bar(instant)
                elif kind == 'daily_exit':
                    self.process_daily_exit(instant)
                elif kind == 'entry':
                    self.process_entries(instant)
                else:
                    for portfolio in self.portfolios.values():
                        portfolio.snapshot(instant)
            except Exception as error:
                # Entry events checkpoint per ticker; replaying uses saved decisions
                # and entry IDs. Other event types roll back before retrying.
                if kind != 'entry':
                    for mode, state in before.items():
                        self.portfolios[mode].state = state
                self.error = {'at': instant.isoformat(), 'event': kind, 'reason': str(error)}
                self.save('paused_quota' if isinstance(error, QuotaError) else 'paused_error')
                raise
            self.index += 1
            self.save('running')
        return self.save('complete_with_coverage_gaps' if self.exclusions or self.coverage else 'complete')


def write_html(path, report):
    import html
    rows = []
    for mode, result in report['results'].items():
        rows.append(f'<tr><td>{mode}</td><td>${result["final_equity"]:,.2f}</td><td>{result["return_pct"]:.2f}%</td><td>${result["realized_pnl"]:,.2f}</td><td>${result["unrealized_pnl"]:,.2f}</td><td>{result["entries"]}</td><td>{result["closed_trades"]}</td><td>{result["sampled_max_drawdown_pct"]:.2f}%</td></tr>')
    assumptions_html = ''.join('<li>' + html.escape(note) + '</li>' for note in report['assumptions'])
    detail = html.escape(json.dumps({'results': report['results'], 'exclusions': report['exclusions'],
                                    'coverage_gaps': report['coverage_gaps'], 'error': report['error']}, indent=2))
    points = [point for r in report['results'].values() for point in r['equity_curve']]
    chart = ''
    if len(points) > 1:
        times = [timestamp(p['at']).timestamp() for p in points]
        first, last = min(times), max(times)
        low, high = min(p['equity'] for p in points), max(p['equity'] for p in points)
        padding = max((high - low) * .1, 1)
        low, high = low - padding, high + padding
        lines = []
        for mode, color in [('fixed', '#58c6ff'), ('intelligent', '#f7b65a')]:
            line = ' '.join(f'{80 + 790 * (timestamp(p["at"]).timestamp() - first) / max(last - first, 1):.1f},{25 + 215 * (high - p["equity"]) / (high - low):.1f}' for p in report['results'][mode]['equity_curve'])
            lines.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{line}"/>')
        chart = f'<h2>Sampled equity</h2><p><span style="color:#58c6ff">Fixed $5,000</span> · <span style="color:#f7b65a">Confidence sizing</span></p><svg viewBox="0 0 900 280" role="img" aria-label="Historical simulated equity for both sizing policies"><text x="0" y="30" fill="#e4edf9">${high:,.0f}</text><text x="0" y="240" fill="#e4edf9">${low:,.0f}</text>{"".join(lines)}<text x="80" y="270" fill="#e4edf9">{html.escape(report["start"])}</text><text x="780" y="270" fill="#e4edf9">{html.escape(report["end"])}</text></svg>'
    progress = f'{report["processed_events"]} / {report["total_events"]} events processed'
    if not report['completed']:
        progress += '. Partial results: the requested period has not finished.'
    path.write_text(f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Clef historical replay</title><style>body{{font:16px system-ui;background:#101827;color:#e4edf9;margin:32px;max-width:1200px}}td,th{{padding:10px;border-bottom:1px solid #40506a;text-align:left}}li{{margin:12px 0}}pre{{white-space:pre-wrap;background:#182338;padding:20px}}h1{{font-size:30px}}svg{{width:100%;background:#182338}}.scroll{{overflow:auto}}</style><h1>Clef A/B historical replay</h1><p>{html.escape(report["start"])} through {html.escape(report["end"])} · {html.escape(report["status"])}</p><p>{html.escape(progress)}</p><p>Watchlist: {html.escape(", ".join(report["watchlist_symbols"]))}. Excluded symbols: {len(report["exclusions"])}. Missing analysis/execution samples: {len(report["coverage_gaps"])}.</p><div class="scroll"><table><tr><th>Sizing</th><th>Equity</th><th>Return</th><th>Realized P/L</th><th>Open P/L</th><th>Entries</th><th>Closed trades</th><th>Sampled drawdown</th></tr>{"".join(rows)}</table></div>{chart}<h2>Assumptions and limitations</h2><ul>{assumptions_html}</ul><details><summary>Trades, open holdings, equity points, and data coverage</summary><pre>{detail}</pre></details></html>')


def run_backtest(api, clef, cfg, root, file, start, end, repeat=False, slippage_bps=10):
    from .watchlist import parse_watchlist
    if any(dt.date.fromisoformat(day).isoformat() != day for day in [start, end]):
        raise ValueError('Dates must use YYYY-MM-DD format.')
    if not math.isfinite(slippage_bps) or not 0 <= slippage_bps <= 100:
        raise ValueError('Slippage must be between 0 and 100 basis points per side.')
    week, items = parse_watchlist(file, dt.date.fromisoformat(start), allow_historical=True)
    if start > end or start < week or dt.date.fromisoformat(end) >= dt.datetime.now(ET).date():
        raise ValueError('Use a historical range on or after the watchlist date, ending before today.')
    if not repeat and monday(dt.date.fromisoformat(start)).isoformat() != week:
        raise ValueError('Start within the original watchlist week, or explicitly choose --repeat-watchlist.')
    identity = fingerprint({'cfg': cfg | {'strategy': 'ab'}, 'items': items, 'week': week,
                            'start': start, 'end': end, 'repeat': repeat, 'slippage_bps': slippage_bps, 'replay_version': 2})[:20]
    directory = root / 'artifacts' / 'backtests' / identity
    history = History(api, root.parent / '.clef-backtest-data', start, end, [i['symbol'] for i in items] + ['QQQ'])
    with exclusive(directory / 'run.lock'):
        history.validate_actions()
        replay = Backtest(history, clef, cfg, items, week, start, end, directory, repeat, slippage_bps)
        logging.info('Historical replay: %s; checkpoints resume when the same command is rerun.', directory)
        try:
            report = replay.run()
        except (ValueError, RuntimeError, OSError) as error:
            raise RuntimeError(f'{error}\nReplay paused; partial report: {directory / "report.html"}. Rerun the same command to resume.') from error
    return directory, report
