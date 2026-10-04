from .market import ET, monday, timestamp


def ema(values, period):
    if not values:
        return []
    result = [float(values[0])]
    alpha = 2 / (period + 1)
    for value in values[1:]:
        result.append(alpha * float(value) + (1 - alpha) * result[-1])
    return result


def context(bundle, benchmark_daily):
    daily, weekly = bundle['1Day'], bundle['1Week']
    benchmark = {timestamp(r['t']).astimezone(ET).date(): r['c'] for r in benchmark_daily}
    ratios = [{'date': timestamp(r['t']).astimezone(ET).date().isoformat(), 'ratio': r['c'] / benchmark[timestamp(r['t']).astimezone(ET).date()]}
              for r in daily if timestamp(r['t']).astimezone(ET).date() in benchmark]
    ratios = ratios[-252:]
    benchmark_weeks = {}
    for row in benchmark_daily:
        benchmark_weeks[monday(timestamp(row['t']).astimezone(ET).date())] = row['c']
    weekly_ratios = [{'week': timestamp(row['t']).astimezone(ET).date().isoformat(),
                      'ratio': row['c'] / benchmark_weeks[timestamp(row['t']).astimezone(ET).date()]}
                     for row in weekly if timestamp(row['t']).astimezone(ET).date() in benchmark_weeks][-52:]
    values = {
        'glossary': {'HTF': 'Often high tight flag in this watchlist; can also mean higher timeframe. Resolve from chart and annotation; do not assume.',
                     'blue skies': 'Price discovery above chart resistance; not proof of an all-time high without sufficient history.',
                     'RS': 'Relative strength versus QQQ, distinct from RSI.',
                     'RS new high bp': 'Relative strength making a new high before price.',
                     '8 week': '8-period EMA on completed weekly candles.',
                     'ER gap': 'Earnings-related gap; price bars alone do not verify the earnings event.',
                     '21ema': '21-period daily EMA unless the annotation explicitly specifies another timeframe.'},
        'daily_ema_21': round(ema([r['c'] for r in daily], 21)[-1], 4),
        'daily_ema_50': round(ema([r['c'] for r in daily], 50)[-1], 4),
        'weekly_ema_8': round(ema([r['c'] for r in weekly], 8)[-1], 4) if weekly else None,
        'daily_high_252_sessions': max(r['h'] for r in daily[-252:]),
        'daily_252_sessions_available': len(daily[-252:]),
        'relative_strength': {'benchmark': 'QQQ', 'definition': 'ticker daily close divided by QQQ daily close on matching completed sessions',
                              'recent_daily_ratios': ratios[-40:],
                              'latest_ratio': ratios[-1]['ratio'] if ratios else None,
                              'high_in_available_252_session_window': max(r['ratio'] for r in ratios) if ratios else None,
                              'available_sessions': len(ratios)},
        'limits': 'Do not infer verified earnings dates, IPO history, or true all-time highs from this limited price history. Watchlist grades express the author’s opinion; they do not replace chart confirmation or model confidence.'
    }
    values['relative_strength']['recent_weekly_ratios'] = weekly_ratios
    values['relative_strength']['weekly_high_in_available_52_week_window'] = max(r['ratio'] for r in weekly_ratios) if weekly_ratios else None
    return values
