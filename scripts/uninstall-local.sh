#!/usr/bin/env bash
# Reverse install-local.sh.  Config (~/.config/lantern) is preserved.
set -euo pipefail

APP_ID="nz.ursa.Lantern"
PREFIX="${HOME}/.local"

rm -f  "${PREFIX}/bin/lantern"
rm -rf "${PREFIX}/share/lantern"
rm -f  "${PREFIX}/share/applications/${APP_ID}.desktop"
rm -f  "${PREFIX}/share/metainfo/${APP_ID}.metainfo.xml"
rm -f  "${PREFIX}/share/icons/hicolor/scalable/apps/${APP_ID}.svg"
rm -rf "${PREFIX}/share/fonts/IBMPlexMono"

command -v update-desktop-database >/dev/null && \
    update-desktop-database "${PREFIX}/share/applications" >/dev/null 2>&1 || true
command -v gtk-update-icon-cache >/dev/null && \
    gtk-update-icon-cache -f -t "${PREFIX}/share/icons/hicolor" >/dev/null 2>&1 || true
command -v fc-cache >/dev/null && \
    fc-cache -f "${PREFIX}/share/fonts" >/dev/null 2>&1 || true

echo "Lantern removed. (~/.config/lantern kept — rm it manually if you want it gone.)"
