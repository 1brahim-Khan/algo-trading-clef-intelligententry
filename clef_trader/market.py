import datetime as dt
import math
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
UTC = dt.timezone.utc


def timestamp(value):
    result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Market timestamps must have timezone information.')
    return result


def monday(day):
    return day - dt.timedelta(days=day.weekday())


def session_time(session, key):
    return dt.datetime.fromisoformat(session['date'] + 'T' + session[key]).replace(tzinfo=ET)


def combine(rows, start):
    return {'t': start.isoformat(), 'o': rows[0]['o'], 'h': max(r['h'] for r in rows),
            'l': min(r['l'] for r in rows), 'c': rows[-1]['c'], 'v': sum(r['v'] for r in rows)}


def clean_bars(rows):
    result = []
    for row in rows:
        timestamp(row['t'])
        if any(isinstance(row[k], bool) or not math.isfinite(float(row[k])) for k in ('o', 'h', 'l', 'c', 'v')):
            raise ValueError('Non-finite market data.')
        if min(row[k] for k in ('o', 'h', 'l', 'c')) <= 0 or row['v'] < 0 or row['l'] > min(row['o'], row['c']) or row['h'] < max(row['o'], row['c']):
            raise ValueError('Invalid OHLCV data.')
        result.append({k: row[k] for k in ('t', 'o', 'h', 'l', 'c', 'v')})
    return sorted({r['t']: r for r in result}.values(), key=lambda r: timestamp(r['t']))


def hourly(rows, sessions):
    groups = {}
    for row in rows:
        instant = timestamp(row['t']).astimezone(ET)
        session = sessions[instant.date().isoformat()]
        opening = session_time(session, 'open')
        start = opening + dt.timedelta(hours=int((instant - opening).total_seconds() // 3600))
        groups.setdefault(start, []).append(row)
    # Never pretend a half-hour candle is a completed hourly candle.
    return [combine(group, start) for start, group in sorted(groups.items())
            if len(group) == 2 and timestamp(group[1]['t']) - timestamp(group[0]['t']) == dt.timedelta(minutes=30)]


def weekly(rows, current_week):
    groups = {}
    for row in rows:
        day = timestamp(row['t']).astimezone(ET).date()
        start = monday(day)
        if start < current_week:
            groups.setdefault(start, []).append(row)
    return [combine(group, dt.datetime.combine(start, dt.time(), ET)) for start, group in sorted(groups.items())]


def stop_candidates(bundle):
    rows = bundle['30Min']
    price = float(rows[-1]['c'])
    tr = [max(r['h'] - r['l'], abs(r['h'] - prior['c']), abs(r['l'] - prior['c'])) for prior, r in zip(rows, rows[1:])]
    atr = sum(tr[-14:]) / min(14, len(tr)) if tr else price * .02
    choices = {'swing_30m': min(r['l'] for r in rows[-8:]) - .1 * atr,
               'volatility': price - 2 * atr}
    if bundle['1Day']:
        choices['daily_support'] = min(r['l'] for r in bundle['1Day'][-5:]) - .1 * atr
    return {key: round(value, 2 if value >= 1 else 4) for key, value in choices.items()
            if 0 < value < price * .998}


class Market:
    def __init__(self, api, cfg):
        self.api, self.cfg = api, cfg
        self.sessions_cache = {}

    def sessions(self, start, end):
        key = (str(start), str(end))
        if key not in self.sessions_cache:
            self.sessions_cache[key] = {s['date']: s for s in self.api.calendar(str(start), str(end))}
        return self.sessions_cache[key]

    def bars(self, symbol, timeframe, start, end):
        params = {'timeframe': timeframe, 'start': start.isoformat(), 'end': end.isoformat(),
                  'feed': 'sip', 'adjustment': 'split', 'limit': 10000, 'sort': 'asc'}
        result, pages = [], set()
        while True:
            page = self.api.data('/stocks/' + symbol + '/bars', params)
            result.extend(page.get('bars') or [])
            token = page.get('next_page_token')
            if not token:
                break
            if token in pages or len(pages) > 20:
                raise RuntimeError('Unexpected market-data pagination.')
            pages.add(token)
            params['page_token'] = token
        return clean_bars(result)

    def bundle(self, symbol, slot):
        cutoff = slot - dt.timedelta(minutes=self.cfg['data_delay_minutes'], seconds=10)
        start = dt.datetime.combine(cutoff.date() - dt.timedelta(days=35), dt.time(), ET)
        sessions = self.sessions(start.date(), cutoff.date())
        intraday = []
        for row in self.bars(symbol, '30Min', start, cutoff):
            instant = timestamp(row['t']).astimezone(ET)
            session = sessions.get(instant.date().isoformat())
            if not session:
                continue
            opening, closing = session_time(session, 'open'), session_time(session, 'close')
            if opening <= instant and instant + dt.timedelta(minutes=30) <= min(closing, cutoff):
                intraday.append(row)
        if len(intraday) < 20:
            raise RuntimeError('Insufficient completed 30-minute candles.')
        last_end = timestamp(intraday[-1]['t']) + dt.timedelta(minutes=30)
        if cutoff - last_end > dt.timedelta(minutes=5):
            raise RuntimeError('Latest completed consolidated candle is stale; entry skipped.')
        daily_start = dt.datetime.combine(cutoff.date() - dt.timedelta(days=750), dt.time(), ET)
        daily = [r for r in self.bars(symbol, '1Day', daily_start, cutoff)
                 if timestamp(r['t']).astimezone(ET).date() < cutoff.date()]
        if len(daily) < 30:
            raise RuntimeError('Insufficient completed daily candles.')
        result = {'30Min': intraday[-100:], '1Hour': hourly(intraday, sessions)[-100:],
                  '1Day': daily[-180:], '1Week': weekly(daily, monday(cutoff.date()))[-100:]}
        return result, cutoff
