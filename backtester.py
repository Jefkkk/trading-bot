# -*- coding: utf-8 -*-
"""
Backtester v5  --  alle signaalfuncties identiek aan strategy.py.
PnL: 1 contract = 1 USD notional (Gate.io USDT perps)
"""
import logging, math
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import List, Optional
from dataclasses import dataclass, field

TZ = ZoneInfo('Europe/Brussels')
from indicators import (
    ema, sma, rsi, macd, bollinger_bands, bollinger_width,
    stochastic, vwap, atr, atr_stop, adx, obv, obv_slope,
    supertrend, calculate_volatility_score
)

logger = logging.getLogger('Backtester')


@dataclass
class Trade:
    symbol: str; strategy: str; direction: str
    entry_price: float; entry_index: int; entry_ts: str
    contracts: int; leverage: int
    stop_loss: float; take_profit: float
    exit_price: float = 0.0; exit_index: int = 0
    exit_ts: str = ''; exit_reason: str = ''
    pnl: float = 0.0; pnl_pct: float = 0.0; closed: bool = False
    fees: float = 0.0
    confidence: float = 0.0        # signaal confidence bij entry
    roi_trailing: bool = False     # True = trailing modus actief (geen vaste TP)
    roi_trail_activated: bool = False  # True = ROI target bereikt, trail loopt


@dataclass
class BacktestResult:
    symbol: str; strategy: str; period_from: str; period_to: str
    trades: List[Trade] = field(default_factory=list)
    total_pnl: float = 0.0; win_count: int = 0; loss_count: int = 0
    max_drawdown: float = 0.0; sharpe_ratio: float = 0.0
    initial_balance: float = 0.0; final_balance: float = 0.0
    equity_curve: List[float] = field(default_factory=list)
    timestamps: List[str] = field(default_factory=list)
    total_fees: float = 0.0

    @property
    def total_trades(self): return len(self.trades)
    @property
    def win_rate(self): return (self.win_count/self.total_trades*100) if self.total_trades else 0.0
    @property
    def profit_factor(self):
        g=sum(t.pnl for t in self.trades if t.pnl>0)
        l=abs(sum(t.pnl for t in self.trades if t.pnl<0))
        return g/l if l>0 else float('inf')
    @property
    def avg_win(self):
        w=[t.pnl for t in self.trades if t.pnl>0]; return sum(w)/len(w) if w else 0.0
    @property
    def avg_loss(self):
        l=[t.pnl for t in self.trades if t.pnl<0]; return sum(l)/len(l) if l else 0.0
    @property
    def return_pct(self):
        return (self.final_balance-self.initial_balance)/self.initial_balance*100 if self.initial_balance else 0.0
    @property
    def avg_trade_pct(self):
        return sum(t.pnl_pct for t in self.trades)/self.total_trades if self.total_trades else 0.0
    @property
    def best_trade(self): return max((t.pnl for t in self.trades), default=0.0)
    @property
    def worst_trade(self): return min((t.pnl for t in self.trades), default=0.0)
    @property
    def expectancy(self):
        """Gemiddelde PnL per trade — de echte maatstaf voor edge."""
        return self.total_pnl / self.total_trades if self.total_trades else 0.0
    @property
    def avg_duration(self):
        """Gemiddeld aantal bars per trade."""
        if not self.trades: return 0
        return sum(t.exit_index - t.entry_index for t in self.trades) / len(self.trades)
    @property
    def max_consecutive_losses(self):
        cur = mx = 0
        for t in self.trades:
            if t.pnl <= 0: cur += 1; mx = max(mx, cur)
            else: cur = 0
        return mx
    @property
    def max_consecutive_wins(self):
        cur = mx = 0
        for t in self.trades:
            if t.pnl > 0: cur += 1; mx = max(mx, cur)
            else: cur = 0
        return mx
    @property
    def calmar_ratio(self):
        """Return % / Max Drawdown % — hoe hoger hoe beter."""
        if self.max_drawdown == 0: return 0.0
        return round(self.return_pct / self.max_drawdown, 2)
    @property
    def pnl_after_fees(self):
        return round(self.total_pnl - self.total_fees, 4)
    @property
    def long_stats(self):
        lt = [t for t in self.trades if t.direction == 'long']
        w = sum(1 for t in lt if t.pnl > 0)
        pnl = sum(t.pnl for t in lt)
        return {'count': len(lt), 'wins': w, 'wr': round(w/len(lt)*100,1) if lt else 0, 'pnl': round(pnl,4)}
    @property
    def short_stats(self):
        st = [t for t in self.trades if t.direction == 'short']
        w = sum(1 for t in st if t.pnl > 0)
        pnl = sum(t.pnl for t in st)
        return {'count': len(st), 'wins': w, 'wr': round(w/len(st)*100,1) if st else 0, 'pnl': round(pnl,4)}


# -- helpers ------------------------------------------------------------------
def _p(lst, d=0.0):
    return lst[-2] if len(lst)>=2 else (lst[-1] if lst else d)

