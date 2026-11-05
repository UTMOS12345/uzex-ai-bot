#!/usr/bin/env bash
# Runs ON THE VPS as root: pull the latest code, reinstall / rebuild only what changed, restart, verify.
set -euo pipefail
APP=/opt/uzex-bot
cd "$APP"

old=$(sudo -u uzex git rev-parse HEAD)
sudo -u uzex git pull --ff-only -q
new=$(sudo -u uzex git rev-parse HEAD)
if [ "$old" = "$new" ]; then echo "Already up to date ($new)"; fi
changed=$(sudo -u uzex git diff --name-only "$old" "$new")
[ -n "$changed" ] && echo "Changed files:" && echo "$changed" | sed 's/^/  /'

if echo "$changed" | grep -q '^requirements.txt$'; then
    echo "-> requirements.txt changed: installing packages"
    sudo -u uzex .venv/bin/pip install -q --no-cache-dir -r requirements.txt
fi

if echo "$changed" | grep -qE '^(build_db|products|textnorm)\.py$'; then
    echo "-> data-build code changed: rebuilding uzex.duckdb"
    sudo -u uzex unzip -q -o uzex_spot_data.zip
    sudo -u uzex .venv/bin/python build_db.py uzex_spot_data uzex.new.duckdb
    sudo -u uzex mv uzex.new.duckdb uzex.duckdb
    rm -rf uzex_spot_data
fi

if [ -f deploy/uzex-bot.service ] && ! cmp -s deploy/uzex-bot.service /etc/systemd/system/uzex-bot.service; then
    echo "-> service file changed: reloading systemd"
    cp deploy/uzex-bot.service /etc/systemd/system/uzex-bot.service
    systemctl daemon-reload
fi

systemctl restart uzex-bot
sleep 6
if systemctl is-active --quiet uzex-bot && journalctl -u uzex-bot --since "-10s" --no-pager | grep -q "Run polling"; then
    echo "OK: bot restarted and polling ($(git log -1 --format='%h %s'))"
else
    echo "PROBLEM: bot did not start correctly. Last log lines:"
    journalctl -u uzex-bot -n 30 --no-pager
    exit 1
fi
