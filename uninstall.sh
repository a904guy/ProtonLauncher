#!/usr/bin/env bash
# Proton Launcher uninstaller.
#
#   uninstall.sh           remove the app, keep your Windows prefixes (installed programs and saves)
#   uninstall.sh --purge   also delete all prefixes, settings and logs
set -euo pipefail

DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
INSTALL_DIR="$DATA_HOME/protonlauncher"
APPS_DIR="$DATA_HOME/applications"

purge=0
case "${1:-}" in
    --purge) purge=1 ;;
    "") ;;
    -h|--help) sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1 (try --help)" >&2; exit 2 ;;
esac

rm -f "$HOME/.local/bin/proton-launch" \
      "$APPS_DIR/protonlauncher.desktop" \
      "$APPS_DIR"/protonlauncher-*.desktop \
      "$DATA_HOME/icons/hicolor/scalable/apps/protonlauncher.svg" \
      "$DATA_HOME/nautilus-python/extensions/protonlauncher_nautilus.py" \
      "$DATA_HOME/nautilus/scripts/Run with Proton" \
      "$DATA_HOME/nautilus/scripts/Proton Launcher…"
rm -rf "$DATA_HOME/nautilus-python/extensions/__pycache__/protonlauncher_nautilus"* \
       "$INSTALL_DIR/icons" "$INSTALL_DIR/__pycache__"
rm -f "$INSTALL_DIR/protonlauncher.py" "$INSTALL_DIR/protonlauncher.svg" "$INSTALL_DIR/uninstall.sh"
command -v update-desktop-database >/dev/null && update-desktop-database -q "$APPS_DIR" || true
if [ -f "$DATA_HOME/icons/hicolor/icon-theme.cache" ] && command -v gtk-update-icon-cache >/dev/null; then
    gtk-update-icon-cache -f -q -t "$DATA_HOME/icons/hicolor" 2>/dev/null || true
fi

if [ "$purge" = 1 ]; then
    rm -rf "$INSTALL_DIR" "${XDG_CONFIG_HOME:-$HOME/.config}/protonlauncher" "${XDG_CACHE_HOME:-$HOME/.cache}/protonlauncher"
    echo "Proton Launcher removed, including all prefixes, settings and logs."
else
    rmdir "$INSTALL_DIR" 2>/dev/null || true
    echo "Proton Launcher removed."
    if [ -d "$INSTALL_DIR/prefixes" ]; then
        echo "Your Windows prefixes are still in $INSTALL_DIR/prefixes"
        echo "Delete them with: rm -rf \"$INSTALL_DIR\""
    fi
fi
if pgrep -x nautilus >/dev/null 2>&1; then
    echo "Run 'nautilus -q' to drop the menu item from open Files windows."
fi
