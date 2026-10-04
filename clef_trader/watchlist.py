import datetime as dt
import json
import re
from .market import monday


def parse_watchlist(path, today):
    text = path.read_text()
    if path.suffix.lower() == '.json':
        data = json.loads(text)
    else:
        data = {'tickers': []}
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            match = re.fullmatch(r'#?\s*[Ww]eek\s*:\s*(\d{4}-\d{2}-\d{2})', line)
            if match:
                data['week'] = match[1]
                continue
            if line.startswith('#'):
                continue
            match = re.match(r'^([A-Za-z][A-Za-z0-9.\-]{0,14})\s*[:|]\s*(.+)', line)
            if match:
                data['tickers'].append({'symbol': match[1].upper(), 'annotation': match[2]})
            elif data['tickers']:
                data['tickers'][-1]['annotation'] += '\n' + line
            else:
                raise ValueError('Use SYMBOL: annotation, with a Week: YYYY-MM-DD line.')
    week = dt.date.fromisoformat(data['week'])
    if week.weekday() != 0 or week < monday(today):
        raise ValueError('Week must be the Monday of the current or a future week.')
    items = data['tickers']
    if not isinstance(items, list) or not 1 <= len(items) <= 50:
        raise ValueError('Provide 1 through 50 annotated tickers.')
    seen = set()
    normalized = []
    for item in items:
        symbol, annotation = item['symbol'].strip().upper(), item['annotation'].strip()
        if not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}', symbol) or symbol in seen:
            raise ValueError('Invalid or duplicate watchlist symbol.')
        if not 5 <= len(annotation) <= 4000:
            raise ValueError('Each annotation must contain 5 through 4,000 characters.')
        seen.add(symbol)
        normalized.append({'symbol': symbol, 'annotation': annotation})
    return week.isoformat(), normalized
