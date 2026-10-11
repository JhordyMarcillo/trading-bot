"""Servidor web: /health para UptimeRobot y / con el dashboard de operaciones."""
import html
import json
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import requests

import trader

CACHE_SECONDS = 30
_cache = {"t": 0.0, "data": None}
_lock = threading.Lock()


def get_data(cfg):
    with _lock:
        if _cache["data"] and time.time() - _cache["t"] < CACHE_SECONDS:
            return _cache["data"]
        data = _build(cfg)
        _cache.update(t=time.time(), data=data)
        return data


def _build(cfg):
    t = cfg.get("trading", {})
    data = {
        "enabled": bool(t.get("enabled") and trader.enabled()),
        "hold": [], "swing": [], "trades": [],
        "hold_budget": t.get("hold", {}).get("max_total_usdt"),
        "swing_budget": t.get("swing", {}).get("budget_usdt"),
        "realized": 0.0, "sells": 0, "wins": 0, "buys": {"hold": 0, "swing": 0},
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }
    if not data["enabled"]:
        return data
    symbols = sorted(set(t.get("hold", {}).get("symbols", [])) | set(t.get("swing", {}).get("symbols", [])))
    prices = {
        x["symbol"]: float(x["price"])
        for x in requests.get(
            cfg["api_base_url"] + "/api/v3/ticker/price",
            params={"symbols": json.dumps(symbols, separators=(",", ":"))}, timeout=15,
        ).json()
    }
    for symbol in symbols:
        orders = trader.bot_orders(symbol)
        for kind in ("hold", "swing"):
            s = trader.summarize(symbol, kind, orders)
            data["trades"] += s["rows"]
            data["buys"][kind] += s["buys"]
            if kind == "swing":
                data["realized"] += float(s["realized"])
                data["sells"] += s["sells"]
                data["wins"] += s["wins"]
            if s["qty"] > 0:
                value = float(s["qty"]) * prices[symbol]
                cost = float(s["cost"])
                data[kind].append({
                    "symbol": symbol, "qty": float(s["qty"]), "cost": cost, "avg": cost / float(s["qty"]),
                    "price": prices[symbol], "value": value, "unrealized": value - cost, "buys": s["buys"],
                })
    data["trades"].sort(key=lambda r: r["time"], reverse=True)
    return data


def _money(x):
    return f"{x:+,.2f}" if x else "0.00"


def _cls(x):
    return "pos" if x and x > 0 else "neg" if x and x < 0 else ""


