#!/usr/bin/env bash
#
# Tidy Downloads — installer.
# Sets up a private virtual environment, installs dependencies, creates the
# `tidy` shortcut command, and registers the background LaunchAgent so the
# tool starts automatically at login.
#
set -euo pipefail

LABEL="com.user.tidydownloads"
APP_DIR="$HOME/.tidy-downloads"
VENV="$APP_DIR/venv"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$SCRIPT_DIR/tidy_downloads.py"
PLIST_DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
TEMPLATE="$SCRIPT_DIR/$LABEL.plist.template"
BIN_DIR="$HOME/.local/bin"
SHORTCUT="$BIN_DIR/tidy"

echo "==> Tidy Downloads installer"

# 1) Pick a Python 3 to *build* the venv.
#
# IMPORTANT (macOS): macOS Full Disk Access cannot be granted to a symlinked
# interpreter, and Apple's built-in /usr/bin/python3 can ONLY create
# symlinked venvs ("cannot create venvs without using symlinks"). So we prefer
# a Homebrew Python, which lets us build the venv with --copies — giving the
# agent its own REAL binary that you can actually select in the Full Disk
# Access picker. If no Homebrew Python is found we fall back to the system one
# (you'll then have to grant access to the resolved framework binary).
COPIES_FLAG="--copies"
PYBOOT=""
for cand in /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3.12 \
            /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if [[ -x "$cand" ]]; then PYBOOT="$cand"; break; fi
done
if [[ -z "$PYBOOT" ]]; then
    echo "    No Homebrew Python found. Falling back to the system python3."
    echo "    TIP: 'brew install python' gives a cleaner setup (selectable in"
    echo "         Full Disk Access). Continuing with the system Python..."
    PYBOOT="$(command -v python3 || true)"
    COPIES_FLAG=""   # Apple's python can't do --copies
fi
if [[ -z "$PYBOOT" ]]; then
    echo "ERROR: no python3 found. Install it (e.g. 'brew install python') and re-run." >&2
    exit 1
fi
echo "==> Using base Python: $PYBOOT"

# 2) Create the private virtual environment and install dependencies.
echo "==> Creating virtual environment at $VENV"
mkdir -p "$APP_DIR"
rm -rf "$VENV"
"$PYBOOT" -m venv $COPIES_FLAG "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip >/dev/null
echo "==> Installing dependencies (this can take a minute)..."
"$VENV/bin/python" -m pip install -r "$SCRIPT_DIR/requirements.txt"

VENV_PY="$VENV/bin/python"

# 3) Create the `tidy` shortcut command.
echo "==> Installing 'tidy' shortcut to $SHORTCUT"
mkdir -p "$BIN_DIR"
cat > "$SHORTCUT" <<EOF
#!/usr/bin/env bash
exec "$VENV_PY" "$SCRIPT" "\$@"
EOF
chmod +x "$SHORTCUT"

if ! echo ":$PATH:" | grep -q ":$BIN_DIR:"; then
    echo "    NOTE: $BIN_DIR is not on your PATH. Add this line to ~/.zshrc:"
    echo "        export PATH=\"\$HOME/.local/bin:\$PATH\""
    echo "    Then open a new Terminal window. (You can also run the full path:"
    echo "        $SHORTCUT --sweep )"
fi

# 4) Generate the LaunchAgent plist from the template.
echo "==> Registering LaunchAgent"
mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s|__PYTHON__|$VENV_PY|g" \
    -e "s|__SCRIPT__|$SCRIPT|g" \
    -e "s|__APPDIR__|$APP_DIR|g" \
    "$TEMPLATE" > "$PLIST_DEST"

# 5) Load it (modern command, with a fallback for older macOS).
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if ! launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST" 2>/dev/null; then
    launchctl unload "$PLIST_DEST" 2>/dev/null || true
    launchctl load "$PLIST_DEST"
fi

sleep 1
echo
if launchctl list | grep -q "$LABEL"; then
    echo "✅ Installed and running."
else
    echo "⚠️  Installed, but it doesn't appear to be running yet. Check $APP_DIR/stderr.log"
fi

cat <<EOF

------------------------------------------------------------------
ONE MORE STEP — grant permission to read your files:
  System Settings → Privacy & Security → Full Disk Access
  Add (or enable) the Python at:
      $VENV_PY
  Without this, macOS may silently block access to Downloads/Desktop.
------------------------------------------------------------------

Done! New downloads will now be sorted automatically.

Useful commands (run in Terminal):
  tidy --sweep          # organize files already sitting in your folders
  tidy --cleanup        # see old/junk files (changes nothing)
  tail -f ~/Downloads/.tidy-downloads-log.txt   # watch it work

To stop/uninstall:  ./uninstall_launch_agent.sh
EOF
