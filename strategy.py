# -*- coding: utf-8 -*-
import logging
import asyncio
from typing import Dict, List, Optional, Tuple
from gate_client import GateFuturesClient
from risk_manager import RiskManager
from indicators import (
    ema, rsi, macd, bollinger_bands, bollinger_width,
    stochastic, vwap, atr, atr_stop, obv_slope, adx,
    supertrend, calculate_volatility_score
)

logger = logging.getLogger('Strategy')

# Telegram notificaties (optioneel)
try:
    from telegram_notify import (
        notify, notify_trade, notify_close, notify_alert,
        notify_liquidation_warning, notify_funding, notify_daily_summary,
        ENABLED as TG_ENABLED
    )
except ImportError:
    TG_ENABLED = False

# Trade memory + equity curve
try:
    from trade_memory import (
        TradeEvaluator, log_entry, log_exit,
        build_snapshot, detect_regime, init_db,
        log_equity, get_daily_pnl
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
                 symbols: List[str], dry_run: bool = False):
        self.client  = client
        self.risk    = risk
        self.symbols = symbols
        self.dry_run = dry_run  # Paper trading: log alles maar geen echte orders
        self.sl_tp:    Dict[str, dict] = {}
        self.trade_ids: Dict[str, int]  = {}
        if dry_run:
            logger.info("📝 PAPER TRADING MODE — geen echte orders")

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
        """
        BTC v3 — Dual mode: TREND + RANGE auto-switch op ADX.
        
        ADX > 25: TREND MODE (pullback + trend-following)
        ADX < 20: RANGE MODE (BB bounce + RSI extreme)
        ADX 20-25: beide actief, lagere confidence
        """
        c, h, l, v = o['closes'], o['highs'], o['lows'], o['volumes']
        e21 = ema(c, 21); e50 = ema(c, 50); rv = rsi(c, 14)
        a = adx(h, l, c, 14)
        bbu, bbm, bbl = bollinger_bands(c, 20, 2.0)
        
        if not all([e21, e50, rv]) or len(c) < 3:
            return 'none', 0.0
        
        price = c[-1]; cr = rv[-1]
        cur_adx = a[-1] if a else 15
        trend_bull = e21[-1] > e50[-1]
        trend_bear = e21[-1] < e50[-1]
        
        # Multi-TF trend override
        if trend and trend.get('closes'):
            tc = trend['closes']
            te21 = ema(tc, 21); te50 = ema(tc, 50)
            if te21 and te50:
                trend_bull = te21[-1] > te50[-1]
                trend_bear = te21[-1] < te50[-1]
        
        dist_e21 = (price - e21[-1]) / e21[-1] if e21[-1] else 0
        
        # ══════ RANGE MODE (ADX < 25) ══════
        # Mean-reversion: koop bij lower BB + RSI oversold, verkoop bij upper BB + overbought
        if cur_adx < 25 and bbu and bbl and len(bbl) >= 2:
            # Long: prijs raakt/doorbreekt lower BB + RSI laag
            if len(bbl) >= 2 and c[-2] < bbl[-2] and price > bbl[-1] and cr < 40:
                conf = 0.78 if cr < 28 else 0.68
                return 'long', conf
            # Short: prijs raakt upper BB + RSI hoog
            if len(bbu) >= 2 and c[-2] > bbu[-2] and price < bbu[-1] and cr > 60:
                conf = 0.78 if cr > 72 else 0.68
                return 'short', conf
            # RSI extreme bounce (zeldzamer maar sterker)
            if cr < 25:
                return 'long', 0.82
            if cr > 75:
                return 'short', 0.82
        
        # ══════ TREND MODE (ADX >= 20) ══════
        if not trend_bull and not trend_bear:
            return 'none', 0.0
        
        adx_bonus = 0.05 if cur_adx > 30 else 0.0
        
        # Trigger A: EMA Pullback
        if trend_bull and -0.015 < dist_e21 < 0.005 and cr < 50:
            base = 0.72 if cr < 40 else 0.65
            return 'long', min(base + adx_bonus, 0.90)
        if trend_bear and -0.005 < dist_e21 < 0.015 and cr > 50:
            base = 0.72 if cr > 60 else 0.65
            return 'short', min(base + adx_bonus, 0.90)
        
        # Trigger B: RSI extreme in trend
        if trend_bull and cr < 35:
            return 'long', min(0.80 + adx_bonus, 0.92)
        if trend_bear and cr > 65:
            return 'short', min(0.80 + adx_bonus, 0.92)
        
        # Trigger C: Trend-following
        if trend_bull and 40 < cr < 65 and cur_adx > 22:
            e21s = _slope(e21, 5) if len(e21) > 5 else 0
            if dist_e21 > 0 and e21s > 0.0005:
                return 'long', min(0.60 + adx_bonus, 0.75)
        if trend_bear and 35 < cr < 60 and cur_adx > 22:
            e21s = _slope(e21, 5) if len(e21) > 5 else 0
            if dist_e21 < 0 and e21s < -0.0005:
                return 'short', min(0.60 + adx_bonus, 0.75)
        
        return 'none', 0.0

    # ==========================================================================
    # ETH  --  Dual mode: Trend pullback + Range mean-reversion (v2)
    #
    # ADX > 25: Trend mode (EMA pullback + MACD confirmation)
    # ADX < 20: Range mode (BB bounce + RSI extreme)
    # Signaalfrequentie target: 5-15% (niet 96% zoals de oude versie)
    # ==========================================================================
    def _signal_eth(self, o: dict) -> Tuple[str, float]:
        c, h, l, v = o['closes'], o['highs'], o['lows'], o['volumes']
        e21 = ema(c, 21); e50 = ema(c, 50); rv = rsi(c, 14)
        a = adx(h, l, c, 14)
        bbu, bbm, bbl = bollinger_bands(c, 20, 2.0)
        ml, sl_line, hist = macd(c, 12, 26, 9)

        if not all([e21, e50, rv, bbu]) or len(c) < 3:
            return 'none', 0.0

        price = c[-1]; cr = rv[-1]
        cur_adx = a[-1] if a else 15
        dist_e21 = (price - e21[-1]) / e21[-1] if e21[-1] else 0
        trend_bull = e21[-1] > e50[-1]
        trend_bear = e21[-1] < e50[-1]

        # ══════ RANGE MODE (ADX < 25) ══════
        if cur_adx < 25 and bbl and len(bbl) >= 2:
            if len(bbl) >= 2 and c[-2] < bbl[-2] and price > bbl[-1] and cr < 40:
                conf = 0.78 if cr < 28 else 0.68
                return 'long', conf
            if len(bbu) >= 2 and c[-2] > bbu[-2] and price < bbu[-1] and cr > 60:
                conf = 0.78 if cr > 72 else 0.68
                return 'short', conf
            # RSI < 25 / > 75 verwijderd: verliespatroon (WR 23%)

        # ══════ TREND MODE ══════
        if not trend_bull and not trend_bear:
            return 'none', 0.0

        adx_bonus = 0.05 if cur_adx > 30 else 0.0
        macd_bull = sl_line and len(ml) >= 2 and ml[-2] <= sl_line[-2] and ml[-1] > sl_line[-1]
        macd_bear = sl_line and len(ml) >= 2 and ml[-2] >= sl_line[-2] and ml[-1] < sl_line[-1]

        # Pullback + MACD
        if trend_bull and -0.015 < dist_e21 < 0.005 and cr < 50:
            base = 0.72 if macd_bull else 0.65
            return 'long', min(base + adx_bonus, 0.90)
        if trend_bear and -0.005 < dist_e21 < 0.015 and cr > 50:
            base = 0.72 if macd_bear else 0.65
            return 'short', min(base + adx_bonus, 0.90)

        # RSI extreme in trend
        if trend_bull and cr < 35:
            return 'long', min(0.78 + adx_bonus, 0.90)
        if trend_bear and cr > 65:
            return 'short', min(0.78 + adx_bonus, 0.90)

        # Trend-following fallback
        if trend_bull and 40 < cr < 65 and cur_adx > 22:
            e21s = _slope(e21, 5) if len(e21) > 5 else 0
            if dist_e21 > 0 and e21s > 0.0005:
                return 'long', min(0.58 + adx_bonus, 0.72)
        if trend_bear and 35 < cr < 60 and cur_adx > 22:
            e21s = _slope(e21, 5) if len(e21) > 5 else 0
            if dist_e21 < 0 and e21s < -0.0005:
                return 'short', min(0.58 + adx_bonus, 0.72)

        return 'none', 0.0

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
        """XRP v3 — Dual mode: trend pullback + range mean-reversion."""
        c, h, l, v = o['closes'], o['highs'], o['lows'], o['volumes']
        e21 = ema(c, 21); e50 = ema(c, 50); rv = rsi(c, 14)
        a = adx(h, l, c, 14)
        bbu, bbm, bbl = bollinger_bands(c, 20, 2.0)

        if not all([e21, e50, rv, bbu]) or len(c) < 3:
            return 'none', 0.0

        price = c[-1]; cr = rv[-1]
        cur_adx = a[-1] if a else 15
        dist_e21 = (price - e21[-1]) / e21[-1] if e21[-1] else 0
        trend_bull = e21[-1] > e50[-1]
        trend_bear = e21[-1] < e50[-1]

        # ══════ RANGE MODE (ADX < 25) ══════
        if cur_adx < 25 and bbl and len(bbl) >= 2:
            if len(bbl) >= 2 and c[-2] < bbl[-2] and price > bbl[-1] and cr < 40:
                return 'long', 0.78 if cr < 28 else 0.68
            if len(bbu) >= 2 and c[-2] > bbu[-2] and price < bbu[-1] and cr > 60:
                return 'short', 0.78 if cr > 72 else 0.68
            # RSI < 25 / > 75 verwijderd: verliespatroon (WR 23%)

        # ══════ TREND MODE ══════
        # Trigger A: Pullback
        if trend_bull and -0.015 < dist_e21 < 0.005 and cr < 50:
            base = 0.85 if cr < 30 else 0.78 if cr < 38 else 0.68
            if c[-1] < c[-2]: base += 0.03
            return 'long', min(base, 0.95)
        if trend_bear and -0.005 < dist_e21 < 0.015 and cr > 50:
            base = 0.85 if cr > 70 else 0.78 if cr > 62 else 0.68
            if c[-1] > c[-2]: base += 0.03
            return 'short', min(base, 0.95)

        # Trigger B: BB bounce
        if bbl and len(bbl) >= 2:
            if c[-2] < bbl[-2] and price > bbl[-1] and cr < 35:
                return 'long', 0.88 if cr < 25 else 0.75
            if c[-2] > bbu[-2] and price < bbu[-1] and cr > 65:
                return 'short', 0.88 if cr > 75 else 0.75

        # Trigger C: Trend-following (verlaagde slope drempel)
        if trend_bull and 35 < cr < 60:
            e21s = _slope(e21, 5) if len(e21) > 5 else 0
            if dist_e21 > 0 and e21s > 0.0003:
                return 'long', 0.62
        if trend_bear and 40 < cr < 65:
            e21s = _slope(e21, 5) if len(e21) > 5 else 0
            if dist_e21 < 0 and e21s < -0.0003:
                return 'short', 0.62

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
    # ADA  --  Dual mode: Trend + Range (v2 — herschreven)
    # Zelfde framework als BTC/ETH/XRP: ADX-based mode switch
    # ==========================================================================
    def _signal_ada(self, o: dict) -> Tuple[str, float]:
        c, h, l, v = o['closes'], o['highs'], o['lows'], o['volumes']
        e21 = ema(c, 21); e50 = ema(c, 50); rv = rsi(c, 14)
        a = adx(h, l, c, 14)
        bbu, bbm, bbl = bollinger_bands(c, 20, 2.0)

        if not all([e21, e50, rv, bbu]) or len(c) < 3:
            return 'none', 0.0

        price = c[-1]; cr = rv[-1]
        cur_adx = a[-1] if a else 15
        dist_e21 = (price - e21[-1]) / e21[-1] if e21[-1] else 0
        trend_bull = e21[-1] > e50[-1]
        trend_bear = e21[-1] < e50[-1]

        # ══════ RANGE MODE (ADX < 25) ══════
        if cur_adx < 25 and bbl and len(bbl) >= 2:
            if len(bbl) >= 2 and c[-2] < bbl[-2] and price > bbl[-1] and cr < 40:
                return 'long', 0.78 if cr < 28 else 0.68
            if len(bbu) >= 2 and c[-2] > bbu[-2] and price < bbu[-1] and cr > 60:
                return 'short', 0.78 if cr > 72 else 0.68
            # RSI < 25 / > 75 verwijderd: verliespatroon (WR 23%)

        # ══════ TREND MODE ══════
        if not trend_bull and not trend_bear:
            return 'none', 0.0

        adx_bonus = 0.05 if cur_adx > 30 else 0.0

        # Pullback
        if trend_bull and -0.015 < dist_e21 < 0.005 and cr < 50:
            base = 0.72 if cr < 40 else 0.65
            return 'long', min(base + adx_bonus, 0.90)
        if trend_bear and -0.005 < dist_e21 < 0.015 and cr > 50:
            base = 0.72 if cr > 60 else 0.65
            return 'short', min(base + adx_bonus, 0.90)

        # RSI extreme
        if trend_bull and cr < 35: return 'long', 0.80
        if trend_bear and cr > 65: return 'short', 0.80

        # Trend-following
        if trend_bull and 40 < cr < 65 and cur_adx > 22:
            e21s = _slope(e21, 5) if len(e21) > 5 else 0
            if dist_e21 > 0 and e21s > 0.0005:
                return 'long', min(0.58 + adx_bonus, 0.72)
        if trend_bear and 35 < cr < 60 and cur_adx > 22:
            e21s = _slope(e21, 5) if len(e21) > 5 else 0
            if dist_e21 < 0 and e21s < -0.0005:
                return 'short', min(0.58 + adx_bonus, 0.72)

        return 'none', 0.0

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

            # === VERLIESFILTER: blokkeer historisch verliesgevende patronen ===
            if sig != 'none':
                sig, conf = self._loss_filter(ohlcv, sig, conf)

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

    def _loss_filter(self, ohlcv: dict, sig: str, conf: float) -> Tuple[str, float]:
        """
        VERLIESFILTER — gebaseerd op analyse van 20.000+ verliezende trades.
        Blokkeert trades die historisch verliesgevend zijn.
        
        6 filters:
        1. RSI extremen (< 25 of > 75) → vallend mes
        2. Tegen de EMA trend → counter-trend verliest
        3. Onder lower BB (long) → prijs valt door
        4. ADX 20-25 = niemandsland → geen edge
        5. Confidence > 82% = overconfident
        6. Prijs > 2% van EMA21 → te ver uitgerekt
        """
        c = ohlcv['closes']; h = ohlcv['highs']; l = ohlcv['lows']
        rv = rsi(c, 14)
        e21 = ema(c, 21); e50 = ema(c, 50)
        a = adx(h, l, c, 14)
        bbu, bbm, bbl = bollinger_bands(c, 20, 2.0)
        
        cr = rv[-1] if rv else 50
        cur_adx = a[-1] if a else 15
        
        # Filter 1: RSI extremen — grootste verliezer (WR 23% bij RSI<25)
        if cr < 25 or cr > 75:
            logger.debug(f"Loss filter: RSI extreme ({cr:.0f})")
            return 'none', 0.0
        
        # Filter 2: VERWIJDERD — blokkeerde range-mode mean-reversion trades
        # Range mode is bewust counter-trend, dat is het hele punt
        
        # Filter 3: Onder lower BB = vallend mes (WR 34%)
        if sig == 'long' and bbl and c[-1] < bbl[-1]:
            logger.debug(f"Loss filter: long onder lower BB")
            return 'none', 0.0
        if sig == 'short' and bbu and c[-1] > bbu[-1]:
            logger.debug(f"Loss filter: short boven upper BB")
            return 'none', 0.0
        
        # Filter 4: ADX niemandsland (20-25) — geen edge
        if 20 <= cur_adx <= 25:
            logger.debug(f"Loss filter: ADX niemandsland ({cur_adx:.0f})")
            return 'none', 0.0
        
        # Filter 5: Overconfident (conf > 82% → WR daalt)
        if conf > 0.82:
            conf = 0.82
        
        # Filter 6: Te ver van EMA21
        if e21 and e21[-1] > 0:
            dist = (c[-1] - e21[-1]) / e21[-1]
            if sig == 'long' and dist < -0.02:
                logger.debug(f"Loss filter: long te ver onder EMA21 ({dist*100:.1f}%)")
                return 'none', 0.0
            if sig == 'short' and dist > 0.02:
                logger.debug(f"Loss filter: short te ver boven EMA21 ({dist*100:.1f}%)")
                return 'none', 0.0
        
        return sig, conf

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

            # === TRAILING STOP: verplaats SL mee als prijs gunstig beweegt ===
            if not hit_sl and not hit_tp:
                entry = sl_tp.get('entry', mark)
                # Bereken hoeveel de prijs gunstig bewogen is
                if is_long:
                    favorable = (mark - entry) / entry if entry > 0 else 0
                else:
                    favorable = (entry - mark) / entry if entry > 0 else 0
                # Na 1% gunstige beweging: verplaats SL naar break-even
                if favorable > 0.01 and ((is_long and sl < entry) or (not is_long and sl > entry)):
                    new_sl = entry  # break-even
                    self.sl_tp[contract]['sl'] = new_sl
                    logger.info(f"[{contract}] Trailing SL → break-even: {new_sl:.6f}")
                    # Update op exchange
                    try:
                        await self.client.cancel_all_price_orders(contract)
                        await self.client.place_stop_loss(contract, is_long, new_sl, abs(size))
                        await self.client.place_take_profit(contract, is_long, tp, abs(size))
                    except Exception:
                        pass
                # Na 2% gunstige beweging: lock 50% van winst
                elif favorable > 0.02:
                    lock_pct = favorable * 0.5
                    new_sl = entry * (1 + lock_pct) if is_long else entry * (1 - lock_pct)
                    if (is_long and new_sl > sl) or (not is_long and new_sl < sl):
                        self.sl_tp[contract]['sl'] = new_sl
                        logger.info(f"[{contract}] Trailing SL → profit lock: {new_sl:.6f} ({lock_pct*100:.1f}%)")
                        try:
                            await self.client.cancel_all_price_orders(contract)
                            await self.client.place_stop_loss(contract, is_long, new_sl, abs(size))
                            await self.client.place_take_profit(contract, is_long, tp, abs(size))
                        except Exception:
                            pass
                continue

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
                    # Telegram notificatie bij close
                    if TG_ENABLED:
                        try:
                            lev = self.sl_tp.get(contract, {}).get('leverage', 1) or 1
                            direction = 'long' if size > 0 else 'short'
                            roi = pnl / (abs(size) / lev) * 100 if lev > 0 and size != 0 else 0
                            await notify_close(contract, direction, pnl, roi, reason)
                        except Exception:
                            pass
                    # Equity curve log
                    if MEMORY_ENABLED:
                        try:
                            acc = await self.client.get_account()
                            if acc:
                                log_equity(float(acc.get('available', 0)), 'trade_close',
                                           symbol=contract)
                        except Exception:
                            pass
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

        # === DRAWDOWN CIRCUIT BREAKER ===
        if self.risk.check_drawdown(balance):
            logger.warning("🚨 CIRCUIT BREAKER — drawdown limiet bereikt, geen trading")
            if TG_ENABLED:
                await notify_alert("Circuit Breaker",
                    f"Drawdown > {self.risk.max_drawdown_pct:.0%}. Alle trading gestopt.")
            return

        if self.risk.is_daily_loss_exceeded(balance):
            logger.warning("Dagverlies limiet bereikt — geen nieuwe trades")
            if TG_ENABLED:
                await notify_alert("Daggrens", "Trading gepauzeerd.")
            return

        # === BTC CORRELATIE CHECK ===
        btc_bullish = None
        try:
            btc_raw = await self.client.get_candles('BTC_USDT', '4h', 60)
            if btc_raw and len(btc_raw) >= 50:
                btc_c = [float(c[4] if isinstance(c, list) else c.get('c', 0)) for c in btc_raw]
                be21 = ema(btc_c, 21); be50 = ema(btc_c, 50)
                if be21 and be50:
                    btc_bullish = be21[-1] > be50[-1]
                    logger.info(f"BTC trend: {'BULL' if btc_bullish else 'BEAR'}")
        except Exception:
            pass

        await self._manage_open_positions()
        all_pos = await self.client.get_positions()
        open_contracts = set()

        for p in all_pos:
            size = int(p.get('size', 0))
            if size == 0:
                continue
            contract = p['contract']
            open_contracts.add(contract)

            # === LIQUIDATIE CHECK ===
            try:
                liq = float(p.get('liq_price', 0))
                mark = float(p.get('mark_price', 0))
                if liq > 0 and mark > 0:
                    dist = ((mark - liq) / mark * 100) if size > 0 else ((liq - mark) / mark * 100)
                    if dist < 3:
                        logger.warning(f"🚨 LIQUIDATIE [{contract}] {dist:.1f}% — auto-reduce!")
                        await self.client.reduce_position(contract, 50)
                        if TG_ENABLED:
                            await notify_liquidation_warning(contract, liq, mark, dist)
                    elif dist < 5:
                        logger.warning(f"⚠ LIQUIDATIE [{contract}] {dist:.1f}%")
                        if TG_ENABLED:
                            await notify_alert("Liq risico", f"{contract}: {dist:.1f}%")
            except Exception:
                pass

            # === FUNDING RATE LOG ===
            try:
                fr = await self.client.get_funding_rate(contract)
                if fr and abs(fr['funding_rate']) > 0.0003:
                    rate = fr['funding_rate']
                    recv = (rate < 0 and size > 0) or (rate > 0 and size < 0)
                    logger.info(f"[{contract}] Funding: {rate*100:.4f}% ({'✓' if recv else '✗'})")
            except Exception:
                pass

        logger.info(f"Open posities: {open_contracts or 'geen'}")

        # === MAX POSITIES CHECK ===
        if not self.risk.can_open_position(len(open_contracts)):
            logger.info("Geen nieuwe trades: max posities of circuit breaker")
            return

        for symbol in self.symbols:
            try:
                if symbol in open_contracts:
                    continue

                ohlcv = await self._fetch_ohlcv(symbol)
                if not ohlcv:
                    logger.warning(f"[{symbol}] Onvoldoende candles")
                    continue

                # Multi-TF trend data
                trend_data = None
                try:
                    trend_data = await self._fetch_trend_data(symbol)
                except Exception as te:
                    logger.debug(f"[{symbol}] Trend data mislukt: {te}")

                signal, confidence = self.generate_signal(ohlcv, symbol, trend_data)

                # BTC correlatie penalty op altcoins
                if btc_bullish is not None and 'BTC' not in symbol:
                    if signal == 'long' and not btc_bullish:
                        confidence *= 0.7
                        logger.info(f"[{symbol}] BTC bearish → long conf {confidence:.0%}")
                    elif signal == 'short' and btc_bullish:
                        confidence *= 0.8

                price      = ohlcv['closes'][-1]
                volatility = calculate_volatility_score(ohlcv['closes'])
                interval   = ohlcv.get('interval', '?')
                trend_tf   = trend_data['interval'] if trend_data else '-'
                logger.info(
                    f"[{symbol}] {signal.upper():5} conf={confidence:.0%} "
                    f"prijs={price:.6f} vol={volatility:.2f} tf={interval}+{trend_tf}"
                )
                if signal == 'none' or confidence < 0.45:
                    continue
                atr_mult        = self._get_atr_multiplier(symbol)
                tp_ratio        = self._get_tp_ratio(symbol)
                sl_pct, tp_pct  = atr_stop(ohlcv['highs'], ohlcv['lows'],
                                            ohlcv['closes'], multiplier=atr_mult,
                                            tp_ratio=tp_ratio)
                leverage = self.risk.get_optimal_leverage(volatility, sl_pct=sl_pct)
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
                    f"{' [PAPER]' if self.dry_run else ''}"
                )

                # === PAPER TRADING MODE ===
                if self.dry_run:
                    logger.info(f"[{symbol}] 📝 PAPER TRADE — niet uitgevoerd")
                    # Log als echte trade in memory voor tracking
                    if MEMORY_ENABLED:
                        try:
                            snap = build_snapshot(ohlcv, {'signal': signal, 'atr_mult': atr_mult})
                            regime = detect_regime(snap)
                            trade_id = log_entry(
                                symbol=symbol, strategy=symbol.split('_')[0].lower(),
                                direction=signal, entry_price=price,
                                contracts=contracts, leverage=leverage,
                                confidence=confidence, snapshot=snap,
                                market_regime=regime,
                            )
                            self.trade_ids[symbol] = trade_id
                            logger.info(f"[{symbol}] Paper trade #{trade_id} gelogd")
                        except Exception:
                            pass
                    continue

                # === ORDER EXECUTIE MET RETRY ===
                result = None
                for attempt in range(3):
                    result = await self.client.place_order(symbol, order_size)
                    if result:
                        break
                    logger.warning(f"[{symbol}] Order poging {attempt+1}/3 mislukt, retry...")
                    await asyncio.sleep(1)
                if not result:
                    logger.error(f"[{symbol}] Order mislukt: {self.client.last_error}")
                    continue
                sl_price = self.risk.get_stop_loss_price(price, is_long, sl_pct)
                tp_price = self.risk.get_take_profit_price(price, is_long, tp_pct)
                await self.client.cancel_all_price_orders(symbol)
                sl_ok = await self.client.place_stop_loss(symbol, is_long, sl_price, contracts)
                tp_ok = await self.client.place_take_profit(symbol, is_long, tp_price, contracts)
                self.sl_tp[symbol] = {
                    'sl': sl_price, 'tp': tp_price, 'is_long': is_long,
                    'leverage': leverage, 'entry': price,
                }
                logger.info(
                    f"[{symbol}] OK entry~{price:.6f} "
                    f"SL={sl_price:.6f}[{'OK' if sl_ok else 'FAIL'}] "
                    f"TP={tp_price:.6f}[{'OK' if tp_ok else 'FAIL'}]"
                )

                # Telegram notificatie
                if TG_ENABLED:
                    roi_tp = tp_pct * leverage * 100
                    await notify_trade(symbol, signal, price, confidence, leverage,
                                       sl_price, tp_price, roi_tp)

                # Equity curve log
                if MEMORY_ENABLED:
                    log_equity(balance, 'trade_open', symbol=symbol)

                # Trade memory
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
