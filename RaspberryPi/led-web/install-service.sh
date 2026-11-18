#!/usr/bin/env bash
# Installs and starts the led-web systemd service for the current user,
# from the directory this script lives in.
#
# Usage: ./install-service.sh /dev/serial/by-id/<the Nano's entry>
set -euo pipefail

if [ $# -ne 1 ]; then
    echo "Usage: $0 <serial device of the Nano>" >&2
    echo "Available devices:" >&2
    ls /dev/serial/by-id/ >&2 2>/dev/null || echo "  (none found)" >&2
    exit 1
fi

dir="$(cd "$(dirname "$0")" && pwd)"
user="$(id -un)"

if [ ! -x "$dir/venv/bin/gunicorn" ]; then
    echo "Missing venv; run: python3 -m venv venv && venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

sed -e "s|__USER__|$user|" -e "s|__DIR__|$dir|g" -e "s|__LED_PORT__|$1|" \
    "$dir/led-web.service" | sudo tee /etc/systemd/system/led-web.service >/dev/null

sudo usermod -aG dialout "$user"
sudo systemctl daemon-reload
sudo systemctl enable led-web
sudo systemctl restart led-web
systemctl status led-web --no-pager
