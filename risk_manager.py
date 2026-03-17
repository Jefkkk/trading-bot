# risk_manager.py
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Beschermt je kapitaal via daglimieten,
# trailing stops en positiebeheer
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

from datetime import datetime, date
from logger import log, log_daily_summary
from config import MAX_DAILY_LOSS_PCT, TRAILING_STOP_ENABLED, TRAILING_STOP_PCT


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DAGELIJKSE TELLER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class DailyTracker:
    """
    Houdt dagelijkse statistieken bij.
    Reset automatisch om middernacht.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.date         = date.today()
        self.total_trades = 0
        self.wins         = 0
        self.losses       = 0
        self.total_pnl    = 0.0
        self.start_balance = None
        log("🔄 Dagelijkse teller gereset")

    def check_new_day(self):
        """Reset automatisch bij nieuwe dag."""
        if date.today() != self.date:
            log_daily_summary(
                self.total_trades,
                self.wins,
                self.losses,
                self.total_pnl
            )
            self.reset()

    def register_trade(self, pnl: float):
        """Registreer een afgesloten trade."""
        self.check_new_day()
        self.total_trades += 1
        self.total_pnl    += pnl

        if pnl > 0:
            self.wins   += 1
            log(f"✅ Winnende trade: +{pnl:.4f} USDT "
                f"(dag totaal: {self.total_pnl:+.4f} USDT)")
        else:
            self.losses += 1
            log(f"❌ Verliezende trade: {pnl:.4f} USDT "
                f"(dag totaal: {self.total_pnl:+.4f} USDT)")

    def is_daily_limit_reached(self, current_balance: float) -> bool:
        """
        Controleer of het dagelijkse verlies de limiet bereikt heeft.
        Stopt de bot als dit het geval is.
        """
        self.check_new_day()

        if self.start_balance is None:
            self.start_balance = current_balance
            return False

        loss_pct = (self.start_balance - current_balance) / self.start_balance

        if loss_pct >= MAX_DAILY_LOSS_PCT:
            log(
                f"🛑 DAGELIJKSE VERLIES LIMIET BEREIKT: "
                f"{loss_pct*100:.1f}% verlies "
                f"(max: {MAX_DAILY_LOSS_PCT*100:.0f}%) — "
                f"Bot gestopt tot morgen"
            )
            return True

        remaining = (MAX_DAILY_LOSS_PCT - loss_pct) * self.start_balance
        log(f"📊 Dagverlies: {loss_pct*100:.1f}% | "
            f"Nog {remaining:.2f} USDT buffer over")
        return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# POSITIE BEWAKER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class PositionManager:
    """
    Beheert open posities en trailing stops
    voor alle coins tegelijk.
    """

    def __init__(self):
        # { symbol: positie_info }
        self.positions = {}

    def open_position(self, symbol, side, entry_price,
                      amount, sl_pct, tp_pct):
        """Registreer een nieuwe open positie."""
        if side == "buy":
            sl_price = entry_price * (1 - sl_pct)
            tp_price = entry_price * (1 + tp_pct)
        else:
            sl_price = entry_price * (1 + sl_pct)
            tp_price = entry_price * (1 - tp_pct)

        self.positions[symbol] = {
            "side":          side,
            "entry_price":   entry_price,
            "amount":        amount,
            "sl_price":      sl_price,
            "tp_price":      tp_price,
            "highest_price": entry_price,   # Voor trailing stop
            "lowest_price":  entry_price,   # Voor trailing stop short
            "opened_at":     datetime.now(),
        }
        log(f"📂 Positie geopend: {side.upper()} {symbol} "
            f"@ {entry_price} | SL: {sl_price:.4f} | TP: {tp_price:.4f}")

    def has_position(self, symbol) -> bool:
        """Controleer of er al een open positie is voor dit symbool."""
        return symbol in self.positions

    def update_trailing_stop(self, symbol, current_price):
        """
        Pas trailing stop aan als prijs gunstig beweegt.
        Stop-loss volgt de prijs omhoog (long) of omlaag (short).
        """
        if not TRAILING_STOP_ENABLED:
            return
        if symbol not in self.positions:
            return

        pos = self.positions[symbol]

        if pos["side"] == "buy":
            # Prijs steeg → stop omhoog aanpassen
            if current_price > pos["highest_price"]:
                pos["highest_price"] = current_price
                new_sl = current_price * (1 - TRAILING_STOP_PCT)

                if new_sl > pos["sl_price"]:
                    old_sl = pos["sl_price"]
                    pos["sl_price"] = new_sl
                    log(f"📈 Trailing stop aangepast {symbol}: "
                        f"{old_sl:.4f} → {new_sl:.4f} "
                        f"(prijs: {current_price:.4f})")

        elif pos["side"] == "sell":
            # Prijs daalde → stop omlaag aanpassen
            if current_price < pos["lowest_price"]:
                pos["lowest_price"] = current_price
                new_sl = current_price * (1 + TRAILING_STOP_PCT)

                if new_sl < pos["sl_price"]:
                    old_sl = pos["sl_price"]
                    pos["sl_price"] = new_sl
                    log(f"📉 Trailing stop aangepast {symbol}: "
                        f"{old_sl:.4f} → {new_sl:.4f} "
                        f"(prijs: {current_price:.4f})")

    def should_close(self, symbol, current_price):
        """
        Controleer of een positie gesloten moet worden.
        Geeft terug: ('sl', reden) / ('tp', reden) / (None, None)
        """
        if symbol not in self.positions:
            return None, None

        pos = self.positions[symbol]

        if pos["side"] == "buy":
            if current_price <= pos["sl_price"]:
                return "sl", f"Stop-loss geraakt @ {current_price:.4f}"
            if current_price >= pos["tp_price"]:
                return "tp", f"Take-profit geraakt @ {current_price:.4f}"

        elif pos["side"] == "sell":
            if current_price >= pos["sl_price"]:
                return "sl", f"Stop-loss geraakt @ {current_price:.4f}"
            if current_price <= pos["tp_price"]:
                return "tp", f"Take-profit geraakt @ {current_price:.4f}"

        return None, None

    def calculate_pnl(self, symbol, close_price):
        """Bereken PnL van een positie bij sluiting."""
        if symbol not in self.positions:
            return 0

        pos    = self.positions[symbol]
        entry  = pos["entry_price"]
        amount = pos["amount"]

        if pos["side"] == "buy":
            pnl = (close_price - entry) * amount
        else:
            pnl = (entry - close_price) * amount

        return round(pnl, 6)

    def close_position(self, symbol, close_price, reason=""):
        """Verwijder positie uit geheugen na sluiting."""
        if symbol not in self.positions:
            return 0

        pnl = self.calculate_pnl(symbol, close_price)
        pos = self.positions.pop(symbol)

        duration = datetime.now() - pos["opened_at"]
        minutes  = int(duration.total_seconds() / 60)

        log(f"📁 Positie gesloten: {symbol} | "
            f"Reden: {reason} | "
            f"PnL: {pnl:+.4f} USDT | "
            f"Duur: {minutes} min")

        return pnl

    def get_open_symbols(self):
        """Geef lijst van symbolen met open posities."""
        return list(self.positions.keys())
```

---

## 🛡️ Wat beschermt de risk manager?

**`DailyTracker`** — reset elke dag om middernacht en stopt de bot automatisch bij 15% dagverlies

**`PositionManager`** — beheert alle open posities tegelijk voor de 4 coins:
```
Positie geopend BTC @ €65.000
  ↓ prijs stijgt naar €66.000
  ↓ trailing stop volgt mee → SL nu op €64.990
  ↓ prijs zakt terug naar €64.990
  ↓ automatisch gesloten met winst ✅

Zonder trailing stop → wachten op originele SL van €63.050
                     → minder winst of zelfs verlies
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
├── strategy.py       ✅ klaar
├── risk_manager.py   ✅ klaar  ← net gedaan
└── main.py           ← laatste stap! 🎯
