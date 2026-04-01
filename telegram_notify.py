# -*- coding: utf-8 -*-
"""
Telegram Notificaties voor Jef Bot

Setup:
1. Maak een bot via @BotFather op Telegram → krijg je BOT_TOKEN
2. Start een chat met je bot en stuur /start
3. Haal je chat_id op via https://api.telegram.org/bot<TOKEN>/getUpdates
4. Zet environment variables:
   TELEGRAM_BOT_TOKEN=123456:ABC-DEF
   TELEGRAM_CHAT_ID=987654321

Gebruik in code:
   from telegram_notify import notify, notify_trade, notify_alert
   await notify("Bot gestart!")
   await notify_trade("BTC_USDT", "long", 87000, 0.85, 10)
"""

import os
import logging
import aiohttp
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Europe/Brussels')
logger = logging.getLogger('Telegram')

BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
CHAT_ID   = os.environ.get('TELEGRAM_CHAT_ID', '')
API_URL   = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

# Stille modus: als geen token geconfigureerd, doe niets (geen errors)
ENABLED = bool(BOT_TOKEN and CHAT_ID)


async def notify(message: str, silent: bool = False):
    """Stuur een bericht naar Telegram. Faalt stilletjes als niet geconfigureerd."""
    if not ENABLED:
        return
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(API_URL, json={
                'chat_id': CHAT_ID,
                'text': message,
                'parse_mode': 'HTML',
                'disable_notification': silent,
            }, timeout=aiohttp.ClientTimeout(total=10))
    except Exception as e:
        logger.debug(f"Telegram fout (niet kritisch): {e}")


async def notify_trade(symbol: str, direction: str, price: float,
                       confidence: float, leverage: int,
                       sl: float = 0, tp: float = 0, roi_tp: float = 0):
    """Notificatie bij trade opening."""
    arrow = "🟢 LONG" if direction == 'long' else "🔴 SHORT"
    now = datetime.now(TZ).strftime('%H:%M')
    roi_str = f"\n💎 TP ROI: {roi_tp:.0f}%" if roi_tp else ""
    msg = (
        f"{arrow} <b>{symbol.replace('_USDT','')}</b>\n"
        f"⏰ {now} | 📊 Conf: {confidence:.0%} | ⚡ {leverage}×\n"
        f"💰 Entry: ${price:.6f}\n"
        f"🛑 SL: ${sl:.6f}\n"
        f"🎯 TP: ${tp:.6f}{roi_str}"
    )
    await notify(msg)


async def notify_close(symbol: str, direction: str, pnl: float,
                       roi_pct: float, reason: str):
    """Notificatie bij trade sluiting."""
    emoji = "✅" if pnl >= 0 else "❌"
    now = datetime.now(TZ).strftime('%H:%M')
    msg = (
        f"{emoji} <b>CLOSE {symbol.replace('_USDT','')}</b>\n"
        f"⏰ {now} | {direction.upper()} → {reason}\n"
        f"💰 PnL: {'+' if pnl>=0 else ''}{pnl:.4f} USDT\n"
        f"📈 ROI: {'+' if roi_pct>=0 else ''}{roi_pct:.1f}%"
    )
    await notify(msg)


async def notify_alert(title: str, message: str, urgent: bool = True):
    """Waarschuwing — liquidatie, stalled, daggrens etc."""
    msg = f"⚠️ <b>{title}</b>\n{message}"
    await notify(msg, silent=not urgent)


async def notify_daily_summary(balance: float, daily_pnl: float,
                               trades_today: int, win_count: int):
    """Dagelijkse samenvatting."""
    now = datetime.now(TZ).strftime('%d/%m/%Y')
    wr = (win_count / trades_today * 100) if trades_today > 0 else 0
    emoji = "📈" if daily_pnl >= 0 else "📉"
    msg = (
        f"📋 <b>Dagrapport {now}</b>\n\n"
        f"💰 Balans: ${balance:.2f}\n"
        f"{emoji} Dag PnL: {'+' if daily_pnl>=0 else ''}{daily_pnl:.4f} USDT\n"
        f"📊 Trades: {trades_today} (WR: {wr:.0f}%)\n"
    )
    await notify(msg, silent=True)


async def notify_liquidation_warning(symbol: str, liq_price: float,
                                     current_price: float, distance_pct: float):
    """URGENTE liquidatie waarschuwing."""
    msg = (
        f"🚨🚨 <b>LIQUIDATIE WAARSCHUWING</b> 🚨🚨\n\n"
        f"<b>{symbol.replace('_USDT','')}</b>\n"
        f"Huidige prijs: ${current_price:.6f}\n"
        f"Liquidatie prijs: ${liq_price:.6f}\n"
        f"Afstand: <b>{distance_pct:.1f}%</b>\n\n"
        f"⚠️ Overweeg positie te verkleinen!"
    )
    await notify(msg, silent=False)


async def notify_funding(symbol: str, rate: float, direction: str, est_earning: float):
    """Funding rate opportuniteit."""
    msg = (
        f"💸 <b>Funding {symbol.replace('_USDT','')}</b>\n"
        f"Rate: {rate*100:.4f}% per 8u\n"
        f"Richting: {direction.upper()}\n"
        f"Geschatte verdienste: ${est_earning:.4f}"
    )
    await notify(msg, silent=True)
