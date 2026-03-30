# -*- coding: utf-8 -*-
import asyncio
import threading
import logging
import os
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, jsonify, request, render_template_string

TZ = ZoneInfo('Europe/Brussels')
from gate_client import GateFuturesClient
from backtester import Backtester, STRATEGIES
try:
    from trade_memory import TradeEvaluator, init_db, get_stats_summary
    init_db()
    _evaluator = TradeEvaluator()
    MEMORY_OK = True
except Exception:
    MEMORY_OK = False
    _evaluator = None
from strategy import TradingStrategy
from risk_manager import RiskManager

logger = logging.getLogger('WebServer')
app = Flask(__name__)

# Globale bot state
bot_state = {
    'running': False,
    'thread': None,
    'log_buffer': [],
    'last_cycle': None,
    'positions': [],
    'balance': 0.0,
    'daily_pnl': 0.0,
    'trade_count': 0,
    # Heartbeat & monitoring
    'started_at': None,        # wanneer bot gestart
    'cycle_count': 0,          # totaal aantal cycli
    'last_error': None,        # laatste foutmelding
    'error_count': 0,          # fouten sinds start
    'cycles_without_trade': 0, # cycli sinds laatste trade
    'last_trade_at': None,     # wanneer laatste trade
    'last_signal': {},         # laatst gezien signaal per coin
}

SYMBOLS = ['BTC_USDT', 'ETH_USDT', 'XRP_USDT', 'FARTCOIN_USDT', 'ADA_USDT']

def normalize_candles(raw):
    """Gate.io kan candles als dict of als lijst teruggeven. Normaliseer naar dict formaat."""
    if not raw:
        return []
    first = raw[0]
    if isinstance(first, dict):
        # Controleer of velden aanwezig zijn, anders hernoem
        if 't' in first:
            return raw  # al correct formaat
        # Soms gebruikt Gate.io andere veldnamen
        return [{'t': c.get('time', c.get('timestamp', i)),
                 'o': c.get('open', c.get('o', 0)),
                 'h': c.get('high', c.get('h', 0)),
                 'l': c.get('low',  c.get('l', 0)),
                 'c': c.get('close', c.get('c', 0)),
                 'v': c.get('volume', c.get('v', c.get('base_volume', 0)))}
                for i, c in enumerate(raw)]
    elif isinstance(first, list):
        # Lijst formaat: [timestamp, open, high, low, close, volume]
        return [{'t': c[0], 'o': c[1], 'h': c[2], 'l': c[3], 'c': c[4], 'v': c[5] if len(c) > 5 else 0}
                for c in raw]
    return raw


def get_client():
    return GateFuturesClient(
        os.environ.get('GATE_API_KEY', ''),
        os.environ.get('GATE_API_SECRET', '')
    )

# --- Bot Control ------------------------------------------------------------

def bot_loop():
    async def _run():
        client = get_client()
        risk = RiskManager(
            max_leverage       = 20,
            risk_per_trade     = 0.02,
            max_trade_usd      = 3.0,
            max_total_exposure = 0.50,
            stop_loss_pct      = 0.20,
            take_profit_pct    = 0.30,
            max_daily_loss_usd = 5.0,
            max_daily_loss_pct = 0.20,
        )
        strategy = TradingStrategy(client, risk, SYMBOLS)
        bot_state['started_at'] = datetime.now(TZ).isoformat()
        while bot_state['running']:
            try:
                prev_count = risk.trade_count_today
                await strategy.run_cycle()
                bot_state['last_cycle']  = datetime.now(TZ).isoformat()
                bot_state['cycle_count'] += 1
                bot_state['daily_pnl']   = risk.daily_pnl
                bot_state['trade_count'] = risk.trade_count_today
                acc = await client.get_account()
                if acc:
                    bot_state['balance'] = float(acc.get('available', 0))
                pos = await client.get_positions()
                bot_state['positions'] = [
                    p for p in (pos or []) if int(p.get('size', 0)) != 0
                ]
                # Track trades
                if risk.trade_count_today > prev_count:
                    bot_state['last_trade_at'] = datetime.now(TZ).isoformat()
                    bot_state['cycles_without_trade'] = 0
                else:
                    bot_state['cycles_without_trade'] += 1
                # Heartbeat bestand schrijven (voor externe monitoring)
                try:
                    with open('heartbeat.txt', 'w') as hb:
                        hb.write(datetime.now(TZ).isoformat())
                except:
                    pass
            except Exception as e:
                logger.error(f"Bot loop error: {e}")
                bot_state['last_error'] = f"{datetime.now(TZ).strftime('%H:%M:%S')} {str(e)[:100]}"
                bot_state['error_count'] += 1
            await asyncio.sleep(60)
        await client.close()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run())
    finally:
        loop.close()

@app.route('/api/bot/start', methods=['POST'])
def start_bot():
    if bot_state['running']:
        return jsonify({'status': 'already_running'})
    bot_state['running'] = True
    bot_state['cycle_count'] = 0
    bot_state['error_count'] = 0
    bot_state['last_error'] = None
    bot_state['cycles_without_trade'] = 0
    bot_state['started_at'] = datetime.now(TZ).isoformat()
    t = threading.Thread(target=bot_loop, daemon=True)
    t.start()
    bot_state['thread'] = t
    return jsonify({'status': 'started'})

@app.route('/api/bot/stop', methods=['POST'])
def stop_bot():
    bot_state['running'] = False
    return jsonify({'status': 'stopped'})

@app.route('/api/bot/status')
def bot_status():
    # Bereken uptime en stalled detectie
    now = datetime.now(TZ)
    uptime_str = ''
    if bot_state['started_at']:
        started = datetime.fromisoformat(bot_state['started_at'])
        delta = now - started
        hours, rem = divmod(int(delta.total_seconds()), 3600)
        mins = rem // 60
        uptime_str = f"{hours}u {mins}m"

    last_cycle_ago = ''
    stalled = False
    if bot_state['last_cycle']:
        last = datetime.fromisoformat(bot_state['last_cycle'])
        ago = (now - last).total_seconds()
        if ago < 120:
            last_cycle_ago = f"{int(ago)}s geleden"
        elif ago < 7200:
            last_cycle_ago = f"{int(ago//60)}m geleden"
        else:
            last_cycle_ago = f"{int(ago//3600)}u {int((ago%3600)//60)}m geleden"
        # Stalled: meer dan 5 minuten geen cyclus terwijl bot "draait"
        if bot_state['running'] and ago > 300:
            stalled = True

    last_trade_ago = ''
    if bot_state['last_trade_at']:
        lt = datetime.fromisoformat(bot_state['last_trade_at'])
        ta = (now - lt).total_seconds()
        if ta < 3600:
            last_trade_ago = f"{int(ta//60)}m geleden"
        elif ta < 86400:
            last_trade_ago = f"{int(ta//3600)}u geleden"
        else:
            last_trade_ago = f"{int(ta//86400)}d geleden"

    # Bepaal overall status
    if not bot_state['running']:
        health = 'offline'
    elif stalled:
        health = 'stalled'
    elif bot_state['error_count'] > 10:
        health = 'degraded'
    else:
        health = 'online'

    return jsonify({
        'running':              bot_state['running'],
        'health':               health,
        'last_cycle':           bot_state['last_cycle'],
        'last_cycle_ago':       last_cycle_ago,
        'balance':              bot_state['balance'],
        'daily_pnl':            bot_state['daily_pnl'],
        'trade_count':          bot_state['trade_count'],
        'positions':            bot_state['positions'],
        'uptime':               uptime_str,
        'started_at':           bot_state['started_at'],
        'cycle_count':          bot_state['cycle_count'],
        'error_count':          bot_state['error_count'],
        'last_error':           bot_state['last_error'],
        'cycles_without_trade': bot_state['cycles_without_trade'],
        'last_trade_at':        bot_state['last_trade_at'],
        'last_trade_ago':       last_trade_ago,
        'stalled':              stalled,
    })

# --- Live Data ---------------------------------------------------------------

@app.route('/api/ticker/<symbol>')
def get_ticker(symbol):
    import requests as req
    try:
        url = 'https://api.gateio.ws/api/v4/futures/usdt/tickers'
        r = req.get(url, params={'contract': symbol}, timeout=10)
        data = r.json()
        return jsonify(data[0] if data else {})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/candles/<symbol>')
def get_candles(symbol):
    import requests as req
    interval = request.args.get('interval', '5m')
    limit    = int(request.args.get('limit', 200))
    try:
        url = 'https://api.gateio.ws/api/v4/futures/usdt/candlesticks'
        r = req.get(url, params={'contract': symbol, 'interval': interval, 'limit': limit}, timeout=15)
        return jsonify(normalize_candles(r.json()))
    except Exception as e:
        return jsonify([]), 500

@app.route('/api/account')
def get_account():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        async def _fetch():
            client = get_client()
            a = await client.get_account()
            p = await client.get_positions()
            await client.close()
            return {'account': a, 'positions': p or []}
        data = loop.run_until_complete(_fetch())
        return jsonify(data)
    except Exception as e:
        return jsonify({'account': None, 'positions': []}), 500
    finally:
        loop.close()

# --- Backtesting -------------------------------------------------------------

@app.route('/api/strategies')
def get_strategies():
    """Geef lijst van beschikbare strategieën."""
    return jsonify({
        'strategies': [
            {'id': 'btc_trend',         'name': 'BTC Trend (EMA ribbon + ADX)',       'coin': 'BTC'},
            {'id': 'eth_squeeze',       'name': 'ETH Squeeze Breakout (BB + OBV)',    'coin': 'ETH'},
            {'id': 'xrp_roi',       'name': 'XRP ROI (4H pullback + BB bounce)',    'coin': 'XRP'},
            {'id': 'fartcoin_momentum', 'name': 'FARTCOIN Momentum (trend + dip)',     'coin': 'FART'},
            {'id': 'fartcoin_bb',       'name': 'FARTCOIN Bollinger Bands (4H)',      'coin': 'FART'},
            {'id': 'ada_supertrend',    'name': 'ADA Supertrend + OBV',               'coin': 'ADA'},
            {'id': 'rsi_meanrev',       'name': 'RSI Mean-Reversion (alle coins)',    'coin': 'ALL'},
            {'id': 'ema_crossover',     'name': 'EMA 9/21 Crossover (alle coins)',    'coin': 'ALL'},
            {'id': 'bb_bounce',         'name': 'BB Bounce (alle coins)',             'coin': 'ALL'},
            {'id': 'general_consensus', 'name': 'Algemeen Consensus (alle coins)',     'coin': 'ALL'},
        ]
    })

