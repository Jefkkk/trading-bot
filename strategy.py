# strategy.py
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Strategie logica: EMA, RSI, ATR berekeningen
# en koop/verkoop signalen per coin
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

import pandas as pd
import numpy as np
from logger import log, log_signal


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HULPFUNCTIES — indicatoren berekenen
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def candles_to_dataframe(ohlcv):
    """Zet ruwe candle data om naar een pandas DataFrame."""
    df = pd.DataFrame(
        ohlcv,
        columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    return df


def calculate_ema(df, period):
    """Bereken Exponential Moving Average."""
    return df["close"].ewm(span=period, adjust=False).mean()


def calculate_rsi(df, period):
    """Bereken Relative Strength Index (0–100)."""
    delta  = df["close"].diff()
    gain   = delta.clip(lower=0)
    loss   = -delta.clip(upper=0)
    avg_gain = gain.ewm(span=period, adjust=False).mean()
    avg_loss = loss.ewm(span=period, adjust=False).mean()
    rs  = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def calculate_atr(df, period):
    """
    Bereken Average True Range.
    Geeft de gemiddelde volatiliteit terug als percentage van de prijs.
    """
    high  = df["high"]
    low   = df["low"]
    close = df["close"].shift(1)

    tr = pd.concat([
        high - low,
        (high - close).abs(),
        (low  - close).abs()
    ], axis=1).max(axis=1)

    atr     = tr.ewm(span=period, adjust=False).mean()
    atr_pct = atr / df["close"]   # Als percentage van prijs
    return atr_pct


def get_dynamic_sl_tp(atr_value, config):
    """
    Bereken dynamische SL en TP op basis van ATR.
    Respecteert altijd de min/max grenzen uit config.
    """
    sl = atr_value * config["atr_sl_mult"]
    tp = atr_value * config["atr_tp_mult"]

    # Grenzen toepassen
    sl = max(config["sl_min_pct"], min(sl, config["sl_max_pct"]))
    tp = max(config["tp_min_pct"], min(tp, config["tp_max_pct"]))

    return round(sl, 4), round(tp, 4)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# STRATEGIEËN
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def strategy_ema_crossover(df, config):
    """
    EMA Crossover strategie — voor BTC en ETH.

    BUY signaal  : korte EMA kruist BOVEN lange EMA
    SELL signaal : korte EMA kruist ONDER lange EMA
    HOLD         : geen kruising
    """
    ema_short = calculate_ema(df, config["ema_short"])
    ema_long  = calculate_ema(df, config["ema_long"])
    atr       = calculate_atr(df, config["atr_period"])

    # Huidige en vorige waarden
    ema_s_now  = ema_short.iloc[-1]
    ema_s_prev = ema_short.iloc[-2]
    ema_l_now  = ema_long.iloc[-1]
    ema_l_prev = ema_long.iloc[-2]
    atr_now    = atr.iloc[-1]

    # Dynamische SL/TP
    sl_pct, tp_pct = get_dynamic_sl_tp(atr_now, config)

    # Signaal bepalen
    if ema_s_prev <= ema_l_prev and ema_s_now > ema_l_now:
        signal = "buy"
    elif ema_s_prev >= ema_l_prev and ema_s_now < ema_l_now:
        signal = "sell"
    else:
        signal = "hold"

    log_signal(
        config.get("symbol", "?"),
        signal, ema_s_now, ema_l_now
    )

    return {
        "signal": signal,
        "sl_pct": sl_pct,
        "tp_pct": tp_pct,
        "atr":    round(atr_now, 6),
    }


def strategy_rsi_ema(df, config):
    """
    RSI + EMA combo strategie — voor SOL.

    BUY signaal  : EMA crossover omhoog + RSI was oversold
    SELL signaal : EMA crossover omlaag + RSI was overbought
    HOLD         : condities niet voldaan
    """
    ema_short = calculate_ema(df, config["ema_short"])
    ema_long  = calculate_ema(df, config["ema_long"])
    rsi       = calculate_rsi(df, config["rsi_period"])
    atr       = calculate_atr(df, config["atr_period"])

    ema_s_now  = ema_short.iloc[-1]
    ema_s_prev = ema_short.iloc[-2]
    ema_l_now  = ema_long.iloc[-1]
    ema_l_prev = ema_long.iloc[-2]
    rsi_now    = rsi.iloc[-1]
    rsi_prev   = rsi.iloc[-2]
    atr_now    = atr.iloc[-1]

    sl_pct, tp_pct = get_dynamic_sl_tp(atr_now, config)

    # EMA kruising + RSI bevestiging
    ema_cross_up   = ema_s_prev <= ema_l_prev and ema_s_now > ema_l_now
    ema_cross_down = ema_s_prev >= ema_l_prev and ema_s_now < ema_l_now
    rsi_was_low    = rsi_prev < config["rsi_oversold"]
    rsi_was_high   = rsi_prev > config["rsi_overbought"]

    if ema_cross_up and rsi_was_low:
        signal = "buy"
    elif ema_cross_down and rsi_was_high:
        signal = "sell"
    else:
        signal = "hold"

    log_signal(
        config.get("symbol", "?"),
        signal, ema_s_now, ema_l_now, rsi_now
    )

    return {
        "signal": signal,
        "sl_pct": sl_pct,
        "tp_pct": tp_pct,
        "atr":    round(atr_now, 6),
        "rsi":    round(rsi_now, 2),
    }


def strategy_rsi_reversion(df, config):
    """
    RSI Mean Reversion strategie — voor FART.

    BUY signaal  : RSI was extreem oversold en begint te stijgen
    SELL signaal : RSI was extreem overbought en begint te dalen
    HOLD         : RSI in neutrale zone
    """
    rsi = calculate_rsi(df, config["rsi_period"])
    atr = calculate_atr(df, config["atr_period"])

    rsi_now  = rsi.iloc[-1]
    rsi_prev = rsi.iloc[-2]
    atr_now  = atr.iloc[-1]

    sl_pct, tp_pct = get_dynamic_sl_tp(atr_now, config)

    # RSI keert om vanuit extreem niveau
    if rsi_prev < config["rsi_oversold"] and rsi_now > rsi_prev:
        signal = "buy"
    elif rsi_prev > config["rsi_overbought"] and rsi_now < rsi_prev:
        signal = "sell"
    else:
        signal = "hold"

    log_signal(
        config.get("symbol", "?"),
        signal,
        rsi_now, rsi_prev,   # EMA velden hergebruikt voor RSI waarden
        rsi_now
    )

    return {
        "signal": signal,
        "sl_pct": sl_pct,
        "tp_pct": tp_pct,
        "atr":    round(atr_now, 6),
        "rsi":    round(rsi_now, 2),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HOOFD DISPATCHER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_signal(ohlcv, config):
    """
    Roept de juiste strategie aan per coin
    op basis van de config instelling.
    """
    if not ohlcv or len(ohlcv) < 30:
        log(f"⚠️  Te weinig candles voor {config.get('symbol')} — skip")
        return {"signal": "hold", "sl_pct": 0.03, "tp_pct": 0.06}

    df       = candles_to_dataframe(ohlcv)
    strategy = config.get("strategy")

    if strategy == "ema_crossover":
        return strategy_ema_crossover(df, config)
    elif strategy == "rsi_ema":
        return strategy_rsi_ema(df, config)
    elif strategy == "rsi_reversion":
        return strategy_rsi_reversion(df, config)
    else:
        log(f"❌ Onbekende strategie: {strategy}")
        return {"signal": "hold", "sl_pct": 0.03, "tp_pct": 0.06}
```

---

## 🧠 Samenvatting van de logica
```
BTC & ETH  →  EMA crossover
               Kort kruist boven lang = BUY  📈
               Kort kruist onder lang = SELL 📉

SOL        →  RSI + EMA combo
               EMA crossover + RSI bevestiging nodig
               Minder valse signalen dan pure EMA

FART       →  RSI mean reversion
               Extreem oversold + keert om = BUY  🚀
               Extreem overbought + keert om = SELL 💨
```

---

## ✅ Bestanden status
```
├── Procfile          ✅ klaar
├── runtime.txt       ✅ klaar
├── requirements.txt  ✅ klaar
├── config.py         ✅ klaar
├── .env              ✅ klaar
├── exchange.py       ✅ klaar
├── logger.py         ✅ klaar
├── strategy.py       ✅ klaar  ← net gedaan
├── risk_manager.py   ← volgende stap 🎯
└── main.py           ← als laatste
