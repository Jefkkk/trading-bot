# main.py
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Hoofdprogramma — verbindt alle modules
# en draait 24/7 op Railway
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

import time
import schedule
from datetime import datetime

from config import COIN_CONFIGS, TIMEFRAME, PAPER_TRADING, MIN_TRADE_USDT
from exchange import (
    create_exchange, get_balance, get_candles,
    check_symbol_available, set_leverage,
    place_order, close_position, get_current_price
)
from strategy import get_signal
from risk_manager import DailyTracker, PositionManager
from logger import log, log_trade


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GLOBALE OBJECTEN
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

exchange = None
tracker  = DailyTracker()
manager  = PositionManager()

# Beschikbare symbolen (gecheckt bij opstart)
active_symbols = []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# OPSTART
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def startup():
    """Initialiseer de bot bij opstart."""
    global exchange, active_symbols

    log("=" * 60)
    log("🤖 TRADING BOT GESTART")
    log(f"   Modus     : {'📝 PAPER TRADING' if PAPER_TRADING else '💰 LIVE TRADING'}")
    log(f"   Timeframe : {TIMEFRAME}")
    log(f"   Datum     : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log("=" * 60)

    # Verbinding maken
    exchange = create_exchange()

    # Balance ophalen
    balance = get_balance(exchange)
    tracker.start_balance = balance

    # Controleer welke symbolen beschikbaar zijn
    for symbol, config in COIN_CONFIGS.items():
        config["symbol"] = symbol   # Symbool toevoegen aan config
        if check_symbol_available(exchange, symbol):
            set_leverage(exchange, symbol, config["leverage"])
            active_symbols.append(symbol)

    if not active_symbols:
        log("❌ Geen beschikbare symbolen gevonden — bot gestopt")
        return False

    log(f"✅ Actieve symbolen: {', '.join(active_symbols)}")
    log("🚀 Bot is klaar om te handelen!")
    return True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HOOFD TRADING LOOP
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def trading_cycle():
    """
    Wordt elke 15 minuten uitgevoerd.
    1. Check dagelijks verlies limiet
    2. Update open posities (trailing stop / SL / TP)
    3. Zoek nieuwe signalen voor coins zonder positie
    """
    log("─" * 40)
    log(f"🔁 Nieuwe cyclus: {datetime.now().strftime('%H:%M:%S')}")

    # ── Stap 1: dagverlies check ──────────────────
    balance = get_balance(exchange)
    if tracker.is_daily_limit_reached(balance):
        log("⏸️  Bot gepauzeerd tot morgen wegens dagverlies limiet")
        return

    # ── Stap 2: open posities bewaken ────────────
    for symbol in manager.get_open_symbols():
        current_price = get_current_price(exchange, symbol)
        if not current_price:
            continue

        # Trailing stop updaten
        manager.update_trailing_stop(symbol, current_price)

        # Checken of SL of TP geraakt is
        reason_code, reason_msg = manager.should_close(symbol, current_price)

        if reason_code:
            pnl = manager.calculate_pnl(symbol, current_price)
            pos = manager.positions[symbol]

            # Positie sluiten op exchange
            close_position(
                exchange, symbol,
                pos["side"], pos["amount"],
                PAPER_TRADING
            )

            # Registreren en loggen
            manager.close_position(symbol, current_price, reason_msg)
            tracker.register_trade(pnl)

            log_trade(
                symbol     = symbol,
                side       = pos["side"],
                price      = current_price,
                amount     = pos["amount"],
                sl         = pos["sl_price"],
                tp         = pos["tp_price"],
                pnl        = pnl,
                status     = "closed"
            )

    # ── Stap 3: nieuwe signalen zoeken ───────────
    for symbol in active_symbols:

        # Sla over als er al een positie open is
        if manager.has_position(symbol):
            log(f"⏭️  {symbol} — positie al open, skip")
            continue

        config = COIN_CONFIGS[symbol]

        # Candles ophalen
        ohlcv = get_candles(exchange, symbol, TIMEFRAME, limit=100)
        if not ohlcv:
            continue

        # Signaal berekenen
        result = get_signal(ohlcv, config)
        signal = result["signal"]
        sl_pct = result["sl_pct"]
        tp_pct = result["tp_pct"]

        if signal == "hold":
            log(f"⏸️  {symbol} — geen signaal")
            continue

        # Huidige prijs ophalen
        current_price = get_current_price(exchange, symbol)
        if not current_price:
            continue

        # Inzetbedrag bepalen (random tussen MIN en MAX)
        import random
        trade_usdt = round(
            random.uniform(MIN_TRADE_USDT, config["trade_usdt"]), 2
        )

        # Minimum check
        if trade_usdt < MIN_TRADE_USDT:
            log(f"⚠️  {symbol} — inzet te laag ({trade_usdt} USDT), skip")
            continue

        # Order plaatsen
        order = place_order(
            exchange    = exchange,
            symbol      = symbol,
            side        = signal,
            amount_usdt = trade_usdt,
            price       = current_price,
            sl_pct      = sl_pct,
            tp_pct      = tp_pct,
            leverage    = config["leverage"],
            paper_trading = PAPER_TRADING
        )

        if order:
            # Positie registreren in manager
            contracts = (trade_usdt * config["leverage"]) / current_price
            manager.open_position(
                symbol       = symbol,
                side         = signal,
                entry_price  = current_price,
                amount       = contracts,
                sl_pct       = sl_pct,
                tp_pct       = tp_pct
            )

            log_trade(
                symbol  = symbol,
                side    = signal,
                price   = current_price,
                amount  = contracts,
                sl      = current_price * (1 - sl_pct),
                tp      = current_price * (1 + tp_pct),
                status  = "open"
            )

    log(f"✅ Cyclus klaar | Open posities: "
        f"{len(manager.get_open_symbols())}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DAGELIJKSE SAMENVATTING INPLANNEN
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def daily_summary():
    """Stuur dagelijkse samenvatting om 23:59."""
    from logger import log_daily_summary
    log_daily_summary(
        tracker.total_trades,
        tracker.wins,
        tracker.losses,
        tracker.total_pnl
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ENTRY POINT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

if __name__ == "__main__":

    # Opstart
    if not startup():
        exit(1)

    # Eerste cyclus direct uitvoeren
    trading_cycle()

    # Inplannen elke 15 minuten
    schedule.every(15).minutes.do(trading_cycle)

    # Dagelijkse samenvatting om 23:59
    schedule.every().day.at("23:59").do(daily_summary)

    log("⏰ Scheduler actief — cyclus elke 15 minuten")

    # ── Oneindige loop voor Railway ───────────────
    while True:
        schedule.run_pending()
        time.sleep(30)   # Check elke 30 seconden
```

---

## 🎉 De bot is compleet!
```
├── Procfile          ✅ klaar
├── runtime.txt       ✅ klaar
├── requirements.txt  ✅ klaar
├── config.py         ✅ klaar
├── .env              ✅ klaar
├── exchange.py       ✅ klaar
├── logger.py         ✅ klaar
├── strategy.py       ✅ klaar
├── risk_manager.py   ✅ klaar
└── main.py           ✅ klaar  ← net gedaan
