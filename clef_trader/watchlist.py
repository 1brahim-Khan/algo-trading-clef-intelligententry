import datetime as dt
import json
import re
from .market import monday


MONTHS = 'January February March April May June July August September October November December'.split()


def pasted_watchlist(text):
    heading = re.search(r'(' + '|'.join(MONTHS) + r')\s+(\d{1,2})(?:st|nd|rd|th)?\s+(\d{4})\s+Weekly\s+Watchlist', text, re.I)
    explicit = re.search(r'Week\s*:\s*(\d{4}-\d{2}-\d{2})', text, re.I)
    if heading:
        month = next(i + 1 for i, name in enumerate(MONTHS) if name.lower() == heading[1].lower())
        week = dt.date(int(heading[3]), month, int(heading[2])).isoformat()
    elif explicit:
        week = explicit[1]
    else:
        raise ValueError('Include a dated Weekly Watchlist heading or Week: YYYY-MM-DD.')
    matches = list(re.finditer(r'\$([A-Z][A-Z0-9.\-]{0,14})\b', text))
    items, context = [], ''
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[match.end():end].strip()
        grade = re.search(r'(?:\bGrade\s+([A-F][+-]?)|\b([A-F][+-]?))(?=\s*(?:$|With\b|Market\s+context\b|Overall\b))', body, re.I)
        annotation = body[:grade.end()].strip() if grade else body
        if not annotation:
            raise ValueError('Every ticker needs an annotation.')
        item = {'symbol': match[1], 'annotation': annotation}
        if grade:
            item['grade'] = (grade[1] or grade[2]).upper()
        items.append(item)
        if grade and body[grade.end():].strip():
            # Treat the trailing market paragraph as context, including any $QQQ
            # reference inside it. It is not another entry target.
            absolute_end = match.end() + len(text[match.end():end]) - len(text[match.end():end].lstrip()) + grade.end()
            context = text[absolute_end:].strip()
            break
    return {'week': week, 'tickers': items, 'market_context': context}


def parse_watchlist(path, today, allow_historical=False):
    text = path.read_text()
    if path.suffix.lower() == '.json':
        data = json.loads(text)
    elif '$' in text:
        data = pasted_watchlist(text)
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
    if week.weekday() != 0 or (not allow_historical and week < monday(today)):
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
        result = {'symbol': symbol, 'annotation': annotation}
        if item.get('grade'):
            result['grade'] = str(item['grade'])
        if data.get('market_context'):
            result['market_context'] = str(data['market_context'])[:8000]
        normalized.append(result)
    return week.isoformat(), normalized
