# -*- coding: utf-8 -*-
import logging
import asyncio
from typing import Dict, List, Optional, Tuple
from gate_client import GateFuturesClient
from risk_manager import RiskManager
from indicators import (
    ema, rsi, macd, bollinger_bands, bollinger_width,
    stochastic, vwap, atr, atr_stop, obv_slope,
    supertrend, calculate_volatility_score
)

logger = logging.getLogger('Strategy')

# Trade memory: self-kritisch leersysteem
try:
    from trade_memory import (
        TradeEvaluator, log_entry, log_exit,
        build_snapshot, detect_regime, init_db
    )
    init_db()
    _evaluator = TradeEvaluator()
    MEMORY_ENABLED = True
    logger.info("Trade memory actief")
except Exception as e:
    MEMORY_ENABLED = False
    logger.warning(f"Trade memory niet beschikbaar: {e}")

CONTRACT_SIZES = {
    'BTC_USDT':      1,
    'ETH_USDT':      1,
    'XRP_USDT':      1,
    'FARTCOIN_USDT': 1,
    'ADA_USDT':      1,
}

COIN_INTERVALS = {
    'BTC_USDT':      '4h',
    'ETH_USDT':      '1h',
    'XRP_USDT':      '4h',
    'FARTCOIN_USDT': '5m',
    'ADA_USDT':      '1h',
}

# Trend filter timeframe (hoger dan entry TF)
TREND_INTERVALS = {
    'BTC_USDT':      '1d',   # 4h entry → 1d trend
    'ETH_USDT':      '4h',   # 1h entry → 4h trend
    'XRP_USDT':      '1d',   # 4h entry → 1d trend
    'FARTCOIN_USDT': '1h',   # 5m entry → 1h trend
    'ADA_USDT':      '4h',   # 1h entry → 4h trend
}

# Max leverage per coin (conservatiever voor BTC/ETH)
COIN_MAX_LEVERAGE = {
    'BTC_USDT':      10,
    'ETH_USDT':      15,
    'XRP_USDT':      20,
    'FARTCOIN_USDT': 10,   # meme coin cap
    'ADA_USDT':      15,
}

MIN_CANDLES = 100


def _prev(lst: list, default=0.0):
    return lst[-2] if len(lst) >= 2 else (lst[-1] if lst else default)


def _slope(lst: list, period: int = 5) -> float:
    """Eenvoudige slope: (laatste - eerste) / eerste over periode candles."""
    if len(lst) < period + 1:
        return 0.0
    start = lst[-(period + 1)]
    end   = lst[-1]
    return (end - start) / start if start != 0 else 0.0


def _rolling_vwap(highs, lows, closes, volumes, period: int = 20) -> float:
    """VWAP over de laatste `period` candles  --  zinvoller dan cumulatief."""
    n = min(period, len(closes))
    h = highs[-n:]; l = lows[-n:]; c = closes[-n:]; v = volumes[-n:]
    tv = sum(((h[i]+l[i]+c[i])/3) * v[i] for i in range(n))
    sv = sum(v)
    return tv / sv if sv > 0 else closes[-1]


def _trend_strength(e_fast, e_slow) -> float:
    """
    Meting van trendsterkte als procentuele spread tussen twee EMAs.
    Positief = bullish, negatief = bearish.
    Vervangt de defecte ADX implementatie.
    """
    if not e_fast or not e_slow or e_slow[-1] == 0:
        return 0.0
    return (e_fast[-1] - e_slow[-1]) / e_slow[-1]


