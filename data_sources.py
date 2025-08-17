import requests

BASE_URL = "https://api.binance.com/api/v3/klines"


def get_klines(symbol: str, interval: str = "1h", limit: int = 100):
    """
    Получение свечей (OHLCV) с Binance.
    symbol: торговая пара (например, BTCUSDT)
    interval: интервал свечей (1m, 5m, 15m, 1h, 4h, 1d)
    limit: количество свечей
    """
    url = f"{BASE_URL}?symbol={symbol}&interval={interval}&limit={limit}"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    raw = resp.json()

    # Преобразуем в список словарей
    data = []
    for candle in raw:
        data.append({
            "time": candle[0],
            "open": float(candle[1]),
            "high": float(candle[2]),
            "low": float(candle[3]),
            "close": float(candle[4]),
            "volume": float(candle[5]),
        })
    return data
