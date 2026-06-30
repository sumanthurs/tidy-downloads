#!/usr/bin/env bash
#
# Tidy Downloads — uninstaller.
# Stops the background tool, removes it from login, and deletes the `tidy`
# shortcut. It NEVER touches or deletes any of your files — your Downloads and
# Desktop are left exactly as they are.
#
set -euo pipefail

LABEL="com.user.tidydownloads"
APP_DIR="$HOME/.tidy-downloads"
PLIST_DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
SHORTCUT="$HOME/.local/bin/tidy"

echo "==> Stopping and removing Tidy Downloads"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null \
    || launchctl unload "$PLIST_DEST" 2>/dev/null \
    || true

rm -f "$PLIST_DEST"
rm -f "$SHORTCUT"

echo "✅ Stopped and removed from login."
echo "   Your files were NOT touched."
echo

read -r -p "Also delete the app folder ($APP_DIR: venv + logs)? [y/N] " ans
if [[ "${ans:-N}" =~ ^[Yy]$ ]]; then
    rm -rf "$APP_DIR"
    echo "Removed $APP_DIR."
else
    echo "Left $APP_DIR in place."
fi
