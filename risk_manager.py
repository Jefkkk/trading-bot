# -*- coding: utf-8 -*-
import logging
from datetime import date

logger = logging.getLogger('RiskManager')


class RiskManager:
    def __init__(
        self,
        max_leverage:       int   = 20,
        risk_per_trade:     float = 0.02,
        max_trade_usd:      float = 3.0,
        max_total_exposure: float = 0.50,
        stop_loss_pct:      float = 0.20,
        take_profit_pct:    float = 0.30,
        max_daily_loss_usd: float = 5.0,
        max_daily_loss_pct: float = 0.20,
        max_drawdown_pct:   float = 0.10,   # Circuit breaker: stop bij -10%
        max_concurrent_pos: int   = 3,       # Max 3 gelijktijdige posities
    ):
        self.max_leverage        = max_leverage
        self.risk_per_trade      = risk_per_trade
        self.max_trade_usd       = max_trade_usd
        self.max_total_exposure  = max_total_exposure
        self.stop_loss_pct       = stop_loss_pct
        self.take_profit_pct     = take_profit_pct
        self.max_daily_loss_usd  = max_daily_loss_usd
        self.max_daily_loss_pct  = max_daily_loss_pct
        self.max_drawdown_pct    = max_drawdown_pct
        self.max_concurrent_pos  = max_concurrent_pos

        self.daily_pnl           = 0.0
        self.daily_start_balance = 0.0
        self.last_reset_date     = date.today()
        self.trade_count_today   = 0
        self.peak_balance        = 0.0      # Hoogste balans ooit (voor drawdown)
        self.circuit_breaker     = False     # True = alles gestopt

    def reset_daily_if_needed(self, balance: float):
        today = date.today()
        if today != self.last_reset_date:
            logger.info(f"Nieuwe dag | gisteren PnL: {self.daily_pnl:+.4f} USDT")
            self.daily_pnl           = 0.0
            self.daily_start_balance = balance
            self.last_reset_date     = today
            self.trade_count_today   = 0
        elif self.daily_start_balance == 0.0:
            self.daily_start_balance = balance

    def is_daily_loss_exceeded(self, balance: float) -> bool:
        if self.daily_start_balance <= 0:
            return False
        # Controleer absolute dollar grens
        if self.daily_pnl <= -self.max_daily_loss_usd:
            logger.warning(
                f"DAGVERLIES LIMIET: ${abs(self.daily_pnl):.2f} verlies "
                f"(max ${self.max_daily_loss_usd})  --  trading gestopt"
            )
            return True
        # Controleer procentuele grens
        loss_pct = (self.daily_start_balance - balance) / self.daily_start_balance
        if loss_pct >= self.max_daily_loss_pct:
            logger.warning(
                f"DAGVERLIES LIMIET: {loss_pct:.1%} verlies "
                f"(max {self.max_daily_loss_pct:.0%})  --  trading gestopt"
            )
            return True
        return False

    def calculate_position_size(self, balance: float, price: float,
                                leverage: int,
                                contract_multiplier: float = 1.0) -> int:
        """
        Berekent contracts met een dubbele cap:
        1. Percentage-gebaseerd (risk_per_trade% van balans)
        2. Harde dollar cap (max_trade_usd margin)

        Gate.io USDT perps: 1 contract = 1 USD notional
        margin = contracts / leverage
        """
        if price <= 0 or self.stop_loss_pct <= 0:
            return 0

        # Methode 1: procentueel
        risk_amt      = balance * self.risk_per_trade
        contracts_pct = int(risk_amt / (contract_multiplier * self.stop_loss_pct))

        # Methode 2: vaste dollar cap
        # margin = max_trade_usd  →  contracts = max_trade_usd * leverage
        contracts_usd = int(self.max_trade_usd * leverage)

        # Gebruik de KLEINSTE van beide
        contracts = min(contracts_pct, contracts_usd)
        contracts = max(1, contracts)

        # Exposure cap: margin mag niet meer zijn dan max_total_exposure% van balans
        margin     = contracts / leverage
        max_margin = balance * self.max_total_exposure
        if margin > max_margin:
            contracts = max(1, int(max_margin * leverage))

        # Minimum balans bescherming: niet traden als balans te laag is
        if balance < 5.0:
            logger.warning(f"Balans ${balance:.2f} te laag (min $5)  --  geen trade")
            return 0

        logger.info(
            f"Sizing: bal=${balance:.2f} max_trade=${self.max_trade_usd} "
            f"lev={leverage}x → {contracts} contracts "
            f"margin=${contracts/leverage:.2f} "
            f"max_loss=${contracts*self.stop_loss_pct:.2f}"
        )
        return contracts

    def get_stop_loss_price(self, entry: float, is_long: bool,
                            sl_pct: float = None) -> float:
        pct = sl_pct if sl_pct is not None else self.stop_loss_pct
        return round(entry * ((1 - pct) if is_long else (1 + pct)), 8)

    def get_take_profit_price(self, entry: float, is_long: bool,
                              tp_pct: float = None) -> float:
        pct = tp_pct if tp_pct is not None else self.take_profit_pct
        return round(entry * ((1 + pct) if is_long else (1 - pct)), 8)

    def get_optimal_leverage(self, volatility_score: float,
                             sl_pct: float = 0.0) -> int:
        """
        Leverage gebaseerd op daadwerkelijke SL percentage.
        Regel: max verlies op SL hit = 80% van margin.
        → max_lev = 0.80 / sl_pct
          SL 1%  → max 80x (gecapped op max_leverage)
          SL 5%  → max 16x
          SL 10% → max 8x
          SL 20% → max 4x
          SL 30% → max 2x
        Bij hoge volatiliteit extra korting.
        """
        if sl_pct > 0:
            safe_lev = int(0.80 / sl_pct)
        else:
            safe_lev = self.max_leverage

        # Volatiliteitsdiscount
        if   volatility_score > 0.75: discount = 0.40
        elif volatility_score > 0.50: discount = 0.60
        elif volatility_score > 0.25: discount = 0.80
        else:                         discount = 1.00

        lev = int(safe_lev * discount)
        return max(1, min(lev, self.max_leverage))

    def update_pnl(self, pnl: float):
        self.daily_pnl         += pnl
        self.trade_count_today += 1
        logger.info(
            f"PnL: {pnl:+.4f} | dag={self.daily_pnl:+.4f} USDT | "
            f"trades={self.trade_count_today}"
        )

    def update_peak_balance(self, balance: float):
        """Track hoogste balans voor drawdown berekening."""
        if balance > self.peak_balance:
            self.peak_balance = balance

    def check_drawdown(self, balance: float) -> bool:
        """
        Circuit breaker: stop ALLE trading als drawdown > max_drawdown_pct.
        Returns True als trading geblokkeerd moet worden.
        """
        if self.peak_balance <= 0:
            self.peak_balance = balance
            return False
        self.update_peak_balance(balance)
        drawdown = (self.peak_balance - balance) / self.peak_balance
        if drawdown >= self.max_drawdown_pct:
            if not self.circuit_breaker:
                logger.warning(
                    f"🚨 CIRCUIT BREAKER: drawdown {drawdown:.1%} >= {self.max_drawdown_pct:.0%} "
                    f"(peak=${self.peak_balance:.2f} → ${balance:.2f})"
                )
                self.circuit_breaker = True
            return True
        return False

    def can_open_position(self, current_open: int) -> bool:
        """Check of er ruimte is voor een nieuwe positie."""
        if self.circuit_breaker:
            logger.warning("Circuit breaker actief — geen nieuwe trades")
            return False
        if current_open >= self.max_concurrent_pos:
            logger.info(f"Max posities bereikt ({current_open}/{self.max_concurrent_pos})")
            return False
        return True

    def get_trailing_stop(self, entry: float, current: float, is_long: bool,
                          atr_value: float, atr_mult: float = 1.5) -> float:
        """
        ATR-gebaseerde trailing stop.
        - Long: stop = current_price - ATR * mult (stijgt mee, daalt nooit)
        - Short: stop = current_price + ATR * mult (daalt mee, stijgt nooit)
        """
        trail_dist = atr_value * atr_mult
        if is_long:
            return max(entry * 0.95, current - trail_dist)  # nooit lager dan -5% van entry
        else:
            return min(entry * 1.05, current + trail_dist)

    def get_partial_tp_levels(self, entry: float, is_long: bool,
                              sl_pct: float) -> list:
        """
        Partial take-profit ladder:
        - 50% sluiten bij 1.5× risk (1:1.5 R/R)
        - 25% sluiten bij 3× risk (1:3 R/R)
        - 25% laten lopen met trailing stop
        """
        r1 = sl_pct * 1.5  # 1:1.5 risk/reward
        r2 = sl_pct * 3.0  # 1:3 risk/reward
        if is_long:
            tp1 = round(entry * (1 + r1), 8)
            tp2 = round(entry * (1 + r2), 8)
        else:
            tp1 = round(entry * (1 - r1), 8)
            tp2 = round(entry * (1 - r2), 8)
        return [
            {'pct': 0.50, 'price': tp1, 'label': '1.5R'},
            {'pct': 0.25, 'price': tp2, 'label': '3R'},
            # Resterende 25% trailing stop
        ]
