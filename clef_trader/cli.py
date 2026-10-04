import argparse
import atexit
import datetime as dt
import json
import logging
import os
from pathlib import Path
import sys
import time

from .api import Alpaca
from .config import cache_dir, load_config, load_env
from .decision import Clef, make_request
from .engine import Engine
from .market import ET, Market, session_time, stop_candidates
from .store import Store, exclusive
from .watchlist import parse_watchlist


def compare(root, other):
    result = {}
    for path in [root, other]:
        state = path / 'state' / 'paper.sqlite3'
        if not state.exists():
            result[path.name] = {'status': 'No paper run recorded yet.'}
            continue
        store = Store(state)
        snapshots = store.rows('SELECT * FROM snapshots ORDER BY at')
        pnl = snapshots[-1]['equity'] - snapshots[0]['equity'] if snapshots else None
        result[path.name] = {
            'strategy': load_config(path)['strategy'],
            'equity_change_since_first_snapshot': pnl,
            'latest_snapshot': dict(snapshots[-1]) if snapshots else None,
            'owned': {s: str(q) for s, q in store.owned().items()},
            'filled_entries': len(store.rows("SELECT 1 FROM orders WHERE side='buy' AND CAST(filled_qty AS REAL)>0")),
            'filled_exits': len(store.rows("SELECT 1 FROM orders WHERE side='sell' AND CAST(filled_qty AS REAL)>0")),
            'note': 'Account equity includes all activity. Compare equal starting balances and matching decision cache keys.'
        }
        store.db.close()
    return result


def doctor(api, clef, cfg, root):
    diagnostic_store = Store(root / 'state' / 'paper.sqlite3')
    try:
        account = Engine(api, diagnostic_store, clef, cfg, root).initialize()
    finally:
        diagnostic_store.db.close()
    print(f"Paper account connected: {account['id']}; equity ${float(account['equity']):,.2f}")
    now = dt.datetime.now(ET)
    sessions = api.calendar((now.date() - dt.timedelta(days=10)).isoformat(), now.date().isoformat())
    completed = [s for s in sessions if session_time(s, 'close') < now - dt.timedelta(minutes=cfg['data_delay_minutes'])]
    if not completed:
        raise RuntimeError('No completed session available for data/image diagnostics.')
    slot = session_time(completed[-1], 'close') + dt.timedelta(minutes=cfg['data_delay_minutes'], seconds=10)
    market = Market(api, cfg)
    bundle, cutoff = market.bundle('AAPL', slot)
    payload, _ = make_request('AAPL', 'Diagnostic only: assess whether there is clear price-action confirmation. No orders will be sent.', bundle, cutoff, stop_candidates(bundle), cfg['model'], market.technical_context(bundle, cutoff))
    result, _ = clef.evaluate(payload, stop_candidates(bundle))
    print(f"Consolidated data, four chart images, and {cfg['model']} response validated; action={result['action']}.")
    print('No order was submitted. Diagnostics count against the free AI quota.')


