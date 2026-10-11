"""Operaciones en la TESTNET de Binance (dinero ficticio).

La URL está fija a propósito: este módulo no puede operar con dinero real.
Las claves se crean en https://testnet.binance.vision

Todas las órdenes del bot llevan el prefijo "bot_" en su clientOrderId. La posición
del bot se reconstruye leyendo ese historial, así que no depende de archivos locales
y nunca toca monedas que hayas comprado por tu cuenta.
"""
import hashlib
import hmac
import os
import time
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from urllib.parse import urlencode

import requests

TESTNET_URL = "https://testnet.binance.vision"
PREFIX = "bot_"
since_ms = 0  # se ignoran las órdenes anteriores a esta fecha (config: trading.since)


def set_since(iso_date):
    """Permite empezar de cero sin borrar el historial de la testnet."""
    global since_ms
    since_ms = int(datetime.fromisoformat(iso_date).timestamp() * 1000) if iso_date else 0


def enabled():
    return bool(os.environ.get("BINANCE_TESTNET_KEY") and os.environ.get("BINANCE_TESTNET_SECRET"))


def _signed(method, path, params=None):
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 10000
    query = urlencode(params)
    sig = hmac.new(os.environ["BINANCE_TESTNET_SECRET"].encode(), query.encode(), hashlib.sha256).hexdigest()
    r = requests.request(
        method, f"{TESTNET_URL}{path}?{query}&signature={sig}",
        headers={"X-MBX-APIKEY": os.environ["BINANCE_TESTNET_KEY"]}, timeout=15,
    )
    if not r.ok:
        raise RuntimeError(f"Binance testnet {r.status_code}: {r.text}")
    return r.json()


def _free_balance(asset):
    account = _signed("GET", "/api/v3/account")
    for b in account["balances"]:
        if b["asset"] == asset:
            return Decimal(b["free"])
    return Decimal(0)


def _step_size(symbol):
    r = requests.get(f"{TESTNET_URL}/api/v3/exchangeInfo", params={"symbol": symbol}, timeout=15)
    r.raise_for_status()
    info = r.json()["symbols"][0]
    for f in info["filters"]:
        if f["filterType"] == "LOT_SIZE":
            return Decimal(f["stepSize"]), info["baseAsset"]
    raise RuntimeError("No se encontró LOT_SIZE")


def kind_of(order):
    """'hold' (largo plazo) si el id lleva la marca h; el resto es 'swing' (corto plazo)."""
    return "hold" if order["clientOrderId"][len(PREFIX):].startswith("h") else "swing"


def _order(symbol, side, kind="swing", **extra):
    tag = "h" if kind == "hold" else "s"
    params = {
        "symbol": symbol, "side": side, "type": "MARKET",
        "newClientOrderId": f"{PREFIX}{tag}{int(time.time() * 1000)}_{side[0]}",
    }
    params.update(extra)
    return _signed("POST", "/api/v3/order", params)


def usdt_balance():
    return _free_balance("USDT")


def bot_orders(symbol):
    """Órdenes ejecutadas del bot (y solo del bot) para un par, de más antigua a más nueva."""
    rows = _signed("GET", "/api/v3/allOrders", {"symbol": symbol, "limit": 1000})
    mine = [
        o for o in rows
        if o["time"] >= since_ms
        and o.get("clientOrderId", "").startswith(PREFIX)
        and o["status"] == "FILLED" and Decimal(o["executedQty"]) > 0
    ]
    return sorted(mine, key=lambda o: o["time"])


def summarize(symbol, kind="swing", orders=None):
    """Posición abierta, costo y ganancia realizada de una cartera del bot en un par."""
    qty = Decimal(0)
    cost = Decimal(0)
    realized = Decimal(0)
    sells = wins = 0
    rows = []
    buys = 0
    last_buy = 0
    for o in (bot_orders(symbol) if orders is None else orders):
        if kind_of(o) != kind:
            continue
        eq = Decimal(o["executedQty"])
        quote = Decimal(o["cummulativeQuoteQty"])
        row = {
            "time": o["time"], "symbol": symbol, "side": o["side"], "kind": kind,
            "qty": float(eq), "price": float(quote / eq), "quote": float(quote), "pnl": None,
        }
        if o["side"] == "BUY":
            buys += 1
            last_buy = max(last_buy, o["time"])
        if o["side"] == "BUY":
            qty += eq
            cost += quote
        else:
            if qty > 0:
                sold_cost = cost if eq >= qty * Decimal("0.99") else cost * eq / qty
                pnl = quote - sold_cost
                realized += pnl
                sells += 1
                wins += pnl > 0
                row["pnl"] = float(pnl)
                cost -= sold_cost
                qty = Decimal(0) if eq >= qty * Decimal("0.99") else qty - eq
            if qty == 0:
                cost = Decimal(0)
        rows.append(row)
    return {
        "qty": qty, "cost": cost, "realized": realized, "sells": sells, "wins": wins,
        "rows": rows, "buys": buys, "last_buy": last_buy,
    }


def market_buy(symbol, usdt_amount, kind="swing"):
    order = _order(symbol, "BUY", kind, quoteOrderQty=str(usdt_amount))
    return f"🧪 [TESTNET] COMPRA {symbol}: {order['executedQty']} por {order['cummulativeQuoteQty']} USDT"


def market_sell(symbol, qty):
    """Vende hasta `qty` (la posición del bot), nunca más de lo que hay libre."""
    step, base = _step_size(symbol)
    qty = min(Decimal(qty), _free_balance(base))
    qty = ((qty // step) * step).quantize(step, rounding=ROUND_DOWN)
    if qty <= 0:
        return f"🧪 [TESTNET] Señal de venta en {symbol}, pero no hay cantidad para vender."
    order = _order(symbol, "SELL", quantity=format(qty, "f"))
    return f"🧪 [TESTNET] VENTA {symbol}: {order['executedQty']} por {order['cummulativeQuoteQty']} USDT"


def test_roundtrip(symbol, usdt_amount):
    """Compra una cantidad pequeña y vende exactamente lo comprado."""
    buy = _order(symbol, "BUY", quoteOrderQty=str(usdt_amount))
    lines = [f"🧪 [TESTNET] COMPRA {symbol}: {buy['executedQty']} por {buy['cummulativeQuoteQty']} USDT"]
    lines.append(market_sell(symbol, buy["executedQty"]))
    return lines
