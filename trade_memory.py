# -*- coding: utf-8 -*-
"""
Trade Memory  --  zelfkritisch leersysteem voor Jef Bot.

Werking:
1. Bij elke trade-entry: sla indicator-snapshot op in SQLite
2. Bij elke trade-sluiting: evalueer of het signaal correct was
3. Pas confidence-drempel per coin dynamisch aan op basis van recente winrate
4. Genereer een rapport dat het dashboard kan ophalen

Geen ML  --  pure statistiek op eigen trade-historiek.
"""

import sqlite3
import json
import logging
import math
from datetime import datetime, timedelta
from typing import Dict, Optional, List
from dataclasses import dataclass

logger = logging.getLogger('TradeMemory')

DB_PATH = 'trade_memory.db'

# Minimaal trades vereist vooraleer we drempel aanpassen
MIN_TRADES_FOR_ADAPTATION = 10
# Rollenvenster: laatste N trades tellen mee
ROLLING_WINDOW = 20
# Als recente winrate onder dit zakt: drempel verhogen
WINRATE_LOW_THRESHOLD  = 0.40
# Als recente winrate boven dit stijgt: drempel verlagen (meer kansen)
WINRATE_HIGH_THRESHOLD = 0.60
# Maximale aanpassing per evaluatie
MAX_THRESH_DELTA = 0.05


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Maak tabellen aan als ze nog niet bestaan."""
    with _db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol        TEXT    NOT NULL,
            strategy      TEXT    NOT NULL,
            direction     TEXT    NOT NULL,
            entry_price   REAL    NOT NULL,
            exit_price    REAL,
            entry_ts      TEXT    NOT NULL,
            exit_ts       TEXT,
            exit_reason   TEXT,
            pnl           REAL,
            pnl_pct       REAL,
            contracts     INTEGER,
            leverage      INTEGER,
            confidence    REAL,
            snapshot      TEXT,   -- JSON met indicator-waarden bij entry
            market_regime TEXT,   -- 'trending' / 'sideways' / 'volatile'
            closed        INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS thresholds (
            symbol        TEXT PRIMARY KEY,
            threshold     REAL    NOT NULL DEFAULT 0.45,
            updated_ts    TEXT    NOT NULL,
            reason        TEXT
        );

        CREATE TABLE IF NOT EXISTS evaluations (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol        TEXT    NOT NULL,
            eval_ts       TEXT    NOT NULL,
            trades_count  INTEGER,
            win_rate      REAL,
            avg_pnl       REAL,
            sharpe        REAL,
            old_threshold REAL,
            new_threshold REAL,
            action        TEXT,
            notes         TEXT
        );
        """)
    logger.info("Trade memory DB geïnitialiseerd")


# --- Trade opslaan ----------------------------------------------------------

