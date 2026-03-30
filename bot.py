# -*- coding: utf-8 -*-
import asyncio
import logging
import os
from datetime import datetime
from strategy import TradingStrategy
from risk_manager import RiskManager
from gate_client import GateFuturesClient

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[logging.StreamHandler(), logging.FileHandler('bot.log')]
)
logger = logging.getLogger('JefBot')

SYMBOLS = ['BTC_USDT', 'ETH_USDT', 'XRP_USDT', 'FARTCOIN_USDT', 'ADA_USDT']
CYCLE_SECONDS = int(os.environ.get('CYCLE_INTERVAL_SECONDS', '60'))

# Heartbeat bestand — externe tools kunnen dit monitoren
HEARTBEAT_FILE = 'heartbeat.txt'


def write_heartbeat(cycle_count: int, errors: int):
    """Schrijf heartbeat naar bestand voor externe monitoring."""
    try:
        with open(HEARTBEAT_FILE, 'w') as f:
            f.write(f"{datetime.now().isoformat()}\n")
            f.write(f"cycles={cycle_count}\n")
            f.write(f"errors={errors}\n")
    except Exception:
        pass


async def main():
    api_key    = os.environ.get('GATE_API_KEY', '')
    api_secret = os.environ.get('GATE_API_SECRET', '')
    if not api_key or not api_secret:
        raise SystemExit("GATE_API_KEY en GATE_API_SECRET zijn vereist")

    logger.info(f"Jef Bot gestart | symbolen: {SYMBOLS} | cyclus: {CYCLE_SECONDS}s")
    client = GateFuturesClient(api_key, api_secret)
    risk   = RiskManager(
        max_leverage       = 20,
        risk_per_trade     = 0.02,
        max_trade_usd      = 3.0,
        max_total_exposure = 0.50,
        stop_loss_pct      = 0.20,
        take_profit_pct    = 0.30,
        max_daily_loss_usd = 5.0,
        max_daily_loss_pct = 0.20,
    )
    strat  = TradingStrategy(client, risk, SYMBOLS)
    started = datetime.now()
    cycle_count = 0
    error_count = 0

    try:
        while True:
            try:
                await strat.run_cycle()
                cycle_count += 1
                write_heartbeat(cycle_count, error_count)
                if cycle_count % 60 == 0:  # elke ~60 cycli een status log
                    uptime = datetime.now() - started
                    logger.info(
                        f"♥ Heartbeat | uptime={uptime} | "
                        f"cycli={cycle_count} | fouten={error_count}"
                    )
            except Exception as e:
                error_count += 1
                logger.error(f"Cyclus fout #{error_count}: {e}", exc_info=True)
                write_heartbeat(cycle_count, error_count)
            await asyncio.sleep(CYCLE_SECONDS)
    finally:
        await client.close()


if __name__ == '__main__':
    asyncio.run(main())
