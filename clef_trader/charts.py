import io
from PIL import Image, ImageDraw, ImageFont
from .market import timestamp


def render(symbol, timeframe, rows):
    from .features import ema
    overlays = []
    for period, color in ([(21, '#f5bf58'), (50, '#63b8fb')] if timeframe == '1Day' else [(8, '#c69afb')] if timeframe == '1Week' else []):
        overlays.append((period, color, ema([r['c'] for r in rows], period)[-90:]))
    rows = rows[-90:]
    if not rows:
        raise ValueError('Cannot render an empty chart.')
    image = Image.new('RGB', (1200, 760), '#0e1724')
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=18)
    small = ImageFont.load_default(size=14)
    draw.text((28, 22), f'{symbol}  |  {timeframe}  |  completed candles  |  consolidated SIP (15+ min delay)', fill='#f4f7ff', font=font)
    left, right, top, bottom = 45, 1090, 90, 525
    low, high = min(r['l'] for r in rows), max(r['h'] for r in rows)
    pad = max((high - low) * .08, high * .001)
    low, high = low - pad, high + pad
    def y(price):
        return bottom - (price - low) / (high - low) * (bottom - top)
    step = (right - left) / len(rows)
    for j, (period, color, values) in enumerate(overlays):
        draw.text((45 + j * 200, 58), f'EMA {period}: {values[-1]:.2f}', fill=color, font=small)
        points = [(left + (i + .5) * step, y(value)) for i, value in enumerate(values)]
        # Clip the moving-average line to the price panel.
        visible = [(x, max(top, min(bottom, height))) for x, height in points]
        if len(visible) > 1:
            draw.line(visible, fill=color, width=2)
    for i in range(6):
        price = low + (high - low) * i / 5
        height = y(price)
        draw.line((left, height, right, height), fill='#26384e')
        draw.text((right + 12, height - 8), f'{price:.2f}', fill='#c2cfdf', font=small)
    volume_max = max(r['v'] for r in rows) or 1
    for i, row in enumerate(rows):
        x = left + (i + .5) * step
        color = '#43d9a3' if row['c'] >= row['o'] else '#fa7683'
        draw.line((x, y(row['l']), x, y(row['h'])), fill=color, width=1)
        a, b = sorted([y(row['o']), y(row['c'])])
        width = max(1, step * .3)
        draw.rectangle((x - width, a, x + width, max(a + 1, b)), fill=color)
        draw.rectangle((x - width, 685 - row['v'] / volume_max * 115, x + width, 685), fill=color)
    draw.text((left, 547), f'Volume (full-market consolidated trades)  |  peak {volume_max:,.0f}', fill='#c2cfdf', font=small)
    for i in sorted(set([0, len(rows) // 3, len(rows) * 2 // 3, len(rows) - 1])):
        text = timestamp(rows[i]['t']).strftime('%m/%d %H:%M')
        draw.text((min(left + i * step, right - 110), 699), text, fill='#c2cfdf', font=small)
    draw.text((left, 733), 'Chart timestamps retain source timezone. Decisions also receive exact OHLCV values.', fill='#869bb4', font=small)
    output = io.BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()
