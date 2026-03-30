# -*- coding: utf-8 -*-
import logging
from datetime import date

logger = logging.getLogger('RiskManager')


class RiskManager:
    def __init__(
        self,
        max_leverage:       int   = 20,
        risk_per_trade:     float = 0.02,
        max_trade_usd:      float = 3.0,    # Harde cap: max $3 margin per trade
        max_total_exposure: float = 0.50,   # Max 50% van balans tegelijk in markt
        stop_loss_pct:      float = 0.20,   # 20% SL (ruimer, minder noise stops)
        take_profit_pct:    float = 0.30,   # 30% TP (1:1.5 R/R)
        max_daily_loss_usd: float = 5.0,    # Stop bij $5 dagverlies
        max_daily_loss_pct: float = 0.20,   # Of bij 20% van balans (wat eerst bereikt wordt)
    ):
        self.max_leverage        = max_leverage
        self.risk_per_trade      = risk_per_trade
        self.max_trade_usd       = max_trade_usd
        self.max_total_exposure  = max_total_exposure
        self.stop_loss_pct       = stop_loss_pct
        self.take_profit_pct     = take_profit_pct
        self.max_daily_loss_usd  = max_daily_loss_usd
        self.max_daily_loss_pct  = max_daily_loss_pct

        self.daily_pnl           = 0.0
        self.daily_start_balance = 0.0
        self.last_reset_date     = date.today()
        self.trade_count_today   = 0

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
