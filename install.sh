#!/usr/bin/env bash
# Proton Launcher installer. Installs for the current user only; no sudo.
#
#   ./install.sh               install or update
#   ./install.sh --default     also make it the default app for .exe/.msi files
#   ./install.sh --scripts     use the Files "Scripts" menu instead of the extension
#
# One-liner (downloads the latest version from GitHub):
#   curl -fsSL https://raw.githubusercontent.com/a904guy/ProtonLauncher/main/install.sh | bash
set -euo pipefail

REPO="a904guy/ProtonLauncher"
BRANCH="main"

DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
INSTALL_DIR="$DATA_HOME/protonlauncher"
BIN_DIR="$HOME/.local/bin"
APPS_DIR="$DATA_HOME/applications"
ICON_PATH="$DATA_HOME/icons/hicolor/scalable/apps/protonlauncher.svg"
EXT_DIR="$DATA_HOME/nautilus-python/extensions"
SCRIPTS_DIR="$DATA_HOME/nautilus/scripts"
MIME_TYPES="application/x-msdownload application/vnd.microsoft.portable-executable application/x-ms-dos-executable application/x-msi application/x-bat"

make_default=0
force_scripts=0
for arg in "$@"; do
    case "$arg" in
        --default) make_default=1 ;;
        --scripts) force_scripts=1 ;;
        -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
    esac
done

bold=$(tput bold 2>/dev/null || true); dim=$(tput dim 2>/dev/null || true); reset=$(tput sgr0 2>/dev/null || true)
say()  { printf '%s==>%s %s\n' "$bold" "$reset" "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf '%s!!%s  %s\n' "$bold" "$reset" "$*" >&2; }

# --- find the source files (cloned repo, or download when piped from curl) ---
src=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "$(dirname "${BASH_SOURCE[0]}")/src/protonlauncher.py" ]; then
    src="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
else
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    url="https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz"
    say "Downloading Proton Launcher from github.com/$REPO"
    if command -v curl >/dev/null; then
        curl -fsSL "$url" | tar -xz -C "$tmp"
    elif command -v wget >/dev/null; then
        wget -qO- "$url" | tar -xz -C "$tmp"
    else
        warn "Need curl or wget to download. Or clone the repo and run ./install.sh"; exit 1
    fi
    src="$(find "$tmp" -mindepth 1 -maxdepth 1 -type d | head -n1)"
fi

# --- requirements ---
if ! command -v python3 >/dev/null || ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))'; then
    warn "Python 3.8 or newer is required (Proton itself needs it too)."; exit 1
fi

say "Installing Proton Launcher"
install -Dm755 "$src/src/protonlauncher.py" "$INSTALL_DIR/protonlauncher.py"
install -Dm755 "$src/uninstall.sh" "$INSTALL_DIR/uninstall.sh"
mkdir -p "$BIN_DIR"
ln -sf "$INSTALL_DIR/protonlauncher.py" "$BIN_DIR/proton-launch"
install -Dm644 "$src/data/protonlauncher.svg" "$ICON_PATH"
install -Dm644 "$src/data/protonlauncher.svg" "$INSTALL_DIR/protonlauncher.svg"
# An existing user icon cache hides icons added after it was built; refresh it if there is one.
if [ -f "$DATA_HOME/icons/hicolor/icon-theme.cache" ] && command -v gtk-update-icon-cache >/dev/null; then
    gtk-update-icon-cache -f -q -t "$DATA_HOME/icons/hicolor" 2>/dev/null || true
fi
launcher="$INSTALL_DIR/protonlauncher.py"
mkdir -p "$APPS_DIR"
# Absolute icon path: shows up regardless of icon theme caches.
sed -e "s|@LAUNCHER@|\"$launcher\"|" -e "s|@ICON@|$INSTALL_DIR/protonlauncher.svg|" \
    "$src/data/protonlauncher.desktop.in" > "$APPS_DIR/protonlauncher.desktop.tmp"
mv "$APPS_DIR/protonlauncher.desktop.tmp" "$APPS_DIR/protonlauncher.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database -q "$APPS_DIR" || true
note "command:    proton-launch  ${dim}($BIN_DIR)${reset}"
note "Open With:  Proton Launcher  ${dim}(works in any file manager)${reset}"

