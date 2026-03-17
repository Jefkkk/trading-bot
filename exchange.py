# exchange.py
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Alle communicatie met Gate.io via ccxt
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

import ccxt
import os
from dotenv import load_dotenv
from logger import log

load_dotenv()  # Laadt API keys uit .env bestand

def create_exchange():
    """Maak verbinding met Gate.io futures."""
    exchange = ccxt.gateio({
        "apiKey":  os.getenv("GATE_API_KEY"),
        "secret":  os.getenv("GATE_API_SECRET"),
        "options": {
            "defaultType": "future",   # Altijd futures, nooit spot
        },
    })
    exchange.set_sandbox_mode(False)   # True = testnet, False = live
    log("✅ Verbinding met Gate.io gemaakt")
    return exchange


def get_balance(exchange):
    """Haal beschikbaar USDT saldo op."""
    try:
        balance = exchange.fetch_balance()
        usdt = balance["USDT"]["free"]
        log(f"💰 Beschikbaar saldo: {usdt:.2f} USDT")
        return usdt
    except Exception as e:
        log(f"❌ Fout bij ophalen balance: {e}")
        return 0


def get_candles(exchange, symbol, timeframe, limit=100):
    """
    Haal historische candles op voor een symbool.
    Geeft een lijst terug van [timestamp, open, high, low, close, volume]
    """
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        log(f"📊 {limit} candles opgehaald voor {symbol}")
        return ohlcv
    except Exception as e:
        log(f"❌ Fout bij ophalen candles voor {symbol}: {e}")
        return []


def check_symbol_available(exchange, symbol):
    """Controleer of een futures paar beschikbaar is op Gate.io."""
    try:
        markets = exchange.load_markets()
        if symbol in markets:
            log(f"✅ {symbol} beschikbaar op Gate.io")
            return True
        else:
            log(f"⚠️  {symbol} NIET beschikbaar op Gate.io — overgeslagen")
            return False
    except Exception as e:
        log(f"❌ Fout bij checken symbool {symbol}: {e}")
        return False


def set_leverage(exchange, symbol, leverage):
    """Stel leverage in voor een symbool."""
    try:
        exchange.set_leverage(leverage, symbol)
        log(f"⚙️  Leverage ingesteld op {leverage}x voor {symbol}")
    except Exception as e:
        log(f"❌ Fout bij instellen leverage voor {symbol}: {e}")


def place_order(exchange, symbol, side, amount_usdt, price,
                sl_pct, tp_pct, leverage, paper_trading=True):
    """
    Plaats een futures order met stop-loss en take-profit.

    side        : 'buy' (long) of 'sell' (short)
    amount_usdt : inzet in USDT (bv. 3)
    price       : huidige marktprijs
    sl_pct      : stop-loss percentage (bv. 0.03 = 3%)
    tp_pct      : take-profit percentage (bv. 0.06 = 6%)
    """
    try:
        # ── Bereken positiegrootte ────────────────
        contracts = (amount_usdt * leverage) / price

        # ── Bereken SL en TP prijzen ──────────────
        if side == "buy":
            sl_price = round(price * (1 - sl_pct), 4)
            tp_price = round(price * (1 + tp_pct), 4)
        else:  # sell / short
            sl_price = round(price * (1 + sl_pct), 4)
            tp_price = round(price * (1 - tp_pct), 4)

        log(f"📋 Order voorbereiding: {side.upper()} {symbol}")
        log(f"   Prijs: {price} | Contracts: {contracts:.4f}")
        log(f"   SL: {sl_price} ({sl_pct*100:.1f}%) | "
            f"TP: {tp_price} ({tp_pct*100:.1f}%)")

        # ── Paper trading: simuleer order ─────────
        if paper_trading:
            log(f"📝 [PAPER] Order gesimuleerd — geen echt geld gebruikt")
            return {
                "id":       "PAPER_ORDER",
                "symbol":   symbol,
                "side":     side,
                "price":    price,
                "amount":   contracts,
                "sl":       sl_price,
                "tp":       tp_price,
                "status":   "paper",
            }

        # ── Live order plaatsen ───────────────────
        order = exchange.create_order(
            symbol   = symbol,
            type     = "market",
            side     = side,
            amount   = contracts,
            params   = {
                "stopLossPrice":   sl_price,
                "takeProfitPrice": tp_price,
            }
        )
        log(f"✅ Live order geplaatst: ID {order['id']}")
        return order

    except Exception as e:
        log(f"❌ Fout bij plaatsen order voor {symbol}: {e}")
        return None


def close_position(exchange, symbol, side, amount, paper_trading=True):
    """Sluit een open positie."""
    try:
        if paper_trading:
            log(f"📝 [PAPER] Positie gesloten voor {symbol}")
            return True

        close_side = "sell" if side == "buy" else "buy"
        exchange.create_order(
            symbol = symbol,
            type   = "market",
            side   = close_side,
            amount = amount,
            params = {"reduceOnly": True}
        )
        log(f"✅ Positie gesloten voor {symbol}")
        return True

    except Exception as e:
        log(f"❌ Fout bij sluiten positie {symbol}: {e}")
        return False


def get_current_price(exchange, symbol):
    """Haal de huidige marktprijs op."""
    try:
        ticker = exchange.fetch_ticker(symbol)
        return ticker["last"]
    except Exception as e:
        log(f"❌ Fout bij ophalen prijs {symbol}: {e}")
        return None
```

---

## 🔍 Wat doet elk onderdeel?

**`create_exchange()`** — maakt de verbinding met Gate.io, laadt je API keys veilig uit `.env`

**`get_balance()`** — checkt hoeveel USDT je hebt voor je begint te handelen

**`get_candles()`** — haalt 100 candles op per coin voor de strategie berekeningen

**`check_symbol_available()`** — checkt automatisch of FART en andere coins beschikbaar zijn als futures

**`set_leverage()`** — stelt 5x leverage in per coin voor de eerste trade

**`place_order()`** — het belangrijkste: plaatst een order met automatische SL en TP. In **paper trading modus simuleert** hij alles zonder echt geld

**`close_position()`** — sluit een positie netjes af

**`get_current_price()`** — haalt live prijs op voor trailing stop berekeningen

---

## ✅ Bestanden status
```
├── Procfile          ✅ klaar
├── runtime.txt       ✅ klaar
├── requirements.txt  ✅ klaar
├── config.py         ✅ klaar
├── .env              ✅ klaar
├── exchange.py       ✅ klaar  ← net gedaan
├── strategy.py       ← volgende stap
├── risk_manager.py   ← daarna
├── logger.py         ← daarna
└── main.py           ← als laatste
