import json
import math
import os
from pathlib import Path


def load_env(path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or not key.strip().isidentifier():
            raise ValueError('Invalid .env entry; use KEY=value lines.')
        os.environ.setdefault(key.strip(), value.strip().strip('\"\''))


def load_config(root):
    cfg = json.loads((root / 'config.json').read_text())
    if cfg['strategy'] not in {'fixed', 'intelligent'}:
        raise ValueError('strategy must be fixed or intelligent')
    ranges = {'capital_limit': (1, 100000), 'fixed_notional': (1, 10000),
              'entry_probability': (0.5, 1), 'entry_confidence': (0, 1),
              'data_delay_minutes': (15, 60), 'close_exit_minutes': (1, 15),
              'entry_cutoff_minutes': (15, 120), 'max_entry_deviation_pct': (0.1, 10),
              'max_stop_distance_pct': (0.1, 30), 'poll_seconds': (5, 60),
              'max_ai_calls_per_utc_day': (1, 1000), 'max_estimated_neurons_per_utc_day': (1, 10000)}
    for name, (low, high) in ranges.items():
        number = cfg[name]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or not low <= number <= high:
            raise ValueError(f'Invalid {name}: expected {low} through {high}.')
    if cfg['model'] not in {'clef', 'clef-flash'}:
        raise ValueError('Only Clef models are supported.')
    tiers = cfg['confidence_tiers']
    if not tiers or any(len(t) != 2 or not 0 <= t[0] <= 1 or not 1 <= t[1] <= 10000 for t in tiers):
        raise ValueError('Invalid confidence tiers.')
    if tiers != sorted(tiers) or any(a[0] >= b[0] or a[1] > b[1] for a, b in zip(tiers, tiers[1:])):
        raise ValueError('Confidence and allocation tiers must increase.')
    return cfg


def cache_dir(root):
    return Path(os.environ.get('CLEF_CACHE_DIR', str(root.parent / '.clef-shared-cache'))).expanduser().resolve()


def allocation(cfg, probability):
    if cfg['strategy'] == 'fixed':
        return float(cfg['fixed_notional'])
    eligible = [amount for threshold, amount in cfg['confidence_tiers'] if probability >= threshold]
    return float(eligible[-1]) if eligible else 0.0