def _slope(lst, n=5):
    if len(lst)<n+1: return 0.0
    s=lst[-(n+1)]; return (lst[-1]-s)/abs(s) if s else 0.0

def _rvwap(h,l,c,v,n=20):
    k=min(n,len(c))
    tv=sum(((h[i]+l[i]+c[i])/3)*v[i] for i in range(-k,0))
    sv=sum(v[-k:])
    return tv/sv if sv>0 else c[-1]

def _adx_last(h,l,c,period=14):
    vals=adx(h,l,c,period); return vals[-1] if vals else 0.0


# ==============================================================================
# SIGNAALFUNCTIES  (v6  --  active scoring model, sync met strategy.py)
# ==============================================================================

def _active_score(votes):
    """Majority voting: percentage van actieve indicatoren in dominante richting."""
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


def _sig_btc(c, h, l, v):
    """BTC v3: Dual mode — trend + range auto-switch op ADX."""
    e21=ema(c,21); e50=ema(c,50); rv=rsi(c,14); a=adx(h,l,c,14)
    bbu,bbm,bbl=bollinger_bands(c,20,2.0)
    if not all([e21,e50,rv]) or len(c)<3: return 'none',0.0
    price=c[-1]; cr=rv[-1]; cur_adx=a[-1] if a else 15
    trend_bull=e21[-1]>e50[-1]; trend_bear=e21[-1]<e50[-1]
    dist_e21=(price-e21[-1])/e21[-1] if e21[-1] else 0
    # RANGE MODE
    if cur_adx<25 and bbu and bbl and len(bbl)>=2:
        if price<bbl[-1] and cr<30: return 'long', 0.80 if cr<25 else 0.70
        if price>bbu[-1] and cr>70: return 'short', 0.80 if cr>75 else 0.70
        if cr<25: return 'long', 0.82
        if cr>75: return 'short', 0.82
    # TREND MODE
    if not trend_bull and not trend_bear: return 'none',0.0
    ab=0.05 if cur_adx>30 else 0.0
    if trend_bull and -0.015<dist_e21<0.005 and cr<50:
        return 'long', min((0.72 if cr<40 else 0.65)+ab, 0.90)
    if trend_bear and -0.005<dist_e21<0.015 and cr>50:
        return 'short', min((0.72 if cr>60 else 0.65)+ab, 0.90)
    if trend_bull and cr<35: return 'long', min(0.80+ab, 0.92)
    if trend_bear and cr>65: return 'short', min(0.80+ab, 0.92)
    if trend_bull and 40<cr<65 and cur_adx>22:
        if dist_e21>0 and _slope(e21,5)>0.0005: return 'long', min(0.60+ab, 0.75)
    if trend_bear and 35<cr<60 and cur_adx>22:
        if dist_e21<0 and _slope(e21,5)<-0.0005: return 'short', min(0.60+ab, 0.75)
    return 'none',0.0


def _sig_eth(c, h, l, v):
    """ETH v2: Dual mode — trend + range."""
    e21=ema(c,21); e50=ema(c,50); rv=rsi(c,14); a=adx(h,l,c,14)
    bbu,bbm,bbl=bollinger_bands(c,20,2.0)
    ml,sl,hist=macd(c,12,26,9)
    if not all([e21,e50,rv,bbu]) or len(c)<3: return 'none',0.0
    price=c[-1]; cr=rv[-1]; cur_adx=a[-1] if a else 15
    dist_e21=(price-e21[-1])/e21[-1] if e21[-1] else 0
    trend_bull=e21[-1]>e50[-1]; trend_bear=e21[-1]<e50[-1]
    # RANGE MODE
    if cur_adx<25 and bbl and len(bbl)>=2:
        if price<bbl[-1] and cr<30: return 'long', 0.80 if cr<25 else 0.70
        if price>bbu[-1] and cr>70: return 'short', 0.80 if cr>75 else 0.70
        if cr<25: return 'long', 0.82
        if cr>75: return 'short', 0.82
    # TREND MODE
    if not trend_bull and not trend_bear: return 'none',0.0
    ab=0.05 if cur_adx>30 else 0.0
    macd_b=sl and len(ml)>=2 and ml[-2]<=sl[-2] and ml[-1]>sl[-1]
    macd_s=sl and len(ml)>=2 and ml[-2]>=sl[-2] and ml[-1]<sl[-1]
    if trend_bull and -0.015<dist_e21<0.005 and cr<50:
        return 'long', min((0.72 if macd_b else 0.65)+ab, 0.90)
    if trend_bear and -0.005<dist_e21<0.015 and cr>50:
        return 'short', min((0.72 if macd_s else 0.65)+ab, 0.90)
    if trend_bull and cr<35: return 'long', min(0.78+ab, 0.90)
    if trend_bear and cr>65: return 'short', min(0.78+ab, 0.90)
    if trend_bull and 40<cr<65 and cur_adx>22:
        if dist_e21>0 and _slope(e21,5)>0.0005: return 'long', min(0.58+ab, 0.72)
    if trend_bear and 35<cr<60 and cur_adx>22:
        if dist_e21<0 and _slope(e21,5)<-0.0005: return 'short', min(0.58+ab, 0.72)
    return 'none',0.0

