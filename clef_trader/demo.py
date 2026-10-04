import datetime as dt
import json
import math
from .charts import render
from .config import allocation
from .market import ET, combine, stop_candidates


def bundle():
    def rows(count, spacing, start):
        result = []
        previous = 180.0
        for i in range(count):
            close = 180 + i * .18 + math.sin(i / 5) * 2
            result.append({'t': (start + spacing * i).isoformat(), 'o': round(previous, 3),
                           'h': round(max(previous, close) + .7, 3), 'l': round(min(previous, close) - .8, 3),
                           'c': round(close, 3), 'v': int(100000 + i * 2000 + (1 + math.sin(i)) * 20000)})
            previous = close
        return result
    thirty = rows(90, dt.timedelta(minutes=30), dt.datetime(2026, 9, 28, 9, 30, tzinfo=ET))
    daily = rows(150, dt.timedelta(days=1), dt.datetime(2026, 4, 1, tzinfo=ET))
    hour = [combine(thirty[i:i + 2], dt.datetime.fromisoformat(thirty[i]['t'])) for i in range(0, 90, 2)]
    week = [combine(daily[i:i + 5], dt.datetime.fromisoformat(daily[i]['t'])) for i in range(0, 150, 5)]
    return {'30Min': thirty, '1Hour': hour, '1Day': daily, '1Week': week}


def generate(root, cfg):
    output = root / 'artifacts' / 'demo'
    output.mkdir(parents=True, exist_ok=True)
    data = bundle()
    for frame, bars in data.items():
        (output / f'{frame}.png').write_bytes(render('DEMO (synthetic)', frame, bars))
    report = {'synthetic': True, 'strategy': cfg['strategy'], 'stop_candidates': stop_candidates(data),
              'sizing_examples': {str(p): allocation(cfg, p) for p in [.72, .85, .92, .97]},
              'note': 'Synthetic rendering demonstration only. No market API, model call, or order.'}
    (output / 'demo.json').write_text(json.dumps(report, indent=2))
    return output, report