def render(data):
    e = html.escape
    if not data["enabled"]:
        body = "<p class='note'>El trading en testnet no está activo (faltan claves o está desactivado en config.json).</p>"
    else:
        def tot(kind, key):
            return sum(p[key] for p in data[kind])

        h_inv, h_val = tot("hold", "cost"), tot("hold", "value")
        s_unreal = tot("swing", "unrealized")
        win = f"{data['wins']}/{data['sells']}" if data["sells"] else "—"
        cards = (
            f"<div class='card'><span>🌱 Largo plazo: invertido</span><b>{h_inv:,.2f} USDT</b></div>"
            f"<div class='card'><span>🌱 Largo plazo: valor / resultado</span><b class='{_cls(h_val - h_inv)}'>{h_val:,.2f} ({_money(h_val - h_inv)})</b></div>"
            f"<div class='card'><span>⚡ Corto plazo: realizada</span><b class='{_cls(data['realized'])}'>{_money(data['realized'])} USDT</b></div>"
            f"<div class='card'><span>⚡ Corto plazo: no realizada</span><b class='{_cls(s_unreal)}'>{_money(s_unreal)} USDT</b></div>"
            f"<div class='card'><span>Compras largo / corto</span><b>{data['buys']['hold']} / {data['buys']['swing']}</b></div>"
            f"<div class='card'><span>Ventas ganadoras (corto)</span><b>{win}</b></div>"
        )

        def table(kind, empty):
            rows = "".join(
                f"<tr><td>{e(p['symbol'])}</td><td>{p['qty']:.6f}</td><td>{p['buys']}</td><td>{p['cost']:,.2f}</td>"
                f"<td>{p['avg']:,.4f}</td><td>{p['price']:,.4f}</td><td>{p['value']:,.2f}</td>"
                f"<td class='{_cls(p['unrealized'])}'>{_money(p['unrealized'])}</td></tr>"
                for p in data[kind]
            ) or f"<tr><td colspan='8' class='note'>{empty}</td></tr>"
            return (
                "<div class='scroll'><table><tr><th>Activo</th><th>Cantidad</th><th>Compras</th><th>Costo</th>"
                f"<th>Precio medio</th><th>Precio actual</th><th>Valor</th><th>No realizada</th></tr>{rows}</table></div>"
            )

        names = {"hold": "🌱 Largo", "swing": "⚡ Corto"}
        trades = "".join(
            f"<tr><td>{datetime.fromtimestamp(r['time'] / 1000, timezone.utc):%Y-%m-%d %H:%M}</td>"
            f"<td>{e(r['symbol'])}</td><td>{names[r['kind']]}</td>"
            f"<td class='{'pos' if r['side'] == 'BUY' else 'neg'}'>{'COMPRA' if r['side'] == 'BUY' else 'VENTA'}</td>"
            f"<td>{r['qty']:.6f}</td><td>{r['price']:,.4f}</td><td>{r['quote']:,.2f}</td>"
            f"<td class='{_cls(r['pnl'])}'>{_money(r['pnl']) if r['pnl'] is not None else '—'}</td></tr>"
            for r in data["trades"][:200]
        ) or "<tr><td colspan='8' class='note'>Aún no hay operaciones</td></tr>"
        body = (
            f"<div class='cards'>{cards}</div>"
            f"<h2>🌱 Cartera de largo plazo <small>(compras periódicas, nunca se vende · tope {data['hold_budget']} USDT)</small></h2>"
            + table("hold", "Aún no hay compras de largo plazo")
            + f"<h2>⚡ Cartera de corto plazo <small>(compra y venta por cruce de medias · presupuesto {data['swing_budget']} USDT)</small></h2>"
            + table("swing", "Sin posiciones abiertas")
            + "<h2>Historial de operaciones <small>(últimas 200)</small></h2><div class='scroll'><table><tr><th>Fecha</th><th>Par</th>"
            f"<th>Cartera</th><th>Tipo</th><th>Cantidad</th><th>Precio</th><th>USDT</th><th>Resultado</th></tr>{trades}</table></div>"
        )
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60"><title>Monitor de trading</title>
<style>
:root{{--bg:#f6f7f9;--fg:#1c1f26;--card:#fff;--line:#e3e6eb;--mut:#69707d;--pos:#0a8a4a;--neg:#c0392b}}
@media(prefers-color-scheme:dark){{:root{{--bg:#14161a;--fg:#e8eaee;--card:#1d2026;--line:#2c3038;--mut:#9aa1ad;--pos:#3ecf8e;--neg:#ff6b5e}}}}
body{{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:15px system-ui,sans-serif}}
main{{max-width:960px;margin:0 auto}}h1{{font-size:20px;margin:0}}h2{{font-size:16px;margin:24px 0 8px}}
.sub{{color:var(--mut);font-size:13px;margin:4px 0 16px}}.badge{{background:#e8a317;color:#000;border-radius:4px;padding:1px 6px;font-size:12px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px}}
.card span{{display:block;color:var(--mut);font-size:12px}}.card b{{font-size:18px}}
.scroll{{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:8px}}
table{{width:100%;border-collapse:collapse;white-space:nowrap}}th,td{{padding:8px 12px;text-align:right;border-bottom:1px solid var(--line)}}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){{text-align:left}}th{{color:var(--mut);font-weight:500;font-size:12px}}
h2 small{{color:var(--mut);font-weight:400;font-size:12px}}.pos{{color:var(--pos)}}.neg{{color:var(--neg)}}.note{{color:var(--mut)}}
</style></head><body><main>
<h1>Monitor de trading <span class="badge">TESTNET · dinero ficticio</span></h1>
<p class="sub">Actualizado {e(data['updated'])} · se recarga solo cada 60 s</p>
{body}</main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, ctype, body=b""):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _route(self):
        url = urlparse(self.path)
        if url.path == "/health":
            return self._send(200, "text/plain", b"ok")
        needed = os.environ.get("DASHBOARD_KEY")
        if needed and parse_qs(url.query).get("key", [""])[0] != needed:
            return self._send(401, "text/plain; charset=utf-8", "Acceso no autorizado".encode())
        if self.command == "HEAD":
            return self._send(200, "text/html")
        try:
            data = get_data(self.server.cfg)
        except Exception as e:  # noqa: BLE001
            return self._send(500, "text/plain; charset=utf-8", f"Error: {e}".encode())
        if url.path == "/api/trades":
            return self._send(200, "application/json", json.dumps(data).encode())
        self._send(200, "text/html; charset=utf-8", render(data).encode())

    do_GET = do_HEAD = _route

    def log_message(self, *args):
        pass


def start_server(port, cfg):
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.cfg = cfg
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Dashboard en el puerto {port} (/health para UptimeRobot)")