def _sig_xrp(c, h, l, v):
    """XRP v3: Dual mode — trend pullback + range mean-reversion."""
    e21=ema(c,21); e50=ema(c,50); rv=rsi(c,14); a=adx(h,l,c,14)
    bbu,bbm,bbl=bollinger_bands(c,20,2.0)
    if not all([e21,e50,rv,bbu]) or len(c)<3: return 'none',0.0
    price=c[-1]; cr=rv[-1]; cur_adx=a[-1] if a else 15
    dist_e21=(price-e21[-1])/e21[-1] if e21[-1] else 0
    trend_bull=e21[-1]>e50[-1]; trend_bear=e21[-1]<e50[-1]
    # RANGE MODE
    if cur_adx<25 and bbl and len(bbl)>=2:
        if price<bbl[-1] and cr<30: return 'long', 0.80 if cr<25 else 0.70
        if price>bbu[-1] and cr>70: return 'short', 0.80 if cr>75 else 0.70
        if cr<25: return 'long', 0.82
        if cr>75: return 'short', 0.82
    # TREND MODE
    if trend_bull and -0.015<dist_e21<0.005 and cr<50:
        base=0.85 if cr<30 else 0.78 if cr<38 else 0.68
        if c[-1]<c[-2]: base+=0.03
        return 'long', min(base, 0.95)
    if trend_bear and -0.005<dist_e21<0.015 and cr>50:
        base=0.85 if cr>70 else 0.78 if cr>62 else 0.68
        if c[-1]>c[-2]: base+=0.03
        return 'short', min(base, 0.95)
    if bbl and len(bbl)>=2:
        if c[-2]<bbl[-2] and price>bbl[-1] and cr<35: return 'long', 0.88 if cr<25 else 0.75
        if c[-2]>bbu[-2] and price<bbu[-1] and cr>65: return 'short', 0.88 if cr>75 else 0.75
    if trend_bull and 35<cr<60:
        if dist_e21>0 and _slope(e21,5)>0.0003: return 'long', 0.62
    if trend_bear and 40<cr<65:
        if dist_e21<0 and _slope(e21,5)<-0.0003: return 'short', 0.62
    return 'none',0.0

def _sig_fartcoin(c, h, l, v):
    """FARTCOIN: Trend + Dip entry. 3 hard gates → alleen hoge kwaliteit trades."""
    e5=ema(c,5); e13=ema(c,13); e21=ema(c,21); e50=ema(c,50)
    rv=rsi(c,7); rv14=rsi(c,14)
    if not all([e5,e13,e21,e50,rv,rv14]): return 'none',0.0
    if len(v)<20 or len(c)<6: return 'none',0.0

    price=c[-1]; cr=rv[-1]; cr14=rv14[-1]
    pm3=(c[-1]-c[-4])/c[-4] if c[-4]>0 else 0

    # GATE 1: Trend (verlaagd: 0.15% slope)
    e50s=_slope(e50,10)
    tup=e50s>0.0015; tdn=e50s<-0.0015
    if not tup and not tdn: return 'none',0.0

    # GATE 2: EMA ribbon (versoepeld: 2 van 3 aligned)
    rbull=(e5[-1]>e13[-1] and e13[-1]>e21[-1]) or (e5[-1]>e21[-1] and e13[-1]>e21[-1])
    rbear=(e5[-1]<e13[-1] and e13[-1]<e21[-1]) or (e5[-1]<e21[-1] and e13[-1]<e21[-1])
    if tup and not rbull: return 'none',0.0
    if tdn and not rbear: return 'none',0.0

    # GATE 3: Dip/rally aanwezig (verlaagd: 0.2%)
    has_dip=tup and pm3<-0.002
    has_rally=tdn and pm3>0.002
    if not has_dip and not has_rally: return 'none',0.0

    votes=[]

    # 1. Dip/rally diepte
    if has_dip:
        if pm3<-0.008: votes.extend([1,1,1])
        elif pm3<-0.005: votes.extend([1,1])
        else: votes.append(1)
    elif has_rally:
        if pm3>0.008: votes.extend([-1,-1,-1])
        elif pm3>0.005: votes.extend([-1,-1])
        else: votes.append(-1)

    # 2. RSI zone
    if tup and cr<40: votes.extend([1,1])
    elif tup and cr<50: votes.append(1)
    elif tdn and cr>60: votes.extend([-1,-1])
    elif tdn and cr>50: votes.append(-1)
    else: votes.append(0)

    # 3. RSI 14 keert om
    pcr14=_p(rv14)
    if cr14<45 and cr14>pcr14: votes.append(1)
    elif cr14>55 and cr14<pcr14: votes.append(-1)
    else: votes.append(0)

    # 4. Prijs nabij EMA 21
    d21=(price-e21[-1])/e21[-1] if e21[-1]>0 else 0
    if tup and d21<0.005: votes.append(1)
    elif tdn and d21>-0.005: votes.append(-1)
    else: votes.append(0)

    # 5. Volume bonus
    if len(v)>=20:
        avg_vol=sum(v[-20:-1])/19
        if v[-1]>avg_vol*1.8:
            if has_dip: votes.append(1)
            elif has_rally: votes.append(-1)

    sig,conf=_active_score(votes)
    if sig=='long' and tdn: return 'none',0.0
    if sig=='short' and tup: return 'none',0.0
    return (sig,conf) if conf>=0.55 else ('none',0.0)


