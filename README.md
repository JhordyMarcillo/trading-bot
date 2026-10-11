# Monitor de precios Binance → Telegram

Lee precios públicos de Binance y te avisa por Telegram. No usa claves de Binance ni opera.

## Alertas
- Caída o subida fuerte en 24h (`drop_24h_percent`, `rise_24h_percent`)
- Precio sobre/bajo un nivel que tú fijas (`price_levels`)
- Cruce de medias móviles SMA rápida/lenta (`sma_fast`, `sma_slow`)
- Cada alerta tiene enfriamiento (`cooldown_minutes`) para no repetirse.

## Configurar Telegram
1. En Telegram habla con **@BotFather** → `/newbot` → copia el token.
2. Escríbele un mensaje cualquiera a tu bot nuevo.
3. Abre `https://api.telegram.org/bot<TOKEN>/getUpdates` y busca `"chat":{"id": ...}`: ese es tu chat id.
4. Copia `.env.example` a `.env` y pega ambos valores. Nunca compartas el token.

## Uso
```
pip install -r requirements.txt
python monitor.py --test   # manda un mensaje de prueba
python monitor.py --once   # una sola revisión
python monitor.py          # bucle continuo (Ctrl+C para parar)
```

Si Binance bloquea tu país, cambia `api_base_url` en `config.json` (por ejemplo `https://api.binance.us`).
Edita `symbols` y los niveles a tu gusto.
