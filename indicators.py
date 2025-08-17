import numpy as np


def sma(values, period=14):
    """
    Простое скользящее среднее (Simple Moving Average)
    """
    if len(values) < period:
        return None
    return np.mean(values[-period:])


def rsi(values, period=14):
    """
    Индекс относительной силы (Relative Strength Index)
    """
    if len(values) < period + 1:
        return None

    deltas = np.diff(values)
    seed = deltas[:period]
    up = seed[seed >= 0].sum() / period
    down = -seed[seed < 0].sum() / period if any(seed < 0) else 0

    rs = up / down if down != 0 else 0
    rsi_series = np.zeros_like(values)
    rsi_series[:period] = 100. - 100. / (1. + rs)

    for i in range(period, len(values)):
        delta = deltas[i - 1]
        upval = max(delta, 0)
        downval = -min(delta, 0)

        up = (up * (period - 1) + upval) / period
        down = (down * (period - 1) + downval) / period

        rs = up / down if down != 0 else 0
        rsi_series[i] = 100. - 100. / (1. + rs)

    return rsi_series[-1]