def _sig_ada(c, h, l, v):
    """ADA v2: Dual mode — trend + range."""
    e21=ema(c,21); e50=ema(c,50); rv=rsi(c,14); a=adx(h,l,c,14)
    bbu,bbm,bbl=bollinger_bands(c,20,2.0)
    if not all([e21,e50,rv,bbu]) or len(c)<3: return 'none',0.0
    price=c[-1]; cr=rv[-1]; cur_adx=a[-1] if a else 15
    dist_e21=(price-e21[-1])/e21[-1] if e21[-1] else 0
    trend_bull=e21[-1]>e50[-1]; trend_bear=e21[-1]<e50[-1]
    # RANGE MODE
    if cur_adx<25 and bbl and len(bbl)>=2:
        if price<bbl[-1] and cr<30: return 'long', 0.80 if cr<25 else 0.70
        if price>bbu[-1] and cr>70: return 'short', 0.80 if cr>75 else 0.70
        if cr<25: return 'long', 0.82
        if cr>75: return 'short', 0.82
    # TREND MODE
    if not trend_bull and not trend_bear: return 'none',0.0
    ab=0.05 if cur_adx>30 else 0.0
    if trend_bull and -0.015<dist_e21<0.005 and cr<50:
        return 'long', min((0.72 if cr<40 else 0.65)+ab, 0.90)
    if trend_bear and -0.005<dist_e21<0.015 and cr>50:
        return 'short', min((0.72 if cr>60 else 0.65)+ab, 0.90)
    if trend_bull and cr<35: return 'long', 0.80
    if trend_bear and cr>65: return 'short', 0.80
    if trend_bull and 40<cr<65 and cur_adx>22:
        if dist_e21>0 and _slope(e21,5)>0.0005: return 'long', min(0.58+ab, 0.72)
    if trend_bear and 35<cr<60 and cur_adx>22:
        if dist_e21<0 and _slope(e21,5)<-0.0005: return 'short', min(0.58+ab, 0.72)
    return 'none',0.0

def _sig_fartcoin_bb(c, h, l, v):
    """
    FARTCOIN Bollinger Bands mean-reversion (4H) — v2.
    1. EMA 50 trendfilter (responsief genoeg voor meme coin)
    2. RSI(14) bevestiging (versoepeld: < 45 voor long, > 55 voor short)
    3. Zonder trendfilter: alleen als RSI extreem is (< 30 / > 70)
    """
    bbu, bbm, bbl = bollinger_bands(c, 20, 2.0)
    rv = rsi(c, 14)
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
            return 'long', 0.85       # trend + RSI bevestigd
        elif trend_up and cur_rsi < 55:
            return 'long', 0.70       # trend OK, RSI neutraal
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
            return 'short', 0.65      # extreem overbought
        return 'none', 0.0

    return 'none', 0.0


def _sig_rsi_meanrev(c, h, l, v):
    """Pure RSI mean-reversion: koop oversold, verkoop overbought. Werkt op alle coins."""
    rv = rsi(c, 14)
    e50 = ema(c, 50)
    if not rv or not e50 or len(rv) < 2: return 'none', 0.0
    cr = rv[-1]; pcr = rv[-2]
    # Long: RSI < 30 en keert om (stijgt)
    if cr < 30 and cr > pcr:
        return 'long', 0.80
    elif cr < 25:
        return 'long', 0.90  # extreem oversold
    # Short: RSI > 70 en keert om (daalt)
    if cr > 70 and cr < pcr:
        return 'short', 0.80
    elif cr > 75:
        return 'short', 0.90
    return 'none', 0.0


def _sig_ema_cross(c, h, l, v):
    """EMA 9/21 crossover met EMA 50 trendfilter. Simpel maar effectief."""
    e9 = ema(c, 9); e21 = ema(c, 21); e50 = ema(c, 50)
    if not all([e9, e21, e50]) or len(e9) < 2 or len(e21) < 2: return 'none', 0.0
    # Cross detectie
    cross_up   = _p(e9) <= _p(e21) and e9[-1] > e21[-1]
    cross_down = _p(e9) >= _p(e21) and e9[-1] < e21[-1]
    # Trendfilter: alleen in richting van EMA 50
    if cross_up and c[-1] > e50[-1]:
        return 'long', 0.75
    if cross_down and c[-1] < e50[-1]:
        return 'short', 0.75
    return 'none', 0.0