# --- GNOME Files integration ---
remove_scripts() { rm -f "$SCRIPTS_DIR/Run with Proton" "$SCRIPTS_DIR/Proton Launcher…"; }
install_scripts() {
    mkdir -p "$SCRIPTS_DIR"
    cat > "$SCRIPTS_DIR/Run with Proton" <<EOF
#!/bin/sh
# Installed by Proton Launcher
set -f
IFS='
'
for f in \$NAUTILUS_SCRIPT_SELECTED_FILE_PATHS; do
    setsid "$launcher" --gui run "\$f" >/dev/null 2>&1 &
done
EOF
    cat > "$SCRIPTS_DIR/Proton Launcher…" <<EOF
#!/bin/sh
# Installed by Proton Launcher
f=\$(printf '%s\n' "\$NAUTILUS_SCRIPT_SELECTED_FILE_PATHS" | head -n1)
[ -n "\$f" ] && exec "$launcher" --gui menu "\$f"
EOF
    chmod +x "$SCRIPTS_DIR/Run with Proton" "$SCRIPTS_DIR/Proton Launcher…"
}

has_nautilus_python() {
    local lib
    for lib in /usr/lib*/nautilus/extensions-*/libnautilus-python.so /usr/lib/*/nautilus/extensions-*/libnautilus-python.so; do
        [ -e "$lib" ] && return 0
    done
    return 1
}

nautilus_python_hint() {
    if command -v apt >/dev/null; then echo "sudo apt install python3-nautilus"
    elif command -v dnf >/dev/null; then echo "sudo dnf install nautilus-python"
    elif command -v pacman >/dev/null; then echo "sudo pacman -S python-nautilus"
    elif command -v zypper >/dev/null; then echo "sudo zypper install python3-nautilus"
    else echo "install your distro's nautilus-python package"
    fi
}

restart_files=0
if command -v nautilus >/dev/null; then
    if [ "$force_scripts" = 0 ] && has_nautilus_python; then
        install -Dm644 "$src/src/protonlauncher_nautilus.py" "$EXT_DIR/protonlauncher_nautilus.py"
        remove_scripts
        note "Files:      right-click a .exe -> Run with Proton"
        restart_files=1
    else
        install_scripts
        rm -f "$EXT_DIR/protonlauncher_nautilus.py"
        note "Files:      right-click a .exe -> Scripts -> Run with Proton"
        [ "$force_scripts" = 0 ] && note "${dim}For a top-level menu item: $(nautilus_python_hint), then run this installer again.${reset}"
    fi
fi

if [ "$make_default" = 1 ]; then
    mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}"
    # shellcheck disable=SC2086
    xdg-mime default protonlauncher.desktop $MIME_TYPES
    note "Double-clicking .exe and .msi files now runs them with Proton."
fi

# --- check for Proton ---
echo
if python3 "$launcher" list >/dev/null 2>&1; then
    say "Found Proton:"
    python3 "$launcher" list --names | sed 's/^/    /'
else
    warn "No Proton found yet. In Steam: Library -> search \"Proton Experimental\" -> Install."
    warn "Proton Launcher will pick it up automatically once it is installed."
fi

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo; note "${dim}$BIN_DIR is not on your PATH; log out and back in to use proton-launch in a terminal.${reset}" ;;
esac

if [ "$restart_files" = 1 ] && pgrep -x nautilus >/dev/null 2>&1; then
    echo
    if [ -r /dev/tty ] && (: </dev/tty) 2>/dev/null; then
        printf 'Files must restart to load the menu item. This closes open Files windows. Restart now? [Y/n] '
        read -r answer </dev/tty || answer=n
        case "$answer" in
            [nN]*) note "Run 'nautilus -q' (or log out and in) when convenient." ;;
            *) nautilus -q >/dev/null 2>&1 || true; note "Files restarted." ;;
        esac
    else
        note "Run 'nautilus -q' (or log out and in) so Files loads the new menu item."
    fi
fi

echo
say "Done. Uninstall any time with: $INSTALL_DIR/uninstall.sh"