class TradingStrategy:
    def __init__(self, client: GateFuturesClient, risk: RiskManager,
                 symbols: List[str]):
        self.client  = client
        self.risk    = risk
        self.symbols = symbols
        self.sl_tp:    Dict[str, dict] = {}
        self.trade_ids: Dict[str, int]  = {}   # symbol -> memory trade_id

    async def _balance(self) -> float:
        acc = await self.client.get_account()
        return float(acc.get('available', 0)) if acc else 0.0

    async def _fetch_ohlcv(self, symbol: str) -> Optional[dict]:
        interval = COIN_INTERVALS.get(symbol, '1h')
        limit    = 200
        raw = await self.client.get_candles(symbol, interval, limit)
        if not raw or len(raw) < MIN_CANDLES:
            return None
        candles = []
        for c in raw:
            if isinstance(c, list):
                candles.append({'t': c[0], 'o': c[1], 'h': c[2],
                                'l': c[3], 'c': c[4],
                                'v': c[5] if len(c) > 5 else 0})
            else:
                candles.append(c)
        return {
            'opens':    [float(c.get('o', 0)) for c in candles],
            'highs':    [float(c.get('h', 0)) for c in candles],
            'lows':     [float(c.get('l', 0)) for c in candles],
            'closes':   [float(c.get('c', 0)) for c in candles],
            'volumes':  [float(c.get('v', 0)) for c in candles],
            'interval': interval,
        }

    async def _fetch_trend_data(self, symbol: str) -> Optional[dict]:
        """Haal hogere timeframe data op voor trend filter (multi-TF)."""
        trend_tf = TREND_INTERVALS.get(symbol)
        if not trend_tf:
            return None
        raw = await self.client.get_candles(symbol, trend_tf, 220)
        if not raw or len(raw) < 50:
            return None
        candles = []
        for c in raw:
            if isinstance(c, list):
                candles.append({'t': c[0], 'o': c[1], 'h': c[2],
                                'l': c[3], 'c': c[4],
                                'v': c[5] if len(c) > 5 else 0})
            else:
                candles.append(c)
        return {
            'closes':  [float(c.get('c', 0)) for c in candles],
            'highs':   [float(c.get('h', 0)) for c in candles],
            'lows':    [float(c.get('l', 0)) for c in candles],
            'volumes': [float(c.get('v', 0)) for c in candles],
            'interval': trend_tf,
        }

    # ==========================================================================
    # NIEUW SCORINGSMODEL (v6)
    #
    # Verschil met v5:
    # - "Active scoring": alleen indicatoren met een mening tellen mee
    #   → neutrale indicatoren verlagen de confidence NIET meer
    # - Harde gates omgezet naar soft scores (dragen bij ipv blokkeren)
    # - Lagere drempels (45-55%) omdat de scores nu betrouwbaarder zijn
    # - Elke indicator geeft +1 (bullish), -1 (bearish), of 0 (neutraal)
    #   Confidence = abs(som) / aantal_actieve_indicatoren
    # ==========================================================================

    def _active_score(self, votes: list) -> Tuple[str, float]:
        """
        Majority voting: welk percentage van actieve indicatoren kiest dezelfde richting?
        Voorbeeld: 4 long, 2 short → confidence = 4/6 = 67%
        (Vorige versie: netto score 2/6 = 33% → te streng, blokkeerde bijna alles)
        """
        active = [v for v in votes if v != 0]
        if len(active) < 2:
            return 'none', 0.0
        longs  = sum(1 for v in active if v > 0)
        shorts = sum(1 for v in active if v < 0)
        if longs > shorts:
            return 'long', longs / len(active)
        elif shorts > longs:
            return 'short', shorts / len(active)
        return 'none', 0.0

    # ==========================================================================
    # BTC  --  Multi-TF Trend + Pullback (v2)
    #
    # DESIGN: Gebaseerd op Binance-analyse + eigen ROI framework
    #
    # TREND FILTER (1d candles via trend_data):
    #   EMA 21 > EMA 50 → uptrend, alleen long
    #   ADX > 25 → trend sterk genoeg (sideways = geen trade)
    #
    # ENTRY (4h candles):
    #   Trigger A: EMA pullback — prijs daalt naar EMA 21 in uptrend
    #   Trigger B: RSI extreme bounce — RSI < 30 met volume spike
    #
    # VOLUME GATE: volume > 1.5x 20-bar gemiddelde
    #
    # Max leverage: 10x (BTC is te groot voor 20x)
    # ==========================================================================
    def _signal_btc(self, o: dict, trend: Optional[dict] = None) -> Tuple[str, float]:
        c, h, l, v = o['closes'], o['highs'], o['lows'], o['volumes']

        e21 = ema(c, 21); e50 = ema(c, 50)
        rv  = rsi(c, 14)
        a   = adx(h, l, c, 14)

        if not all([e21, e50, rv]) or len(c) < 3:
            return 'none', 0.0

        price = c[-1]; cr = rv[-1]
        cur_adx = a[-1] if a else 0

        # --- TREND (multi-TF als beschikbaar, anders zelfde TF) ---
        trend_bull = e21[-1] > e50[-1]
        trend_bear = e21[-1] < e50[-1]
        if trend and trend.get('closes'):
            tc = trend['closes']
            te21 = ema(tc, 21); te50 = ema(tc, 50)
            if te21 and te50:
                trend_bull = te21[-1] > te50[-1]
                trend_bear = te21[-1] < te50[-1]
        if not trend_bull and not trend_bear:
            return 'none', 0.0

        # --- ADX: soft gate (< 20 = blok, 20-25 = lagere conf, > 25 = normaal) ---
        if cur_adx < 20:
            return 'none', 0.0
        adx_bonus = 0.05 if cur_adx > 30 else 0.0

        # --- Volume: bonus (niet gate!) ---
        vol_bonus = 0.0
        if len(v) >= 20:
            avg_vol = sum(v[-20:]) / 20
            if avg_vol > 0 and v[-1] > avg_vol * 1.5:
                vol_bonus = 0.05

        dist_e21 = (price - e21[-1]) / e21[-1] if e21[-1] else 0

        # --- TRIGGER A: EMA Pullback (verruimd: dist < 1.5%, RSI < 50) ---
        if trend_bull and -0.015 < dist_e21 < 0.005 and cr < 50:
            base = 0.70
            if cr < 35: base = 0.82
            elif cr < 40: base = 0.75
            # Candle richting = bonus, niet gate
            if c[-1] < c[-2]: base += 0.03
            conf = min(base + adx_bonus + vol_bonus, 0.95)
            return 'long', conf

        if trend_bear and -0.005 < dist_e21 < 0.015 and cr > 50:
            base = 0.70
            if cr > 65: base = 0.82
            elif cr > 60: base = 0.75
            if c[-1] > c[-2]: base += 0.03
            conf = min(base + adx_bonus + vol_bonus, 0.95)
            return 'short', conf

        # --- TRIGGER B: RSI extreme ---
        if trend_bull and cr < 35:
            conf = min(0.80 + adx_bonus + vol_bonus, 0.95)
            return 'long', conf

        if trend_bear and cr > 65:
            conf = min(0.80 + adx_bonus + vol_bonus, 0.95)
            return 'short', conf

        # --- TRIGGER C: Trend-following (wanneer A en B niet vuren) ---
        # Simpele trendbevestiging: EMA aligned + RSI niet extreem + ADX sterk
        if trend_bull and 40 < cr < 65 and cur_adx > 25:
            # Prijs boven EMA21 en EMA21 stijgt
            e21_slope = _slope(e21, 5) if e21 and len(e21) > 5 else 0
            if dist_e21 > 0 and e21_slope > 0.001:
                base = 0.62 + adx_bonus + vol_bonus
                return 'long', min(base, 0.78)

        if trend_bear and 35 < cr < 60 and cur_adx > 25:
            e21_slope = _slope(e21, 5) if e21 and len(e21) > 5 else 0
            if dist_e21 < 0 and e21_slope < -0.001:
                base = 0.62 + adx_bonus + vol_bonus
                return 'short', min(base, 0.78)

        return 'none', 0.0

    # ==========================================================================
    # ETH  --  Hybride: BB squeeze breakout OF EMA trend + MACD
    # ==========================================================================
    def _signal_eth(self, o: dict) -> Tuple[str, float]:
        c, h, l, v = o['closes'], o['highs'], o['lows'], o['volumes']

        e9  = ema(c, 9); e21 = ema(c, 21); e50 = ema(c, 50)
        ml, sl_line, hist = macd(c, 12, 26, 9)
        bbu, bbm, bbl = bollinger_bands(c, 20, 2.0)
        bb_w = bollinger_width(c, 20)
        rv   = rsi(c, 14)
        from indicators import obv
        obv_vals = obv(c, v)
        obv_ema  = ema(obv_vals, 10) if len(obv_vals) >= 10 else []

        if not all([e9, e21, e50, sl_line, bbu, bb_w, rv]):
            return 'none', 0.0

        price   = c[-1]
        cur_rsi = rv[-1]

        # Squeeze detectie (bonus, niet vereist)
        ref_n     = min(30, len(bb_w))
        avg_width = sum(bb_w[-ref_n:]) / ref_n
        in_squeeze  = bb_w[-1] < avg_width * 0.80  # versoepeld van 0.70
        was_squeeze = len(bb_w) > 8 and min(bb_w[-9:-1]) < avg_width * 0.80
        squeeze_breakout = was_squeeze and not in_squeeze

        votes = []

        # 1. EMA trend alignment
        if e9[-1] > e21[-1] > e50[-1]:     votes.extend([1, 1])
        elif e9[-1] < e21[-1] < e50[-1]:   votes.extend([-1, -1])
        else:                               votes.append(0)

        # 2. Squeeze breakout (bonus als het er is)
        if squeeze_breakout:
            bb_mid_dist = (price - bbm[-1]) / bbm[-1] if bbm[-1] > 0 else 0
            if bb_mid_dist > 0.001:    votes.extend([1, 1])  # sterk signaal
            elif bb_mid_dist < -0.001: votes.extend([-1, -1])
            else:                      votes.append(0)

        # 3. MACD crossover
        if _prev(ml) <= _prev(sl_line) and ml[-1] > sl_line[-1]:  votes.append(1)
        elif _prev(ml) >= _prev(sl_line) and ml[-1] < sl_line[-1]: votes.append(-1)
        else: votes.append(0)

        # 4. MACD histogram richting
        if len(hist) >= 2:
            if hist[-1] > 0 and hist[-1] > hist[-2]:   votes.append(1)
            elif hist[-1] < 0 and hist[-1] < hist[-2]: votes.append(-1)
            else: votes.append(0)

        # 5. OBV richting
        obv_up = bool(obv_ema and len(obv_ema) >= 2 and obv_ema[-1] > obv_ema[-2])
        if obv_up:                          votes.append(1)
        elif obv_ema and len(obv_ema) >= 2: votes.append(-1)  # OBV daalt
        else:                               votes.append(0)

        # 6. RSI momentum
        if cur_rsi > 45 and cur_rsi < 70 and cur_rsi > _prev(rv): votes.append(1)
        elif cur_rsi < 55 and cur_rsi > 30 and cur_rsi < _prev(rv): votes.append(-1)
        elif cur_rsi < 25:  votes.append(1)   # oversold bounce kans
        elif cur_rsi > 75:  votes.append(-1)  # overbought
        else: votes.append(0)

        # 7. Prijs vs BB midden
        if bbm and bbm[-1] > 0:
            if price > bbm[-1]:   votes.append(1)
            elif price < bbm[-1]: votes.append(-1)
            else:                 votes.append(0)

        sig, conf = self._active_score(votes)
        THRESH = 0.55
        return (sig, conf) if conf >= THRESH else ('none', 0.0)

    # ==========================================================================
    # XRP  --  4H ROI strategie: Trend pullback + BB extreme bounce
    #
    # Ontworpen voor 30% ROI met 15-20x leverage.
    # 2 onafhankelijke triggers, elk met 3 niet-gecorreleerde filters.
    # Signaalfrequentie: < 1% (extreem selectief).
    #
    # Trigger A: EMA pullback in trend (dip-entry)
    # Trigger B: BB bounce bij extreme RSI (mean-reversion)
    #
    # Confidence ≥ 80% → trailing profit lock na 30% ROI
    # Confidence < 80% → vaste TP op 30% ROI
    # ==========================================================================
    def _signal_xrp(self, o: dict) -> Tuple[str, float]:
        c, h, l, v = o['closes'], o['highs'], o['lows'], o['volumes']

        e21 = ema(c, 21); e50 = ema(c, 50)
        rv  = rsi(c, 14)
        bbu, bbm, bbl = bollinger_bands(c, 20, 2.0)

        if not all([e21, e50, rv, bbu]) or len(c) < 3 or len(rv) < 3:
            return 'none', 0.0

        price = c[-1]; cr = rv[-1]
        e50s = _slope(e50, 10)

        # ── TRIGGER A: Trend + Pullback (verruimd) ──
        trend_bull = e21[-1] > e50[-1]
        trend_bear = e21[-1] < e50[-1]
        dist_e21 = (price - e21[-1]) / e21[-1] if e21[-1] else 0

        # Long: uptrend + prijs binnen 1.5% van EMA21 + RSI < 50
        if trend_bull and -0.015 < dist_e21 < 0.005 and cr < 50:
            base = 0.68
            if cr < 30: base = 0.85       # sterke oversold
            elif cr < 38: base = 0.78     # oversold
            elif cr < 45: base = 0.72     # licht oversold
            # Bearish candle = bonus, niet gate
            if c[-1] < c[-2]: base += 0.03
            return 'long', min(base, 0.95)

        # Short: downtrend + prijs binnen 1.5% van EMA21 + RSI > 50
        if trend_bear and -0.005 < dist_e21 < 0.015 and cr > 50:
            base = 0.68
            if cr > 70: base = 0.85
            elif cr > 62: base = 0.78
            elif cr > 55: base = 0.72
            if c[-1] > c[-2]: base += 0.03
            return 'short', min(base, 0.95)

        # ── TRIGGER B: BB bounce + RSI (verruimd: RSI < 35 ipv 28) ──
        if len(bbl) >= 2:
            prev_below = c[-2] < bbl[-2]
            now_above  = price > bbl[-1]
            prev_above = c[-2] > bbu[-2]
            now_below  = price < bbu[-1]

            if prev_below and now_above and cr < 35 and abs(e50s) > 0.001:
                conf = 0.88 if cr < 25 else 0.75
                return 'long', conf

            if prev_above and now_below and cr > 65 and abs(e50s) > 0.001:
                conf = 0.88 if cr > 75 else 0.75
                return 'short', conf

        # ── TRIGGER C: Trend-following (wanneer A en B niet vuren) ──
        # EMA aligned + RSI niet extreem + prijs boven/onder EMA21
        if trend_bull and 35 < cr < 60:
            e21_slope = _slope(e21, 5)
            if dist_e21 > 0 and e21_slope > 0.001:
                return 'long', 0.65

        if trend_bear and 40 < cr < 65:
            e21_slope = _slope(e21, 5)
            if dist_e21 < 0 and e21_slope < -0.001:
                return 'short', 0.65

        return 'none', 0.0

    # ==========================================================================
    # FARTCOIN  --  Trend + Dip/Rally entry (contrarian op korte termijn)
    #
    # Kern inzicht: op 5m beweegt prijs mean-reverting. Sterke up-moves
    # beginnen na een dip (pm3 < 0), niet na een rally.
    # Strategie: trend via EMA 50, entry op korte dip/rally in trendrichting.
    # Selectiviteit: alleen traden als trend sterk genoeg is en dip diep genoeg.
    # ==========================================================================
    def _signal_fartcoin(self, o: dict) -> Tuple[str, float]:
        c, h, l, v = o['closes'], o['highs'], o['lows'], o['volumes']

        e5  = ema(c, 5)
        e13 = ema(c, 13)
        e21 = ema(c, 21)
        e50 = ema(c, 50)
        rv  = rsi(c, 7)
        rv14 = rsi(c, 14)

        if not all([e5, e13, e21, e50, rv, rv14]):
            return 'none', 0.0
        if len(v) < 20 or len(c) < 6:
            return 'none', 0.0

        price   = c[-1]
        cur_rsi = rv[-1]
        cur_rsi14 = rv14[-1]

        # Korte termijn momentum (3 en 5 candles)
        pm3 = (c[-1] - c[-4]) / c[-4] if c[-4] > 0 else 0

        # === GATE 1: Trend via EMA 50 slope (verlaagd: 0.15% ipv 0.3%) ===
        e50_slope = _slope(e50, 10)
        trend_up   = e50_slope > 0.0015
        trend_down = e50_slope < -0.0015
        if not trend_up and not trend_down:
            return 'none', 0.0

        # === GATE 2: EMA ribbon (versoepeld: 2 van 3 aligned volstaat) ===
        ribbon_bull = (e5[-1] > e13[-1] and e13[-1] > e21[-1]) or \
                      (e5[-1] > e21[-1] and e13[-1] > e21[-1])
        ribbon_bear = (e5[-1] < e13[-1] and e13[-1] < e21[-1]) or \
                      (e5[-1] < e21[-1] and e13[-1] < e21[-1])
        if trend_up and not ribbon_bull:
            return 'none', 0.0
        if trend_down and not ribbon_bear:
            return 'none', 0.0

        # === GATE 3: Dip/rally (verlaagd: 0.2% ipv 0.3%) ===
        has_dip   = trend_up and pm3 < -0.002
        has_rally = trend_down and pm3 > 0.002
        if not has_dip and not has_rally:
            return 'none', 0.0

        # Alle gates gepasseerd — nu scoren
        votes = []

        # 1. Dip/rally diepte (hoe dieper, hoe beter)
        if has_dip:
            if pm3 < -0.008:    votes.extend([1, 1, 1])   # diepe dip
            elif pm3 < -0.005:  votes.extend([1, 1])
            else:               votes.append(1)
        elif has_rally:
            if pm3 > 0.008:    votes.extend([-1, -1, -1])
            elif pm3 > 0.005:  votes.extend([-1, -1])
            else:              votes.append(-1)

        # 2. RSI in trendrichting-zone
        if trend_up and cur_rsi < 40:      votes.extend([1, 1])   # oversold in uptrend
        elif trend_up and cur_rsi < 50:    votes.append(1)
        elif trend_down and cur_rsi > 60:  votes.extend([-1, -1])
        elif trend_down and cur_rsi > 50:  votes.append(-1)
        else:                              votes.append(0)

        # 3. RSI 14 keert om
        prev_rsi14 = _prev(rv14)
        if cur_rsi14 < 45 and cur_rsi14 > prev_rsi14:  votes.append(1)
        elif cur_rsi14 > 55 and cur_rsi14 < prev_rsi14: votes.append(-1)
        else: votes.append(0)

        # 4. Prijs nabij EMA 21 (support/resistance)
        dist_e21 = (price - e21[-1]) / e21[-1] if e21[-1] > 0 else 0
        if trend_up and dist_e21 < 0.005:     votes.append(1)   # dicht bij support
        elif trend_down and dist_e21 > -0.005: votes.append(-1)
        else: votes.append(0)

        # 5. Volume bonus
        avg_vol = sum(v[-20:-1]) / 19
        if v[-1] > avg_vol * 1.8:
            if has_dip:   votes.append(1)
            elif has_rally: votes.append(-1)

        sig, conf = self._active_score(votes)

        # Blokkeer tegen-trend
        if sig == 'long' and trend_down: return 'none', 0.0
        if sig == 'short' and trend_up:  return 'none', 0.0

        THRESH = 0.55
        return (sig, conf) if conf >= THRESH else ('none', 0.0)

    # ==========================================================================
    # ADA  --  Supertrend + EMA + OBV + MACD (ADX als score, niet gate)
    # ==========================================================================
    def _signal_ada(self, o: dict) -> Tuple[str, float]:
        c, h, l, v = o['closes'], o['highs'], o['lows'], o['volumes']

        from indicators import adx as _adx
        e20 = ema(c, 20); e50 = ema(c, 50)
        rv  = rsi(c, 14)
        st_line, st_dir = supertrend(h, l, c, factor=3.0, period=10)
        ovb = obv_slope(c, v, 14)
        ml, sl_line, hist = macd(c, 12, 26, 9)

        if not all([e20, e50, rv, st_line, st_dir, sl_line]):
            return 'none', 0.0

        price   = c[-1]
        cur_rsi = rv[-1]
        cur_st  = st_dir[-1]

        votes = []

        # 1. Supertrend richting (dubbel gewicht — kern indicator)
        if cur_st == 1:    votes.extend([1, 1])
        elif cur_st == -1: votes.extend([-1, -1])
        else:              votes.append(0)

        # 2. Supertrend flip (extra bonus bij verse flip)
        prev_st = _prev(st_dir, cur_st)
        if prev_st == -1 and cur_st == 1:   votes.append(1)
        elif prev_st == 1 and cur_st == -1: votes.append(-1)
        # geen else — flip is puur bonus

        # 3. EMA 20/50 alignment
        if e20[-1] > e50[-1]:   votes.append(1)
        elif e20[-1] < e50[-1]: votes.append(-1)
        else:                   votes.append(0)

        # 4. EMA 20 slope
        e20_slope = _slope(e20, 5)
        if e20_slope > 0.001:    votes.append(1)
        elif e20_slope < -0.001: votes.append(-1)
        else:                    votes.append(0)

        # 5. OBV richting
        if ovb > 0:    votes.append(1)
        elif ovb < 0:  votes.append(-1)
        else:          votes.append(0)

        # 6. MACD histogram
        if hist[-1] > 0:   votes.append(1)
        elif hist[-1] < 0: votes.append(-1)
        else:               votes.append(0)

        # 7. RSI zone
        if cur_rsi < 35:   votes.append(1)
        elif cur_rsi > 65: votes.append(-1)
        elif 40 < cur_rsi < 60: votes.append(0)  # neutrale zone
        else:               votes.append(0)

        # 8. ADX als bonus (trending = hogere confidence, niet als gate)
        adx_vals = _adx(h, l, c, 14)
        if adx_vals and adx_vals[-1] > 20:
            # ADX bevestigt: voeg extra stem toe in dominante richting
            if sum(v for v in votes if v != 0) > 0:  votes.append(1)
            elif sum(v for v in votes if v != 0) < 0: votes.append(-1)

        sig, conf = self._active_score(votes)
        THRESH = 0.55
        return (sig, conf) if conf >= THRESH else ('none', 0.0)

    # ==========================================================================
    # FARTCOIN BB  --  Bollinger Bands mean-reversion (4H) — v2
    # 1. EMA 50 trendfilter (responsief voor meme coin)
    # 2. RSI(14) bevestiging (versoepeld)
    # 3. Fallback: extreem RSI zonder trendfilter
    # 4. Max hold 24 bars (4 dagen)
    # ==========================================================================
    def _signal_fartcoin_bb(self, o: dict) -> Tuple[str, float]:
        c = o['closes']
        bbu, bbm, bbl = bollinger_bands(c, 20, 2.0)
        rv  = rsi(c, 14)
        e50 = ema(c, 50)

        if not bbu or len(bbu) < 2 or len(c) < 2 or not rv:
            return 'none', 0.0

        price      = c[-1]
        prev_close = c[-2]
        cur_rsi    = rv[-1]
        has_trend  = bool(e50)
        trend_up   = has_trend and price > e50[-1]
        trend_down = has_trend and price < e50[-1]

        prev_upper = bbu[-2]; prev_lower = bbl[-2]
        cur_upper  = bbu[-1]; cur_lower  = bbl[-1]

        # LONG: bounce van lower band
        if prev_close < prev_lower and price > cur_lower:
            if trend_up and cur_rsi < 45:
                return 'long', 0.85
            elif trend_up and cur_rsi < 55:
                return 'long', 0.70
            elif cur_rsi < 30:
                return 'long', 0.65       # extreem oversold, geen trend nodig
            return 'none', 0.0

        # SHORT: rejection van upper band
        if prev_close > prev_upper and price < cur_upper:
            if trend_down and cur_rsi > 55:
                return 'short', 0.85
            elif trend_down and cur_rsi > 45:
                return 'short', 0.70
            elif cur_rsi > 70:
                return 'short', 0.65
            return 'none', 0.0

        return 'none', 0.0

    # -- Router ----------------------------------------------------------------

    def generate_signal(self, ohlcv: dict, symbol: str = '',
                        trend_data: Optional[dict] = None) -> Tuple[str, float]:
        try:
            if   'BTC'      in symbol: sig, conf = self._signal_btc(ohlcv, trend_data)
            elif 'ETH'      in symbol: sig, conf = self._signal_eth(ohlcv)
            elif 'XRP'      in symbol: sig, conf = self._signal_xrp(ohlcv)
            elif 'FARTCOIN' in symbol: sig, conf = self._signal_fartcoin(ohlcv)
            elif 'ADA'      in symbol: sig, conf = self._signal_ada(ohlcv)
            else:                      sig, conf = self._signal_btc(ohlcv, trend_data)

            # Adaptieve drempel: als recente winrate laag is, is de drempel verhoogd
            # Safety cap: drempel kan nooit boven 0.60 (anders blokkeert alles)
            if MEMORY_ENABLED and sig != 'none':
                adapted_thresh = _evaluator.get_threshold(symbol, default=0.45)
                adapted_thresh = min(adapted_thresh, 0.60)  # hard cap
                if conf < adapted_thresh:
                    logger.warning(
                        f"[{symbol}] Signaal {sig} conf={conf:.0%} < "
                        f"adaptieve drempel {adapted_thresh:.0%} → geblokkeerd"
                    )
                    return 'none', 0.0

            return sig, conf
        except Exception as e:
            logger.error(f"Signaal fout [{symbol}]: {e}", exc_info=True)
            return 'none', 0.0

    def _get_atr_multiplier(self, symbol: str) -> float:
        """
        ATR multiplier schaalt mee met het timeframe.
        Korte TF → lage multiplier → snelle trades (2-4% SL)
        Lange TF → hoge multiplier → swing trades (15-30% SL)
        """
        interval = COIN_INTERVALS.get(symbol, '1h')
        tf_multiplier = {
            '5m':  1.5,    # SL ~1.5-3%   → trades duren ~5-15 candles
            '15m': 2.0,    # SL ~3-5%     → trades duren ~8-20 candles
            '1h':  3.0,    # SL ~5-10%    → trades duren ~10-25 candles
            '4h':  5.0,    # SL ~10-20%   → trades duren ~10-20 candles
            '1d':  7.0,    # SL ~15-30%   → trades duren ~5-15 candles
        }.get(interval, 3.0)

        # Coin-specifieke extra factor voor volatielere coins
        coin_factor = {
            'BTC_USDT':      1.0,   # standaard
            'ETH_USDT':      1.0,
            'XRP_USDT':      1.2,   # iets volatieler
            'FARTCOIN_USDT': 1.3,   # meme coin, meer ruimte
            'ADA_USDT':      1.0,
        }.get(symbol, 1.0)

        return round(tf_multiplier * coin_factor, 1)

    def _get_tp_ratio(self, symbol: str) -> float:
        """
        TP/SL ratio per timeframe:
          5m/15m → 1:1 (symmetrisch, hogere winrate nodig maar bereikbaar)
          1h     → 1:1.2
          4h/1d  → 1:1.5 (swing trades, compenseer lagere WR met R/R)
        """
        interval = COIN_INTERVALS.get(symbol, '1h')
        return {
            '5m':  1.0,
            '15m': 1.0,
            '1h':  1.2,
            '4h':  1.5,
            '1d':  1.5,
        }.get(interval, 1.2)

    async def _manage_open_positions(self):
        positions = await self.client.get_positions()
        for pos in positions:
            contract = pos.get('contract', '')
            size     = int(pos.get('size', 0))
            if size == 0:
                continue
            entry   = float(pos.get('entry_price', 0))
            mark    = float(pos.get('mark_price', entry))
            pnl     = float(pos.get('unrealised_pnl', 0))
            is_long = size > 0
            sl_tp   = self.sl_tp.get(contract)
            if not sl_tp and entry > 0:
                sl = self.risk.get_stop_loss_price(entry, is_long)
                tp = self.risk.get_take_profit_price(entry, is_long)
                self.sl_tp[contract] = {'sl': sl, 'tp': tp, 'is_long': is_long}
                sl_tp = self.sl_tp[contract]
            if not sl_tp:
                continue
            sl, tp  = sl_tp['sl'], sl_tp['tp']
            hit_sl  = (mark <= sl) if is_long else (mark >= sl)
            hit_tp  = (mark >= tp) if is_long else (mark <= tp)
            if hit_sl or hit_tp:
                reason = "SL" if hit_sl else "TP"
                logger.info(f"[{contract}] Software {reason}: mark={mark:.6f} pnl={pnl:+.4f}")
                await self.client.cancel_all_price_orders(contract)
                result = await self.client.place_order(contract, -size, reduce_only=True)
                if result:
                    self.risk.update_pnl(pnl)
                    # Trade memory: log exit en evalueer (VOOR sl_tp.pop!)
                    if MEMORY_ENABLED:
                        try:
                            tid = self.trade_ids.pop(contract, None)
                            if tid:
                                lev  = self.sl_tp.get(contract, {}).get('leverage', 1) or 1
                                margin = abs(size) / lev if lev > 0 else abs(size)
                                pnl_pct = pnl / margin * 100 if margin > 0 else 0
                                log_exit(tid, mark, pnl, pnl_pct, reason)
                                report = _evaluator.evaluate_symbol(contract)
                                if report.get('action') != 'wachten':
                                    logger.info(
                                        f"[{contract}] Zelfevaluatie: {report.get('action')} "
                                        f"winrate={report.get('win_rate',0):.0%} "
                                        f"drempel {report.get('old_threshold',0):.3f}→{report.get('new_threshold',0):.3f}"
                                    )
                        except Exception as me:
                            logger.warning(f"[{contract}] Memory exit fout: {me}")
                    self.sl_tp.pop(contract, None)

    async def run_cycle(self):
        logger.info("-" * 60)
        logger.info("Cyclus gestart")
        balance = await self._balance()
        if balance <= 0:
            logger.error("Balans ophalen mislukt of = 0")
            return
        logger.info(f"Balans: {balance:.4f} USDT")
        self.risk.reset_daily_if_needed(balance)
        if self.risk.is_daily_loss_exceeded(balance):
            logger.warning("Dagverlies limiet bereikt  --  geen nieuwe trades")
            return
        await self._manage_open_positions()
        all_pos = await self.client.get_positions()
        open_contracts = {p['contract'] for p in all_pos if int(p.get('size', 0)) != 0}
        logger.info(f"Open posities: {open_contracts or 'geen'}")
        for symbol in self.symbols:
            try:
                if symbol in open_contracts:
                    continue
                ohlcv = await self._fetch_ohlcv(symbol)
                if not ohlcv:
                    logger.warning(f"[{symbol}] Onvoldoende candles")
                    continue

                # Multi-TF: haal hogere timeframe data op voor trend filter
                trend_data = None
                try:
                    trend_data = await self._fetch_trend_data(symbol)
                except Exception as te:
                    logger.debug(f"[{symbol}] Trend data ophalen mislukt: {te}")
                    trend_data = None

                signal, confidence = self.generate_signal(ohlcv, symbol, trend_data)
                price      = ohlcv['closes'][-1]
                volatility = calculate_volatility_score(ohlcv['closes'])
                interval   = ohlcv.get('interval', '?')
                trend_tf   = trend_data['interval'] if trend_data else '-'
                logger.info(
                    f"[{symbol}] {signal.upper():5} conf={confidence:.0%} "
                    f"prijs={price:.6f} vol={volatility:.2f} tf={interval}+{trend_tf}"
                )
                if signal == 'none':
                    continue
                atr_mult        = self._get_atr_multiplier(symbol)
                tp_ratio        = self._get_tp_ratio(symbol)
                sl_pct, tp_pct  = atr_stop(ohlcv['highs'], ohlcv['lows'],
                                            ohlcv['closes'], multiplier=atr_mult,
                                            tp_ratio=tp_ratio)
                leverage = self.risk.get_optimal_leverage(volatility, sl_pct=sl_pct)
                # Per-coin leverage cap
                max_lev = COIN_MAX_LEVERAGE.get(symbol, 20)
                leverage = min(leverage, max_lev)
                lev_ok = await self.client.set_leverage(symbol, leverage)
                if not lev_ok:
                    logger.warning(f"[{symbol}] Leverage mislukt, overgeslagen")
                    continue
                contracts = self.risk.calculate_position_size(
                    balance, price, leverage,
                    contract_multiplier=CONTRACT_SIZES.get(symbol, 1)
                )
                if contracts <= 0:
                    logger.warning(f"[{symbol}] Contracts=0, overgeslagen")
                    continue
                is_long    = signal == 'long'
                order_size = contracts if is_long else -contracts
                logger.info(
                    f"[{symbol}] ORDER {signal.upper()} | "
                    f"contracts={contracts} lev={leverage}x "
                    f"sl={sl_pct:.1%} tp={tp_pct:.1%}"
                )
                result = await self.client.place_order(symbol, order_size)
                if not result:
                    logger.error(f"[{symbol}] Order mislukt: {self.client.last_error}")
                    continue
                sl_price = self.risk.get_stop_loss_price(price, is_long, sl_pct)
                tp_price = self.risk.get_take_profit_price(price, is_long, tp_pct)
                await self.client.cancel_all_price_orders(symbol)
                sl_ok = await self.client.place_stop_loss(symbol, is_long, sl_price, contracts)
                tp_ok = await self.client.place_take_profit(symbol, is_long, tp_price, contracts)
                self.sl_tp[symbol] = {'sl': sl_price, 'tp': tp_price, 'is_long': is_long, 'leverage': leverage}
                logger.info(
                    f"[{symbol}] OK entry~{price:.6f} "
                    f"SL={sl_price:.6f}[{'OK' if sl_ok else 'FAIL'}] "
                    f"TP={tp_price:.6f}[{'OK' if tp_ok else 'FAIL'}]"
                )

                # Trade memory: sla entry op
                if MEMORY_ENABLED:
                    try:
                        snap    = build_snapshot(ohlcv, {'signal': signal, 'atr_mult': atr_mult})
                        regime  = detect_regime(snap)
                        trade_id = log_entry(
                            symbol=symbol,
                            strategy=symbol.split('_')[0].lower(),
                            direction=signal,
                            entry_price=price,
                            contracts=contracts,
                            leverage=leverage,
                            confidence=confidence,
                            snapshot=snap,
                            market_regime=regime,
                        )
                        self.trade_ids[symbol] = trade_id
                        logger.info(f"[{symbol}] Trade #{trade_id} opgeslagen in memory")
                    except Exception as me:
                        logger.warning(f"[{symbol}] Memory entry fout: {me}")

                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"[{symbol}] Fout: {e}", exc_info=True)
        logger.info("Cyclus klaar")
