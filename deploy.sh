#!/usr/bin/env bash
# Runs ON YOUR PC:  ./deploy.sh "what changed"
# Commits + pushes local changes, then updates and restarts the bot on the VPS.
set -euo pipefail
cd "$(dirname "$0")"
VPS=$(cat .vps 2>/dev/null || true)
[ -z "$VPS" ] && { echo "Create the file .vps with your server, e.g.:  echo root@SERVER_IP > .vps"; exit 1; }

if [ -n "$(git status --porcelain)" ]; then
    git add -A
    git commit -q -m "${1:-update}"
    echo "Committed: ${1:-update}"
fi
git push -q
echo "Pushed. Updating $VPS ..."
ssh "$VPS" 'bash /opt/uzex-bot/deploy/update.sh'
