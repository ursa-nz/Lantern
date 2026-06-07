#!/usr/bin/env bash
# Build a Lantern .deb package.
#
# Output: build/lantern_<version>_all.deb
#
# The package is Architecture: all — Lantern is Python plus a bundled, pure-JS
# marp-cli, and it leans on the distro's own GTK 4 / libadwaita / GtkSourceView /
# WebKitGTK (via PyGObject) and Node rather than shipping them. So one package
# installs on amd64 and arm64 alike. pandoc (for PPTX export) is a Recommends.
#
# Usage: scripts/build-deb.sh [version]
#   version defaults to $LANTERN_VERSION, else the newest <release> in the
#   AppStream metainfo (e.g. 1.0.5).
set -euo pipefail

APP_ID="nz.ursa.Lantern"
PKG="lantern"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)"
cd "${HERE}"

c_blue='\033[1;34m'; c_red='\033[1;31m'; c_reset='\033[0m'
log() { printf "${c_blue}==>${c_reset} %s\n" "$*"; }
err() { printf "${c_red}error:${c_reset} %s\n" "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

have dpkg-deb || err "dpkg-deb missing. Install: sudo apt install dpkg-dev"

# ---------- Version ----------
VERSION="${1:-${LANTERN_VERSION:-}}"
if [ -z "${VERSION}" ]; then
    VERSION="$(grep -oP '<release version="\K[^"]+' "data/${APP_ID}.metainfo.xml" | head -1)"
fi
[ -n "${VERSION}" ] || err "could not determine version (pass it as an argument)"
log "Building ${PKG} ${VERSION} (Architecture: all)"

# ---------- Stage the app into the package root ----------
ROOT="build/deb/${PKG}_${VERSION}_all"
rm -rf "${ROOT}"
mkdir -p "${ROOT}"

DESTDIR="${ROOT}" PREFIX="/usr" SHAREDIR="/usr/share/lantern" \
    PYTHON="/usr/bin/python3" ./scripts/stage-app.sh

# ---------- Docs: copyright + changelog ----------
DOCDIR="${ROOT}/usr/share/doc/${PKG}"
mkdir -p "${DOCDIR}"

cat > "${DOCDIR}/copyright" <<'COPYRIGHT'
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: Lantern
Source: https://forge.ursa.nz

Files: *
Copyright: 2026 ursa.nz
License: GPL-3.0-or-later
 This program is free software: you can redistribute it and/or modify it under
 the terms of the GNU General Public License as published by the Free Software
 Foundation, either version 3 of the License, or (at your option) any later
 version.
 .
 On Debian systems the full text of the GNU General Public License version 3
 can be found in /usr/share/common-licenses/GPL-3.

Files: usr/share/fonts/IBMPlexMono/*
Copyright: 2017 IBM Corp.
License: OFL-1.1
 Bundled IBM Plex Mono, under the SIL Open Font License 1.1. The full text
 ships alongside the fonts as LICENSE.txt.
COPYRIGHT

# A Debian-format changelog, newest first, one stanza per AppStream release.
# Guarantees a top entry for the package version even if it's a tag ahead of
# the metainfo (dated today in that case).
CHANGELOG="${DOCDIR}/changelog"
: > "${CHANGELOG}"
emit_stanza() {
    local ver="$1" date="$2"
    local rfc; rfc="$(date -R -d "${date}" 2>/dev/null || date -R)"
    {
        printf '%s (%s) unstable; urgency=medium\n\n' "${PKG}" "${ver}"
        printf '  * Release %s. See the AppStream metainfo for full notes.\n\n' "${ver}"
        printf ' -- ursa.nz <code@ursa.nz>  %s\n\n' "${rfc}"
    } >> "${CHANGELOG}"
}
top_seen=""
while IFS='|' read -r ver date; do
    [ -n "${ver}" ] || continue
    [ "${ver}" = "${VERSION}" ] && top_seen=1
    emit_stanza "${ver}" "${date}"
done < <(grep -oP '<release version="\K[^"]+(?=" date="[^"]+")|(?<= date=")[^"]+(?=")' \
            "data/${APP_ID}.metainfo.xml" | paste -d'|' - -)
# Prepend a stanza for the build version if the metainfo doesn't carry it yet.
if [ -z "${top_seen}" ]; then
    tmp="$(mktemp)"
    { ver="${VERSION}" date="$(date +%Y-%m-%d)"
      rfc="$(date -R)"
      printf '%s (%s) unstable; urgency=medium\n\n' "${PKG}" "${VERSION}"
      printf '  * Release %s.\n\n' "${VERSION}"
      printf ' -- ursa.nz <code@ursa.nz>  %s\n\n' "${rfc}"
      cat "${CHANGELOG}"; } > "${tmp}"
    mv "${tmp}" "${CHANGELOG}"
fi
gzip -9n "${CHANGELOG}"

# ---------- Control + maintainer scripts ----------
INSTALLED_KB="$(du -s -k "${ROOT}" | cut -f1)"
mkdir -p "${ROOT}/DEBIAN"

cat > "${ROOT}/DEBIAN/control" <<CONTROL
Package: ${PKG}
Version: ${VERSION}
Section: graphics
Priority: optional
Architecture: all
Maintainer: ursa.nz <code@ursa.nz>
Installed-Size: ${INSTALLED_KB}
Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1, gir1.2-gtksource-5, gir1.2-webkit-6.0, nodejs, xdg-utils
Recommends: pandoc
Homepage: https://forge.ursa.nz
Description: Author Markdown slide decks with live preview
 Lantern is a small GUI for making Marp slide decks on GNOME. Edit on the left,
 see the slide render on the right, and present from the same window. A deck
 saves as one .lantern file, keeping its slides, images, and fonts together.
 .
 Export to PDF and HTML works out of the box; PowerPoint export uses pandoc
 (the Recommends), so install it for .pptx.
CONTROL

# Refresh the shared desktop/icon/MIME caches after install and removal so the
# launcher, icon, and .lantern association show up (and clear) cleanly. The
# font cache is left to fontconfig's own dpkg trigger on /usr/share/fonts.
cat > "${ROOT}/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    [ -x "$(command -v update-desktop-database)" ] && update-desktop-database -q /usr/share/applications || true
    [ -x "$(command -v gtk-update-icon-cache)" ] && gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
    [ -x "$(command -v update-mime-database)" ] && update-mime-database /usr/share/mime || true
fi
exit 0
POSTINST

cat > "${ROOT}/DEBIAN/postrm" <<'POSTRM'
#!/bin/sh
set -e
if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
    [ -x "$(command -v update-desktop-database)" ] && update-desktop-database -q /usr/share/applications || true
    [ -x "$(command -v gtk-update-icon-cache)" ] && gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
    [ -x "$(command -v update-mime-database)" ] && update-mime-database /usr/share/mime || true
fi
exit 0
POSTRM

chmod 755 "${ROOT}/DEBIAN/postinst" "${ROOT}/DEBIAN/postrm"

# ---------- Normalise permissions ----------
# The source tree and npm's output can be group-writable (umask / cloud mounts),
# which dpkg-deb would carry into the package (lintian: non-standard-*-perm).
# Clearing the group/other write bits turns 0664->0644 and 0775->0755 across the
# tree while leaving execute bits (launchers, marp's CLI) intact.
chmod -R go-w "${ROOT}"

# ---------- Build ----------
mkdir -p build
DEB="build/${PKG}_${VERSION}_all.deb"
# --root-owner-group writes files as root:root without needing fakeroot.
log "Running dpkg-deb..."
dpkg-deb --root-owner-group --build "${ROOT}" "${DEB}"

log "Done."
echo
echo "Package written to:  ${DEB}"
echo "Inspect:             dpkg-deb --info ${DEB}  /  dpkg-deb --contents ${DEB}"
echo "Install:             sudo apt install ./${DEB}   (pulls the GTK/WebKit deps)"