@app.route('/api/backtest', methods=['POST'])
def run_backtest():
    import requests as req
    body = request.json or {}
    symbol    = body.get('symbol', 'BTC_USDT')
    interval  = body.get('interval', '1h')
    limit     = int(body.get('limit', 500))
    balance   = float(body.get('balance', 100))
    risk_pct  = float(body.get('risk_pct', 0.02))
    sl_pct    = float(body.get('sl_pct', 0.20))
    tp_pct    = float(body.get('tp_pct', 0.30))
    leverage  = int(body.get('leverage', 20))
    strategy  = body.get('strategy', 'general_consensus')
    date_from = body.get('date_from', '') or None
    date_to   = body.get('date_to',   '') or None
    # Nieuwe parameters
    fee_pct          = float(body.get('fee_pct', 0.075))
    cooldown_bars    = int(body.get('cooldown_bars', 3))
    max_hold_bars    = int(body.get('max_hold_bars', 0))
    direction_filter = body.get('direction_filter', 'both')
    trailing_stop    = float(body.get('trailing_stop_pct', 0))
    breakeven_trigger= float(body.get('breakeven_trigger', 0))
    max_consec_losses= int(body.get('max_consec_losses', 0))
    # ROI mode
    use_roi_mode     = bool(body.get('use_roi_mode', False))
    tp_roi           = float(body.get('tp_roi', 30))
    sl_roi           = float(body.get('sl_roi', 12))
    trail_after_roi  = bool(body.get('trail_after_roi', True))
    trail_roi_conf   = float(body.get('trail_roi_conf', 0.80))
    trail_roi_dist   = float(body.get('trail_roi_distance', 0.5))

    # Valideer strategie
    if strategy not in STRATEGIES:
        strategy = 'general_consensus'

    try:
        url = 'https://api.gateio.ws/api/v4/futures/usdt/candlesticks'
        params = {'contract': symbol, 'interval': interval, 'limit': limit}
        resp = req.get(url, params=params, timeout=20)
        resp.raise_for_status()
        raw = resp.json()
        candles = normalize_candles(raw) if raw else []
    except Exception as e:
        logger.error(f'Candle fetch fout: {e}')
        return jsonify({'error': f'Kan geen data ophalen van Gate.io: {str(e)}'}), 400

    if not candles:
        return jsonify({'error': 'Geen candle data beschikbaar'}), 400

    bt = Backtester(
        initial_balance=balance,
        risk_per_trade=risk_pct,
        stop_loss_pct=sl_pct,
        take_profit_pct=tp_pct,
        max_leverage=leverage,
        strategy=strategy,
        use_atr_sl=True,
        interval=interval,
        fee_pct=fee_pct,
        cooldown_bars=cooldown_bars,
        max_hold_bars=max_hold_bars,
        direction_filter=direction_filter,
        trailing_stop_pct=trailing_stop,
        breakeven_trigger=breakeven_trigger,
        max_consec_losses=max_consec_losses,
        use_roi_mode=use_roi_mode,
        tp_roi=tp_roi,
        sl_roi=sl_roi,
        trail_after_roi=trail_after_roi,
        trail_roi_conf=trail_roi_conf,
        trail_roi_distance=trail_roi_dist,
    )
    result = bt.run(candles, symbol, date_from=date_from, date_to=date_to)

    trades_json = [
        {
            'direction':   t.direction,
            'entry_price': t.entry_price,
            'entry_ts':    t.entry_ts,
            'exit_price':  t.exit_price,
            'exit_ts':     t.exit_ts,
            'pnl':         round(t.pnl, 4),
            'pnl_pct':     round(t.pnl_pct, 2),
            'exit_reason': t.exit_reason,
            'leverage':    t.leverage,
            'contracts':   t.contracts,
            'sl_pct':      round(abs(t.stop_loss - t.entry_price) / t.entry_price * 100, 1) if t.entry_price else 0,
            'tp_pct':      round(abs(t.take_profit - t.entry_price) / t.entry_price * 100, 1) if t.entry_price else 0,
            'confidence':  round(t.confidence, 2),
            'roi_trailing': t.roi_trailing,
        }
        for t in result.trades
    ]

    # Equity curve decimeren voor snellere UI
    eq = result.equity_curve
    ts = result.timestamps
    step = max(1, len(eq) // 300)
    eq_dec = eq[::step]
    ts_dec = ts[::step] if len(ts) >= len(eq) else [''] * len(eq_dec)

    return jsonify({
        'symbol':          result.symbol,
        'strategy':        result.strategy,
        'interval':        interval,
        'atr_multiplier':  bt._amult,
        'period_from':     result.period_from,
        'period_to':       result.period_to,
        'total_trades':    result.total_trades,
        'win_rate':        round(result.win_rate, 1),
        'win_count':       result.win_count,
        'loss_count':      result.loss_count,
        'total_pnl':       round(result.total_pnl, 4),
        'return_pct':      round(result.return_pct, 2),
        'profit_factor':   round(result.profit_factor, 2) if result.profit_factor != float('inf') else 999,
        'max_drawdown':    round(result.max_drawdown, 2),
        'sharpe_ratio':    round(result.sharpe_ratio, 2),
        'calmar_ratio':    result.calmar_ratio,
        'initial_balance': result.initial_balance,
        'final_balance':   round(result.final_balance, 4),
        'avg_win':         round(result.avg_win, 4),
        'avg_loss':        round(result.avg_loss, 4),
        'avg_trade_pct':   round(result.avg_trade_pct, 2),
        'best_trade':      round(result.best_trade, 4),
        'worst_trade':     round(result.worst_trade, 4),
        'expectancy':      round(result.expectancy, 4),
        'avg_duration':    round(result.avg_duration, 1),
        'max_consec_losses': result.max_consecutive_losses,
        'max_consec_wins':   result.max_consecutive_wins,
        'total_fees':      round(result.total_fees, 4),
        'pnl_after_fees':  result.pnl_after_fees,
        'long_stats':      result.long_stats,
        'short_stats':     result.short_stats,
        'equity_curve':    eq_dec,
        'equity_ts':       ts_dec,
        'trades':          trades_json,
        'candle_count':    len(candles),
    })


@app.route('/api/balance')
def get_balance_overview():
    """Volledige balans breakdown: beschikbaar, in margin, onrealized PnL, totaal."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        async def _fetch():
            client = get_client()
            acc  = await client.get_account()
            pos  = await client.get_positions()
            await client.close()
            if not acc:
                return None
            available   = float(acc.get('available', 0))
            total_eq    = float(acc.get('total', available))
            order_margin= float(acc.get('order_margin', 0))
            pos_margin  = float(acc.get('position_margin', 0))
            unrealised  = sum(float(p.get('unrealised_pnl', 0)) for p in (pos or []) if int(p.get('size', 0)) != 0)
            open_pos    = [p for p in (pos or []) if int(p.get('size', 0)) != 0]
            return {
                'available':    round(available, 4),
                'total':        round(total_eq, 4),
                'in_margin':    round(pos_margin + order_margin, 4),
                'unrealised':   round(unrealised, 4),
                'open_count':   len(open_pos),
                'positions':    [{
                    'contract':       p.get('contract', ''),
                    'direction':      'long' if int(p.get('size', 0)) > 0 else 'short',
                    'size':           abs(int(p.get('size', 0))),
                    'entry_price':    float(p.get('entry_price', 0)),
                    'mark_price':     float(p.get('mark_price', 0)),
                    'margin':         float(p.get('margin', 0)),
                    'leverage':       int(p.get('leverage', 1)),
                    'unrealised_pnl': round(float(p.get('unrealised_pnl', 0)), 4),
                } for p in open_pos]
            }
        data = loop.run_until_complete(_fetch())
        if not data:
            return jsonify({'error': 'Kan account niet ophalen'}), 500
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        loop.close()


# --- Trade Memory ------------------------------------------------------------

@app.route('/api/memory/stats')
def memory_stats():
    if not MEMORY_OK or not _evaluator:
        return jsonify({'enabled': False})
    try:
        stats = _evaluator.get_stats_summary()
        report = _evaluator.full_report(SYMBOLS)
        return jsonify({
            'enabled':       True,
            'stats':         stats,
            'evaluations':   report['symbols'],
        })
    except Exception as e:
        return jsonify({'enabled': False, 'error': str(e)}), 500

@app.route('/api/memory/trades')
def memory_trades():
    if not MEMORY_OK or not _evaluator:
        return jsonify({'trades': []})
    symbol = request.args.get('symbol')
    limit  = int(request.args.get('limit', 50))
    try:
        trades = _evaluator.get_trade_history(symbol=symbol, limit=limit)
        # Verwijder grote snapshot JSON uit lijst voor snelheid
        for t in trades:
            if 'snapshot' in t and isinstance(t['snapshot'], str):
                import json as _json
                try: t['snapshot'] = _json.loads(t['snapshot'])
                except: pass
        return jsonify({'trades': trades})
    except Exception as e:
        return jsonify({'trades': [], 'error': str(e)}), 500

@app.route('/api/memory/evaluations')
def memory_evaluations():
    if not MEMORY_OK or not _evaluator:
        return jsonify({'evaluations': []})
    symbol = request.args.get('symbol')
    try:
        evals = _evaluator.get_evaluation_history(symbol=symbol, limit=30)
        return jsonify({'evaluations': evals})
    except Exception as e:
        return jsonify({'evaluations': [], 'error': str(e)}), 500


# --- Diagnostics -------------------------------------------------------------

@app.route('/api/diag')
def diagnostics():
    """Test API verbinding en geef exacte foutmeldingen terug."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        async def _diag():
            client = get_client()
            results = {}

            # Test 1: account ophalen
            acc = await client.get_account()
            if acc:
                results['account'] = {
                    'ok': True,
                    'available': acc.get('available', '?'),
                    'total':     acc.get('total',     '?'),
                }
            else:
                results['account'] = {'ok': False, 'error': client.last_error}

            # Test 2: BTC ticker (geen auth)
            ticker = await client.get_ticker('BTC_USDT')
            results['ticker'] = {'ok': bool(ticker), 'price': ticker.get('last') if ticker else None}

            # Test 3: leverage instellen
            if results['account']['ok']:
                lev = await client.set_leverage('BTC_USDT', 5)
                results['leverage'] = {'ok': lev, 'error': client.last_error if not lev else ''}

            await client.close()
            return results

        data = loop.run_until_complete(_diag())
        return jsonify({'status': 'ok', 'tests': data})
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 500
    finally:
        loop.close()


# --- Manual Trading ----------------------------------------------------------

@app.route('/api/manual/signal/<symbol>')
def get_signal(symbol):
    """Bereken AI signaal voor een symbool op basis van recente candles."""
    import requests as req
    try:
        url = 'https://api.gateio.ws/api/v4/futures/usdt/candlesticks'
        r = req.get(url, params={'contract': symbol, 'interval': '15m', 'limit': 150}, timeout=15)
        raw = normalize_candles(r.json())
        if len(raw) < 60:
            return jsonify({'signal': 'none', 'confidence': 0, 'reason': 'Onvoldoende data'})
    except Exception as e:
        return jsonify({'signal': 'none', 'confidence': 0, 'reason': str(e)}), 500

    from indicators import ema, rsi, macd, bollinger_bands, stochastic, vwap, calculate_volatility_score
    closes  = [float(c['c']) for c in raw]
    highs   = [float(c['h']) for c in raw]
    lows    = [float(c['l']) for c in raw]
    volumes = [float(c['v']) for c in raw]

    ema9   = ema(closes, 9)
    ema21  = ema(closes, 21)
    ema50  = ema(closes, 50)
    rsi_v  = rsi(closes, 14)
    ml, sl2, hist = macd(closes)
    bb_u, bb_m, bb_l = bollinger_bands(closes, 20)
    sk, sd = stochastic(highs, lows, closes, 14, 3)
    vw     = vwap(highs, lows, closes, volumes)

    if not all([ema9, ema21, ema50, rsi_v, sl2, bb_u, sk, sd]):
        return jsonify({'signal': 'none', 'confidence': 0, 'reason': 'Indicatoren niet beschikbaar'})

    price = closes[-1]
    reasons_long, reasons_short = [], []

    if ema9[-1] > ema21[-1] > ema50[-1]:
        reasons_long.append('EMA trend opwaarts (9>21>50)')
    elif ema9[-1] < ema21[-1] < ema50[-1]:
        reasons_short.append('EMA trend neerwaarts (9<21<50)')

    if len(ema9)>1 and ema9[-2]<=ema21[-2] and ema9[-1]>ema21[-1]:
        reasons_long.append('EMA gouden kruis')
    elif len(ema9)>1 and ema9[-2]>=ema21[-2] and ema9[-1]<ema21[-1]:
        reasons_short.append('EMA doodskruis')

    cur_rsi = rsi_v[-1]
    if 45 < cur_rsi < 65:
        reasons_long.append(f'RSI neutraal-bullish ({cur_rsi:.0f})')
    elif 35 < cur_rsi < 55:
        reasons_short.append(f'RSI neutraal-bearish ({cur_rsi:.0f})')
    if cur_rsi > 70:
        reasons_short.append(f'RSI overkocht ({cur_rsi:.0f})')
    elif cur_rsi < 30:
        reasons_long.append(f'RSI oververkocht ({cur_rsi:.0f})')

    if len(ml)>1 and ml[-2]<=sl2[-2] and ml[-1]>sl2[-1]:
        reasons_long.append('MACD kruist omhoog')
    elif len(ml)>1 and ml[-2]>=sl2[-2] and ml[-1]<sl2[-1]:
        reasons_short.append('MACD kruist omlaag')

    if hist[-1] > 0:
        reasons_long.append('MACD histogram positief')
    else:
        reasons_short.append('MACD histogram negatief')

    if price > vw[-1]:
        reasons_long.append('Prijs boven VWAP')
    else:
        reasons_short.append('Prijs onder VWAP')

    if sk[-1] < 25 and len(sk)>1 and sk[-1] > sk[-2]:
        reasons_long.append(f'Stochastic oversold keerpunt ({sk[-1]:.0f})')
    elif sk[-1] > 75 and len(sk)>1 and sk[-1] < sk[-2]:
        reasons_short.append(f'Stochastic overbought keerpunt ({sk[-1]:.0f})')

    if price > bb_m[-1]:
        reasons_long.append('Prijs boven Bollinger middenlijn')
    else:
        reasons_short.append('Prijs onder Bollinger middenlijn')

    vol = calculate_volatility_score(closes)
    total = len(reasons_long) + len(reasons_short)
    if total == 0:
        return jsonify({'signal': 'none', 'confidence': 0, 'reason': 'Geen duidelijk signaal', 'rsi': round(cur_rsi,1), 'price': price})

    lc = len(reasons_long) / total
    sc = len(reasons_short) / total

    if lc > sc and lc >= 0.55:
        return jsonify({'signal': 'long', 'confidence': round(lc*100), 'reasons': reasons_long, 'counter': reasons_short, 'rsi': round(cur_rsi,1), 'price': price, 'volatility': round(vol,2)})
    elif sc > lc and sc >= 0.55:
        return jsonify({'signal': 'short', 'confidence': round(sc*100), 'reasons': reasons_short, 'counter': reasons_long, 'rsi': round(cur_rsi,1), 'price': price, 'volatility': round(vol,2)})
    return jsonify({'signal': 'none', 'confidence': round(max(lc,sc)*100), 'reasons': reasons_long, 'counter': reasons_short, 'rsi': round(cur_rsi,1), 'price': price, 'volatility': round(vol,2)})


@app.route('/api/manual/trade', methods=['POST'])
def manual_trade():
    body = request.json or {}
    symbol    = body.get('symbol', 'BTC_USDT')
    direction = body.get('direction', 'long')   # 'long' of 'short'
    size_usd  = float(body.get('size_usd', 10)) # USDT bedrag
    leverage  = int(body.get('leverage', 5))
    sl_pct    = float(body.get('sl_pct', 1.2)) / 100
    tp_pct    = float(body.get('tp_pct', 3)) / 100

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        async def _trade():
            client = get_client()

            # 1. Leverage instellen VOOR de order
            lev_ok = await client.set_leverage(symbol, leverage)
            if not lev_ok:
                logger.warning(f"Leverage instellen mislukt, doorgaan met standaard leverage")

            # 2. Huidige prijs ophalen
            ticker = await client.get_ticker(symbol)
            if not ticker:
                await client.close()
                return {'ok': False, 'error': 'Kan prijs niet ophalen van Gate.io'}
            price = float(ticker.get('last', 0))
            if price <= 0:
                await client.close()
                return {'ok': False, 'error': 'Ongeldige prijs ontvangen'}

            # 3. Aantal contracts berekenen
            # Gate.io USDT perpetuals: 1 contract = 1 USD notional voor alle paren
            # Margin = contracts / leverage
            # Contracts = size_usd (margin) * leverage
            contracts = max(1, int(size_usd * leverage))

            is_long  = direction == 'long'
            order_size = contracts if is_long else -contracts

            # 4. Marktorder plaatsen
            result = await client.place_order(symbol, order_size)
            if not result:
                # Haal de exacte fout op van de laatste API call
                err = client.last_error or 'Order mislukt'
                await client.close()
                return {'ok': False, 'error': f'Gate.io: {err}'}

            # 5. SL en TP berekenen
            sl_price = round(price * (1 - sl_pct) if is_long else price * (1 + sl_pct), 8)
            tp_price = round(price * (1 + tp_pct) if is_long else price * (1 - tp_pct), 8)

            # ROI berekening: ROI% = prijs% × leverage
            sl_roi = round(sl_pct * 100 * leverage, 1)
            tp_roi = round(tp_pct * 100 * leverage, 1)

            # 6. Bestaande SL/TP orders annuleren voor dit contract
            await client.cancel_all_price_orders(symbol)

            # 7. Stop Loss plaatsen op exchange
            sl_result = await client.place_stop_loss(symbol, is_long, sl_price, contracts)
            tp_result = await client.place_take_profit(symbol, is_long, tp_price, contracts)

            await client.close()

            return {
                'ok':        True,
                'order_id':  result.get('id'),
                'price':     price,
                'contracts': contracts,
                'leverage':  leverage,
                'sl':        sl_price,
                'tp':        tp_price,
                'sl_roi':    sl_roi,
                'tp_roi':    tp_roi,
                'sl_placed': bool(sl_result),
                'tp_placed': bool(tp_result),
                'direction': direction,
                'symbol':    symbol,
            }
        res = loop.run_until_complete(_trade())
        return jsonify(res)
    except Exception as e:
        logger.error(f'Manual trade fout: {e}')
        return jsonify({'ok': False, 'error': str(e)}), 500
    finally:
        loop.close()


@app.route('/api/manual/close', methods=['POST'])
def manual_close():
    body   = request.json or {}
    symbol = body.get('symbol', 'BTC_USDT')
    loop   = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        async def _close():
            client = get_client()
            pos = await client.get_position(symbol)
            if not pos or int(pos.get('size', 0)) == 0:
                await client.close()
                return {'ok': False, 'error': 'Geen open positie gevonden'}
            size = int(pos['size'])
            pnl  = float(pos.get('unrealised_pnl', 0))

            # Annuleer eerst alle SL/TP orders
            existing_orders = await client.get_price_orders(symbol)
            cancelled_count = len(existing_orders) if existing_orders else 0
            await client.cancel_all_price_orders(symbol)

            # Sluit positie met market order (reduce_only)
            result = await client.place_order(symbol, -size, reduce_only=True)
            await client.close()
            return {
                'ok': bool(result),
                'pnl': pnl,
                'sl_tp_cancelled': cancelled_count,
            }
        res = loop.run_until_complete(_close())
        return jsonify(res)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
    finally:
        loop.close()


@app.route('/api/manual/position/<symbol>')
def get_position(symbol):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        async def _fetch():
            client = get_client()
            pos    = await client.get_position(symbol)
            ticker = await client.get_ticker(symbol)
            # Haal actieve SL/TP trigger orders op van exchange
            price_orders = await client.get_price_orders(symbol)
            await client.close()

            # Parse SL/TP uit exchange orders
            exchange_sl = None
            exchange_tp = None
            for order in (price_orders or []):
                trigger = order.get('trigger', {})
                initial = order.get('initial', {})
                trigger_price = float(trigger.get('price', 0))
                is_reduce = initial.get('reduce_only', False)
                rule = trigger.get('rule', 0)
                if is_reduce and trigger_price > 0:
                    size = int(pos.get('size', 0)) if pos else 0
                    is_long = size > 0
                    # SL: rule 2 voor long (prijs daalt tot), rule 1 voor short (prijs stijgt tot)
                    # TP: rule 1 voor long (prijs stijgt tot), rule 2 voor short (prijs daalt tot)
                    if is_long:
                        if rule == 2: exchange_sl = trigger_price
                        elif rule == 1: exchange_tp = trigger_price
                    else:
                        if rule == 1: exchange_sl = trigger_price
                        elif rule == 2: exchange_tp = trigger_price

            return {
                'position': pos,
                'ticker': ticker,
                'exchange_sl': exchange_sl,
                'exchange_tp': exchange_tp,
                'price_orders_count': len(price_orders or []),
            }
        data = loop.run_until_complete(_fetch())
        return jsonify(data)
    except Exception as e:
        return jsonify({'position': None, 'ticker': None, 'exchange_sl': None, 'exchange_tp': None}), 500
    finally:
        loop.close()


# --- Frontend ----------------------------------------------------------------

HTML_PAGE = r"""
<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Jef Bot</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}

:root {
  --bg:         #f5f5f7;
  --bg1:        #ffffff;
  --bg2:        #ffffff;
  --bg3:        #f5f5f7;
  --bg4:        #e8e8ed;
  --border:     rgba(0,0,0,0.08);
  --border2:    rgba(0,0,0,0.14);
  --text:       #1d1d1f;
  --text2:      #515154;
  --text3:      #86868b;
  --green:      #34c759;
  --green-bg:   rgba(52,199,89,0.10);
  --green-bd:   rgba(52,199,89,0.22);
  --red:        #ff3b30;
  --red-bg:     rgba(255,59,48,0.08);
  --red-bd:     rgba(255,59,48,0.20);
  --blue:       #007aff;
  --blue-bg:    rgba(0,122,255,0.08);
  --blue-bd:    rgba(0,122,255,0.20);
  --yellow:     #ff9f0a;
  --yellow-bg:  rgba(255,159,10,0.08);
  --radius:     16px;
  --radius-sm:  12px;
  --radius-xs:  10px;
  --font:       'Inter', -apple-system, sans-serif;
  --mono:       'SF Mono', ui-monospace, monospace;
  --shadow-sm:  0 1px 3px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04);
  --shadow:     0 4px 16px rgba(0,0,0,0.08), 0 1px 4px rgba(0,0,0,0.04);
  --blur:       saturate(180%) blur(20px);
}

body{background:var(--bg);color:var(--text);font-family:var(--font);font-size:14px;line-height:1.5;min-height:100vh}
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--bg4);border-radius:2px}

.app{display:flex;flex-direction:column;min-height:100vh}

/* ── Titlebar ── */
.titlebar{
  display:flex;align-items:center;justify-content:space-between;
  padding:0 28px;height:52px;
  background:rgba(255,255,255,0.85);
  backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur);
  border-bottom:1px solid var(--border);
  position:sticky;top:0;z-index:200;
}
.titlebar-left{display:flex;align-items:center;gap:20px}
.app-name{font-size:15px;font-weight:600;letter-spacing:-.4px;color:var(--text)}
.app-name span{color:var(--text3);font-weight:400}

.live-badge{
  display:flex;align-items:center;gap:5px;
  font-size:11px;font-weight:500;
  color:var(--green);padding:3px 10px;
  background:var(--green-bg);border:1px solid var(--green-bd);
  border-radius:20px
}
.live-dot{width:5px;height:5px;border-radius:50%;background:currentColor;animation:pulse 2s infinite}
.live-badge.offline{color:var(--text3);background:transparent;border-color:var(--border)}
.live-badge.offline .live-dot{animation:none}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.4;transform:scale(.8)}}

.titlebar-right{display:flex;align-items:center;gap:14px}
.balance-chip{font-size:13px;font-weight:500;color:var(--text2)}
.balance-chip strong{color:var(--text);font-weight:600}

/* ── Nav ── */
.nav{
  display:flex;gap:2px;padding:8px 20px;
  background:rgba(255,255,255,0.8);
  backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur);
  border-bottom:1px solid var(--border);overflow-x:auto
}
.nav-btn{
  background:none;border:none;cursor:pointer;
  color:var(--text3);font-family:var(--font);
  font-size:13px;font-weight:500;
  padding:6px 14px;border-radius:8px;
  transition:all .15s;white-space:nowrap;letter-spacing:-.1px
}
.nav-btn:hover{color:var(--text2);background:rgba(0,0,0,0.05)}
.nav-btn.active{color:var(--text);background:rgba(0,0,0,0.07)}

/* ── Main ── */
main{flex:1;padding:20px 24px;max-width:1400px;width:100%;margin:0 auto}
.panel{display:none}
.panel.active{display:block;animation:fadeIn .18s ease}
@keyframes fadeIn{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:translateY(0)}}

/* ── Grid ── */
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
.grid-3{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:12px}
.grid-4{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:12px}
@media(max-width:1100px){.grid-4{grid-template-columns:repeat(2,1fr)}}
@media(max-width:800px){.grid-2,.grid-3{grid-template-columns:1fr}.grid-4{grid-template-columns:repeat(2,1fr)}}

/* ── Cards ── */
.card{
  background:var(--bg1);
  border:1px solid var(--border);
  border-radius:var(--radius);
  padding:18px 20px;
  box-shadow:var(--shadow-sm);
  transition:box-shadow .2s
}
.card:hover{box-shadow:var(--shadow)}
.card-sm{padding:14px 16px}
.card-title{
  font-size:11px;font-weight:600;letter-spacing:.04em;
  text-transform:uppercase;color:var(--text3);margin-bottom:10px
}

/* ── Stat ── */
.stat-val{font-size:28px;font-weight:300;letter-spacing:-.5px;line-height:1;margin-bottom:3px;color:var(--text)}
.stat-label{font-size:12px;color:var(--text3)}
.up{color:var(--green)}
.down{color:var(--red)}
.neu{color:var(--blue)}
.warn{color:var(--yellow)}

/* ── Ticker ── */
.ticker-strip{
  display:flex;gap:0;overflow-x:auto;
  background:var(--bg1);border:1px solid var(--border);
  border-radius:var(--radius);margin-bottom:12px;
  box-shadow:var(--shadow-sm)
}
.ticker-item{
  display:flex;flex-direction:column;gap:2px;
  padding:12px 18px;border-right:1px solid var(--border);
  min-width:115px;flex-shrink:0
}
.ticker-item:last-child{border-right:none}
.ticker-sym{font-size:10px;font-weight:600;color:var(--text3);letter-spacing:.06em;text-transform:uppercase}
.ticker-price{font-size:15px;font-weight:500;letter-spacing:-.3px;color:var(--text)}
.ticker-chg{font-size:11px;font-weight:500}

/* ── Buttons ── */
.btn{
  display:inline-flex;align-items:center;justify-content:center;gap:6px;
  padding:9px 18px;border-radius:var(--radius-xs);
  font-family:var(--font);font-size:13px;font-weight:500;
  cursor:pointer;transition:all .15s;border:1px solid transparent;
  letter-spacing:-.1px;white-space:nowrap
}
.btn:disabled{opacity:.35;cursor:not-allowed}
.btn-primary{background:var(--blue);color:#fff;border-color:var(--blue)}
.btn-primary:hover:not(:disabled){background:#0071f3}
.btn-long{background:var(--green-bg);color:var(--green);border-color:var(--green-bd)}
.btn-long:hover:not(:disabled){background:rgba(52,199,89,.18)}
.btn-short{background:var(--red-bg);color:var(--red);border-color:var(--red-bd)}
.btn-short:hover:not(:disabled){background:rgba(255,59,48,.15)}
.btn-ghost{background:var(--bg3);color:var(--text2);border-color:var(--border)}
.btn-ghost:hover:not(:disabled){background:var(--bg4);color:var(--text)}
.btn-danger{background:var(--red-bg);color:var(--red);border-color:var(--red-bd)}
.btn-danger:hover:not(:disabled){background:rgba(255,59,48,.15)}
.btn-lg{padding:12px 24px;font-size:15px;border-radius:var(--radius-sm)}
.btn-sm{padding:5px 12px;font-size:12px}
.btn-icon{width:32px;height:32px;padding:0;border-radius:8px;font-size:15px}

.ctrl-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px}

/* ── Chart ── */
.chart-wrap{position:relative;height:200px}
.chart-wrap-lg{position:relative;height:280px}

/* ── Inputs ── */
.field{display:flex;flex-direction:column;gap:5px}
.field label{font-size:11px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--text3)}
.field input,.field select{
  background:var(--bg3);border:1px solid var(--border);
  color:var(--text);padding:9px 12px;
  font-family:var(--font);font-size:13px;
  border-radius:var(--radius-xs);outline:none;
  transition:border-color .15s,box-shadow .15s;appearance:none
}
.field input:focus,.field select:focus{border-color:var(--blue);box-shadow:0 0 0 3px rgba(0,122,255,.12)}
.field select{background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%2386868b'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 10px center;padding-right:28px}
.form-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:14px}
@media(max-width:700px){.form-grid{grid-template-columns:1fr 1fr}}

/* ── Table ── */
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:8px 14px;font-size:11px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--text3);border-bottom:1px solid var(--border)}
td{padding:10px 14px;border-bottom:1px solid rgba(0,0,0,0.04);color:var(--text2)}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(0,0,0,0.02)}

/* ── Badges ── */
.badge{display:inline-flex;align-items:center;padding:2px 9px;border-radius:20px;font-size:11px;font-weight:600;letter-spacing:.02em;text-transform:uppercase}
.badge-long{background:var(--green-bg);color:var(--green);border:1px solid var(--green-bd)}
.badge-short{background:var(--red-bg);color:var(--red);border:1px solid var(--red-bd)}
.badge-tp{background:var(--green-bg);color:var(--green)}
.badge-sl{background:var(--red-bg);color:var(--red)}
.badge-end{background:var(--bg3);color:var(--text3)}

/* ── Section title ── */
.section-title{font-size:20px;font-weight:600;letter-spacing:-.4px;color:var(--text);margin-bottom:16px}

/* ── Loading / empty ── */
.loading{display:flex;align-items:center;justify-content:center;gap:10px;color:var(--text3);font-size:13px;padding:40px}
.spinner{width:18px;height:18px;border:2px solid var(--border);border-top-color:var(--blue);border-radius:50%;animation:spin .6s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.empty{text-align:center;padding:48px;color:var(--text3);font-size:13px}

/* ── Log box ── */
.log-box{
  background:var(--bg3);border:1px solid var(--border);
  border-radius:var(--radius-sm);padding:14px;
  font-family:var(--mono);font-size:11.5px;
  line-height:1.8;min-height:80px;max-height:200px;
  overflow-y:auto;color:var(--text3)
}
.log-info{color:var(--text2)}
.log-ok{color:var(--green)}
.log-warn{color:var(--yellow)}
.log-error{color:var(--red)}

/* ── Signal hero ── */
.signal-hero{
  display:flex;flex-direction:column;align-items:center;
  justify-content:center;padding:24px 20px;border-radius:var(--radius);
  border:1px solid var(--border);margin-bottom:12px;text-align:center;
  background:var(--bg3);transition:all .3s
}
.signal-hero.long-sig{background:var(--green-bg);border-color:var(--green-bd)}
.signal-hero.short-sig{background:var(--red-bg);border-color:var(--red-bd)}
.signal-dir{font-size:34px;font-weight:300;letter-spacing:-1px;margin-bottom:4px}
.signal-conf{font-size:12px;color:var(--text3)}

/* ── Progress ── */
.progress-wrap{margin:14px 0}
.progress-labels{display:flex;justify-content:space-between;font-size:11px;color:var(--text3);margin-bottom:5px}
.progress-track{height:4px;background:var(--bg4);border-radius:2px;overflow:hidden}
.progress-fill{height:100%;border-radius:2px;transition:width .6s ease,background .3s}

/* ── Sep ── */
.sep{height:1px;background:var(--border);margin:14px 0}

/* ── Toast ── */
#toast{
  position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(80px);
  background:var(--bg1);border:1px solid var(--border2);
  border-radius:var(--radius-sm);padding:11px 20px;
  font-size:13px;font-weight:500;color:var(--text);
  box-shadow:var(--shadow);z-index:9999;
  transition:transform .3s cubic-bezier(.34,1.56,.64,1);white-space:nowrap
}
#toast.show{transform:translateX(-50%) translateY(0)}
#toast.ok{border-color:var(--green-bd);color:var(--green)}
#toast.err{border-color:var(--red-bd);color:var(--red)}

/* ── Date input ── */
input[type=date]{color-scheme:light}
</style>
</head>
<body>
<div class="app">

<!-- TITLEBAR -->
<header class="titlebar">
  <div class="titlebar-left">
    <span class="app-name">Jef<span>Bot</span></span>
    <div id="bot-pill" class="live-badge offline">
      <span class="live-dot"></span>
      <span id="pill-text">Offline</span>
    </div>
  </div>
  <div class="titlebar-right">
    <span class="balance-chip" id="balance-chip">Balans: <strong>—</strong></span>
    <span style="font-size:12px;color:var(--text3)" id="last-cycle-label"></span>
  </div>
</header>

<!-- NAV -->
<nav class="nav">
  <button class="nav-btn active" onclick="showTab('dashboard',this)">Dashboard</button>
  <button class="nav-btn" onclick="showTab('manual',this)">Manueel Traden</button>
  <button class="nav-btn" onclick="showTab('backtest',this)">Backtesting</button>
  <button class="nav-btn" onclick="showTab('positions',this)">Posities</button>
  <button class="nav-btn" onclick="showTab('settings',this)">Instellingen</button>
  <button class="nav-btn" onclick="showTab('memory',this)">Leersysteem</button>
</nav>

<main>

<!-- ══════════ DASHBOARD ══════════ -->
<div id="panel-dashboard" class="panel active">

  <div class="ctrl-row">
    <button class="btn btn-long btn-lg" id="btn-start" onclick="startBot()">Start Bot</button>
    <button class="btn btn-danger btn-lg" id="btn-stop" onclick="stopBot()" disabled>Stop Bot</button>
    <button class="btn btn-ghost btn-sm btn-icon" onclick="loadBalanceOverview()" title="Vernieuwen" style="margin-left:auto;">↻</button>
  </div>

  <!-- Bot Status Indicator -->
  <div id="bot-health-bar" style="display:flex;align-items:center;gap:12px;padding:10px 16px;border-radius:var(--radius-xs);margin-bottom:12px;background:var(--bg3);border:1px solid var(--border);font-size:12px">
    <div id="health-dot" style="width:10px;height:10px;border-radius:50%;background:var(--text3);flex-shrink:0"></div>
    <div id="health-label" style="font-weight:600;color:var(--text3);min-width:70px">Offline</div>
    <div style="display:flex;gap:16px;flex-wrap:wrap;color:var(--text2)">
      <span>Uptime: <b id="health-uptime">—</b></span>
      <span>Cyclus: <b id="health-cycle">—</b></span>
      <span>Laatste trade: <b id="health-trade">—</b></span>
      <span>Fouten: <b id="health-errors">0</b></span>
      <span id="health-stalled" style="display:none;color:var(--yellow);font-weight:600">⚠ Bot reageert niet</span>
      <span id="health-last-error" style="display:none;color:var(--red);font-size:11px"></span>
    </div>
  </div>

  <!-- Balans cards -->
  <div class="grid-4">
    <div class="card">
      <div class="card-title">Totale balans</div>
      <div class="stat-val neu" id="bal-total">—</div>
      <div class="stat-label">USDT equity</div>
    </div>
    <div class="card">
      <div class="card-title">Beschikbaar</div>
      <div class="stat-val up" id="bal-available">—</div>
      <div class="stat-label">Vrij voor trading</div>
    </div>
    <div class="card">
      <div class="card-title">In margin</div>
      <div class="stat-val warn" id="bal-margin">—</div>
      <div class="stat-label">Geblokkeerd</div>
    </div>
    <div class="card">
      <div class="card-title">Unrealized PnL</div>
      <div class="stat-val" id="bal-unrealised">—</div>
      <div class="stat-label">Open posities</div>
    </div>
  </div>

  <!-- Dag stats strip -->
  <div class="card card-sm" style="display:flex;gap:0;padding:0;margin-bottom:12px;overflow:hidden">
    <div style="flex:1;padding:12px 18px;border-right:1px solid var(--border)">
      <div class="card-title" style="margin-bottom:3px">Dag PnL</div>
      <div class="stat-val" id="stat-pnl" style="font-size:20px">—</div>
    </div>
    <div style="flex:1;padding:12px 18px;border-right:1px solid var(--border)">
      <div class="card-title" style="margin-bottom:3px">Trades vandaag</div>
      <div class="stat-val neu" id="stat-trades" style="font-size:20px">0</div>
    </div>
    <div style="flex:1;padding:12px 18px;border-right:1px solid var(--border)">
      <div class="card-title" style="margin-bottom:3px">Open posities</div>
      <div class="stat-val warn" id="stat-pos" style="font-size:20px">0</div>
    </div>
    <div style="flex:1;padding:12px 18px">
      <div class="card-title" style="margin-bottom:3px">Bot status</div>
      <div id="dash-status" style="font-size:13px;font-weight:500;color:var(--text3);margin-top:3px">Offline</div>
    </div>
  </div>

  <!-- Open posities tabel -->
  <div id="dash-positions-card" class="card" style="display:none;margin-bottom:12px">
    <div class="card-title">Open posities</div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Contract</th><th>Richting</th><th>Leverage</th><th>Entry</th><th>Mark</th><th>Margin</th><th>PnL</th><th>PnL %</th></tr></thead>
        <tbody id="dash-pos-body"></tbody>
      </table>
    </div>
  </div>

  <!-- Ticker strip -->
  <div class="ticker-strip" id="ticker-strip">
    <div class="loading"><div class="spinner"></div>Prijzen laden...</div>
  </div>

  <!-- Charts -->
  <div class="grid-2">
    <div class="card">
      <div class="card-title">BTC / USDT · 1h</div>
      <div class="chart-wrap"><canvas id="chart-btc"></canvas></div>
    </div>
    <div class="card">
      <div class="card-title">ETH / USDT · 1h</div>
      <div class="chart-wrap"><canvas id="chart-eth"></canvas></div>
    </div>
  </div>

  <!-- Log -->
  <div class="card">
    <div class="card-title">Activiteit</div>
    <div class="log-box" id="dash-log">
      <span class="log-info">Welkom bij Jef Bot. Start de bot of voer een manuele trade uit.</span>
    </div>
  </div>
</div>

<!-- ══════════ MANUEEL TRADEN ══════════ -->
<div id="panel-manual" class="panel">
  <div class="section-title">Manueel Traden</div>
  <div class="grid-2" style="align-items:start">

    <div style="display:flex;flex-direction:column;gap:12px">

      <!-- AI Signaal -->
      <div class="card">
        <div class="card-title">AI Signaal</div>
        <div style="display:flex;gap:8px;margin-bottom:12px">
          <div class="field" style="flex:1">
            <label>Symbool</label>
            <select id="sig-sym" onchange="document.getElementById('trade-sym').value=this.value">
              <option value="BTC_USDT">BTC / USDT</option>
              <option value="ETH_USDT">ETH / USDT</option>
              <option value="XRP_USDT">XRP / USDT</option>
              <option value="FARTCOIN_USDT">FARTCOIN / USDT</option>
              <option value="ADA_USDT">ADA / USDT</option>
            </select>
          </div>
          <div class="field" style="justify-content:flex-end">
            <label>&nbsp;</label>
            <button class="btn btn-primary" onclick="fetchSignal()">Analyseer</button>
          </div>
        </div>
        <div id="sig-result" style="display:none">
          <div class="signal-hero none-sig" id="sig-hero">
            <div class="signal-dir" id="sig-dir">—</div>
            <div class="signal-conf" id="sig-conf">Analyseer een symbool</div>
          </div>
          <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:10px">
            <div class="card card-sm" style="text-align:center;padding:10px;background:var(--bg3)">
              <div style="font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:.05em;margin-bottom:3px">Prijs</div>
              <div id="sig-price" style="font-size:14px;font-weight:500;color:var(--text)"></div>
            </div>
            <div class="card card-sm" style="text-align:center;padding:10px;background:var(--bg3)">
              <div style="font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:.05em;margin-bottom:3px">RSI</div>
              <div id="sig-rsi" style="font-size:14px;font-weight:500"></div>
            </div>
            <div class="card card-sm" style="text-align:center;padding:10px;background:var(--bg3)">
              <div style="font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:.05em;margin-bottom:3px">Volatiliteit</div>
              <div id="sig-vol" style="font-size:14px;font-weight:500;color:var(--yellow)"></div>
            </div>
          </div>
          <div id="sig-reasons" style="font-size:12px;line-height:1.9"></div>
        </div>
        <div id="sig-loading" style="display:none" class="loading"><div class="spinner"></div>Analyseren...</div>
      </div>

      <!-- Order -->
      <div class="card">
        <div class="card-title">Order Plaatsen</div>
        <div style="display:flex;flex-direction:column;gap:10px">
          <div class="field">
            <label>Symbool</label>
            <select id="trade-sym">
              <option value="BTC_USDT">BTC / USDT</option>
              <option value="ETH_USDT">ETH / USDT</option>
              <option value="XRP_USDT">XRP / USDT</option>
              <option value="FARTCOIN_USDT">FARTCOIN / USDT</option>
              <option value="ADA_USDT">ADA / USDT</option>
            </select>
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
            <div class="field"><label>Bedrag (USDT)</label><input type="number" id="trade-size" value="3" min="1" max="50" step="0.5"></div>
            <div class="field"><label>Leverage</label>
              <select id="trade-lev" onchange="updatePreview()">
                <option value="2">2×</option><option value="3">3×</option>
                <option value="5">5×</option><option value="10" selected>10×</option>
                <option value="15">15×</option><option value="20">20×</option>
              </select>
            </div>
          </div>
          <div style="display:flex;align-items:center;gap:8px;margin:2px 0">
            <label style="font-size:11px;color:var(--text3)">Modus:</label>
            <label style="font-size:12px;cursor:pointer"><input type="radio" name="sltp-mode" value="roi" checked onchange="switchSLTPMode()"> ROI %</label>
            <label style="font-size:12px;cursor:pointer"><input type="radio" name="sltp-mode" value="price" onchange="switchSLTPMode()"> Prijs %</label>
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
            <div class="field"><label id="lbl-sl">SL ROI %</label><input type="number" id="trade-sl" value="12" min="0.5" max="100" step="0.5" oninput="updatePreview()"></div>
            <div class="field"><label id="lbl-tp">TP ROI %</label><input type="number" id="trade-tp" value="30" min="0.5" max="200" step="0.5" oninput="updatePreview()"></div>
          </div>
          <div id="sltp-preview" style="display:none;background:var(--bg3);border:1px solid var(--border);border-radius:var(--radius-xs);padding:10px 14px">
            <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:3px">
              <span style="color:var(--text3)">Stop Loss</span><span id="prev-sl" style="color:var(--red);font-weight:500"></span>
            </div>
            <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:3px">
              <span style="color:var(--text3)">Take Profit</span><span id="prev-tp" style="color:var(--green);font-weight:500"></span>
            </div>
            <div style="display:flex;justify-content:space-between;font-size:11px">
              <span style="color:var(--text3)">Prijs %</span><span id="prev-pricepct" style="color:var(--blue)"></span>
            </div>
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:2px">
            <button class="btn btn-long btn-lg" id="btn-long-order" onclick="placeTrade('long')">▲ Long</button>
            <button class="btn btn-short btn-lg" id="btn-short-order" onclick="placeTrade('short')">▼ Short</button>
          </div>
        </div>
      </div>
    </div>

    <!-- Positie -->
    <div style="display:flex;flex-direction:column;gap:12px">
      <div class="card">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
          <div class="card-title" style="margin:0">Open Positie</div>
          <button class="btn btn-ghost btn-sm btn-icon" onclick="refreshPosition()" title="Vernieuwen">↻</button>
        </div>
        <div id="pos-empty" class="empty" style="padding:24px 0">Geen open positie</div>
        <div id="pos-detail" style="display:none">
          <div style="text-align:center;padding:16px 0;margin-bottom:14px;border-bottom:1px solid var(--border)">
            <div style="font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px">Unrealized PnL</div>
            <div id="pos-pnl" style="font-size:36px;font-weight:300;letter-spacing:-1px"></div>
            <div id="pos-pnl-pct" style="font-size:14px;margin-top:3px"></div>
            <div id="pos-roi" style="font-size:18px;font-weight:500;margin-top:4px"></div>
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px">
            <div><div style="font-size:11px;color:var(--text3);margin-bottom:2px;text-transform:uppercase;letter-spacing:.04em">Richting</div><div id="pos-dir" style="font-size:14px;font-weight:500"></div></div>
            <div><div style="font-size:11px;color:var(--text3);margin-bottom:2px;text-transform:uppercase;letter-spacing:.04em">Leverage</div><div id="pos-lev" style="font-size:14px;font-weight:500;color:var(--blue)"></div></div>
            <div><div style="font-size:11px;color:var(--text3);margin-bottom:2px;text-transform:uppercase;letter-spacing:.04em">Entry</div><div id="pos-entry" style="font-size:14px;font-weight:500"></div></div>
            <div><div style="font-size:11px;color:var(--text3);margin-bottom:2px;text-transform:uppercase;letter-spacing:.04em">Mark prijs</div><div id="pos-mark" style="font-size:14px;font-weight:500;color:var(--blue)"></div></div>
            <div><div style="font-size:11px;color:var(--text3);margin-bottom:2px;text-transform:uppercase;letter-spacing:.04em">Stop Loss</div><div id="pos-sl" style="font-size:14px;font-weight:500;color:var(--red)"></div></div>
            <div><div style="font-size:11px;color:var(--text3);margin-bottom:2px;text-transform:uppercase;letter-spacing:.04em">Take Profit</div><div id="pos-tp" style="font-size:14px;font-weight:500;color:var(--green)"></div></div>
          </div>
          <div class="progress-wrap">
            <div class="progress-labels">
              <span style="color:var(--red)">SL</span>
              <span id="prog-label" style="color:var(--blue)"></span>
              <span style="color:var(--green)">TP</span>
            </div>
            <div class="progress-track"><div id="prog-fill" class="progress-fill" style="width:50%;background:var(--blue)"></div></div>
          </div>
          <div class="sep"></div>
          <button class="btn btn-danger" style="width:100%;padding:11px" onclick="closePosition()">Sluit Positie</button>
        </div>
      </div>
      <div class="card">
        <div class="card-title">Trade Log</div>
        <div class="log-box" id="manual-log"></div>
      </div>
    </div>
  </div>
</div>

<!-- ══════════ BACKTESTING ══════════ -->
<div id="panel-backtest" class="panel">
  <div class="section-title">Backtesting</div>

  <div class="card" style="margin-bottom:12px">
    <div class="card-title">Configuratie</div>
    <div class="form-grid">
      <div class="field"><label>Symbool</label>
        <select id="bt-sym">
          <option value="BTC_USDT">BTC / USDT</option>
          <option value="ETH_USDT">ETH / USDT</option>
          <option value="XRP_USDT">XRP / USDT</option>
          <option value="FARTCOIN_USDT">FARTCOIN / USDT</option>
          <option value="ADA_USDT">ADA / USDT</option>
        </select>
      </div>
      <div class="field"><label>Strategie</label>
        <select id="bt-strat">
          <option value="general_consensus">Algemeen Consensus</option>
          <option value="btc_trend">BTC Trend (EMA + trendsterkte)</option>
          <option value="eth_squeeze">ETH Squeeze Breakout (BB + OBV)</option>
          <option value="xrp_roi">XRP ROI (4H pullback + BB bounce)</option>
          <option value="fartcoin_momentum">FARTCOIN Momentum (trend + dip)</option>
          <option value="fartcoin_bb">FARTCOIN Bollinger Bands (4H)</option>
          <option value="ada_supertrend">ADA Supertrend + OBV</option>
          <option value="rsi_meanrev">RSI Mean-Reversion (alle coins)</option>
          <option value="ema_crossover">EMA 9/21 Crossover (alle coins)</option>
          <option value="bb_bounce">BB Bounce (alle coins)</option>
        </select>
      </div>
      <div class="field"><label>Interval</label>
        <select id="bt-interval">
          <option value="5m">5 minuten</option><option value="15m">15 minuten</option>
          <option value="1h" selected>1 uur</option><option value="4h">4 uur</option>
          <option value="1d">1 dag</option>
        </select>
      </div>
      <div class="field"><label>Candles</label><input type="number" id="bt-limit" value="500" min="100" max="1000"></div>
      <div class="field"><label>Startkapitaal ($)</label><input type="number" id="bt-bal" value="100" min="10"></div>
      <div class="field"><label>Risico per trade %</label><input type="number" id="bt-risk" value="2" min="0.5" max="10" step="0.5"></div>
    </div>

    <!-- Geavanceerde parameters -->
    <details style="margin-top:10px">
      <summary style="cursor:pointer;font-size:12px;color:var(--blue);font-weight:500;padding:6px 0">▸ Geavanceerde parameters</summary>
      <div class="form-grid" style="margin-top:8px">
        <div class="field"><label>Fee per trade %</label><input type="number" id="bt-fee" value="0.075" min="0" max="1" step="0.005"></div>
        <div class="field"><label>Cooldown (bars)</label><input type="number" id="bt-cooldown" value="3" min="0" max="20"></div>
        <div class="field"><label>Max hold (bars, 0=uit)</label><input type="number" id="bt-maxhold" value="0" min="0" max="200"></div>
        <div class="field"><label>Richting</label>
          <select id="bt-direction">
            <option value="both">Long + Short</option>
            <option value="long_only">Alleen Long</option>
            <option value="short_only">Alleen Short</option>
          </select>
        </div>
        <div class="field"><label>Trailing Stop % (0=uit)</label><input type="number" id="bt-trailing" value="0" min="0" max="20" step="0.1"></div>
        <div class="field"><label>Break-even na % (0=uit)</label><input type="number" id="bt-breakeven" value="0" min="0" max="20" step="0.1"></div>
        <div class="field"><label>Max verliesreeks (0=uit)</label><input type="number" id="bt-maxconsec" value="0" min="0" max="20"></div>
      </div>
      <div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--border)">
        <div style="font-size:12px;color:var(--blue);font-weight:500;margin-bottom:8px">ROI Mode (Gate.io stijl)</div>
        <div class="form-grid">
          <div class="field"><label><input type="checkbox" id="bt-roi-mode" style="margin-right:6px">ROI modus aan</label></div>
          <div class="field"><label>TP ROI %</label><input type="number" id="bt-tp-roi" value="30" min="5" max="200" step="5"></div>
          <div class="field"><label>SL ROI %</label><input type="number" id="bt-sl-roi" value="12" min="2" max="100" step="1"></div>
          <div class="field"><label><input type="checkbox" id="bt-roi-trail" checked style="margin-right:6px">Trail na ROI (sterk signaal)</label></div>
          <div class="field"><label>Trail conf. drempel</label><input type="number" id="bt-roi-conf" value="80" min="50" max="100" step="5"></div>
          <div class="field"><label>Trail afstand %</label><input type="number" id="bt-roi-dist" value="0.5" min="0.1" max="5" step="0.1"></div>
        </div>
      </div>
    </details>
    <div style="background:var(--bg3);border:1px solid var(--border);border-radius:var(--radius-xs);padding:12px 14px;margin-bottom:12px">
      <div class="card-title" style="margin-bottom:8px">Periode afbakening (optioneel)</div>
      <div style="display:grid;grid-template-columns:1fr 1fr auto;gap:10px;align-items:end">
        <div class="field"><label>Van datum</label><input type="date" id="bt-from"></div>
        <div class="field"><label>Tot datum</label><input type="date" id="bt-to"></div>
        <div class="field"><label>&nbsp;</label><button class="btn btn-ghost btn-sm" onclick="clearDates()">Wis</button></div>
      </div>
    </div>
    <button class="btn btn-primary" id="btn-bt" onclick="runBacktest()">Run Backtest</button>
  </div>

  <div id="bt-loading" class="loading" style="display:none"><div class="spinner"></div>Backtest berekenen...</div>

  <div id="bt-results" style="display:none">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;flex-wrap:wrap;gap:8px">
      <div>
        <div style="font-size:16px;font-weight:500;color:var(--text)" id="bt-res-title"></div>
        <div style="font-size:12px;color:var(--text3);margin-top:2px" id="bt-res-period"></div>
      </div>
      <button class="btn btn-ghost btn-sm" onclick="exportCSV()">Export CSV</button>
    </div>
    <div class="grid-4" id="bt-stats" style="margin-bottom:12px"></div>
    <div class="grid-2" style="margin-bottom:12px">
      <div class="card"><div class="card-title">Equity Curve</div><div class="chart-wrap-lg"><canvas id="bt-equity"></canvas></div></div>
      <div class="card"><div class="card-title">Win / Loss</div><div class="chart-wrap-lg"><canvas id="bt-pie"></canvas></div></div>
    </div>
    <div class="card">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;flex-wrap:wrap;gap:8px">
        <div class="card-title" style="margin:0">Alle Trades</div>
        <div style="display:flex;gap:8px;align-items:center">
          <select id="bt-filter" onchange="filterTrades()" style="background:var(--bg3);border:1px solid var(--border);color:var(--text2);padding:5px 10px;font-size:12px;border-radius:var(--radius-xs);font-family:var(--font)">
            <option value="all">Alle trades</option><option value="win">Alleen winsten</option>
            <option value="loss">Alleen verliezen</option><option value="long">Alleen long</option>
            <option value="short">Alleen short</option><option value="tp">Gesloten op TP</option>
            <option value="sl">Gesloten op SL</option>
          </select>
          <span id="bt-trade-count" style="font-size:12px;color:var(--text3)"></span>
        </div>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>#</th><th>Richting</th><th>Entry datum</th><th>Entry $</th><th>Exit datum</th><th>Exit $</th><th>PnL</th><th>PnL %</th><th>SL/TP</th><th>Reden</th><th>Lev.</th></tr></thead>
          <tbody id="bt-trades"></tbody>
        </table>
      </div>
    </div>
  </div>
</div>

<!-- ══════════ POSITIES ══════════ -->
<div id="panel-positions" class="panel">
  <div class="section-title">Open Posities</div>
  <div class="card">
    <div class="table-wrap">
      <table>
        <thead><tr><th>Contract</th><th>Richting</th><th>Grootte</th><th>Entry</th><th>Mark</th><th>Leverage</th><th>PnL</th><th>PnL %</th></tr></thead>
        <tbody id="pos-table-body"><tr><td colspan="8" class="empty">Geen open posities</td></tr></tbody>
      </table>
    </div>
  </div>
</div>

<!-- ══════════ LEERSYSTEEM ══════════ -->
<div id="panel-memory" class="panel">
  <div class="section-title">Leersysteem — Zelfkritische feedback</div>

  <div class="card card-sm" style="margin-bottom:12px;background:var(--blue-bg);border-color:var(--blue-bd)">
    <div style="font-size:13px;color:var(--blue)">
      De bot analyseert elke gesloten trade en past zijn signaaldrempel automatisch aan.
      Bij te veel verliezende trades wordt de drempel verhoogd (minder maar betere trades).
      Bij goede prestaties wordt de drempel iets verlaagd (meer kansen).
    </div>
  </div>

  <div id="mem-loading" class="loading"><div class="spinner"></div>Leersysteem laden...</div>
  <div id="mem-content" style="display:none">

    <!-- Globale stats -->
    <div class="grid-4" id="mem-stats" style="margin-bottom:12px"></div>

    <!-- Per-coin evaluaties -->
    <div class="card" style="margin-bottom:12px">
      <div class="card-title">Adaptieve drempels per coin</div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Coin</th><th>Trades</th><th>Win rate</th><th>Gem. PnL</th><th>SL ratio</th><th>Basis drempel</th><th>Aangepaste drempel</th><th>Status</th></tr></thead>
          <tbody id="mem-thresholds"></tbody>
        </table>
      </div>
    </div>

    <!-- Trade historiek met snapshots -->
    <div class="card" style="margin-bottom:12px">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
        <div class="card-title" style="margin:0">Trade historiek + indicator snapshot</div>
        <select id="mem-sym-filter" onchange="loadMemoryTrades()" style="background:var(--bg3);border:1px solid var(--border);color:var(--text2);padding:5px 10px;font-size:12px;border-radius:var(--radius-xs);font-family:var(--font)">
          <option value="">Alle coins</option>
          <option value="BTC_USDT">BTC</option>
          <option value="ETH_USDT">ETH</option>
          <option value="XRP_USDT">XRP</option>
          <option value="FARTCOIN_USDT">FARTCOIN</option>
          <option value="ADA_USDT">ADA</option>
        </select>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Coin</th><th>Richting</th><th>Entry</th><th>Exit</th><th>PnL</th><th>Reden</th><th>Confidence</th><th>RSI</th><th>ADX</th><th>Regime</th></tr></thead>
          <tbody id="mem-trades"></tbody>
        </table>
      </div>
    </div>

    <!-- Evaluatie log -->
    <div class="card">
      <div class="card-title">Evaluatie log — drempelwijzigingen</div>
      <div id="mem-eval-log" class="log-box" style="max-height:250px;font-size:11.5px"></div>
    </div>
  </div>
</div>

<!-- ══════════ INSTELLINGEN ══════════ -->
<div id="panel-settings" class="panel">
  <div class="section-title">Instellingen</div>
  <div class="grid-2">
    <div class="card">
      <div class="card-title">Risicobeheer</div>
      <table style="width:100%"><tbody>
        <tr><td style="color:var(--text3);padding:8px 0;font-size:13px">Max leverage</td><td style="text-align:right;font-size:13px;color:var(--text)">20×</td></tr>
        <tr><td style="color:var(--text3);padding:8px 0;font-size:13px">Max inzet per trade</td><td style="text-align:right;font-size:13px;color:var(--yellow)">$3.00</td></tr>
        <tr><td style="color:var(--text3);padding:8px 0;font-size:13px">Stop Loss (ATR)</td><td style="text-align:right;font-size:13px;color:var(--red)">dynamisch</td></tr>
        <tr><td style="color:var(--text3);padding:8px 0;font-size:13px">Take Profit (1:2)</td><td style="text-align:right;font-size:13px;color:var(--green)">dynamisch</td></tr>
        <tr><td style="color:var(--text3);padding:8px 0;font-size:13px">Max dagverlies</td><td style="text-align:right;font-size:13px;color:var(--red)">$5 of 20%</td></tr>
        <tr><td style="color:var(--text3);padding:8px 0;font-size:13px">Max exposure</td><td style="text-align:right;font-size:13px;color:var(--yellow)">50%</td></tr>
        <tr><td style="color:var(--text3);padding:8px 0;font-size:13px">Cyclus interval</td><td style="text-align:right;font-size:13px;color:var(--blue)">60 sec</td></tr>
      </tbody></table>
    </div>
    <div class="card">
      <div class="card-title">Strategie — Indicatoren</div>
      <table style="width:100%"><tbody>
        <tr><td style="color:var(--text3);padding:8px 0;font-size:13px">BTC strategie</td><td style="text-align:right;font-size:13px;color:var(--text)">EMA ribbon + trendsterkte</td></tr>
        <tr><td style="color:var(--text3);padding:8px 0;font-size:13px">ETH strategie</td><td style="text-align:right;font-size:13px;color:var(--text)">BB squeeze + OBV</td></tr>
        <tr><td style="color:var(--text3);padding:8px 0;font-size:13px">XRP strategie</td><td style="text-align:right;font-size:13px;color:var(--text)">Mean-rev + vol switch</td></tr>
        <tr><td style="color:var(--text3);padding:8px 0;font-size:13px">FART strategie</td><td style="text-align:right;font-size:13px;color:var(--text)">Momentum + vol spike</td></tr>
        <tr><td style="color:var(--text3);padding:8px 0;font-size:13px">ADA strategie</td><td style="text-align:right;font-size:13px;color:var(--text)">Supertrend + OBV</td></tr>
        <tr><td style="color:var(--text3);padding:8px 0;font-size:13px">Signaaldrempel</td><td style="text-align:right;font-size:13px;color:var(--yellow)">55–65% consensus</td></tr>
      </tbody></table>
    </div>
    <div class="card">
      <div class="card-title">Handelsparen</div>
      <div style="display:flex;flex-direction:column;gap:8px">
        <div style="display:flex;align-items:center;justify-content:space-between;font-size:13px"><span style="color:var(--text2)">BTC / USDT Perpetual</span><span class="badge badge-long">Long / Short</span></div>
        <div style="display:flex;align-items:center;justify-content:space-between;font-size:13px"><span style="color:var(--text2)">ETH / USDT Perpetual</span><span class="badge badge-long">Long / Short</span></div>
        <div style="display:flex;align-items:center;justify-content:space-between;font-size:13px"><span style="color:var(--text2)">XRP / USDT Perpetual</span><span class="badge badge-long">Long / Short</span></div>
        <div style="display:flex;align-items:center;justify-content:space-between;font-size:13px"><span style="color:var(--text2)">FARTCOIN / USDT Perpetual</span><span class="badge badge-long">Long / Short</span></div>
        <div style="display:flex;align-items:center;justify-content:space-between;font-size:13px"><span style="color:var(--text2)">ADA / USDT Perpetual</span><span class="badge badge-long">Long / Short</span></div>
      </div>
    </div>
    <div class="card">
      <div class="card-title">API Configuratie</div>
      <div style="font-size:13px;color:var(--text3);line-height:1.9">
        <p>Sleutels via Railway environment variables:</p><br>
        <code style="background:var(--blue-bg);padding:2px 8px;border-radius:5px;color:var(--blue);font-size:12px">GATE_API_KEY</code><br><br>
        <code style="background:var(--blue-bg);padding:2px 8px;border-radius:5px;color:var(--blue);font-size:12px">GATE_API_SECRET</code>
        <br><br>
        <p style="color:var(--yellow)">Alleen Futures Trading + Read rechten — geen withdrawals.</p>
      </div>
    </div>
  </div>
</div>

</main>
</div>
<div id="toast"></div>

<script>
const SYMBOLS = ['BTC_USDT','ETH_USDT','XRP_USDT','FARTCOIN_USDT','ADA_USDT'];
const SYM_SHORT = {BTC_USDT:'BTC',ETH_USDT:'ETH',XRP_USDT:'XRP',FARTCOIN_USDT:'FART',ADA_USDT:'ADA'};
let btEqChart=null, btPieChart=null, chartBTC=null, chartETH=null;
let pollTimer=null, posTimer=null;
let storedSL=0, storedTP=0, currentPrice=0;

// ── Tabs ──────────────────────────────────────────────────────────────────────
function showTab(id, btn) {
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('panel-'+id).classList.add('active');
  if(btn) btn.classList.add('active');
  if(id==='dashboard') { loadTickers(); loadPriceChart('BTC_USDT','chart-btc','#007aff'); loadPriceChart('ETH_USDT','chart-eth','#34c759'); }
  if(id==='positions') loadPositionsTable();
  if(id==='memory') loadMemory();
}

// ── Toast ─────────────────────────────────────────────────────────────────────
function toast(msg, type='') {
  const t=document.getElementById('toast');
  t.textContent=msg; t.className='show '+(type==='ok'?'ok':type==='err'?'err':'');
  setTimeout(()=>t.className='',3000);
}

// ── Log helpers ───────────────────────────────────────────────────────────────
function dashLog(msg, cls='log-info') {
  const el=document.getElementById('dash-log');
  const d=document.createElement('div');
  d.className=cls; d.textContent='['+new Date().toLocaleTimeString('nl-BE')+'] '+msg;
  el.prepend(d); if(el.children.length>100) el.removeChild(el.lastChild);
}
function manualLog(msg, cls='log-info') {
  const el=document.getElementById('manual-log');
  const d=document.createElement('div');
  d.className=cls; d.textContent='['+new Date().toLocaleTimeString('nl-BE')+'] '+msg;
  el.prepend(d); if(el.children.length>50) el.removeChild(el.lastChild);
}

// ── Bot controls ──────────────────────────────────────────────────────────────
async function startBot() {
  const r=await fetch('/api/bot/start',{method:'POST'});
  const d=await r.json();
  if(d.status==='started'||d.status==='already_running') {
    document.getElementById('btn-start').disabled=true;
    document.getElementById('btn-stop').disabled=false;
    dashLog('Jef Bot gestart — trading actief','log-ok');
    toast('Jef Bot gestart','ok');
    startPoll();
  }
}
async function stopBot() {
  await fetch('/api/bot/stop',{method:'POST'});
  document.getElementById('btn-start').disabled=false;
  document.getElementById('btn-stop').disabled=true;
  setPill(false);
  dashLog('Bot gestopt','log-warn');
  toast('Bot gestopt');
  if(pollTimer){clearInterval(pollTimer);pollTimer=null;}
}
function startPoll(){if(pollTimer)return;pollTimer=setInterval(pollStatus,8000);pollStatus();}
async function pollStatus() {
  try {
    const r=await fetch('/api/bot/status');
    const d=await r.json();
    setPill(d.running);
    if(d.balance>0){
      document.getElementById('balance-chip').innerHTML='Balans: <strong>$'+d.balance.toFixed(2)+'</strong>';
    }
    const pnlEl=document.getElementById('stat-pnl');
    const pnl=d.daily_pnl||0;
    pnlEl.textContent=(pnl>=0?'+':'')+pnl.toFixed(4)+' USDT';
    pnlEl.className='stat-val '+(pnl>=0?'up':'down');
    document.getElementById('stat-trades').textContent=d.trade_count||0;
    document.getElementById('stat-pos').textContent=(d.positions||[]).length;
    if(d.last_cycle) document.getElementById('last-cycle-label').textContent='Laatste cyclus: '+new Date(d.last_cycle).toLocaleTimeString('nl-BE');
    if(!d.running){
      document.getElementById('btn-start').disabled=false;
      document.getElementById('btn-stop').disabled=true;
      if(pollTimer){clearInterval(pollTimer);pollTimer=null;}
    }

    // === Health bar updaten ===
    const dot = document.getElementById('health-dot');
    const label = document.getElementById('health-label');
    const colors = {online:'#34c759',stalled:'#ff9500',degraded:'#ff9500',offline:'#86868b'};
    const labels = {online:'Online',stalled:'⚠ Stalled',degraded:'⚠ Degraded',offline:'Offline'};
    const h = d.health || 'offline';
    dot.style.background = colors[h] || '#86868b';
    // Pulseer animatie als online
    dot.style.boxShadow = h==='online' ? '0 0 6px '+colors[h] : 'none';
    label.textContent = labels[h] || h;
    label.style.color = colors[h] || 'var(--text3)';

    document.getElementById('health-uptime').textContent = d.uptime || '—';
    document.getElementById('health-cycle').textContent = d.last_cycle_ago || '—';
    document.getElementById('health-trade').textContent = d.last_trade_ago || 'nooit';
    document.getElementById('health-errors').textContent = d.error_count || '0';
    document.getElementById('health-errors').style.color = (d.error_count||0)>5 ? 'var(--red)' : 'var(--text2)';

    const stalledEl = document.getElementById('health-stalled');
    stalledEl.style.display = d.stalled ? 'inline' : 'none';

    const errEl = document.getElementById('health-last-error');
    if(d.last_error) {
      errEl.textContent = d.last_error;
      errEl.style.display = 'inline';
    } else {
      errEl.style.display = 'none';
    }

    // Waarschuwing: veel cycli zonder trade
    if(d.cycles_without_trade > 200) {
      document.getElementById('health-trade').style.color = 'var(--red)';
    } else if(d.cycles_without_trade > 50) {
      document.getElementById('health-trade').style.color = 'var(--yellow)';
    } else {
      document.getElementById('health-trade').style.color = 'var(--text2)';
    }

  } catch(e){}
}
function setPill(on){
  const p=document.getElementById('bot-pill');
  const t=document.getElementById('pill-text');
  p.className='live-badge '+(on?'':'offline');
  t.textContent=on?'Live':'Offline';
  const ds=document.getElementById('dash-status');
  if(ds){ds.textContent=on?'Actief':'Offline';ds.style.color=on?'var(--green)':'var(--text3)';}
}

// ── Balance overview ───────────────────────────────────────────────────────
async function loadBalanceOverview() {
  try {
    const r = await fetch('/api/balance');
    const d = await r.json();
    if(d.error) { dashLog('Balans fout: '+d.error, 'log-error'); return; }

    // Grote balans cards
    const fmt = v => '$'+Math.abs(v).toFixed(2);

    const totalEl = document.getElementById('bal-total');
    totalEl.textContent = fmt(d.total);
    totalEl.style.color = d.total >= 0 ? 'var(--blue)' : 'var(--red)';

    document.getElementById('bal-available').textContent = fmt(d.available);

    const marginEl = document.getElementById('bal-margin');
    marginEl.textContent = d.in_margin > 0 ? fmt(d.in_margin) : '$0.00';
    marginEl.style.color = d.in_margin > 0 ? 'var(--yellow)' : 'var(--text3)';

    const pnlEl = document.getElementById('bal-unrealised');
    pnlEl.textContent = (d.unrealised >= 0 ? '+' : '') + d.unrealised.toFixed(4) + ' USDT';
    pnlEl.style.color = d.unrealised >= 0 ? 'var(--green)' : 'var(--red)';

    // Titlebar balance update
    document.getElementById('balance-chip').innerHTML =
      'Balans: <strong>'+fmt(d.total)+'</strong>';

    // Open posities tabel
    const posCard = document.getElementById('dash-positions-card');
    const posBody = document.getElementById('dash-pos-body');
    if(d.positions && d.positions.length > 0) {
      posCard.style.display = 'block';
      posBody.innerHTML = d.positions.map(p => {
        const pnl    = p.unrealised_pnl;
        // PnL% berekenen: gebruik entry/mark/leverage (betrouwbaarder dan margin veld)
        const entry  = p.entry_price || 0;
        const mark   = p.mark_price  || entry;
        const lev    = p.leverage    || 1;
        const isLong = p.direction === 'long';
        let pnlPct = 0;
        if(entry > 0) {
          pnlPct = isLong
            ? ((mark - entry) / entry) * lev * 100
            : ((entry - mark) / entry) * lev * 100;
        } else if(p.margin > 0) {
          pnlPct = (pnl / p.margin) * 100;  // fallback
        }
        const ep     = entry < 1 ? entry.toFixed(6) : entry.toFixed(2);
        const mp     = mark  < 1 ? mark.toFixed(6)  : mark.toFixed(2);
        return `<tr>
          <td style="color:var(--text);font-weight:500">${p.contract.replace('_USDT','/USDT')}</td>
          <td><span class="badge badge-${p.direction}">${p.direction.toUpperCase()}</span></td>
          <td style="color:var(--blue)">${p.leverage}×</td>
          <td>$${ep}</td>
          <td style="color:var(--blue)">$${mp}</td>
          <td style="color:var(--text2)">$${p.margin.toFixed(4)}</td>
          <td class="${pnl>=0?'up':'down'}" style="font-weight:500">${pnl>=0?'+':''}${pnl.toFixed(4)}</td>
          <td class="${pnlPct>=0?'up':'down'}">${pnlPct>=0?'+':''}${pnlPct.toFixed(1)}%</td>
        </tr>`;
      }).join('');
    } else {
      posCard.style.display = 'none';
    }
  } catch(e) {
    dashLog('Balans ophalen mislukt: '+e.message, 'log-error');
  }
}

// ── Tickers ───────────────────────────────────────────────────────────────────
async function loadTickers() {
  const strip=document.getElementById('ticker-strip');
  strip.innerHTML='';
  for(const sym of SYMBOLS){
    try{
      const r=await fetch('/api/ticker/'+sym);
      const d=await r.json();
      if(!d||!d.last)continue;
      const price=parseFloat(d.last);
      const chg=parseFloat(d.change_percentage||0);
      const el=document.createElement('div');
      el.className='ticker-item';
      el.innerHTML=`<span class="ticker-sym">${SYM_SHORT[sym]}</span><span class="ticker-price" style="color:${chg>=0?'var(--text)':'var(--text)'}">${price<1?price.toFixed(5):price.toLocaleString('nl-BE',{maximumFractionDigits:2})}</span><span class="ticker-chg ${chg>=0?'up':'down'}">${chg>=0?'+':''}${chg.toFixed(2)}%</span>`;
      strip.appendChild(el);
    }catch(e){}
  }
}

// ── Price Charts ──────────────────────────────────────────────────────────────
async function loadPriceChart(sym, canvasId, color) {
  try{
    const r=await fetch('/api/candles/'+sym+'?interval=1h&limit=60');
    const data=await r.json();
    if(!data.length)return;
    const labels=data.map(c=>{const d=new Date(parseInt(c.t)*1000);return d.getHours()+':'+(d.getMinutes()+'').padStart(2,'0');});
    const prices=data.map(c=>parseFloat(c.c));
    const ctx=document.getElementById(canvasId).getContext('2d');
    if(canvasId==='chart-btc'&&chartBTC)chartBTC.destroy();
    if(canvasId==='chart-eth'&&chartETH)chartETH.destroy();
    const grad=ctx.createLinearGradient(0,0,0,210);
    grad.addColorStop(0,color+'28'); grad.addColorStop(1,color+'00');
    const ch=new Chart(ctx,{
      type:'line',
      data:{labels,datasets:[{data:prices,borderColor:color,backgroundColor:grad,borderWidth:1.5,pointRadius:0,fill:true,tension:0.3}]},
      options:{
        responsive:true,maintainAspectRatio:false,
        plugins:{legend:{display:false},tooltip:{backgroundColor:'#ffffff',borderColor:'rgba(0,0,0,.08)',borderWidth:1,titleColor:color,bodyColor:'#515154',callbacks:{label:c=>' $'+c.parsed.y.toLocaleString()}}},
        scales:{
          x:{ticks:{color:'#86868b',maxTicksLimit:8,font:{family:'ui-monospace',size:10}},grid:{color:'rgba(0,0,0,.04)'}},
          y:{ticks:{color:'#86868b',font:{family:'ui-monospace',size:10},callback:v=>v>=1000?'$'+Math.round(v/1000)+'k':'$'+v},grid:{color:'rgba(0,0,0,.04)'}}
        }
      }
    });
    if(canvasId==='chart-btc')chartBTC=ch; else chartETH=ch;
  }catch(e){}
}

// ── Positions table ───────────────────────────────────────────────────────────
async function loadPositionsTable() {
  const r=await fetch('/api/account');
  const d=await r.json();
  const positions=(d.positions||[]).filter(p=>parseInt(p.size||0)!==0);
  const tbody=document.getElementById('pos-table-body');
  if(!positions.length){tbody.innerHTML='<tr><td colspan="8" class="empty">Geen open posities</td></tr>';return;}
  tbody.innerHTML=positions.map(p=>{
    const size=parseInt(p.size);
    const dir=size>0?'long':'short';
    const isLong=size>0;
    const pnl=parseFloat(p.unrealised_pnl||0);
    const entry=parseFloat(p.entry_price||0);
    const mark=parseFloat(p.mark_price||entry);
    const lev=parseInt(p.leverage||1);
    let pnlPct=0;
    if(entry>0){
      pnlPct=isLong
        ?((mark-entry)/entry)*lev*100
        :((entry-mark)/entry)*lev*100;
    }
    return `<tr>
      <td style="color:var(--text)">${p.contract}</td>
      <td><span class="badge badge-${dir}">${dir.toUpperCase()}</span></td>
      <td>${Math.abs(size)}</td>
      <td>$${entry.toFixed(entry<1?6:4)}</td>
      <td style="color:var(--blue)">$${mark.toFixed(mark<1?6:4)}</td>
      <td>${lev}×</td>
      <td class="${pnl>=0?'up':'down'}">${pnl>=0?'+':''}${pnl.toFixed(4)} USDT</td>
      <td class="${pnlPct>=0?'up':'down'}">${pnlPct>=0?'+':''}${pnlPct.toFixed(1)}%</td>
    </tr>`;
  }).join('');
}

// ── Manual Trading ────────────────────────────────────────────────────────────
function isRoiMode() {
  return document.querySelector('input[name="sltp-mode"]:checked')?.value === 'roi';
}

function switchSLTPMode() {
  const roi = isRoiMode();
  const lev = parseInt(document.getElementById('trade-lev').value);
  const slInput = document.getElementById('trade-sl');
  const tpInput = document.getElementById('trade-tp');
  if(roi) {
    document.getElementById('lbl-sl').textContent = 'SL ROI %';
    document.getElementById('lbl-tp').textContent = 'TP ROI %';
    // Converteer huidige prijs% → ROI%
    slInput.value = (parseFloat(slInput.value) * lev).toFixed(1);
    tpInput.value = (parseFloat(tpInput.value) * lev).toFixed(1);
    slInput.max = 100; tpInput.max = 200;
  } else {
    document.getElementById('lbl-sl').textContent = 'SL Prijs %';
    document.getElementById('lbl-tp').textContent = 'TP Prijs %';
    // Converteer huidige ROI% → prijs%
    slInput.value = (parseFloat(slInput.value) / lev).toFixed(2);
    tpInput.value = (parseFloat(tpInput.value) / lev).toFixed(2);
    slInput.max = 30; tpInput.max = 50;
  }
  updatePreview();
}

function getSlTpPricePct() {
  // Altijd prijs% retourneren (converteert ROI als nodig)
  const roi = isRoiMode();
  const lev = parseInt(document.getElementById('trade-lev').value);
  let sl = parseFloat(document.getElementById('trade-sl').value);
  let tp = parseFloat(document.getElementById('trade-tp').value);
  if(roi) {
    sl = sl / lev;  // ROI% → prijs%
    tp = tp / lev;
  }
  return { sl: sl/100, tp: tp/100, sl_pct: sl, tp_pct: tp };
}

function updatePreview() {
  if(!currentPrice)return;
  const {sl, tp, sl_pct, tp_pct} = getSlTpPricePct();
  const lev = parseInt(document.getElementById('trade-lev').value);
  const slRoi = sl_pct * lev;
  const tpRoi = tp_pct * lev;
  const decs = currentPrice < 1 ? 6 : 2;
  document.getElementById('sltp-preview').style.display='block';
  document.getElementById('prev-sl').textContent='$'+(currentPrice*(1-sl)).toFixed(decs)+' (SL −'+sl_pct.toFixed(2)+'% / ROI −'+slRoi.toFixed(1)+'%)';
  document.getElementById('prev-tp').textContent='$'+(currentPrice*(1+tp)).toFixed(decs)+' (TP +'+tp_pct.toFixed(2)+'% / ROI +'+tpRoi.toFixed(1)+'%)';
  document.getElementById('prev-pricepct').textContent='SL '+sl_pct.toFixed(2)+'% | TP '+tp_pct.toFixed(2)+'% prijsbeweging @ '+lev+'× leverage';
}

async function fetchSignal() {
  const sym=document.getElementById('sig-sym').value;
  document.getElementById('trade-sym').value=sym;
  document.getElementById('sig-result').style.display='none';
  document.getElementById('sig-loading').style.display='flex';
  try{
    const r=await fetch('/api/manual/signal/'+sym);
    const d=await r.json();
    document.getElementById('sig-loading').style.display='none';
    document.getElementById('sig-result').style.display='block';
    const hero=document.getElementById('sig-hero');
    const dir=document.getElementById('sig-dir');
    const conf=document.getElementById('sig-conf');
    hero.className='signal-hero '+(d.signal==='long'?'long-sig':d.signal==='short'?'short-sig':'none-sig');
    if(d.signal==='long'){dir.textContent='▲ Long';dir.style.color='var(--green)';}
    else if(d.signal==='short'){dir.textContent='▼ Short';dir.style.color='var(--red)';}
    else{dir.textContent='— Neutraal';dir.style.color='var(--text3)';}
    conf.textContent='Zekerheid: '+(d.confidence||0)+'%';
    if(d.price){currentPrice=d.price;document.getElementById('sig-price').textContent='$'+d.price.toLocaleString('nl-BE',{maximumFractionDigits:5});}
    const rsiEl=document.getElementById('sig-rsi');
    rsiEl.textContent=(d.rsi||'—')+'';
    rsiEl.style.color=d.rsi>70?'var(--red)':d.rsi<30?'var(--green)':'var(--text)';
    document.getElementById('sig-vol').textContent=d.volatility?(d.volatility*100).toFixed(1)+'%':'—';
    updatePreview();
    let html='';
    if(d.reasons?.length){html+='<div style="font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--text3);margin-bottom:4px;">Bullish signalen</div>';d.reasons.forEach(r=>{html+=`<div style="color:var(--green);font-size:12px;">✓ ${r}</div>`;});}
    if(d.counter?.length){html+='<div style="font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--text3);margin:8px 0 4px;">Bearish signalen</div>';d.counter.forEach(r=>{html+=`<div style="color:var(--red);font-size:12px;">✗ ${r}</div>`;});}
    document.getElementById('sig-reasons').innerHTML=html;
  }catch(e){
    document.getElementById('sig-loading').style.display='none';
    manualLog('Signaal fout: '+e.message,'log-error');
  }
}

async function placeTrade(dir) {
  const sym=document.getElementById('trade-sym').value;
  const size=parseFloat(document.getElementById('trade-size').value);
  const lev=parseInt(document.getElementById('trade-lev').value);
  const {sl_pct, tp_pct} = getSlTpPricePct();
  const slRoi = sl_pct * lev;
  const tpRoi = tp_pct * lev;
  document.getElementById('btn-long-order').disabled=true;
  document.getElementById('btn-short-order').disabled=true;
  manualLog('Order: '+dir.toUpperCase()+' '+sym+' $'+size+' @ '+lev+'× | SL '+sl_pct.toFixed(2)+'% (ROI −'+slRoi.toFixed(1)+'%) | TP '+tp_pct.toFixed(2)+'% (ROI +'+tpRoi.toFixed(1)+'%)','log-info');
  try{
    // Backend ontvangt altijd prijs% (niet ROI%)
    const r=await fetch('/api/manual/trade',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:sym,direction:dir,size_usd:size,leverage:lev,sl_pct:sl_pct,tp_pct:tp_pct})});
    const d=await r.json();
    if(d.ok){
      storedSL=d.sl; storedTP=d.tp;
      const entryRoiInfo = d.sl_roi ? ' | SL ROI '+d.sl_roi.toFixed(1)+'% | TP ROI '+d.tp_roi.toFixed(1)+'%' : '';
      manualLog('Order OK — entry $'+d.price+' | SL $'+d.sl+' | TP $'+d.tp+entryRoiInfo,'log-ok');
      // Toon SL/TP placement status
      const slStatus = d.sl_placed ? '✓ SL geplaatst op exchange' : '⚠ SL NIET geplaatst!';
      const tpStatus = d.tp_placed ? '✓ TP geplaatst op exchange' : '⚠ TP NIET geplaatst!';
      manualLog(slStatus, d.sl_placed ? 'log-ok' : 'log-error');
      manualLog(tpStatus, d.tp_placed ? 'log-ok' : 'log-error');
      if(!d.sl_placed || !d.tp_placed) {
        toast('Let op: SL of TP niet geplaatst op exchange!','err');
      } else {
        toast('Order + SL/TP uitgevoerd','ok');
      }
      document.getElementById('sltp-preview').style.display='block';
      document.getElementById('prev-sl').textContent='$'+d.sl;
      document.getElementById('prev-tp').textContent='$'+d.tp;
      setTimeout(refreshPosition,2000);
      if(!posTimer)posTimer=setInterval(refreshPosition,10000);
    }else{
      manualLog('Order mislukt: '+(d.error||''),'log-error');
      toast(d.error||'Order mislukt','err');
    }
  }catch(e){toast('Verbindingsfout: '+e.message,'err');}
  finally{document.getElementById('btn-long-order').disabled=false;document.getElementById('btn-short-order').disabled=false;}
}

async function refreshPosition() {
  const sym=document.getElementById('trade-sym').value;
  try{
    const r=await fetch('/api/manual/position/'+sym);
    const d=await r.json();
    const pos=d.position; const ticker=d.ticker;
    if(!pos||parseInt(pos.size||0)===0){
      document.getElementById('pos-empty').style.display='block';
      document.getElementById('pos-detail').style.display='none';
      return;
    }
    document.getElementById('pos-empty').style.display='none';
    document.getElementById('pos-detail').style.display='block';
    const size=parseInt(pos.size);
    const isLong=size>0;
    const entry=parseFloat(pos.entry_price||0);
    const mark=ticker?parseFloat(ticker.last||entry):entry;
    const pnl=parseFloat(pos.unrealised_pnl||0);
    const lev=parseInt(pos.leverage||1);
    const margin=parseFloat(pos.margin||0);
    // PnL% via entry/mark/leverage (betrouwbaarder dan margin veld van Gate.io)
    let pnlPct=0;
    if(entry>0){
      pnlPct=isLong
        ?((mark-entry)/entry)*lev*100
        :((entry-mark)/entry)*lev*100;
    } else if(margin>0){
      pnlPct=pnl/margin*100;
    }
    const pnlEl=document.getElementById('pos-pnl');
    pnlEl.textContent=(pnl>=0?'+':'')+pnl.toFixed(4)+' USDT';
    pnlEl.style.color=pnl>=0?'var(--green)':'var(--red)';
    // Prijs% = prijsverandering, ROI% = rendement op margin
    const pricePct = entry>0 ? (isLong ? (mark-entry)/entry*100 : (entry-mark)/entry*100) : 0;
    document.getElementById('pos-pnl-pct').textContent='Prijs '+(pricePct>=0?'+':'')+pricePct.toFixed(2)+'%';
    document.getElementById('pos-pnl-pct').style.color=pnlPct>=0?'var(--green)':'var(--red)';
    // ROI = prijs% × leverage = rendement op margin
    const roiEl=document.getElementById('pos-roi');
    roiEl.textContent='ROI '+(pnlPct>=0?'+':'')+pnlPct.toFixed(1)+'%';
    roiEl.style.color=pnlPct>=0?'var(--green)':'var(--red)';
    const dirEl=document.getElementById('pos-dir');
    dirEl.textContent=isLong?'▲ Long':'▼ Short';
    dirEl.style.color=isLong?'var(--green)':'var(--red)';
    document.getElementById('pos-lev').textContent=lev+'×';
    document.getElementById('pos-entry').textContent='$'+entry.toFixed(entry<1?6:2);
    document.getElementById('pos-mark').textContent='$'+mark.toFixed(mark<1?6:2);

    // SL/TP: prioriteit = exchange orders > lokaal opgeslagen > berekend
    const exSL = d.exchange_sl || 0;
    const exTP = d.exchange_tp || 0;
    const slPct=parseFloat(document.getElementById('trade-sl').value)/100;
    const tpPct=parseFloat(document.getElementById('trade-tp').value)/100;
    const calcSL = isLong ? entry*(1-slPct) : entry*(1+slPct);
    const calcTP = isLong ? entry*(1+tpPct) : entry*(1-tpPct);
    const sl = exSL || storedSL || calcSL;
    const tp = exTP || storedTP || calcTP;

    // Update storedSL/TP als exchange waarden beschikbaar zijn
    if(exSL) storedSL = exSL;
    if(exTP) storedTP = exTP;

    const slEl = document.getElementById('pos-sl');
    const tpEl = document.getElementById('pos-tp');
    const decs = sl<1?6:2;
    // Bereken SL/TP als ROI%
    const slPricePct = entry>0 ? Math.abs(sl-entry)/entry*100 : 0;
    const tpPricePct = entry>0 ? Math.abs(tp-entry)/entry*100 : 0;
    const slRoiPct = slPricePct * lev;
    const tpRoiPct = tpPricePct * lev;
    slEl.textContent='$'+sl.toFixed(decs)+' (−'+slRoiPct.toFixed(0)+'% ROI)';
    tpEl.textContent='$'+tp.toFixed(decs)+' (+'+tpRoiPct.toFixed(0)+'% ROI)';

    // Waarschuwing als SL/TP niet op exchange staan
    if(!exSL && !exTP && (d.price_orders_count===0)) {
      slEl.textContent += ' ⚠';
      tpEl.textContent += ' ⚠';
      slEl.title = 'Geen SL trigger order gevonden op exchange!';
      tpEl.title = 'Geen TP trigger order gevonden op exchange!';
    } else {
      if(exSL) { slEl.textContent += ' ✓'; slEl.title = 'SL actief op exchange'; }
      if(exTP) { tpEl.textContent += ' ✓'; tpEl.title = 'TP actief op exchange'; }
    }

    const range=Math.abs(tp-sl);
    let prog=50;
    if(range>0)prog=isLong?((mark-sl)/range*100):((sl-mark)/range*100);
    prog=Math.max(2,Math.min(98,prog));
    const fill=document.getElementById('prog-fill');
    fill.style.width=prog+'%';
    fill.style.background=prog>70?'var(--green)':prog<30?'var(--red)':'var(--blue)';
    document.getElementById('prog-label').textContent='ROI '+(pnlPct>=0?'+':'')+pnlPct.toFixed(1)+'%';
  }catch(e){}
}

async function closePosition() {
  const sym=document.getElementById('trade-sym').value;
  if(!confirm('Positie sluiten voor '+sym+'?'))return;
  manualLog('Sluiten: '+sym,'log-warn');
  try{
    const r=await fetch('/api/manual/close',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:sym})});
    const d=await r.json();
    if(d.ok){
      const pnl=d.pnl||0;
      const cancelled=d.sl_tp_cancelled||0;
      manualLog('Gesloten. PnL: '+(pnl>=0?'+':'')+pnl.toFixed(4)+' USDT',pnl>=0?'log-ok':'log-error');
      if(cancelled>0) manualLog(cancelled+' SL/TP order(s) geannuleerd op exchange','log-info');
      toast((pnl>=0?'Winst: +':'Verlies: ')+pnl.toFixed(4)+' USDT',pnl>=0?'ok':'err');
      storedSL=0;storedTP=0;
      setTimeout(refreshPosition,1500);
      if(posTimer){clearInterval(posTimer);posTimer=null;}
    }else{manualLog('Fout: '+(d.error||''),'log-error');toast(d.error||'Fout','err');}
  }catch(e){toast('Verbindingsfout','err');}
}

// ── Backtest ──────────────────────────────────────────────────────────────────
let allBtTrades = [];

function clearDates() {
  document.getElementById('bt-from').value = '';
  document.getElementById('bt-to').value = '';
}

// Auto-selecteer aanbevolen strategie bij coin keuze
document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('bt-sym').addEventListener('change', function() {
    const map = {
      'BTC_USDT':'btc_trend','ETH_USDT':'eth_squeeze',
      'XRP_USDT':'xrp_roi','FARTCOIN_USDT':'fartcoin_momentum','ADA_USDT':'ada_supertrend'
    };
    const intervalMap = {
      'BTC_USDT':'4h','ETH_USDT':'1h','XRP_USDT':'4h','FARTCOIN_USDT':'5m','ADA_USDT':'1h'
    };
    const strat = map[this.value];
    const intv  = intervalMap[this.value];
    if(strat) document.getElementById('bt-strat').value = strat;
    if(intv)  document.getElementById('bt-interval').value = intv;
  });
  // Auto-config voor specifieke strategieën
  document.getElementById('bt-strat').addEventListener('change', function() {
    if(this.value === 'fartcoin_bb') {
      document.getElementById('bt-interval').value = '4h';
      document.getElementById('bt-sym').value = 'FARTCOIN_USDT';
    }
    if(this.value === 'xrp_roi') {
      document.getElementById('bt-interval').value = '4h';
      document.getElementById('bt-sym').value = 'XRP_USDT';
      document.getElementById('bt-roi-mode').checked = true;
      document.getElementById('bt-tp-roi').value = 30;
      document.getElementById('bt-sl-roi').value = 12;
    }
  });
});

async function runBacktest() {
  const btn=document.getElementById('btn-bt');
  btn.disabled=true;
  document.getElementById('bt-results').style.display='none';
  document.getElementById('bt-loading').style.display='flex';

  const body={
    symbol:   document.getElementById('bt-sym').value,
    strategy: document.getElementById('bt-strat').value,
    interval: document.getElementById('bt-interval').value,
    limit:    parseInt(document.getElementById('bt-limit').value),
    balance:  parseFloat(document.getElementById('bt-bal').value),
    risk_pct: parseFloat(document.getElementById('bt-risk').value)/100,
    date_from: document.getElementById('bt-from').value || null,
    date_to:   document.getElementById('bt-to').value   || null,
    // Geavanceerde parameters
    fee_pct:           parseFloat(document.getElementById('bt-fee').value),
    cooldown_bars:     parseInt(document.getElementById('bt-cooldown').value),
    max_hold_bars:     parseInt(document.getElementById('bt-maxhold').value),
    direction_filter:  document.getElementById('bt-direction').value,
    trailing_stop_pct: parseFloat(document.getElementById('bt-trailing').value),
    breakeven_trigger: parseFloat(document.getElementById('bt-breakeven').value),
    max_consec_losses: parseInt(document.getElementById('bt-maxconsec').value),
    // ROI mode
    use_roi_mode:      document.getElementById('bt-roi-mode').checked,
    tp_roi:            parseFloat(document.getElementById('bt-tp-roi').value),
    sl_roi:            parseFloat(document.getElementById('bt-sl-roi').value),
    trail_after_roi:   document.getElementById('bt-roi-trail').checked,
    trail_roi_conf:    parseFloat(document.getElementById('bt-roi-conf').value) / 100,
    trail_roi_distance:parseFloat(document.getElementById('bt-roi-dist').value),
  };

  try{
    const r=await fetch('/api/backtest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d=await r.json();
    if(d.error){toast(d.error,'err');return;}
    allBtTrades = d.trades || [];
    renderBacktest(d);
  }catch(e){toast('Fout: '+e.message,'err');}
  finally{btn.disabled=false;document.getElementById('bt-loading').style.display='none';}
}

function filterTrades() {
  const f = document.getElementById('bt-filter').value;
  let trades = allBtTrades;
  if(f==='win')   trades = trades.filter(t=>t.pnl>0);
  if(f==='loss')  trades = trades.filter(t=>t.pnl<=0);
  if(f==='long')  trades = trades.filter(t=>t.direction==='long');
  if(f==='short') trades = trades.filter(t=>t.direction==='short');
  if(f==='tp')    trades = trades.filter(t=>t.exit_reason==='TP');
  if(f==='sl')    trades = trades.filter(t=>t.exit_reason==='SL');
  renderTradesTable(trades);
}

function renderTradesTable(trades) {
  document.getElementById('bt-trade-count').textContent = trades.length + ' trades';
  document.getElementById('bt-trades').innerHTML = trades.map((t,i) => {
    const ep = t.entry_price<1 ? t.entry_price.toFixed(6) : t.entry_price.toFixed(2);
    const xp = t.exit_price<1  ? t.exit_price.toFixed(6)  : t.exit_price.toFixed(2);
    return `<tr>
      <td style="color:var(--text3);font-size:11px">${i+1}</td>
      <td><span class="badge badge-${t.direction}">${t.direction.toUpperCase()}</span></td>
      <td style="color:var(--text3);font-size:11px">${t.entry_ts||'—'}</td>
      <td>$${ep}</td>
      <td style="color:var(--text3);font-size:11px">${t.exit_ts||'—'}</td>
      <td>$${xp}</td>
      <td class="${t.pnl>=0?'up':'down'}" style="font-weight:500">${t.pnl>=0?'+':''}${t.pnl}</td>
      <td class="${t.pnl_pct>=0?'up':'down'}">${t.pnl_pct>=0?'+':''}${t.pnl_pct}%</td>
      <td style="color:var(--text3);font-size:11px">${t.sl_pct||'—'}%/${t.tp_pct||'—'}%</td>
      <td><span class="badge badge-${t.exit_reason.toLowerCase()}">${t.exit_reason}</span></td>
      <td style="color:var(--text3)">${t.leverage}×</td>
    </tr>`;
  }).join('');
}

function exportCSV() {
  if(!allBtTrades.length){toast('Geen trades om te exporteren','err');return;}
  const headers = ['#','Richting','Entry datum','Entry prijs','Exit datum','Exit prijs','PnL','PnL %','SL%','TP%','Reden','Leverage'];
  const rows = allBtTrades.map((t,i) => [
    i+1, t.direction, t.entry_ts||'', t.entry_price, t.exit_ts||'', t.exit_price,
    t.pnl, t.pnl_pct, t.sl_pct||'', t.tp_pct||'', t.exit_reason, t.leverage
  ]);
  const csv = [headers, ...rows].map(r=>r.join(',')).join('\n');
  const blob = new Blob([csv], {type:'text/csv'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `backtest_${document.getElementById('bt-sym').value}_${new Date().toISOString().slice(0,10)}.csv`;
  a.click();
  toast('CSV geëxporteerd','ok');
}

function renderBacktest(d) {
  // Titel en periode
  const stratNames = {
    'btc_trend':'BTC Trend','eth_squeeze':'ETH Squeeze','xrp_roi':'XRP ROI',
    'fartcoin_momentum':'FARTCOIN Momentum','fartcoin_bb':'FARTCOIN BB',
    'ada_supertrend':'ADA Supertrend','general_consensus':'Consensus',
    'rsi_meanrev':'RSI Mean-Rev','ema_crossover':'EMA Crossover','bb_bounce':'BB Bounce'
  };
  document.getElementById('bt-res-title').textContent =
    d.symbol.replace('_USDT','/USDT') + ' · ' + (stratNames[d.strategy]||d.strategy) + ' · ' + d.candle_count + ' candles';
  document.getElementById('bt-res-period').textContent =
    (d.period_from && d.period_to) ? `Periode: ${d.period_from} → ${d.period_to}` : '';

  // Stat cards — uitgebreid met nieuwe metrics
  const rc  = d.return_pct>=0?'var(--green)':'var(--red)';
  const pnlc= parseFloat(d.total_pnl)>=0?'var(--green)':'var(--red)';
  const ddc = d.max_drawdown>20?'var(--red)':d.max_drawdown>10?'var(--yellow)':'var(--green)';
  const wrc = d.win_rate>=50?'var(--green)':'var(--red)';
  const feePnl = d.pnl_after_fees!=null ? d.pnl_after_fees : d.total_pnl;
  const feePnlC = feePnl>=0?'var(--green)':'var(--red)';
  const ls = d.long_stats||{}; const ss = d.short_stats||{};
  document.getElementById('bt-stats').innerHTML=`
    <div class="card card-sm"><div class="card-title">Rendement</div><div class="stat-val" style="color:${rc}">${d.return_pct>=0?'+':''}${d.return_pct}%</div><div class="stat-label">$${d.initial_balance} → $${d.final_balance}</div></div>
    <div class="card card-sm"><div class="card-title">Win Rate</div><div class="stat-val" style="color:${wrc}">${d.win_rate}%</div><div class="stat-label">${d.win_count}W / ${d.loss_count}L</div></div>
    <div class="card card-sm"><div class="card-title">PnL na fees</div><div class="stat-val" style="color:${feePnlC}">${feePnl>=0?'+':''}$${feePnl.toFixed(2)}</div><div class="stat-label">Fees: $${(d.total_fees||0).toFixed(2)}</div></div>
    <div class="card card-sm"><div class="card-title">Expectancy</div><div class="stat-val" style="color:${(d.expectancy||0)>=0?'var(--green)':'var(--red)'}">$${(d.expectancy||0).toFixed(3)}/trade</div><div class="stat-label">Gem. ${(d.avg_duration||0).toFixed(0)} bars/trade</div></div>
    <div class="card card-sm"><div class="card-title">Max Drawdown</div><div class="stat-val" style="color:${ddc}">${d.max_drawdown}%</div><div class="stat-label">Calmar: ${d.calmar_ratio||0}</div></div>
    <div class="card card-sm"><div class="card-title">Profit Factor</div><div class="stat-val warn">${d.profit_factor===999?'∞':d.profit_factor}</div><div class="stat-label">Sharpe: ${d.sharpe_ratio}</div></div>
    <div class="card card-sm"><div class="card-title">Avg Win / Loss</div><div class="stat-val" style="font-size:18px;color:var(--text)">$${d.avg_win} / $${Math.abs(d.avg_loss)}</div><div class="stat-label">Best $${d.best_trade} / Worst $${d.worst_trade}</div></div>
    <div class="card card-sm"><div class="card-title">Long / Short</div><div class="stat-val" style="font-size:16px"><span style="color:var(--green)">${ls.count||0} (${ls.wr||0}%)</span> / <span style="color:var(--red)">${ss.count||0} (${ss.wr||0}%)</span></div><div class="stat-label">Streaks: ${d.max_consec_wins||0}W / ${d.max_consec_losses||0}L</div></div>
  `;

  // Equity chart met datum labels
  if(btEqChart) btEqChart.destroy();
  const ec=document.getElementById('bt-equity').getContext('2d');
  const isPositive = d.final_balance >= d.initial_balance;
  const lineColor = isPositive ? '#34c759' : '#ff3b30';
  const g=ec.createLinearGradient(0,0,0,290);
  g.addColorStop(0, isPositive?'rgba(52,199,89,.15)':'rgba(255,59,48,.12)');
  g.addColorStop(1, isPositive?'rgba(52,199,89,0)':'rgba(255,69,58,0)');
  const labels = (d.equity_ts||[]).length ? d.equity_ts : d.equity_curve.map((_,i)=>i);
  btEqChart=new Chart(ec,{
    type:'line',
    data:{labels,datasets:[{data:d.equity_curve,borderColor:lineColor,backgroundColor:g,borderWidth:1.5,pointRadius:0,fill:true,tension:.2}]},
    options:{
      responsive:true,maintainAspectRatio:false,
      plugins:{
        legend:{display:false},
        tooltip:{backgroundColor:'#ffffff',borderColor:'rgba(0,0,0,.08)',borderWidth:1,bodyColor:'#515154',
          callbacks:{
            label:c=>' $'+c.parsed.y.toFixed(2),
            title:items=>{const l=items[0].label;return typeof l==='string'?l:'';}
          }
        },
        annotation:{} // toekomstige entry/exit markers
      },
      scales:{
        x:{ticks:{color:'#86868b',maxTicksLimit:6,font:{family:'ui-monospace',size:9}},grid:{color:'rgba(0,0,0,.04)'}},
        y:{ticks:{color:'#86868b',font:{family:'ui-monospace',size:10},callback:v=>'$'+v.toFixed(0)},grid:{color:'rgba(0,0,0,.04)'}}
      }
    }
  });

  // Pie chart
  if(btPieChart) btPieChart.destroy();
  const pc2=document.getElementById('bt-pie').getContext('2d');
  btPieChart=new Chart(pc2,{
    type:'doughnut',
    data:{
      labels:['Winsten','Verliezen'],
      datasets:[{data:[d.win_count,d.loss_count],backgroundColor:['rgba(52,199,89,.7)','rgba(255,59,48,.7)'],borderColor:['#34c759','#ff3b30'],borderWidth:1}]
    },
    options:{
      responsive:true,maintainAspectRatio:false,cutout:'68%',
      plugins:{
        legend:{position:'bottom',labels:{color:'#515154',font:{size:12},padding:16}},
        tooltip:{backgroundColor:'#ffffff',borderColor:'rgba(0,0,0,.08)',borderWidth:1}
      }
    }
  });

  // Trades tabel
  document.getElementById('bt-filter').value = 'all';
  renderTradesTable(allBtTrades);
  document.getElementById('bt-results').style.display='block';
  document.getElementById('bt-results').scrollIntoView({behavior:'smooth',block:'start'});
}

// ── Init ──────────────────────────────────────────────────────────────────────
// ── Trade Memory / Leersysteem ───────────────────────────────────────────────
async function loadMemory() {
  document.getElementById('mem-loading').style.display='flex';
  document.getElementById('mem-content').style.display='none';
  try {
    const r = await fetch('/api/memory/stats');
    const d = await r.json();
    if(!d.enabled) {
      document.getElementById('mem-loading').innerHTML='<span style="color:var(--text3)">Leersysteem niet actief — nog geen trades opgeslagen.</span>';
      return;
    }
    renderMemoryStats(d);
    await loadMemoryTrades();
    await loadMemoryEvals();
    document.getElementById('mem-loading').style.display='none';
    document.getElementById('mem-content').style.display='block';
  } catch(e) {
    document.getElementById('mem-loading').innerHTML='<span style="color:var(--red)">Fout: '+e.message+'</span>';
  }
}

function renderMemoryStats(d) {
  const s = d.stats || {};
  const wr = s.win_rate || 0;
  document.getElementById('mem-stats').innerHTML = `
    <div class="card"><div class="card-title">Totaal trades</div><div class="stat-val neu" style="font-size:24px">${s.total_trades||0}</div></div>
    <div class="card"><div class="card-title">Win rate</div><div class="stat-val" style="font-size:24px;color:${wr>=0.5?'var(--green)':'var(--red)'}">${(wr*100).toFixed(0)}%</div></div>
    <div class="card"><div class="card-title">Totaal PnL</div><div class="stat-val" style="font-size:24px;color:${(s.total_pnl||0)>=0?'var(--green)':'var(--red)'}">${(s.total_pnl>=0?'+':'')+(s.total_pnl||0).toFixed(4)}</div></div>
    <div class="card"><div class="card-title">Actieve drempels</div><div class="stat-val neu" style="font-size:24px">${Object.keys(s.thresholds||{}).length}</div></div>
  `;

  const evals = d.evaluations || {};
  const tbody = document.getElementById('mem-thresholds');
  const coins = ['BTC_USDT','ETH_USDT','XRP_USDT','FARTCOIN_USDT','ADA_USDT'];
  tbody.innerHTML = coins.map(sym => {
    const ev = evals[sym] || {};
    const thresh = (d.stats?.thresholds||{})[sym] || 0.55;
    const wr2 = ev.win_rate || 0;
    const action = ev.action || '—';
    const actionCol = action==='drempel_verhoogd'?'var(--yellow)':action==='drempel_verlaagd'?'var(--green)':'var(--text3)';
    return `<tr>
      <td><strong>${sym.replace('_USDT','')}</strong></td>
      <td>${ev.trades||0}</td>
      <td class="${wr2>=0.5?'up':'down'}">${(wr2*100).toFixed(0)}%</td>
      <td class="${(ev.avg_pnl||0)>=0?'up':'down'}">${(ev.avg_pnl>=0?'+':'')+(ev.avg_pnl||0).toFixed(4)}</td>
      <td style="color:${(ev.sl_ratio||0)>0.6?'var(--red)':'var(--text2)'}">${((ev.sl_ratio||0)*100).toFixed(0)}%</td>
      <td style="color:var(--text3)">0.550</td>
      <td style="font-weight:500;color:var(--blue)">${thresh.toFixed(3)}</td>
      <td style="color:${actionCol};font-size:11px">${action.replace('_',' ')}</td>
    </tr>`;
  }).join('');
}

async function loadMemoryTrades() {
  const sym = document.getElementById('mem-sym-filter').value;
  const url = '/api/memory/trades' + (sym ? '?symbol='+sym : '');
  const r = await fetch(url);
  const d = await r.json();
  const trades = d.trades || [];
  document.getElementById('mem-trades').innerHTML = trades.slice(0,30).map((t,i) => {
    const snap = t.snapshot || {};
    const rsi = snap.rsi ? snap.rsi.toFixed(1) : '—';
    const adx = snap.adx ? snap.adx.toFixed(1) : '—';
    const regime = snap.regime || t.market_regime || '—';
    const conf = t.confidence ? (t.confidence*100).toFixed(0)+'%' : '—';
    const ep = (t.entry_price||0)<1?(t.entry_price||0).toFixed(6):(t.entry_price||0).toFixed(2);
    const xp = t.exit_price?((t.exit_price<1)?t.exit_price.toFixed(6):t.exit_price.toFixed(2)):'open';
    const pnl = t.pnl || 0;
    const reason = t.exit_reason || 'open';
    const regColor = regime==='trending'?'var(--green)':regime==='volatile'?'var(--yellow)':'var(--text3)';
    return `<tr>
      <td style="font-size:11px">${(t.symbol||'').replace('_USDT','')}</td>
      <td><span class="badge badge-${t.direction}">${(t.direction||'').toUpperCase()}</span></td>
      <td style="font-size:11px">${(t.entry_ts||'').substring(0,16)}</td>
      <td style="font-size:11px;color:var(--text3)">${typeof xp==='string'&&xp!=='open'?'$'+xp:xp}</td>
      <td class="${pnl>=0?'up':'down'}" style="font-weight:500">${pnl>=0?'+':''}${pnl.toFixed(4)}</td>
      <td><span class="badge badge-${(reason||'').toLowerCase()}">${reason}</span></td>
      <td style="color:var(--blue)">${conf}</td>
      <td style="color:${parseFloat(rsi)<30||parseFloat(rsi)>70?'var(--yellow)':'var(--text2)'}">${rsi}</td>
      <td style="color:${parseFloat(adx)>25?'var(--green)':'var(--text3)'}">${adx}</td>
      <td style="color:${regColor};font-size:11px">${regime}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="10" class="empty">Nog geen trades opgeslagen</td></tr>';
}

async function loadMemoryEvals() {
  const r = await fetch('/api/memory/evaluations');
  const d = await r.json();
  const evals = d.evaluations || [];
  const log = document.getElementById('mem-eval-log');
  if(!evals.length) {
    log.innerHTML = '<span class="log-info">Nog geen drempelwijzigingen. Minimaal 10 trades nodig per coin.</span>';
    return;
  }
  log.innerHTML = evals.map(ev => {
    const ts = (ev.eval_ts||'').substring(0,16);
    const sym = (ev.symbol||'').replace('_USDT','');
    const delta = ((ev.new_threshold||0.55)-(ev.old_threshold||0.55));
    const cls = ev.action==='drempel_verhoogd'?'log-warn':ev.action==='drempel_verlaagd'?'log-ok':'log-info';
    const arrow = delta>0?'↑':'↓';
    return `<div class="${cls}">[${ts}] ${sym}: ${ev.action?.replace('_',' ')} ${arrow} ${(ev.old_threshold||0).toFixed(3)}→${(ev.new_threshold||0).toFixed(3)} (winrate=${((ev.win_rate||0)*100).toFixed(0)}%, ${ev.trades_count||0} trades) ${ev.notes||''}</div>`;
  }).join('');
}

window.addEventListener('DOMContentLoaded',()=>{
  loadTickers();
  loadPriceChart('BTC_USDT','chart-btc','#007aff');
  loadPriceChart('ETH_USDT','chart-eth','#34c759');
  pollStatus();
  loadBalanceOverview();
  setInterval(loadTickers, 30000);
  setInterval(loadBalanceOverview, 15000);  // elke 15 seconden vernieuwen
});
</script>
</body>
</html>
"""



@app.route('/')
def index():
    return HTML_PAGE

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
