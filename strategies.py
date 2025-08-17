from indicators import sma, rsi


def check_signals(data, symbol):
    """
    Проверка сигналов по SMA и RSI.
    data: список свечей (dict)
    symbol: пара (например, BTCUSDT)
    """
    closes = [c["close"] for c in data]

    sma_fast = sma(closes, 7)
    sma_slow = sma(closes, 25)
    rsi_val = rsi(closes, 14)

    signals = []

    # Проверка пересечения SMA
    if sma_fast and sma_slow:
        if sma_fast > sma_slow:
            signals.append("📈 SMA: тренд вверх (возможная покупка)")
        elif sma_fast < sma_slow:
            signals.append("📉 SMA: тренд вниз (возможная продажа)")

    # Проверка RSI
    if rsi_val:
        if rsi_val < 30:
            signals.append("🔵 RSI ниже 30 → перепроданность (возможный рост)")
        elif rsi_val > 70:
            signals.append("🔴 RSI выше 70 → перекупленность (возможное падение)")

    return signals