def _sig_bb_bounce(c, h, l, v):
    """Generieke BB bounce (alle coins). BB(20,2) + RSI bevestiging."""
    bbu, bbm, bbl = bollinger_bands(c, 20, 2.0)
    rv = rsi(c, 14)
    if not bbu or len(bbu) < 2 or not rv: return 'none', 0.0
    price = c[-1]; prev = c[-2]; cr = rv[-1]
    # Long bounce
    if prev < bbl[-2] and price > bbl[-1] and cr < 40:
        return 'long', 0.75
    # Short rejection
    if prev > bbu[-2] and price < bbu[-1] and cr > 60:
        return 'short', 0.75
    return 'none', 0.0


STRATEGIES = {
    'btc_trend':         _sig_btc,
    'eth_squeeze':       _sig_eth,
    'xrp_roi':           _sig_xrp,
    'fartcoin_momentum': _sig_fartcoin,
    'fartcoin_bb':       _sig_fartcoin_bb,
    'ada_supertrend':    _sig_ada,
    'general_consensus': _sig_btc,  # consensus verwijderd, BTC als default
    'rsi_meanrev':       _sig_rsi_meanrev,
    'ema_crossover':     _sig_ema_cross,
    'bb_bounce':         _sig_bb_bounce,
}

TF_BASE_MULTIPLIERS = {
    '5m':  1.5,
    '15m': 2.0,
    '1h':  3.0,
    '4h':  5.0,
    '1d':  7.0,
}

TF_TP_RATIOS = {
    '5m':  1.0,    # symmetrisch — hogere winrate bereikbaar
    '15m': 1.0,    # symmetrisch
    '1h':  1.2,    # licht asymmetrisch
    '4h':  1.5,    # swing trades — lagere WR gecompenseerd door R/R
    '1d':  1.5,
}

COIN_FACTORS = {
    'btc_trend':         1.0,
    'eth_squeeze':       1.0,
    'xrp_roi':       1.2,
    'fartcoin_momentum': 1.3,
    'fartcoin_bb':       1.0,
    'ada_supertrend':    1.0,
    'general_consensus': 1.0,
    'rsi_meanrev':       1.0,
    'ema_crossover':     1.0,
    'bb_bounce':         1.0,
}

# Interval → candles per dag (voor Sharpe annualisatie)
CANDLES_PER_DAY = {
    '5m': 288, '15m': 96, '1h': 24, '4h': 6, '1d': 1,
}