def main():
    parser = argparse.ArgumentParser(description='Local, paper-only Clef chart trader')
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[1], help='Project root with config.json and .env')
    sub = parser.add_subparsers(dest='command', required=True)
    watch = sub.add_parser('watchlist', help='Import an annotated weekly JSON or text watchlist')
    watch.add_argument('file', type=Path)
    preview = sub.add_parser('preview-watchlist', help='Parse a watchlist, including archived weeks, without activating it')
    preview.add_argument('file', type=Path)
    backtest = sub.add_parser('backtest', help='Read-only historical A/B replay; never sends broker orders')
    backtest.add_argument('file', type=Path, help='Archived annotated watchlist')
    backtest.add_argument('--start', required=True, help='First replay date, YYYY-MM-DD')
    backtest.add_argument('--end', required=True, help='Last replay date, YYYY-MM-DD; open winners are marked at close')
    backtest.add_argument('--repeat-watchlist', action='store_true', help='Explicitly allow entries from this list in later weeks')
    backtest.add_argument('--slippage-bps', type=float, default=10, help='Adverse slippage per side, in basis points (default 10)')
    for name in ['run', 'once', 'doctor', 'status', 'demo', 'pause', 'resume']:
        sub.add_parser(name)
    report = sub.add_parser('compare', help='Compare recorded A/B account results')
    report.add_argument('other_project', type=Path)
    args = parser.parse_args()
    root = args.project.expanduser().resolve()
    cfg = load_config(root)
    load_env(root / '.env')
    if args.command == 'backtest':
        # Do not open, bind, import into, or modify the paper-trading ledger.
        from .backtest import run_backtest
        for name in ('APCA_API_KEY_ID', 'APCA_API_SECRET_KEY', 'CLOUDFLARE_ACCOUNT_ID', 'CLOUDFLARE_API_TOKEN'):
            if not os.environ.get(name):
                raise ValueError(f'Missing {name}. Configure .env locally; historical replay needs Alpaca data and Clef access.')
        logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
        clef = Clef(cache_dir(root), cfg)
        atexit.register(clef.db.close)
        directory, result = run_backtest(Alpaca(), clef, cfg, root, args.file.expanduser().resolve(),
                                        args.start, args.end, args.repeat_watchlist, args.slippage_bps)
        summary = {mode: {k: v for k, v in r.items() if k not in {'trades', 'open_holdings', 'equity_curve', 'skipped_allocations'}}
                   for mode, r in result['results'].items()}
        print(json.dumps({'status': result['status'], 'results': summary,
                          'excluded_symbols': result['exclusions'], 'coverage_gaps': len(result['coverage_gaps'])}, indent=2))
        print(f'Report: {directory / "report.html"}\nDetailed results: {directory / "report.json"}')
        return
    (root / 'state').mkdir(parents=True, exist_ok=True)
    store = Store(root / 'state' / 'paper.sqlite3')
    atexit.register(store.db.close)
    if args.command == 'preview-watchlist':
        week, items = parse_watchlist(args.file.expanduser().resolve(), dt.datetime.now(ET).date(), allow_historical=True)
        print(json.dumps({'week': week, 'tickers': items, 'activated': False}, indent=2))
    elif args.command == 'watchlist':
        week, items = parse_watchlist(args.file.expanduser().resolve(), dt.datetime.now(ET).date())
        if store.rows('SELECT 1 FROM decisions WHERE week=? LIMIT 1', (week,)):
            raise ValueError('This week has already been analyzed. Preserve the audit trail; import a future week.')
        if week == (dt.datetime.now(ET).date() - dt.timedelta(days=dt.datetime.now(ET).weekday())).isoformat():
            # Serialize current-week import with active trading to avoid mid-scan changes.
            with exclusive(root / 'state' / 'run.lock'):
                store.write('INSERT OR REPLACE INTO watchlists VALUES (?,?)', (week, json.dumps(items)))
        else:
            store.write('INSERT OR REPLACE INTO watchlists VALUES (?,?)', (week, json.dumps(items)))
        print(f"Saved {len(items)} tickers for {week}; strategy={cfg['strategy']}.")
    elif args.command == 'status':
        clef = Clef(cache_dir(root), cfg)
        atexit.register(clef.db.close)
        usage = [dict(zip(['day', 'estimated_neurons', 'last_request_neurons'], row))
                 for row in clef.db.execute('SELECT day,neurons,last_request FROM usage ORDER BY day DESC LIMIT 7')]
        print(json.dumps({'strategy': cfg['strategy'], 'entries_paused': (root / 'state' / 'pause').exists(),
                          'shared_ai_usage': usage, **store.summary()}, indent=2))
    elif args.command in {'pause', 'resume'}:
        flag = root / 'state' / 'pause'
        flag.write_text('Entries paused\n') if args.command == 'pause' else flag.unlink(missing_ok=True)
        print('New entries paused; exits remain active.' if args.command == 'pause' else 'New entries enabled.')
    elif args.command == 'demo':
        from .demo import generate
        output, result = generate(root, cfg)
        print(json.dumps(result, indent=2))
        print(f'Charts: {output}')
    elif args.command == 'compare':
        print(json.dumps(compare(root, args.other_project.expanduser().resolve()), indent=2))
    else:
        for name in ('APCA_API_KEY_ID', 'APCA_API_SECRET_KEY', 'CLOUDFLARE_ACCOUNT_ID', 'CLOUDFLARE_API_TOKEN'):
            if not os.environ.get(name):
                raise ValueError(f'Missing {name}. Copy .env.example to .env and configure locally.')
        logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                            handlers=[logging.StreamHandler(), logging.FileHandler(root / 'state' / 'trader.log')])
        api, clef = Alpaca(), Clef(cache_dir(root), cfg)
        atexit.register(clef.db.close)
        with exclusive(root / 'state' / 'run.lock'):
            if args.command == 'doctor':
                doctor(api, clef, cfg, root)
                return
            engine = Engine(api, store, clef, cfg, root)
            engine.initialize()
            while True:
                try:
                    engine.tick(entries=not (root / 'state' / 'pause').exists())
                except Exception as error:
                    engine.log('ERROR', f'Tick failed: {type(error).__name__}: {error}')
                    if args.command == 'once':
                        raise
                if args.command == 'once':
                    break
                time.sleep(cfg['poll_seconds'])


def entrypoint():
    try:
        main()
    except KeyboardInterrupt:
        print('\nStopped.', file=sys.stderr)
    except (ValueError, RuntimeError, KeyError) as error:
        print(f'Error: {error}', file=sys.stderr)
        raise SystemExit(1) from None
