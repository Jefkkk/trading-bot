# -*- coding: utf-8 -*-
import numpy as np
from typing import List, Tuple

def ema(prices: List[float], period: int) -> List[float]:
    if len(prices) < period:
        return []
    k = 2.0 / (period + 1)
    result = [sum(prices[:period]) / period]
    for p in prices[period:]:
        result.append(p * k + result[-1] * (1 - k))
    return result

def sma(prices: List[float], period: int) -> List[float]:
    if len(prices) < period:
        return []
    return [sum(prices[i:i+period]) / period for i in range(len(prices) - period + 1)]

def rsi(prices: List[float], period: int = 14) -> List[float]:
    if len(prices) < period + 1:
        return []
    deltas = [prices[i+1] - prices[i] for i in range(len(prices) - 1)]
    gains  = [max(d, 0.0) for d in deltas]
    losses = [abs(min(d, 0.0)) for d in deltas]
    avg_g  = sum(gains[:period])  / period
    avg_l  = sum(losses[:period]) / period
    result = []
    for i in range(period, len(deltas)):
        avg_g = (avg_g * (period - 1) + gains[i])  / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        rs = avg_g / avg_l if avg_l > 0 else 100.0
        result.append(100.0 - (100.0 / (1.0 + rs)))
    return result

def macd(prices: List[float], fast: int = 12, slow: int = 26,
         signal_period: int = 9) -> Tuple[List[float], List[float], List[float]]:
    ef = ema(prices, fast)
    es = ema(prices, slow)
    n  = min(len(ef), len(es))
    macd_line   = [ef[-(n-i)] - es[-(n-i)] for i in range(n)]
    signal_line = ema(macd_line, signal_period)
    offset      = len(macd_line) - len(signal_line)
    histogram   = [macd_line[offset + i] - signal_line[i] for i in range(len(signal_line))]
    return macd_line, signal_line, histogram

def bollinger_bands(prices: List[float], period: int = 20,
                    std_dev: float = 2.0) -> Tuple[List[float], List[float], List[float]]:
    mid = sma(prices, period)
    if not mid:
        return [], [], []
    upper, lower = [], []
    for i, m in enumerate(mid):
        std = float(np.std(prices[i:i + period]))
        upper.append(m + std_dev * std)
        lower.append(m - std_dev * std)
    return upper, mid, lower

def bollinger_width(prices: List[float], period: int = 20) -> List[float]:
    upper, mid, lower = bollinger_bands(prices, period)
    return [(upper[i] - lower[i]) / mid[i] if mid[i] > 0 else 0
            for i in range(len(mid))]

def atr(highs: List[float], lows: List[float], closes: List[float],
        period: int = 14) -> List[float]:
    if len(closes) < period + 1:
        return []
    trs = [max(highs[i] - lows[i],
               abs(highs[i] - closes[i-1]),
               abs(lows[i]  - closes[i-1]))
           for i in range(1, len(closes))]
    result = [sum(trs[:period]) / period]
    for tr in trs[period:]:
        result.append((result[-1] * (period - 1) + tr) / period)
    return result

def adx(highs: List[float], lows: List[float], closes: List[float],
        period: int = 14) -> List[float]:
    """
    ADX (Average Directional Index)  --  waarden 0-100.
    > 25 = sterke trend, < 20 = geen duidelijke trend.
    Gebruikt Wilder's smoothing voor TR/DM, dan SMA+EMA voor ADX.
    """
    if len(closes) < period * 2 + 2:
        return []

    tr_list, dm_plus, dm_minus = [], [], []
    for i in range(1, len(closes)):
        h, l, pc = highs[i], lows[i], closes[i-1]
        tr_list.append(max(h - l, abs(h - pc), abs(l - pc)))
        up   = highs[i] - highs[i-1]
        down = lows[i-1] - lows[i]
        dm_plus.append(max(up, 0)    if up > down and up > 0   else 0)
        dm_minus.append(max(down, 0) if down > up and down > 0 else 0)

    def wilder(lst, p):
        """Wilder smoothing: som → rolling average."""
        if len(lst) < p: return []
        s = [sum(lst[:p])]
        for x in lst[p:]:
            s.append(s[-1] - s[-1] / p + x)
        return s

    satr = wilder(tr_list, period)
    sdp  = wilder(dm_plus,  period)
    sdm  = wilder(dm_minus, period)
    n    = min(len(satr), len(sdp), len(sdm))

    di_plus  = [100 * sdp[i]  / satr[i] if satr[i] > 0 else 0 for i in range(n)]
    di_minus = [100 * sdm[i] / satr[i]  if satr[i] > 0 else 0 for i in range(n)]
    dx       = [100 * abs(di_plus[i] - di_minus[i]) / (di_plus[i] + di_minus[i])
                if (di_plus[i] + di_minus[i]) > 0 else 0 for i in range(n)]

    # ADX = SMA van eerste 'period' DX waarden, dan Wilder-stijl smoothing
    if len(dx) < period:
        return []
    adx_vals = [sum(dx[:period]) / period]
    for x in dx[period:]:
        adx_vals.append((adx_vals[-1] * (period - 1) + x) / period)

    return adx_vals

