#!/usr/bin/env bash
# Instala el bot como servicio en Ubuntu. Ejecutar desde ~/trading:  bash deploy/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "Falta el archivo .env en $(pwd). Créalo primero (ver README)." >&2
  exit 1
fi

sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip git

python3 -m venv venv
./venv/bin/pip install -q -r requirements.txt

sudo cp deploy/trading-bot.service /etc/systemd/system/trading-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now trading-bot

echo
echo "Listo. Estado:   sudo systemctl status trading-bot"
echo "Logs en vivo:    journalctl -u trading-bot -f"
