#!/usr/bin/env bash
# Sync this plugin to the Pi so OctoPrint loads the new code on its next restart.
#
# Usage: scripts/deploy.sh [--install] [--dry-run] [--restart]
#   (none)     rsync the project to the Pi; re-run `pip install -e` only if pyproject.toml changed
#   --install  also (re)run `pip install -e` on the Pi. Needed once, for the first deploy.
#   --dry-run  show what rsync would change; touch nothing
#   --restart  restart OctoPrint over ssh afterwards (needs passwordless sudo for systemctl).
#              Without it, restart from the OctoPrint web UI: power menu -> Restart OctoPrint.
#
# Configuration, from the environment or scripts/deploy.env (git-ignored, see deploy.env.example):
#   PI_HOST     ssh target, e.g. pi@octopi.local          (required)
#   PI_SRC_DIR  where the source lives on the Pi          (default: octoprint-led, relative to $HOME)
#   PI_PIP      pip of OctoPrint's virtualenv on the Pi   (default: oprint/bin/pip, relative to $HOME)
#   PI_SERVICE  systemd unit, used by --restart only      (default: octoprint)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(dirname "$HERE")"

[[ -f "$HERE/deploy.env" ]] && source "$HERE/deploy.env"
: "${PI_HOST:?Set PI_HOST (e.g. pi@octopi.local) in the environment or scripts/deploy.env}"
PI_SRC_DIR="${PI_SRC_DIR:-octoprint-led}"
PI_PIP="${PI_PIP:-oprint/bin/pip}"
PI_SERVICE="${PI_SERVICE:-octoprint}"

INSTALL=0 DRY=0 RESTART=0
for arg in "$@"; do
  case "$arg" in
    --install) INSTALL=1 ;;
    --dry-run) DRY=1 ;;
    --restart) RESTART=1 ;;
    -h|--help) sed -n '2,17p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

for tool in ssh rsync; do
  command -v "$tool" >/dev/null || { echo "$tool not found" >&2; exit 1; }
done

step() { printf '\n==> %s\n' "$*"; }

# pyproject.toml checksum from the last deploy lives on the Pi, next to the source.
local_sum="$(shasum "$PROJECT/pyproject.toml" | cut -d' ' -f1)"

step "Syncing $PROJECT/ -> $PI_HOST:$PI_SRC_DIR/"
RSYNC_FLAGS=(-az --delete --itemize-changes
  --exclude '__pycache__/' --exclude '*.egg-info/' --exclude '*.pyc' --exclude '.git/'
  --exclude 'scripts/deploy.env' --exclude 'dist/' --exclude 'build/' --exclude '.installed-sum')
[[ $DRY -eq 1 ]] && RSYNC_FLAGS+=(--dry-run)
ssh "$PI_HOST" "mkdir -p '$PI_SRC_DIR'"
rsync "${RSYNC_FLAGS[@]}" "$PROJECT/" "$PI_HOST:$PI_SRC_DIR/"

if [[ $DRY -eq 1 ]]; then
  echo; echo "Dry run: nothing changed on the Pi."
  exit 0
fi

remote_sum="$(ssh "$PI_HOST" "cat '$PI_SRC_DIR/.installed-sum' 2>/dev/null || true")"
if [[ $INSTALL -eq 1 || "$local_sum" != "$remote_sum" ]]; then
  step "Installing editable into OctoPrint's virtualenv ($PI_PIP)"
  ssh "$PI_HOST" "'$PI_PIP' install -e '$PI_SRC_DIR' && echo '$local_sum' > '$PI_SRC_DIR/.installed-sum'"
else
  step "pyproject.toml unchanged since the last install; skipping pip"
fi

if [[ $RESTART -eq 1 ]]; then
  step "Restarting $PI_SERVICE"
  ssh "$PI_HOST" "sudo systemctl restart '$PI_SERVICE'"
  echo "Restarted."
else
  printf '\nSynced. Restart OctoPrint from the web UI (power menu -> Restart OctoPrint) to load Python changes.\n'
  printf 'Template/JS/CSS-only changes usually just need a browser refresh (Ctrl/Cmd+Shift+R).\n'
fi
