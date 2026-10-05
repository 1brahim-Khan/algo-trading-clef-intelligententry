import json
import os
import urllib.error
import urllib.parse
import urllib.request


class APIError(RuntimeError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(f'HTTP {status}: {message[:500]}')


def request_json(url, headers, payload=None, method=None, timeout=20):
    req = urllib.request.Request(url, headers=headers,
                                 data=None if payload is None else json.dumps(payload).encode(),
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status == 204:
                return None
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise APIError(error.code, error.read().decode(errors='replace')) from None


class Alpaca:
    """Paper endpoint is hardcoded. No live-trading option exists."""
    def __init__(self):
        self.headers = {'APCA-API-KEY-ID': os.environ['APCA_API_KEY_ID'],
                        'APCA-API-SECRET-KEY': os.environ['APCA_API_SECRET_KEY'],
                        'Content-Type': 'application/json'}

    def trading(self, path, payload=None, method=None):
        return request_json('https://paper-api.alpaca.markets/v2' + path, self.headers, payload, method)

    def data(self, path, params):
        return request_json('https://data.alpaca.markets/v2' + path + '?' + urllib.parse.urlencode(params), self.headers)

    def corporate_actions(self, params):
        return request_json('https://data.alpaca.markets/v1/corporate-actions?' + urllib.parse.urlencode(params), self.headers)

    def clock(self):
        return self.trading('/clock')

    def account(self):
        return self.trading('/account')

    def positions(self):
        return self.trading('/positions')

    def open_orders(self):
        orders = self.trading('/orders?status=open&limit=500&nested=true')
        if len(orders) >= 500:
            raise RuntimeError('Too many open orders to verify ownership safely.')
        return orders

    def find_order(self, client_id):
        try:
            return self.trading('/orders:by_client_order_id?' + urllib.parse.urlencode({'client_order_id': client_id}))
        except APIError as error:
            if error.status == 404:
                return None
            raise

    def asset(self, symbol):
        return self.trading('/assets/' + urllib.parse.quote(symbol))

    def calendar(self, start, end):
        return self.trading('/calendar?' + urllib.parse.urlencode({'start': start, 'end': end}))

    def submit(self, payload):
        return self.trading('/orders', payload)

    def cancel(self, order_id):
        return self.trading('/orders/' + urllib.parse.quote(order_id), method='DELETE')

    def latest_trade(self, symbol):
        return self.data('/stocks/' + urllib.parse.quote(symbol) + '/trades/latest', {'feed': 'iex'})['trade']
