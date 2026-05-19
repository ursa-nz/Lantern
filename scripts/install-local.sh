#!/usr/bin/env bash
# Local dev install: drop Lantern into ~/.local without root.
# Idempotent — re-run any time to refresh or repair.
set -euo pipefail

APP_ID="nz.ursa.Lantern"
APP_NAME="Lantern"
MARP_VERSION="^4.1.0"

PREFIX="${HOME}/.local"
SHARE="${PREFIX}/share/lantern"          # py package + node_modules live here
BIN="${PREFIX}/bin"
DESKTOP_DIR="${PREFIX}/share/applications"
ICON_DIR="${PREFIX}/share/icons/hicolor/scalable/apps"
METAINFO_DIR="${PREFIX}/share/metainfo"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)"

c_blue='\033[1;34m'; c_yellow='\033[1;33m'; c_red='\033[1;31m'; c_reset='\033[0m'
log()  { printf "${c_blue}==>${c_reset} %s\n" "$*"; }
warn() { printf "${c_yellow}warn:${c_reset} %s\n" "$*" >&2; }
err()  { printf "${c_red}error:${c_reset} %s\n" "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

log "Installing ${APP_NAME} (local, ~/.local)"

# ---------- Dependency checks ----------
have node     || err "Node.js missing. Install: sudo apt install nodejs npm"
have npm      || err "npm missing. Install: sudo apt install npm"
have xdg-open || err "xdg-open missing. Install: sudo apt install xdg-utils"

# Find a python interpreter that has PyGObject available.
# The user's shell `python3` may be a venv that shadows the system one without gi.
PYTHON=""
for cand in /usr/bin/python3 python3; do
    if "$cand" -c "import gi" 2>/dev/null; then
        PYTHON="$(command -v "$cand")"
        break
    fi
done
[ -n "$PYTHON" ] || err "No python3 with PyGObject. Install: sudo apt install python3-gi"
log "Using python: ${PYTHON}"

check_gi() {
    local mod="$1" ver="$2" pkg="$3"
    if ! "$PYTHON" -c "import gi; gi.require_version('${mod}','${ver}'); from gi.repository import ${mod}" 2>/dev/null; then
        err "Missing ${mod} ${ver}. Install: sudo apt install ${pkg}"
    fi
}
check_gi Gtk       4.0 "gir1.2-gtk-4.0"
check_gi Adw       1   "gir1.2-adw-1"
check_gi GtkSource 5   "gir1.2-gtksource-5"
check_gi WebKit    6.0 "gir1.2-webkit-6.0"

# ---------- IBM Plex Mono ----------
# We ship the four core TTFs (Regular/Italic/Bold/BoldItalic) under
# data/fonts/IBMPlexMono and install them into the user's font path so
# the editor's CSS resolves IBM Plex Mono without needing a system pkg.
FONTS_DIR="${HOME}/.local/share/fonts/IBMPlexMono"
log "Installing IBM Plex Mono fonts to ${FONTS_DIR}"
mkdir -p "${FONTS_DIR}"
install -m644 "${HERE}/data/fonts/IBMPlexMono"/*.ttf "${FONTS_DIR}/"
install -m644 "${HERE}/data/fonts/IBMPlexMono/LICENSE.txt" "${FONTS_DIR}/LICENSE.txt"
have fc-cache && fc-cache -f "${FONTS_DIR}" >/dev/null 2>&1 || true

# ---------- marp-cli (isolated) ----------
log "Installing @marp-team/marp-cli@${MARP_VERSION} into ${SHARE}"
mkdir -p "$SHARE"
if [ ! -f "${SHARE}/package.json" ]; then
    cat > "${SHARE}/package.json" <<'JSON'
{
  "name": "lantern-runtime",
  "private": true,
  "version": "1.0.0",
  "description": "Isolated dependency tree for Lantern."
}
JSON
fi
( cd "${SHARE}" && npm install --silent --no-audit --no-fund "@marp-team/marp-cli@${MARP_VERSION}" )

# ---------- Python sources ----------
log "Installing Python package to ${SHARE}/lantern"
rm -rf "${SHARE}/lantern"
cp -r "${HERE}/src/lantern" "${SHARE}/lantern"
# Strip any local __pycache__ from a previous run
find "${SHARE}/lantern" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

# ---------- Launcher ----------
log "Installing launcher to ${BIN}/lantern"
mkdir -p "${BIN}"
sed -e "s|@PYTHON@|${PYTHON}|g" \
    -e "s|@SHAREDIR@|${SHARE}|g" \
    "${HERE}/src/lantern.in" > "${BIN}/lantern"
chmod 755 "${BIN}/lantern"

# ---------- Desktop entry (with bindir substituted) ----------
log "Installing desktop entry, metainfo, icon"
mkdir -p "${DESKTOP_DIR}" "${METAINFO_DIR}" "${ICON_DIR}"

sed "s|@BINDIR@|${BIN}|g" "${HERE}/data/${APP_ID}.desktop.in" \
    > "${DESKTOP_DIR}/${APP_ID}.desktop"
chmod 644 "${DESKTOP_DIR}/${APP_ID}.desktop"

install -m 644 "${HERE}/data/${APP_ID}.metainfo.xml" \
               "${METAINFO_DIR}/${APP_ID}.metainfo.xml"

install -m 644 "${HERE}/data/icons/hicolor/scalable/apps/${APP_ID}.svg" \
               "${ICON_DIR}/${APP_ID}.svg"

# ---------- Cache refresh ----------
have update-desktop-database && update-desktop-database "${DESKTOP_DIR}" >/dev/null 2>&1 || true
have gtk-update-icon-cache   && gtk-update-icon-cache -f -t "${PREFIX}/share/icons/hicolor" >/dev/null 2>&1 || true

log "Done."
echo
echo "Launch '${APP_NAME}' from the GNOME app grid, or run: lantern"
if ! printf '%s' ":$PATH:" | grep -q ":${BIN}:"; then
    warn "${BIN} is not on \$PATH — desktop launch works, but typing 'lantern' in a shell will not."
fi
