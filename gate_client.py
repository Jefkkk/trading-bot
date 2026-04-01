# -*- coding: utf-8 -*-
import hmac
import hashlib
import time
import json
import aiohttp
import logging
from typing import Optional

logger = logging.getLogger('GateClient')
BASE_URL = "https://api.gateio.ws"


class GateFuturesClient:
    def __init__(self, api_key: str, api_secret: str, settle: str = "usdt"):
        self.api_key    = api_key
        self.api_secret = api_secret
        self.settle     = settle
        self.session: Optional[aiohttp.ClientSession] = None
        self.last_error: str = ''

    def _sign(self, method: str, path: str, query: str = "", body: str = "") -> dict:
        ts        = str(int(time.time()))
        body_hash = hashlib.sha512(body.encode('utf-8')).hexdigest()
        sign_str  = f"{method}\n{path}\n{query}\n{body_hash}\n{ts}"
        sig = hmac.new(
            self.api_secret.encode('utf-8'),
            sign_str.encode('utf-8'),
            hashlib.sha512
        ).hexdigest()
        return {
            "KEY": self.api_key, "Timestamp": ts, "SIGN": sig,
            "Content-Type": "application/json", "Accept": "application/json",
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20)
            )
        return self.session

    async def _request(self, method: str, endpoint: str,
                       params: dict = None, data: dict = None) -> Optional[dict]:
        session = await self._get_session()
        path    = f"/api/v4/futures/{self.settle}{endpoint}"
        query   = "&".join(f"{k}={v}" for k, v in (params or {}).items())
        body    = json.dumps(data) if data else ""
        headers = self._sign(method.upper(), path, query, body)
        url     = f"{BASE_URL}{path}" + (f"?{query}" if query else "")
        try:
            async with session.request(
                method, url, headers=headers, data=body or None
            ) as resp:
                text = await resp.text()
                if resp.status not in (200, 201):
                    self.last_error = text[:400]
                    logger.error(f"API {resp.status} [{endpoint}]: {text[:400]}")
                    return None
                self.last_error = ""
                return json.loads(text)
        except Exception as e:
            self.last_error = str(e)
            logger.error(f"Request fout [{endpoint}]: {e}")
            return None

    # -- Marktdata (geen auth nodig) -------------------------------------------

    async def get_candles(self, contract: str, interval: str = "5m",
                          limit: int = 200) -> list:
        session = await self._get_session()
        url = f"{BASE_URL}/api/v4/futures/{self.settle}/candlesticks"
        try:
            async with session.get(
                url, params={"contract": contract, "interval": interval, "limit": limit}
            ) as resp:
                return await resp.json() if resp.status == 200 else []
        except Exception as e:
            logger.error(f"Candles [{contract}]: {e}")
            return []

    async def get_ticker(self, contract: str) -> Optional[dict]:
        session = await self._get_session()
        url = f"{BASE_URL}/api/v4/futures/{self.settle}/tickers"
        try:
            async with session.get(url, params={"contract": contract}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data[0] if data else None
        except Exception as e:
            logger.error(f"Ticker [{contract}]: {e}")
        return None

    # -- Account ---------------------------------------------------------------

    async def get_account(self) -> Optional[dict]:
        return await self._request("GET", "/accounts")

    async def get_positions(self) -> list:
        result = await self._request("GET", "/positions")
        return result or []

    async def get_position(self, contract: str) -> Optional[dict]:
        return await self._request("GET", f"/positions/{contract}")

    # -- Leverage --------------------------------------------------------------

    async def set_leverage(self, contract: str, leverage: int) -> bool:
        """
        Isolated margin leverage instellen voor een contract.
        Gate.io vereist leverage als string, cross_leverage_limit=0 voor isolated.
        Leverage moet VOOR de order ingesteld worden.
        """
        result = await self._request(
            "POST", f"/positions/{contract}/leverage",
            data={"leverage": str(leverage), "cross_leverage_limit": "0"}
        )
        if result is not None:
            actual = result.get("leverage", "?")
            logger.info(f"Leverage [{contract}]: {actual}x (gevraagd: {leverage}x)")
            return True
        logger.error(f"Leverage instellen MISLUKT [{contract}]")
        return False

    # -- Orders ----------------------------------------------------------------

    async def place_order(self, contract: str, size: int,
                          price: float = 0,
                          reduce_only: bool = False) -> Optional[dict]:
        """
        Gate.io USDT perpetual:
          size > 0  = LONG (kopen)
          size < 0  = SHORT (verkopen)
          price = 0 = IOC market order
        """
        data = {
            "contract":    contract,
            "size":        size,
            "price":       "0" if price == 0 else str(price),
            "tif":         "ioc" if price == 0 else "gtc",
            "reduce_only": reduce_only,
        }
        result = await self._request("POST", "/orders", data=data)
        if result:
            logger.info(
                f"Order OK [{contract}]: size={size} "
                f"reduce_only={reduce_only} id={result.get('id')}"
            )
        return result

    async def cancel_all_orders(self, contract: str):
        return await self._request(
            "DELETE", "/orders", params={"contract": contract}
        )

    # -- Price-triggered orders  --  echte SL/TP op de exchange ------------------
    # Gate.io /futures/usdt/price_orders
    # rule: 1 = prijs >= trigger (stijgt tot), 2 = prijs <= trigger (daalt tot)

    async def place_stop_loss(self, contract: str, is_long: bool,
                              trigger_price: float, contracts: int) -> Optional[dict]:
        """
        Stop Loss:
          Long positie  → sluit (sell) wanneer prijs DAALT TOT trigger  → rule 2
          Short positie → sluit (buy)  wanneer prijs STIJGT TOT trigger → rule 1
        """
        close_size = -abs(contracts) if is_long else abs(contracts)
        result = await self._request("POST", "/price_orders", data={
            "initial": {
                "contract": contract, "size": close_size,
                "price": "0", "tif": "ioc", "reduce_only": True,
            },
            "trigger": {
                "strategy_type": 0,
                "price_type":    0,
                "price":         str(round(trigger_price, 8)),
                "rule":          2 if is_long else 1,
                "expiration":    86400,
            },
        })
        if result:
            logger.info(f"SL geplaatst [{contract}]: trigger={trigger_price:.6f} id={result.get('id')}")
        else:
            logger.error(f"SL MISLUKT [{contract}]: trigger={trigger_price:.6f}")
        return result

    async def place_take_profit(self, contract: str, is_long: bool,
                                trigger_price: float, contracts: int) -> Optional[dict]:
        """
        Take Profit:
          Long positie  → sluit (sell) wanneer prijs STIJGT TOT trigger → rule 1
          Short positie → sluit (buy)  wanneer prijs DAALT TOT trigger  → rule 2
        """
        close_size = -abs(contracts) if is_long else abs(contracts)
        result = await self._request("POST", "/price_orders", data={
            "initial": {
                "contract": contract, "size": close_size,
                "price": "0", "tif": "ioc", "reduce_only": True,
            },
            "trigger": {
                "strategy_type": 0,
                "price_type":    0,
                "price":         str(round(trigger_price, 8)),
                "rule":          1 if is_long else 2,
                "expiration":    86400,
            },
        })
        if result:
            logger.info(f"TP geplaatst [{contract}]: trigger={trigger_price:.6f} id={result.get('id')}")
        else:
            logger.error(f"TP MISLUKT [{contract}]: trigger={trigger_price:.6f}")
        return result

    async def cancel_all_price_orders(self, contract: str):
        """Annuleer alle open SL/TP trigger orders voor dit contract."""
        return await self._request(
            "DELETE", "/price_orders",
            params={"contract": contract, "status": "open"}
        )

    async def get_price_orders(self, contract: str) -> list:
        result = await self._request(
            "GET", "/price_orders",
            params={"contract": contract, "status": "open"}
        )
        return result or []

    async def get_funding_rate(self, contract: str) -> Optional[dict]:
        """Haal huidige funding rate op. Rate > 0 = longs betalen, < 0 = shorts betalen."""
        session = await self._get_session()
        url = f"{BASE_URL}/api/v4/futures/{self.settle}/contracts/{contract}"
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return {
                        'funding_rate':      float(data.get('funding_rate', 0)),
                        'funding_next_apply': int(data.get('funding_next_apply', 0)),
                        'mark_price':        float(data.get('mark_price', 0)),
                        'index_price':       float(data.get('index_price', 0)),
                    }
        except Exception as e:
            logger.error(f"Funding rate [{contract}]: {e}")
        return None

    async def reduce_position(self, contract: str, reduce_pct: float = 50) -> Optional[dict]:
        """Verklein een open positie met X%. Gebruikt voor liquidatie-bescherming."""
        pos = await self.get_position(contract)
        if not pos or int(pos.get('size', 0)) == 0:
            return None
        size = int(pos['size'])
        reduce_size = -int(abs(size) * reduce_pct / 100)
        if size < 0:
            reduce_size = abs(reduce_size)
        if reduce_size == 0:
            return None
        logger.warning(f"REDUCE [{contract}]: {reduce_pct}% ({reduce_size} contracts)")
        return await self.place_order(contract, reduce_size, reduce_only=True)

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