class Backtester:
    def __init__(self, initial_balance=100.0, risk_per_trade=0.02,
                 stop_loss_pct=0.20, take_profit_pct=0.30, max_leverage=20,
                 strategy='general_consensus', use_atr_sl=True, interval='1h',
                 # === NIEUWE PARAMETERS ===
                 fee_pct=0.075,          # taker fee per trade (0.075% = Gate.io default)
                 slippage_pct=0.05,     # slippage per trade (0.05% = realistisch)
                 cooldown_bars=3,        # bars wachten na trade-exit
                 max_hold_bars=0,        # 0 = onbeperkt, >0 = sluit na N bars
                 direction_filter='both', # 'both', 'long_only', 'short_only'
                 trailing_stop_pct=0.0,  # 0 = uit, >0 = trailing stop als % van prijs
                 breakeven_trigger=0.0,  # 0 = uit, >0 = verplaats SL naar entry na X% winst
                 max_consec_losses=0,    # 0 = uit, >0 = stop na N opeenvolgende verliezen
                 # === ROI MODE ===
                 use_roi_mode=False,     # True = SL/TP berekend als ROI% / leverage
                 tp_roi=30.0,            # TP als ROI% (30% = standaard)
                 sl_roi=12.0,            # SL als ROI% (12% = standaard)
                 trail_after_roi=True,   # Bij sterk signaal: trail na ROI target
                 trail_roi_conf=0.80,    # Confidence drempel voor trailing modus
                 trail_roi_distance=0.5, # Trail afstand als % van prijs na ROI hit
                 ):
        self.initial_balance  = initial_balance
        self.risk_per_trade   = risk_per_trade
        self.stop_loss_pct    = stop_loss_pct
        self.take_profit_pct  = take_profit_pct
        self.max_leverage     = max_leverage
        self.strategy         = strategy
        self.use_atr_sl       = use_atr_sl
        self.interval         = interval
        self.fee_pct          = fee_pct / 100.0  # opslaan als decimaal
        self.slippage_pct     = slippage_pct / 100.0  # slippage per trade
        self.cooldown_bars    = cooldown_bars
        self.max_hold_bars    = max_hold_bars
        self.direction_filter = direction_filter
        self.trailing_stop_pct = trailing_stop_pct / 100.0
        self.breakeven_trigger = breakeven_trigger / 100.0
        self.max_consec_losses = max_consec_losses
        # ROI mode
        self.use_roi_mode      = use_roi_mode
        self.tp_roi            = tp_roi / 100.0
        self.sl_roi            = sl_roi / 100.0
        self.trail_after_roi   = trail_after_roi
        self.trail_roi_conf    = trail_roi_conf
        self.trail_roi_distance = trail_roi_distance / 100.0

        self._sig   = STRATEGIES.get(strategy, _sig_btc)
        tf_base = TF_BASE_MULTIPLIERS.get(interval, 3.0)
        coin_f  = COIN_FACTORS.get(strategy, 1.0)
        self._amult = round(tf_base * coin_f, 1)
        self._tp_ratio = TF_TP_RATIOS.get(interval, 1.2)
        self._cpd = CANDLES_PER_DAY.get(interval, 24)  # voor Sharpe

        # BB strategie: vaste SL/TP en max hold
        self._is_bb = (strategy == 'fartcoin_bb')
        if self._is_bb:
            self.use_atr_sl = False
            self.max_leverage = 10
            self.stop_loss_pct  = 0.012
            self.take_profit_pct = 0.03
            if self.max_hold_bars == 0:
                self.max_hold_bars = 24

        # XRP ROI strategie: auto-enable ROI mode
        self._is_xrp_roi = (strategy == 'xrp_roi')
        if self._is_xrp_roi:
            self.use_roi_mode = True
            self.use_atr_sl = False
            if self.tp_roi == 0.30:  # default niet gewijzigd
                self.tp_roi = 0.30   # 30% ROI
                self.sl_roi = 0.12   # 12% ROI
            self.trail_after_roi = True
            self.trail_roi_conf = 0.80
            self.max_leverage = 20

    def _size(self, balance, sl_pct, leverage):
        risk_amt = balance * self.risk_per_trade
        c_pct    = max(1, int(risk_amt / sl_pct))
        c_cap    = max(1, int(balance * 0.50 * leverage))
        return min(c_pct, c_cap)

    def _calc_fee(self, contracts, price):
        """Bereken taker fee + slippage voor entry of exit."""
        notional = contracts  # Gate.io: 1 contract = 1 USD notional
        return notional * (self.fee_pct + self.slippage_pct)

    def run(self, candles, symbol, date_from=None, date_to=None):
        def _f(c,k,d=0.0):
            if isinstance(c,list): return float(c[{'o':1,'h':2,'l':3,'c':4,'v':5}.get(k,0)]) if len(c)>5 else d
            return float(c.get(k,d))
        def _ts(c):
            if isinstance(c,list): return int(c[0]) if c else 0
            return int(c.get('t',c.get('time',0)))
        def _fmt(ts):
            try: return datetime.fromtimestamp(ts, tz=TZ).strftime('%Y-%m-%d %H:%M')
            except: return str(ts)

        if date_from or date_to:
            tf = int(datetime.strptime(date_from,'%Y-%m-%d').timestamp()) if date_from else 0
            tt = int(datetime.strptime(date_to,  '%Y-%m-%d').timestamp()) if date_to   else 9999999999
            candles=[c for c in candles if tf<=_ts(c)<=tt]

        if len(candles)<100:
            r=BacktestResult(symbol=symbol,strategy=self.strategy,
                period_from='',period_to='',
                initial_balance=self.initial_balance,final_balance=self.initial_balance)
            r.equity_curve=[self.initial_balance]; return r

        H=[_f(c,'h') for c in candles]; L=[_f(c,'l') for c in candles]
        C=[_f(c,'c') for c in candles]; V=[_f(c,'v') for c in candles]
        TS=[_ts(c) for c in candles]

        result=BacktestResult(symbol=symbol,strategy=self.strategy,
            period_from=_fmt(TS[0]),period_to=_fmt(TS[-1]),
            initial_balance=self.initial_balance)
        balance=self.initial_balance
        result.equity_curve.append(balance)
        result.timestamps.append(_fmt(TS[0]))

        open_trade=None
        WARMUP=100
        cooldown=0
        consec_losses=0       # teller voor opeenvolgende verliezen
        circuit_break=False   # stop trading na max consecutive losses

        for i in range(WARMUP,len(C)):
            price=C[i]; ts=_fmt(TS[i])

            # === OPEN TRADE MANAGEMENT ===
            if open_trade and not open_trade.closed:
                il=open_trade.direction=='long'

                # --- Trailing stop update ---
                if self.trailing_stop_pct > 0:
                    if il:
                        new_trail = H[i] * (1 - self.trailing_stop_pct)
                        if new_trail > open_trade.stop_loss:
                            open_trade.stop_loss = round(new_trail, 8)
                    else:
                        new_trail = L[i] * (1 + self.trailing_stop_pct)
                        if new_trail < open_trade.stop_loss:
                            open_trade.stop_loss = round(new_trail, 8)

                # --- Break-even stop ---
                if self.breakeven_trigger > 0:
                    if il:
                        unrealised_pct = (H[i] - open_trade.entry_price) / open_trade.entry_price
                    else:
                        unrealised_pct = (open_trade.entry_price - L[i]) / open_trade.entry_price
                    if unrealised_pct >= self.breakeven_trigger:
                        # Verplaats SL naar entry + kleine buffer
                        be_price = open_trade.entry_price * (1.001 if il else 0.999)
                        if il and be_price > open_trade.stop_loss:
                            open_trade.stop_loss = round(be_price, 8)
                        elif not il and be_price < open_trade.stop_loss:
                            open_trade.stop_loss = round(be_price, 8)

                # --- SL/TP check (SL EERST om optimistische bias te vermijden) ---
                # --- ROI trailing profit lock ---
                # Als trade in trailing modus is en ROI target bereikt:
                # verplaats SL naar ROI-niveau en laat trail lopen
                if open_trade.roi_trailing and self.use_roi_mode:
                    if il:
                        cur_roi = (H[i] - open_trade.entry_price) / open_trade.entry_price
                    else:
                        cur_roi = (open_trade.entry_price - L[i]) / open_trade.entry_price
                    roi_target = self.tp_roi / open_trade.leverage if open_trade.leverage else self.tp_roi
                    if cur_roi >= roi_target:
                        open_trade.roi_trail_activated = True
                    if open_trade.roi_trail_activated:
                        # Trail SL: lock profit op (high/low - trail_distance)
                        # Maar nooit lager dan ROI target niveau
                        roi_floor_price = open_trade.entry_price * (1 + roi_target) if il \
                            else open_trade.entry_price * (1 - roi_target)
                        if il:
                            trail_sl = H[i] * (1 - self.trail_roi_distance)
                            new_sl = max(trail_sl, roi_floor_price)
                            if new_sl > open_trade.stop_loss:
                                open_trade.stop_loss = round(new_sl, 8)
                        else:
                            trail_sl = L[i] * (1 + self.trail_roi_distance)
                            new_sl = min(trail_sl, roi_floor_price)
                            if new_sl < open_trade.stop_loss:
                                open_trade.stop_loss = round(new_sl, 8)

                hit_sl = L[i] <= open_trade.stop_loss if il else H[i] >= open_trade.stop_loss
                # In trailing ROI modus: geen vaste TP check (laat winst lopen)
                if open_trade.roi_trailing and open_trade.roi_trail_activated:
                    hit_tp = False  # TP disabled, trail SL doet het werk
                else:
                    hit_tp = H[i] >= open_trade.take_profit if il else L[i] <= open_trade.take_profit
                bars_held = i - open_trade.entry_index
                hit_max = self.max_hold_bars > 0 and bars_held >= self.max_hold_bars

                if hit_sl or hit_tp or hit_max:
                    # Bepaal exit reason
                    if hit_sl and open_trade.roi_trail_activated:
                        ep=open_trade.stop_loss; reason='TRAIL'  # trailing profit lock exit
                    elif hit_sl:
                        ep=open_trade.stop_loss; reason='SL'
                    elif hit_tp:
                        ep=open_trade.take_profit; reason='TP'
                    else:
                        ep=price; reason='MAX'

                    chg=(ep-open_trade.entry_price)/open_trade.entry_price if il \
                        else (open_trade.entry_price-ep)/open_trade.entry_price
                    pnl=open_trade.contracts*chg
                    lev=open_trade.leverage or 1; margin=open_trade.contracts/lev

                    # Exit fee
                    exit_fee = self._calc_fee(open_trade.contracts, ep)
                    open_trade.fees += exit_fee
                    result.total_fees += exit_fee
                    pnl -= exit_fee  # fee van PnL aftrekken

                    open_trade.exit_price=ep; open_trade.exit_index=i
                    open_trade.exit_ts=ts; open_trade.exit_reason=reason
                    open_trade.pnl=round(pnl,6)
                    open_trade.pnl_pct=round(pnl/margin*100 if margin>0 else 0,2)
                    open_trade.closed=True
                    balance=max(balance+pnl,0.01)
                    if pnl>0:
                        result.win_count+=1; consec_losses=0
                    else:
                        result.loss_count+=1; consec_losses+=1
                        if self.max_consec_losses>0 and consec_losses>=self.max_consec_losses:
                            circuit_break=True
                    result.trades.append(open_trade); result.total_pnl+=pnl; open_trade=None
                    cooldown=self.cooldown_bars
                result.equity_curve.append(balance); result.timestamps.append(ts); continue

            # === NIEUWE TRADE LOGICA ===
            if cooldown>0:
                cooldown-=1
                result.equity_curve.append(balance); result.timestamps.append(ts); continue

            if circuit_break:
                result.equity_curve.append(balance); result.timestamps.append(ts); continue

            sig,conf=self._sig(C[:i+1],H[:i+1],L[:i+1],V[:i+1])

            # Direction filter
            if sig=='long' and self.direction_filter=='short_only': sig='none'
            if sig=='short' and self.direction_filter=='long_only': sig='none'

            if sig=='none':
                result.equity_curve.append(balance); result.timestamps.append(ts); continue

            # === SL/TP BEREKENING ===
            if self.use_roi_mode:
                # ROI mode: SL/TP als ROI% / leverage
                # Leverage eerst bepalen (nodig voor ROI berekening)
                vol=calculate_volatility_score(C[:i+1])
                # Bij ROI mode: leverage = max_leverage (ROI is al gecalibreerd)
                leverage=self.max_leverage
                if self.strategy=='fartcoin_momentum': leverage=min(leverage,10)
                sl_pct = self.sl_roi / leverage  # bijv 12% / 10x = 1.2%
                tp_pct = self.tp_roi / leverage  # bijv 30% / 10x = 3.0%
            elif self.use_atr_sl:
                sl_pct,tp_pct=atr_stop(H[:i+1],L[:i+1],C[:i+1],self._amult,tp_ratio=self._tp_ratio)
                vol=calculate_volatility_score(C[:i+1])
                if self._is_bb:
                    leverage=self.max_leverage
                else:
                    safe_lev=int(0.80/sl_pct) if sl_pct>0 else self.max_leverage
                    if   vol>0.75: discount=0.40
                    elif vol>0.50: discount=0.60
                    elif vol>0.25: discount=0.80
                    else:          discount=1.00
                    leverage=max(1,min(int(safe_lev*discount),self.max_leverage))
                if self.strategy=='fartcoin_momentum': leverage=min(leverage,10)
            else:
                sl_pct,tp_pct=self.stop_loss_pct,self.take_profit_pct
                vol=calculate_volatility_score(C[:i+1])
                if self._is_bb:
                    leverage=self.max_leverage
                else:
                    safe_lev=int(0.80/sl_pct) if sl_pct>0 else self.max_leverage
                    if   vol>0.75: discount=0.40
                    elif vol>0.50: discount=0.60
                    elif vol>0.25: discount=0.80
                    else:          discount=1.00
                    leverage=max(1,min(int(safe_lev*discount),self.max_leverage))
                if self.strategy=='fartcoin_momentum': leverage=min(leverage,10)

            contracts=self._size(balance,sl_pct,leverage)
            il=sig=='long'
            sl_p=round(price*(1-sl_pct if il else 1+sl_pct),8)
            tp_p=round(price*(1+tp_pct if il else 1-tp_pct),8)

            # Bepaal of deze trade in trailing ROI modus gaat
            use_trail = (self.use_roi_mode and self.trail_after_roi
                         and conf >= self.trail_roi_conf)

            # Entry fee
            entry_fee = self._calc_fee(contracts, price)
            result.total_fees += entry_fee

            open_trade=Trade(symbol=symbol,strategy=self.strategy,
                direction=sig,entry_price=price,entry_index=i,entry_ts=ts,
                contracts=contracts,leverage=leverage,stop_loss=sl_p,take_profit=tp_p,
                fees=entry_fee,confidence=conf,roi_trailing=use_trail)
            result.equity_curve.append(balance); result.timestamps.append(ts)

        # === SLUIT OPEN TRADE AAN EINDE ===
        if open_trade and not open_trade.closed:
            ep=C[-1]; il=open_trade.direction=='long'
            chg=(ep-open_trade.entry_price)/open_trade.entry_price if il \
                else (open_trade.entry_price-ep)/open_trade.entry_price
            pnl=open_trade.contracts*chg
            exit_fee = self._calc_fee(open_trade.contracts, ep)
            open_trade.fees += exit_fee; result.total_fees += exit_fee
            pnl -= exit_fee
            lev=open_trade.leverage or 1; margin=open_trade.contracts/lev
            open_trade.exit_price=ep; open_trade.exit_index=len(C)-1
            open_trade.exit_ts=_fmt(TS[-1]) if TS else ''
            open_trade.exit_reason='END'
            open_trade.pnl=round(pnl,6)
            open_trade.pnl_pct=round(pnl/margin*100 if margin>0 else 0,2)
            open_trade.closed=True; balance+=pnl
            if pnl>0: result.win_count+=1
            else:     result.loss_count+=1
            result.trades.append(open_trade); result.total_pnl+=pnl

        result.final_balance=round(max(balance,0),6)
        result.equity_curve.append(result.final_balance)

        # === MAX DRAWDOWN ===
        peak=max_dd=0.0
        for eq in result.equity_curve:
            if eq>peak: peak=eq
            if peak>0:
                dd=(peak-eq)/peak
                if dd>max_dd: max_dd=dd
        result.max_drawdown=round(max_dd*100,2)

        # === SHARPE RATIO (correct geannualiseerd per timeframe) ===
        curve=result.equity_curve
        if len(curve)>2:
            rets=[(curve[j+1]-curve[j])/curve[j] for j in range(len(curve)-1) if curve[j]>0]
            if len(rets)>1:
                avg=sum(rets)/len(rets)
                std=(sum((r-avg)**2 for r in rets)/len(rets))**0.5
                # Annualiseer: √(candles_per_dag × 365)
                ann_factor = math.sqrt(self._cpd * 365)
                result.sharpe_ratio=round(avg/std*ann_factor if std>0 else 0.0,2)

        return result