def log_entry(
    symbol:     str,
    strategy:   str,
    direction:  str,
    entry_price: float,
    contracts:  int,
    leverage:   int,
    confidence: float,
    snapshot:   dict,
    market_regime: str = 'unknown',
) -> int:
    """Sla een nieuwe trade-entry op. Geeft de trade-ID terug."""
    ts = datetime.now().isoformat()
    with _db() as conn:
        cur = conn.execute(
            """INSERT INTO trades
               (symbol, strategy, direction, entry_price, entry_ts,
                contracts, leverage, confidence, snapshot, market_regime)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (symbol, strategy, direction, entry_price, ts,
             contracts, leverage, confidence,
             json.dumps(snapshot), market_regime)
        )
        trade_id = cur.lastrowid
    logger.info(f"[{symbol}] Trade #{trade_id} opgeslagen (conf={confidence:.0%})")
    return trade_id


def log_exit(
    trade_id:   int,
    exit_price: float,
    pnl:        float,
    pnl_pct:    float,
    exit_reason: str,
):
    """Sluit een trade af en sla het resultaat op."""
    ts = datetime.now().isoformat()
    with _db() as conn:
        conn.execute(
            """UPDATE trades SET
               exit_price=?, exit_ts=?, exit_reason=?, pnl=?, pnl_pct=?, closed=1
               WHERE id=?""",
            (exit_price, ts, exit_reason, pnl, pnl_pct, trade_id)
        )
    logger.info(f"Trade #{trade_id} gesloten: {exit_reason} PnL={pnl:+.4f}")


# --- Snapshot builder -------------------------------------------------------

def build_snapshot(ohlcv: dict, signal_details: dict = None) -> dict:
    """
    Bouw een indicator-snapshot voor de huidige marktcontext.
    Dit wordt opgeslagen bij elke trade-entry.
    """
    from indicators import (
        ema, rsi, macd, bollinger_width, atr,
        calculate_volatility_score, adx
    )
    c = ohlcv.get('closes', [])
    h = ohlcv.get('highs',  [])
    l = ohlcv.get('lows',   [])
    v = ohlcv.get('volumes',[])

    snap = {}
    try:
        e21 = ema(c, 21); e55 = ema(c, 55)
        if e21 and e55 and e55[-1] > 0:
            snap['trend_strength'] = round((e21[-1] - e55[-1]) / e55[-1], 4)

        rv = rsi(c, 14)
        if rv: snap['rsi'] = round(rv[-1], 1)

        ml, sl, hist = macd(c, 12, 26, 9)
        if hist:
            snap['macd_hist']       = round(hist[-1], 6)
            snap['macd_hist_prev']  = round(hist[-2], 6) if len(hist) >= 2 else 0

        bb_w = bollinger_width(c, 20)
        if bb_w:
            avg_w = sum(bb_w[-20:]) / min(20, len(bb_w))
            snap['bb_width']      = round(bb_w[-1], 4)
            snap['bb_width_avg']  = round(avg_w, 4)
            snap['bb_squeeze']    = bb_w[-1] < avg_w * 0.70

        atr_v = atr(h, l, c, 14)
        if atr_v and c[-1] > 0:
            snap['atr_pct']  = round(atr_v[-1] / c[-1], 4)
            avg_atr = sum(atr_v[-20:]) / min(20, len(atr_v))
            snap['atr_ratio'] = round(atr_v[-1] / avg_atr, 2)

        adx_v = adx(h, l, c, 14)
        if adx_v: snap['adx'] = round(adx_v[-1], 1)

        snap['vol_score'] = round(calculate_volatility_score(c), 2)
        snap['price']     = round(c[-1], 6)

        # Marktregime bepalen
        adx_val  = snap.get('adx', 0)
        vol_val  = snap.get('vol_score', 0.5)
        if adx_val > 25:
            regime = 'trending'
        elif vol_val > 0.60:
            regime = 'volatile'
        else:
            regime = 'sideways'
        snap['regime'] = regime

        if signal_details:
            snap.update(signal_details)

    except Exception as e:
        logger.warning(f"Snapshot fout: {e}")

    return snap


def detect_regime(snapshot: dict) -> str:
    return snapshot.get('regime', 'unknown')


# --- Evaluator --------------------------------------------------------------

class TradeEvaluator:
    """
    Analyseert gesloten trades en past confidence-drempels aan.
    Wordt aangeroepen na elke gesloten trade.
    """

    def __init__(self, rolling_window: int = ROLLING_WINDOW):
        self.window = rolling_window
        init_db()

    def get_threshold(self, symbol: str, default: float = 0.45) -> float:
        """Haal de huidige aangepaste drempel op voor dit coin."""
        with _db() as conn:
            row = conn.execute(
                "SELECT threshold FROM thresholds WHERE symbol=?", (symbol,)
            ).fetchone()
        return float(row['threshold']) if row else default

    def evaluate_symbol(self, symbol: str, base_threshold: float = 0.45) -> dict:
        """
        Evalueer recente trades voor dit symbool.
        Past de drempel aan als de winrate te laag of te hoog is.
        Geeft een rapport terug.
        """
        with _db() as conn:
            rows = conn.execute(
                """SELECT pnl, pnl_pct, confidence, exit_reason,
                          snapshot, market_regime, entry_ts
                   FROM trades
                   WHERE symbol=? AND closed=1
                   ORDER BY id DESC LIMIT ?""",
                (symbol, self.window)
            ).fetchall()

        if len(rows) < MIN_TRADES_FOR_ADAPTATION:
            return {
                'symbol': symbol, 'trades': len(rows),
                'action': 'wachten',
                'notes': f"Minimaal {MIN_TRADES_FOR_ADAPTATION} trades nodig (nu {len(rows)})"
            }

        trades = [dict(r) for r in rows]
        pnls   = [t['pnl'] for t in trades if t['pnl'] is not None]
        wins   = sum(1 for p in pnls if p > 0)
        win_rate = wins / len(pnls) if pnls else 0
        avg_pnl  = sum(pnls) / len(pnls) if pnls else 0

        # Sharpe over rolling window
        sharpe = 0.0
        if len(pnls) > 2:
            avg = sum(pnls)/len(pnls)
            std = math.sqrt(sum((p-avg)**2 for p in pnls)/len(pnls))
            sharpe = avg/std if std > 0 else 0.0

        # Huidige drempel
        old_thresh = self.get_threshold(symbol, base_threshold)
        new_thresh = old_thresh
        action = 'geen_aanpassing'
        notes  = []

        # Analyse per marktregime
        regime_stats = {}
        for t in trades:
            snap = json.loads(t['snapshot']) if isinstance(t['snapshot'], str) else (t['snapshot'] or {})
            regime = snap.get('regime', t.get('market_regime', 'unknown'))
            if regime not in regime_stats:
                regime_stats[regime] = {'wins': 0, 'total': 0, 'pnl': 0}
            if t['pnl'] is not None:
                regime_stats[regime]['total'] += 1
                regime_stats[regime]['pnl'] += t['pnl']
                if t['pnl'] > 0:
                    regime_stats[regime]['wins'] += 1

        # Drempel aanpassing
        if win_rate < WINRATE_LOW_THRESHOLD:
            # Te veel verliezende trades  --  drempel verhogen (minder trades, hogere kwaliteit)
            delta     = min(MAX_THRESH_DELTA, (WINRATE_LOW_THRESHOLD - win_rate) * 0.3)
            new_thresh = min(0.70, old_thresh + delta)
            action    = 'drempel_verhoogd'
            notes.append(f"Win rate {win_rate:.0%} < {WINRATE_LOW_THRESHOLD:.0%} → drempel +{delta:.3f}")

        elif win_rate > WINRATE_HIGH_THRESHOLD and avg_pnl > 0:
            # Goede prestaties  --  drempel iets verlagen (meer kansen)
            delta     = min(MAX_THRESH_DELTA * 0.5, (win_rate - WINRATE_HIGH_THRESHOLD) * 0.2)
            new_thresh = max(0.45, old_thresh - delta)
            action    = 'drempel_verlaagd'
            notes.append(f"Win rate {win_rate:.0%} > {WINRATE_HIGH_THRESHOLD:.0%} → drempel −{delta:.3f}")

        # Analyse per confidence-niveau
        high_conf   = [t for t in trades if (t['confidence'] or 0) >= 0.70]
        low_conf    = [t for t in trades if (t['confidence'] or 0) < 0.60]
        hc_win      = sum(1 for t in high_conf if (t['pnl'] or 0) > 0) / max(1, len(high_conf))
        lc_win      = sum(1 for t in low_conf  if (t['pnl'] or 0) > 0) / max(1, len(low_conf))
        if high_conf and hc_win > lc_win + 0.15:
            notes.append(f"Hoge confidence ({hc_win:.0%} win) > lage confidence ({lc_win:.0%} win)  --  drempel verhogen heeft zin")

        # SL ratio analyse
        sl_exits = sum(1 for t in trades if t['exit_reason'] == 'SL')
        sl_ratio = sl_exits / len(trades)
        if sl_ratio > 0.65:
            notes.append(f"SL ratio {sl_ratio:.0%}  --  te veel SL hits, signalen zijn te vroeg")

        # Sla drempel op
        if new_thresh != old_thresh:
            ts = datetime.now().isoformat()
            with _db() as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO thresholds (symbol, threshold, updated_ts, reason)
                       VALUES (?,?,?,?)""",
                    (symbol, new_thresh, ts, '; '.join(notes))
                )
            # Log de evaluatie
            with _db() as conn:
                conn.execute(
                    """INSERT INTO evaluations
                       (symbol, eval_ts, trades_count, win_rate, avg_pnl, sharpe,
                        old_threshold, new_threshold, action, notes)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (symbol, ts, len(trades), win_rate, avg_pnl, sharpe,
                     old_thresh, new_thresh, action, '\n'.join(notes))
                )
            logger.info(
                f"[{symbol}] Drempel {old_thresh:.3f} → {new_thresh:.3f} ({action}): {'; '.join(notes)}"
            )

        return {
            'symbol':        symbol,
            'trades':        len(trades),
            'win_rate':      round(win_rate, 3),
            'avg_pnl':       round(avg_pnl, 4),
            'sharpe':        round(sharpe, 2),
            'sl_ratio':      round(sl_ratio, 2),
            'old_threshold': round(old_thresh, 3),
            'new_threshold': round(new_thresh, 3),
            'action':        action,
            'notes':         notes,
            'regime_stats':  {k: {
                'win_rate': round(v['wins']/v['total'], 2) if v['total'] else 0,
                'avg_pnl':  round(v['pnl']/v['total'], 4) if v['total'] else 0,
                'trades':   v['total']
            } for k, v in regime_stats.items()},
        }

    def full_report(self, symbols: list) -> dict:
        """Genereer een volledig rapport voor alle symbolen."""
        report = {'generated': datetime.now().isoformat(), 'symbols': {}}
        for sym in symbols:
            report['symbols'][sym] = self.evaluate_symbol(sym)
        return report

    def get_trade_history(self, symbol: str = None, limit: int = 100) -> List[dict]:
        """Haal trade-historiek op."""
        with _db() as conn:
            if symbol:
                rows = conn.execute(
                    """SELECT * FROM trades WHERE symbol=? AND closed=1
                       ORDER BY id DESC LIMIT ?""", (symbol, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM trades WHERE closed=1
                       ORDER BY id DESC LIMIT ?""", (limit,)
                ).fetchall()
        return [dict(r) for r in rows]

    def get_evaluation_history(self, symbol: str = None, limit: int = 50) -> List[dict]:
        """Haal evaluatie-historiek op."""
        with _db() as conn:
            if symbol:
                rows = conn.execute(
                    "SELECT * FROM evaluations WHERE symbol=? ORDER BY id DESC LIMIT ?",
                    (symbol, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM evaluations ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(r) for r in rows]

    def get_open_trades(self) -> List[dict]:
        """Haal open (niet gesloten) trades op."""
        with _db() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE closed=0 ORDER BY id DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_stats_summary(self) -> dict:
        """Globale statistieken over alle trades."""
        with _db() as conn:
            total  = conn.execute("SELECT COUNT(*) FROM trades WHERE closed=1").fetchone()[0]
            wins   = conn.execute("SELECT COUNT(*) FROM trades WHERE closed=1 AND pnl>0").fetchone()[0]
            pnl    = conn.execute("SELECT SUM(pnl) FROM trades WHERE closed=1").fetchone()[0] or 0
            by_sym = conn.execute(
                """SELECT symbol,
                          COUNT(*) as trades,
                          SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) as wins,
                          SUM(pnl) as total_pnl,
                          AVG(pnl) as avg_pnl
                   FROM trades WHERE closed=1 GROUP BY symbol"""
            ).fetchall()
            thresholds = conn.execute("SELECT * FROM thresholds").fetchall()

        return {
            'total_trades': total,
            'total_wins':   wins,
            'win_rate':     round(wins/total, 3) if total else 0,
            'total_pnl':    round(pnl, 4),
            'by_symbol':    {r['symbol']: dict(r) for r in by_sym},
            'thresholds':   {r['symbol']: r['threshold'] for r in thresholds},
        }


# --- Dagelijkse verliesgrens ------------------------------------------------

def get_daily_pnl() -> float:
    """Bereken de totale PnL van vandaag (alle gesloten trades)."""
    today = datetime.now().strftime('%Y-%m-%d')
    with _db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE closed=1 AND exit_ts LIKE ?",
            (today + '%',)
        ).fetchone()
    return float(row[0]) if row else 0.0


def get_daily_trade_count() -> int:
    """Aantal trades vandaag."""
    today = datetime.now().strftime('%Y-%m-%d')
    with _db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE entry_ts LIKE ?",
            (today + '%',)
        ).fetchone()
    return int(row[0]) if row else 0


def check_daily_limit(balance: float, max_loss_pct: float = 5.0) -> tuple:
    """
    Check of de dagelijkse verliesgrens bereikt is.
    
    Returns: (is_blocked, daily_pnl, daily_pct, reason)
    """
    daily_pnl = get_daily_pnl()
    daily_pct = (daily_pnl / balance * 100) if balance > 0 else 0
    
    if daily_pct <= -max_loss_pct:
        reason = f"Daggrens bereikt: {daily_pct:.1f}% (limiet: -{max_loss_pct}%)"
        logger.warning(f"🛑 {reason}")
        return True, daily_pnl, daily_pct, reason
    
    return False, daily_pnl, daily_pct, ""
