# config.py — met ATR dynamische stop-loss en take-profit

COIN_CONFIGS = {
    "BTC/USDT:USDT": {
        "strategy":        "ema_crossover",
        "ema_short":       8,
        "ema_long":        18,
        "leverage":        5,
        "trade_usdt":      3,
        "profile":         "gemiddeld-agressief",

        # ── Dynamische SL/TP via ATR ──────────────
        "atr_period":      14,       # ATR berekend over 14 candles
        "atr_sl_mult":     1.5,      # SL = 1.5 × ATR
        "atr_tp_mult":     3.0,      # TP = 3.0 × ATR (altijd 2x de SL)

        # ── Minimum & maximum grenzen ─────────────
        "sl_min_pct":      0.02,     # Nooit minder dan 2% SL
        "sl_max_pct":      0.06,     # Nooit meer dan 6% SL
        "tp_min_pct":      0.04,     # Nooit minder dan 4% TP
        "tp_max_pct":      0.12,     # Nooit meer dan 12% TP
    },
    "ETH/USDT:USDT": {
        "strategy":        "ema_crossover",
        "ema_short":       8,
        "ema_long":        18,
        "leverage":        5,
        "trade_usdt":      3,
        "profile":         "gemiddeld-agressief",

        "atr_period":      14,
        "atr_sl_mult":     1.5,
        "atr_tp_mult":     3.0,

        "sl_min_pct":      0.02,
        "sl_max_pct":      0.06,
        "tp_min_pct":      0.05,
        "tp_max_pct":      0.14,     # ETH iets ruimer
    },
    "SOL/USDT:USDT": {
        "strategy":        "rsi_ema",
        "ema_short":       6,
        "ema_long":        13,
        "rsi_period":      10,
        "rsi_oversold":    32,
        "rsi_overbought":  68,
        "leverage":        5,
        "trade_usdt":      3,
        "profile":         "agressief",

        "atr_period":      10,       # Korter = sneller reageren op SOL
        "atr_sl_mult":     2.0,      # Ruimer want SOL is volatieler
        "atr_tp_mult":     3.5,

        "sl_min_pct":      0.025,
        "sl_max_pct":      0.08,
        "tp_min_pct":      0.06,
        "tp_max_pct":      0.18,
    },
    "FART/USDT:USDT": {
        "strategy":        "rsi_reversion",
        "rsi_period":      6,
        "rsi_oversold":    22,
        "rsi_overbought":  78,
        "leverage":        5,
        "trade_usdt":      3,
        "profile":         "zeer agressief",

        "atr_period":      7,        # Zeer kort voor snelle memecoins
        "atr_sl_mult":     2.5,      # Zeer ruim want FART is explosief
        "atr_tp_mult":     4.0,      # Hoge target

        "sl_min_pct":      0.03,
        "sl_max_pct":      0.10,     # Tot 10% SL toegestaan bij FART
        "tp_min_pct":      0.08,
        "tp_max_pct":      0.25,     # Tot 25% TP mogelijk bij FART
    },
}

# ── Globale instellingen ──────────────────────────
MIN_TRADE_USDT     = 1
MAX_TRADE_USDT     = 3
MAX_DAILY_LOSS_PCT = 0.15
TIMEFRAME          = "15m"
PAPER_TRADING      = True

# ── Trailing stop instelling ──────────────────────
# Als de prijs stijgt, volgt de stop-loss automatisch mee omhoog
TRAILING_STOP_ENABLED = True
TRAILING_STOP_PCT     = 0.015   # Stop volgt op 1.5% afstand van hoogste prijs
```

---

## 🎯 Bonus: Trailing Stop

Ik heb ook een **trailing stop** toegevoegd. Dit is extra slim:
```
Zonder trailing stop:
  Koop op €100 → TP op €106, SL op €97
  Prijs gaat naar €105... zakt terug naar €97 → verlies ❌

Met trailing stop:
  Koop op €100 → prijs stijgt naar €105
  Stop-loss volgt mee → staat nu op €103.42 (1.5% onder €105)
  Prijs zakt terug → automatisch verkoop op €103.42 → WINST ✅
