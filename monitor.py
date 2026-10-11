"""Monitor de precios de Binance con alertas por Telegram.

Solo usa datos públicos de mercado: no necesita claves de Binance ni puede operar.
"""
import json
import os
import sys
import time
from pathlib import Path

import requests

import dashboard
import trader

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "state.json"


def load_env(path=BASE_DIR / ".env"):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print(f"[sin Telegram] {text}")
        return
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=15,
    )
    if not r.ok:
        print(f"Error de Telegram: {r.status_code} {r.text}")


def get_json(base_url, path, params):
    r = requests.get(base_url + path, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def sma(values, n):
    return sum(values[-n:]) / n


def check_symbol(cfg, symbol, state, now):
    base = cfg["api_base_url"]
    cooldown = cfg["cooldown_minutes"] * 60
    alerts = []

    def fire(key, message):
        if now - state.get(key, 0) >= cooldown:
            state[key] = now
            alerts.append(message)

    t = get_json(base, "/api/v3/ticker/24hr", {"symbol": symbol})
    price = float(t["lastPrice"])
    change = float(t["priceChangePercent"])
    print(f"{symbol}: {price:,.2f} ({change:+.2f}% 24h)")

    drop = cfg["drop_24h_percent"]
    rise = cfg["rise_24h_percent"]
    if change <= -drop:
        fire(f"{symbol}:drop", f"🔻 {symbol} cayó {change:.2f}% en 24h\nPrecio: {price:,.2f}")
    if change >= rise:
        fire(f"{symbol}:rise", f"🔺 {symbol} subió {change:.2f}% en 24h\nPrecio: {price:,.2f}")

    levels = cfg.get("price_levels", {}).get(symbol, {})
    if "above" in levels and price >= levels["above"]:
        fire(f"{symbol}:above", f"🎯 {symbol} superó {levels['above']:,}\nPrecio: {price:,.2f}")
    if "below" in levels and price <= levels["below"]:
        fire(f"{symbol}:below", f"🎯 {symbol} bajó de {levels['below']:,}\nPrecio: {price:,.2f}")

    fast, slow = cfg["sma_fast"], cfg["sma_slow"]
    klines = get_json(
        base, "/api/v3/klines",
        {"symbol": symbol, "interval": cfg["sma_interval"], "limit": slow + 2},
    )
    closes = [float(k[4]) for k in klines[:-1]]  # descarta la vela aún abierta
    if len(closes) >= slow + 1:
        prev_diff = sma(closes[:-1], fast) - sma(closes[:-1], slow)
        diff = sma(closes, fast) - sma(closes, slow)
        if prev_diff <= 0 < diff:
            fire(f"{symbol}:golden", f"📈 {symbol}: SMA{fast} cruzó por encima de SMA{slow} ({cfg['sma_interval']})\nPrecio: {price:,.2f}")
        elif prev_diff >= 0 > diff:
            fire(f"{symbol}:death", f"📉 {symbol}: SMA{fast} cruzó por debajo de SMA{slow} ({cfg['sma_interval']})\nPrecio: {price:,.2f}")

    return alerts


def swing_signal(cfg, symbol):
    """Cruce de medias de la cartera de corto plazo: 'buy', 'sell' o None."""
    s = cfg["trading"]["swing"]
    klines = get_json(
        cfg["api_base_url"], "/api/v3/klines",
        {"symbol": symbol, "interval": s["sma_interval"], "limit": s["sma_slow"] + 2},
    )
    closes = [float(k[4]) for k in klines[:-1]]
    if len(closes) < s["sma_slow"] + 1:
        return None
    prev_diff = sma(closes[:-1], s["sma_fast"]) - sma(closes[:-1], s["sma_slow"])
    diff = sma(closes, s["sma_fast"]) - sma(closes, s["sma_slow"])
    if prev_diff <= 0 < diff:
        return "buy"
    if prev_diff >= 0 > diff:
        return "sell"
    return None


def run_swing(cfg):
    """Corto plazo: compra y vende según cruces. Las operaciones son idempotentes:
    no compra si ya hay posición y no vende si no la hay."""
    s = cfg["trading"]["swing"]
    for symbol in s["symbols"]:
        try:
            signal = swing_signal(cfg, symbol)
            if not signal:
                continue
            position = trader.summarize(symbol, "swing")
            if signal == "buy":
                if position["qty"] > 0:
                    continue
                open_cost = sum(trader.summarize(x, "swing")["cost"] for x in s["symbols"])
                if open_cost + s["order_usdt"] > s["budget_usdt"]:
                    send_telegram(f"🧪 [CORTO] Compra de {symbol} omitida: superaría el presupuesto de {s['budget_usdt']} USDT.")
                elif trader.usdt_balance() < s["order_usdt"]:
                    send_telegram(f"🧪 [CORTO] Sin USDT suficiente para comprar {symbol}.")
                else:
                    send_telegram("⚡ [CORTO] " + trader.market_buy(symbol, s["order_usdt"], "swing"))
            elif position["qty"] > 0:
                send_telegram("⚡ [CORTO] " + trader.market_sell(symbol, position["qty"]))
        except Exception as e:  # noqa: BLE001 - un fallo en un par no debe frenar a los demás
            send_telegram(f"⚠️ Error en corto plazo {symbol}: {e}")


def run_hold(cfg, state):
    """Largo plazo: compra periódica (DCA) de varios activos que nunca se venden.
    La fecha de la última compra se lee del historial de Binance, no de un archivo."""
    h = cfg["trading"]["hold"]
    interval = h["interval_hours"] * 3600
    try:
        invested = sum(trader.summarize(x, "hold")["cost"] for x in h["symbols"])
        lines = []
        for symbol in h["symbols"]:
            last = trader.summarize(symbol, "hold")["last_buy"] / 1000
            if time.time() - last < interval:
                continue
            if invested + h["amount_usdt"] > h["max_total_usdt"]:
                if time.time() - state.get("hold:cap_notice", 0) > 86400:  # un aviso al día como máximo
                    state["hold:cap_notice"] = time.time()
                    send_telegram(f"🧪 [LARGO] Compras pausadas: se alcanzó el tope de {h['max_total_usdt']} USDT.")
                break
            if trader.usdt_balance() < h["amount_usdt"]:
                send_telegram("🧪 [LARGO] Sin USDT suficiente para seguir comprando.")
                break
            lines.append(trader.market_buy(symbol, h["amount_usdt"], "hold").replace("🧪 [TESTNET] COMPRA ", ""))
            invested += h["amount_usdt"]
        if lines:
            send_telegram("🌱 [LARGO] Compras periódicas:\n" + "\n".join(lines))
    except Exception as e:  # noqa: BLE001
        send_telegram(f"⚠️ Error en largo plazo: {e}")


def run_once(cfg, state):
    now = time.time()
    for symbol in cfg["symbols"]:
        try:
            for message in check_symbol(cfg, symbol, state, now):
                send_telegram(message)
        except requests.RequestException as e:
            print(f"Error consultando {symbol}: {e}")
    t = cfg.get("trading", {})
    if t.get("enabled") and trader.enabled():
        if t.get("hold", {}).get("enabled"):
            run_hold(cfg, state)
        if t.get("swing", {}).get("enabled"):
            run_swing(cfg)
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def main():
    load_env()
    cfg = load_json(CONFIG_FILE, None)
    if cfg is None:
        sys.exit("Falta config.json")
    state = load_json(STATE_FILE, {})
    trader.set_since(cfg.get("trading", {}).get("since"))

    if "--test" in sys.argv:
        send_telegram("✅ Monitor de precios conectado correctamente.")
        return

    if "--test-order" in sys.argv:
        if not trader.enabled():
            sys.exit("Faltan BINANCE_TESTNET_KEY / BINANCE_TESTNET_SECRET en .env")
        try:
            print(f"Saldo USDT de prueba: {trader.usdt_balance()}")
            for line in trader.test_roundtrip("BTCUSDT", 20):
                print(line)
                send_telegram(line)
        except Exception as e:  # noqa: BLE001
            sys.exit(f"Error en la prueba: {e}")
        return

    once = "--once" in sys.argv
    if os.environ.get("PORT") and not once:
        dashboard.start_server(int(os.environ["PORT"]), cfg)
    while True:
        run_once(cfg, state)
        if once:
            break
        time.sleep(cfg["check_interval_seconds"])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nDetenido.")