def obv(closes: List[float], volumes: List[float]) -> List[float]:
    if not closes or not volumes:
        return []
    result = [0.0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i-1]:
            result.append(result[-1] + volumes[i])
        elif closes[i] < closes[i-1]:
            result.append(result[-1] - volumes[i])
        else:
            result.append(result[-1])
    return result

def obv_slope(closes: List[float], volumes: List[float], period: int = 10) -> float:
    obv_vals = obv(closes, volumes)
    if len(obv_vals) < period + 1:
        return 0.0
    # Normaliseer op gemiddeld volume
    avg_vol = sum(abs(v) for v in obv_vals[-period:]) / period
    slope   = (obv_vals[-1] - obv_vals[-(period+1)]) / period
    return slope / avg_vol if avg_vol > 0 else 0.0

def stochastic(highs: List[float], lows: List[float], closes: List[float],
               k_period: int = 14, d_period: int = 3) -> Tuple[List[float], List[float]]:
    k_vals = []
    for i in range(k_period - 1, len(closes)):
        h = max(highs[i - k_period + 1:i + 1])
        l = min(lows [i - k_period + 1:i + 1])
        k_vals.append(50.0 if h == l else 100.0 * (closes[i] - l) / (h - l))
    return k_vals, sma(k_vals, d_period)

def vwap(highs: List[float], lows: List[float], closes: List[float],
         volumes: List[float]) -> List[float]:
    cum_tv, cum_v, result = 0.0, 0.0, []
    for h, l, c, v in zip(highs, lows, closes, volumes):
        tp     = (h + l + c) / 3.0
        cum_tv += tp * v
        cum_v  += v
        result.append(cum_tv / cum_v if cum_v > 0 else tp)
    return result

def supertrend(highs: List[float], lows: List[float], closes: List[float],
               factor: float = 3.0, period: int = 10) -> Tuple[List[float], List[int]]:
    atr_vals = atr(highs, lows, closes, period)
    if not atr_vals:
        return [], []
    offset = len(closes) - len(atr_vals)
    hl2    = [(highs[i] + lows[i]) / 2 for i in range(len(closes))]
    upper_basic = [hl2[offset+i] + factor * atr_vals[i] for i in range(len(atr_vals))]
    lower_basic = [hl2[offset+i] - factor * atr_vals[i] for i in range(len(atr_vals))]
    upper_band = [upper_basic[0]]
    lower_band = [lower_basic[0]]
    for i in range(1, len(atr_vals)):
        ub = upper_basic[i]
        lb = lower_basic[i]
        ub = min(ub, upper_band[-1]) if closes[offset+i-1] <= upper_band[-1] else ub
        lb = max(lb, lower_band[-1]) if closes[offset+i-1] >= lower_band[-1] else lb
        upper_band.append(ub)
        lower_band.append(lb)
    direction, trend_line = [], []
    d = 1
    for i in range(len(atr_vals)):
        c = closes[offset+i]
        if i > 0:
            if d == 1 and c < lower_band[i]: d = -1
            elif d == -1 and c > upper_band[i]: d = 1
        direction.append(d)
        trend_line.append(lower_band[i] if d == 1 else upper_band[i])
    return trend_line, direction

def atr_stop(highs: List[float], lows: List[float], closes: List[float],
             multiplier: float = 2.0, period: int = 14,
             tp_ratio: float = 1.5) -> Tuple[float, float]:
    """
    ATR-gebaseerde SL als % van prijs.
    tp_ratio: TP = SL × tp_ratio.
      1.0 = symmetrisch (korte timeframes, hogere winrate nodig)
      1.5 = asymmetrisch (langere timeframes, lagere winrate OK)
    Minimum 0.3%, maximum 35%.
    """
    atr_vals = atr(highs, lows, closes, period)
    if not atr_vals or closes[-1] == 0:
        return 0.02, 0.02 * tp_ratio
    sl_pct = (atr_vals[-1] * multiplier) / closes[-1]
    sl_pct = min(0.35, max(0.003, sl_pct))
    tp_pct = min(0.50, sl_pct * tp_ratio)
    return round(sl_pct, 4), round(tp_pct, 4)

def calculate_volatility_score(closes: List[float], period: int = 20) -> float:
    if len(closes) < period + 1:
        return 0.5
    recent  = closes[-(period + 1):]
    returns = [abs((recent[i+1] - recent[i]) / recent[i])
               for i in range(len(recent) - 1) if recent[i] > 0]
    if not returns:
        return 0.5
    return min(1.0, (sum(returns) / len(returns)) / 0.05)
