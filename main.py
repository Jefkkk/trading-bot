# -*- coding: utf-8 -*-
"""
Jef Bot  --  Gate.io Crypto Trading Bot
Entry point voor deployment (Railway / Render / VPS)

Start: python main.py
Of:    gunicorn main:app --bind 0.0.0.0:$PORT
"""

import os
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log', encoding='utf-8'),
    ]
)
logger = logging.getLogger('JefBot')

# Database initialiseren
try:
    from trade_memory import init_db
    init_db()
    logger.info("Trade memory database OK")
except Exception as e:
    logger.warning(f"Trade memory init mislukt (niet kritisch): {e}")

# Flask app importeren (bevat dashboard + bot controls + API)
from web_server import app

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    logger.info(f"Jef Bot dashboard start op poort {port}")
    logger.info(f"Open http://localhost:{port} in je browser")
    app.run(host='0.0.0.0', port=port, debug=False)
