#!/bin/bash
# Install omatv: symlink launcher into ~/.local/bin, copy desktop entry + icon.
set -e

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
APP_DIR="${HOME}/.local/share/applications"
ICON_DIR="${HOME}/.local/share/icons/hicolor/scalable/apps"

mkdir -p "$BIN_DIR" "$APP_DIR" "$ICON_DIR"

# Launcher
ln -sf "$SRC_DIR/run.sh" "$BIN_DIR/omatv"
echo "  ✓ $BIN_DIR/omatv -> run.sh"

# Desktop entry (path points at the launcher)
sed "s|Exec=.*|Exec=$BIN_DIR/omatv|" "$SRC_DIR/io.github.omarchy.omatv.desktop" \
  > "$APP_DIR/io.github.omarchy.omatv.desktop"
echo "  ✓ desktop entry"

# Icon
cp "$SRC_DIR/assets/io.github.omarchy.omatv.svg" "$ICON_DIR/"
echo "  ✓ app icon"

echo
echo "omatv installed. Launch with: omatv [channel]"
echo "If your app menu doesn't show it yet, log out/in or run: update-desktop-database $APP_DIR"
