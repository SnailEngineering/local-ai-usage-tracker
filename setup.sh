#!/usr/bin/env bash
# One-time setup: create .env, add shell aliases, optionally schedule the
# launchd agent. Safe to re-run -- every step is idempotent.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ZSHRC="${ZDOTDIR:-$HOME}/.zshrc"
MARK_BEGIN="# >>> local-ai-usage-tracker >>>"
MARK_END="# <<< local-ai-usage-tracker <<<"

echo "local-ai-usage-tracker setup  (repo: $REPO_DIR)"
echo

# --- .env -------------------------------------------------------------
if [ -f "$REPO_DIR/.env" ]; then
  echo "[.env]      already exists, leaving it alone"
else
  cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
  echo "[.env]      created from .env.example (edit it to add API keys, optional)"
fi

# --- zshrc aliases ------------------------------------------------------
if [ -f "$ZSHRC" ] && grep -qF "$MARK_BEGIN" "$ZSHRC" 2>/dev/null; then
  echo "[.zshrc]    aliases already present, leaving them alone"
else
  {
    echo ""
    echo "$MARK_BEGIN"
    echo "alias aiusage='$REPO_DIR/collect.py'"
    echo "alias aiusage-status='$REPO_DIR/collect.py --status'"
    echo "alias aiusage-dashboard='open $REPO_DIR/dashboard.html'"
    echo "$MARK_END"
  } >> "$ZSHRC"
  echo "[.zshrc]    added aiusage / aiusage-status / aiusage-dashboard aliases"
  echo "            run 'source $ZSHRC' (or open a new shell) to use them now"
fi

# --- launchd (optional, runs collect.py at 09:00 and 21:00 daily) -----
echo
read -r -p "Install the launchd agent to run collect.py twice a day? [y/N] " reply
if [[ "$reply" =~ ^[Yy]$ ]]; then
  PLIST_DEST="$HOME/Library/LaunchAgents/local.ai-usage-tracker.plist"
  sed "s#__REPO_DIR__#$REPO_DIR#g" "$REPO_DIR/local.ai-usage-tracker.plist.template" > "$PLIST_DEST"
  launchctl unload "$PLIST_DEST" 2>/dev/null || true
  launchctl load "$PLIST_DEST"
  launchctl start local.ai-usage-tracker
  echo "[launchd]   installed and started -- logs at $REPO_DIR/data/collect.log"
else
  echo "[launchd]   skipped (run this script again anytime to install it)"
fi

echo
echo "Also worth doing once, by hand, in ~/.claude/settings.json:"
echo '    "cleanupPeriodDays": 3650'
echo "It stops Claude Code from deleting its own JSONL after 30 days, which is"
echo "what this tool archives. See README.md for details."
echo
echo "Done. Try: aiusage && aiusage-dashboard"
