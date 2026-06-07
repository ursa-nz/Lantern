#!/usr/bin/env bash
# Stage the Lantern app tree into a destination root.
#
# Shared by build-deb.sh and build-appimage.sh so the two packages lay the app
# out identically — the same launcher, Python package, bundled marp-cli, desktop
# entry, metainfo, icon, MIME type, and fonts the flatpak ships.
#
# Usage:
#   DESTDIR=<root> [PREFIX=/usr] [SHAREDIR=<prefix>/share/lantern] \
#     [PYTHON=/usr/bin/python3] [MARP_VERSION=^4.1.0] scripts/stage-app.sh
#
# Files land under ${DESTDIR}; the launcher bakes the *runtime* ${PYTHON} and
# ${SHAREDIR} (where the app will live once installed), which need not match
# ${DESTDIR}. node_modules is reused if marp is already present, so repeated
# runs (and CI caches) skip the slow npm install.
set -euo pipefail

APP_ID="nz.ursa.Lantern"

: "${DESTDIR:?set DESTDIR to the staging root}"
PREFIX="${PREFIX:-/usr}"
SHAREDIR="${SHAREDIR:-${PREFIX}/share/lantern}"
PYTHON="${PYTHON:-/usr/bin/python3}"
MARP_VERSION="${MARP_VERSION:-^4.1.0}"

BINDIR="${PREFIX}/bin"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)"

c_blue='\033[1;34m'; c_red='\033[1;31m'; c_reset='\033[0m'
log() { printf "${c_blue}==>${c_reset} %s\n" "$*"; }
err() { printf "${c_red}error:${c_reset} %s\n" "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

have node || err "Node.js missing (needed to fetch marp-cli). Install: sudo apt install nodejs npm"
have npm  || err "npm missing. Install: sudo apt install npm"

# Absolute on-disk locations to write into.
D_SHARE="${DESTDIR}${SHAREDIR}"
D_BIN="${DESTDIR}${BINDIR}"
D_APPS="${DESTDIR}${PREFIX}/share/applications"
D_META="${DESTDIR}${PREFIX}/share/metainfo"
D_ICON="${DESTDIR}${PREFIX}/share/icons/hicolor/scalable/apps"
D_MIME="${DESTDIR}${PREFIX}/share/mime/packages"
D_FONTS="${DESTDIR}${PREFIX}/share/fonts/IBMPlexMono"

mkdir -p "${D_SHARE}" "${D_BIN}" "${D_APPS}" "${D_META}" "${D_ICON}" "${D_MIME}" "${D_FONTS}"

# ---------- marp-cli (bundled, isolated) ----------
if [ -x "${D_SHARE}/node_modules/.bin/marp" ]; then
    log "Reusing bundled marp-cli in ${D_SHARE}/node_modules"
else
    log "Installing @marp-team/marp-cli@${MARP_VERSION} into ${D_SHARE}"
    cat > "${D_SHARE}/package.json" <<'JSON'
{
  "name": "lantern-runtime",
  "private": true,
  "version": "1.0.0",
  "description": "Bundled marp-cli for Lantern."
}
JSON
    ( cd "${D_SHARE}" && npm install --silent --no-audit --no-fund --omit=dev \
        "@marp-team/marp-cli@${MARP_VERSION}" )
fi

# Drop the multi-arch native prebuilds shipped by the `bare-*` packages (pulled
# in transitively by puppeteer's browser-downloader). marp runs on Node, which
# uses those packages' pure-JS path and never loads the .bare binaries — they
# only load under the unrelated "bare" runtime. Removing them keeps the tree
# free of arch-specific objects (so the deb stays genuinely Architecture: all)
# and trims a few megabytes. Verified: marp renders fine without them.
find "${D_SHARE}/node_modules" -type d -name prebuilds -prune -exec rm -rf {} + 2>/dev/null || true

# Strip development cruft npm leaves in the tree — linter/editor configs and
# ignore files that never run. Keeps the bundle smaller and the package clean.
find "${D_SHARE}/node_modules" -type f \( \
        -name '.eslintrc' -o -name '.eslintrc.*' -o -name '.npmignore' \
        -o -name '.editorconfig' -o -name '.gitignore' -o -name '.gitattributes' \
        -o -name '.travis.yml' -o -name '.npmrc' \) -delete 2>/dev/null || true

# ---------- Python package ----------
log "Installing Python package to ${D_SHARE}/lantern"
rm -rf "${D_SHARE}/lantern"
cp -r "${HERE}/src/lantern" "${D_SHARE}/lantern"
find "${D_SHARE}/lantern" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

# ---------- Launcher ----------
log "Installing launcher to ${D_BIN}/lantern (python: ${PYTHON}, share: ${SHAREDIR})"
sed -e "s|@PYTHON@|${PYTHON}|g" \
    -e "s|@SHAREDIR@|${SHAREDIR}|g" \
    "${HERE}/src/lantern.in" > "${D_BIN}/lantern"
chmod 755 "${D_BIN}/lantern"

# ---------- Desktop entry, metainfo, icon, MIME ----------
log "Installing desktop entry, metainfo, icon, MIME type"
sed "s|@BINDIR@|${BINDIR}|g" "${HERE}/data/${APP_ID}.desktop.in" \
    > "${D_APPS}/${APP_ID}.desktop"
chmod 644 "${D_APPS}/${APP_ID}.desktop"

install -m 644 "${HERE}/data/${APP_ID}.metainfo.xml" "${D_META}/${APP_ID}.metainfo.xml"
install -m 644 "${HERE}/data/icons/hicolor/scalable/apps/${APP_ID}.svg" "${D_ICON}/${APP_ID}.svg"
# The flatpak installs the MIME file as <app-id>.xml; match that here.
install -m 644 "${HERE}/data/${APP_ID}.mime.xml" "${D_MIME}/${APP_ID}.xml"

# ---------- IBM Plex Mono (OFL 1.1) ----------
log "Installing IBM Plex Mono fonts to ${D_FONTS}"
install -m 644 "${HERE}/data/fonts/IBMPlexMono"/*.ttf "${D_FONTS}/"
install -m 644 "${HERE}/data/fonts/IBMPlexMono/LICENSE.txt" "${D_FONTS}/LICENSE.txt"

log "Staged Lantern into ${DESTDIR}"
