# logger.py
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Logging van alle bot activiteit
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

import logging
import os
from datetime import datetime

# ── Map aanmaken voor logs ────────────────────────
os.makedirs("logs", exist_ok=True)

# ── Logger configuratie ───────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        # Naar bestand schrijven
        logging.FileHandler(
            f"logs/bot_{datetime.now().strftime('%Y%m%d')}.log"
        ),
        # Ook zichtbaar in Railway console
        logging.StreamHandler()
    ]
)

logger = logging.getLogger("trading_bot")


def log(message: str, level: str = "info"):
    """Schrijf een bericht naar log bestand én Railway console."""
    if level == "info":
        logger.info(message)
    elif level == "warning":
        logger.warning(message)
    elif level == "error":
        logger.error(message)
    elif level == "critical":
        logger.critical(message)


def log_trade(symbol, side, price, amount, sl, tp, pnl=None, status="open"):
    """
    Log een trade met alle details op één lijn.
    Makkelijk terug te vinden in je logbestand.
    """
    pnl_str = f"PnL: {pnl:+.4f} USDT" if pnl is not None else "PnL: open"
    log(
        f"TRADE | {status.upper():<6} | {side.upper():<4} | "
        f"{symbol:<20} | Prijs: {price:<12} | "
        f"Bedrag: {amount:<10.4f} | SL: {sl:<12} | "
        f"TP: {tp:<12} | {pnl_str}"
    )


def log_signal(symbol, signal, ema_short, ema_long, rsi=None):
    """Log een strategie signaal."""
    rsi_str = f"RSI: {rsi:.1f}" if rsi is not None else ""
    log(
        f"SIGNAAL | {signal.upper():<8} | {symbol:<20} | "
        f"EMA kort: {ema_short:.4f} | EMA lang: {ema_long:.4f} | {rsi_str}"
    )


def log_daily_summary(total_trades, wins, losses, total_pnl):
    """Dagelijkse samenvatting — handig om 's ochtends te checken."""
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    log("=" * 60)
    log(f"📊 DAGELIJKSE SAMENVATTING")
    log(f"   Totaal trades : {total_trades}")
    log(f"   Gewonnen      : {wins} ({win_rate:.1f}%)")
    log(f"   Verloren      : {losses}")
    log(f"   Totaal PnL    : {total_pnl:+.4f} USDT")
    log("=" * 60)
```

---

## 📋 Voorbeeld van hoe logs eruitzien in Railway
```
2024-03-17 14:32:01 | INFO | ✅ Verbinding met Gate.io gemaakt
2024-03-17 14:32:02 | INFO | 💰 Beschikbaar saldo: 25.00 USDT
2024-03-17 14:32:03 | INFO | ✅ BTC/USDT:USDT beschikbaar op Gate.io
2024-03-17 14:32:05 | INFO | SIGNAAL | BUY      | BTC/USDT:USDT       | EMA kort: 65234.1 | EMA lang: 64800.2
2024-03-17 14:32:05 | INFO | TRADE   | OPEN   | BUY  | BTC/USDT:USDT       | Prijs: 65234.1    | SL: 63277.1  | TP: 69148.1
2024-03-17 15:14:22 | INFO | TRADE   | CLOSED | BUY  | BTC/USDT:USDT       | Prijs: 65234.1    | PnL: +0.8700 USDT
2024-03-17 23:59:59 | INFO | ════════════════════════════════════════
2024-03-17 23:59:59 | INFO | 📊 DAGELIJKSE SAMENVATTING
2024-03-17 23:59:59 | INFO |    Totaal trades : 8
2024-03-17 23:59:59 | INFO |    Gewonnen      : 5 (62.5%)
2024-03-17 23:59:59 | INFO |    Verloren      : 3
2024-03-17 23:59:59 | INFO |    Totaal PnL    : +2.3400 USDT
2024-03-17 23:59:59 | INFO | ════════════════════════════════════════
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
├── logger.py         ✅ klaar  ← net gedaan
├── strategy.py       ← volgende stap 🎯
├── risk_manager.py   ← daarna
└── main.py           ← als laatste
