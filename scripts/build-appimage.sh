#!/usr/bin/env bash
# Build a self-contained Lantern AppImage.
#
# Output: build/Lantern-<arch>.AppImage
#
# Unlike the deb (which leans on the distro's GTK/WebKit), the AppImage bundles
# the whole GUI stack — Python, PyGObject, GTK 4, libadwaita, GtkSourceView,
# WebKitGTK 6.0 and their helpers — plus Node, marp-cli and pandoc, so it runs
# on a host that has none of them installed. Build it on the OLDEST distro you
# want to support (the CI uses Ubuntu 24.04, the first with webkitgtk-6.0); the
# resulting glibc floor is the build host's.
#
# WebKitGTK 6.0 hardcodes the path to its helper processes and offers no env
# override, so we binary-patch that path to /tmp/lantern-wk and have AppRun point
# it at the bundled helpers. See the comments at patch_webkit below.
#
# Usage: scripts/build-appimage.sh [version]
set -euo pipefail

APP_ID="nz.ursa.Lantern"
APP_NAME="Lantern"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)"
cd "${HERE}"

c_blue='\033[1;34m'; c_yellow='\033[1;33m'; c_red='\033[1;31m'; c_reset='\033[0m'
log()  { printf "${c_blue}==>${c_reset} %s\n" "$*"; }
warn() { printf "${c_yellow}warn:${c_reset} %s\n" "$*" >&2; }
err()  { printf "${c_red}error:${c_reset} %s\n" "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# ---------- Arch + version ----------
ARCH="$(uname -m)"
case "${ARCH}" in
    x86_64)  MULTIARCH="x86_64-linux-gnu" ;;
    aarch64) MULTIARCH="aarch64-linux-gnu" ;;
    *) err "unsupported arch: ${ARCH} (build on x86_64 or aarch64)" ;;
esac
# Trust the toolchain's own multiarch tuple when it's available.
if have gcc; then MULTIARCH="$(gcc -dumpmachine 2>/dev/null || echo "${MULTIARCH}")"; fi
LIBDIR="/usr/lib/${MULTIARCH}"

VERSION="${1:-${LANTERN_VERSION:-}}"
if [ -z "${VERSION}" ]; then
    VERSION="$(grep -oP '<release version="\K[^"]+' "data/${APP_ID}.metainfo.xml" | head -1)"
fi
[ -n "${VERSION}" ] || err "could not determine version (pass it as an argument)"

log "Building ${APP_NAME} ${VERSION} AppImage for ${ARCH} (multiarch ${MULTIARCH})"

# ---------- Sanity: the GUI stack must be present on the build host ----------
[ -f "${LIBDIR}/libwebkitgtk-6.0.so.4" ] || err \
    "libwebkitgtk-6.0 not found in ${LIBDIR}. Install the build deps first:
     gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-gtksource-5 gir1.2-webkit-6.0
     python3-gi libgtk-4-dev (for gtk-update-icon-cache, gdk-pixbuf-query-loaders)"
# gdk-pixbuf-query-loaders often ships in the multiarch lib dir rather than on
# PATH; the gtk plugin calls it by name, so make sure it's reachable.
if ! have gdk-pixbuf-query-loaders; then
    for cand in "${LIBDIR}/gdk-pixbuf-2.0/gdk-pixbuf-query-loaders" \
                "${LIBDIR}/gdk-pixbuf-2.0/"*/gdk-pixbuf-query-loaders; do
        if [ -x "${cand}" ]; then
            export PATH="$(dirname "${cand}"):${PATH}"
            break
        fi
    done
fi
for t in python3 npm gtk-update-icon-cache gdk-pixbuf-query-loaders glib-compile-schemas; do
    have "$t" || err "missing build tool: $t (install the GTK dev/runtime utilities)"
done

# ---------- Tools (linuxdeploy + gtk plugin + appimagetool) ----------
TOOLS="${HOME}/.cache/lantern-appimage/tools"
mkdir -p "${TOOLS}"
# linuxdeploy/appimagetool use FUSE; in CI containers we run them extracted.
export APPIMAGE_EXTRACT_AND_RUN=1

fetch() { # url dest
    local url="$1" dest="$2"
    [ -f "${dest}" ] && return 0
    log "Fetching $(basename "${dest}")"
    if have wget; then wget -qO "${dest}" "${url}"; else curl -fsSL -o "${dest}" "${url}"; fi
    chmod +x "${dest}"
}
LD_TOOL_ARCH="${ARCH}"
fetch "https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous/linuxdeploy-${LD_TOOL_ARCH}.AppImage" \
      "${TOOLS}/linuxdeploy-${LD_TOOL_ARCH}.AppImage"
fetch "https://raw.githubusercontent.com/linuxdeploy/linuxdeploy-plugin-gtk/master/linuxdeploy-plugin-gtk.sh" \
      "${TOOLS}/linuxdeploy-plugin-gtk.sh"
fetch "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${LD_TOOL_ARCH}.AppImage" \
      "${TOOLS}/appimagetool-${LD_TOOL_ARCH}.AppImage"
LINUXDEPLOY="${TOOLS}/linuxdeploy-${LD_TOOL_ARCH}.AppImage"
APPIMAGETOOL="${TOOLS}/appimagetool-${LD_TOOL_ARCH}.AppImage"
export PATH="${TOOLS}:${PATH}"   # the gtk plugin is found via PATH by linuxdeploy

# ---------- AppDir + staged app ----------
APPDIR="${HERE}/build/AppDir"
rm -rf "${APPDIR}"
mkdir -p "${APPDIR}/usr"

log "Staging app tree"
DESTDIR="${APPDIR}" PREFIX="/usr" SHAREDIR="/usr/share/lantern" \
    PYTHON="/usr/bin/python3" ./scripts/stage-app.sh

# ---------- Bundle the Python interpreter + stdlib + PyGObject ----------
PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
PYBIN="$(readlink -f "$(command -v python3)")"
log "Bundling Python ${PYVER} (${PYBIN})"
install -Dm755 "${PYBIN}" "${APPDIR}/usr/bin/python${PYVER}"
ln -sf "python${PYVER}" "${APPDIR}/usr/bin/python3"

# stdlib — trim the parts a GUI app never needs to keep the image lean.
mkdir -p "${APPDIR}/usr/lib"
cp -a "/usr/lib/python${PYVER}" "${APPDIR}/usr/lib/python${PYVER}"
( cd "${APPDIR}/usr/lib/python${PYVER}" && rm -rf \
    test tests idlelib tkinter turtledemo ensurepip lib2to3 \
    config-*/libpython*.a __pycache__ 2>/dev/null || true )
find "${APPDIR}/usr/lib/python${PYVER}" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

# PyGObject: the gi package (with its compiled _gi*.so and overrides).
GI_SRC="$(python3 -c 'import gi, os; print(os.path.dirname(gi.__file__))')"
GI_DEST="${APPDIR}/usr/lib/python3/dist-packages/gi"
mkdir -p "$(dirname "${GI_DEST}")"
cp -a "${GI_SRC}" "${GI_DEST}"
find "${GI_DEST}" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

# ---------- GObject-Introspection typelibs (whole set) ----------
log "Bundling GI typelibs"
mkdir -p "${APPDIR}/usr/lib/girepository-1.0"
cp -a "${LIBDIR}/girepository-1.0/." "${APPDIR}/usr/lib/girepository-1.0/"

# ---------- WebKitGTK helper processes + injected bundle ----------
log "Bundling WebKitGTK helper processes"
cp -a "${LIBDIR}/webkitgtk-6.0" "${APPDIR}/usr/lib/webkitgtk-6.0"

# ---------- GIO modules (glib-networking TLS, proxy) ----------
if [ -d "${LIBDIR}/gio/modules" ]; then
    log "Bundling GIO modules"
    mkdir -p "${APPDIR}/usr/lib/gio/modules"
    cp -a "${LIBDIR}/gio/modules/." "${APPDIR}/usr/lib/gio/modules/" 2>/dev/null || true
    rm -f "${APPDIR}/usr/lib/gio/modules/giomodule.cache"
fi

# ---------- Node + pandoc ----------
log "Bundling node and pandoc"
NODE_BIN="$(readlink -f "$(command -v node)")"
install -Dm755 "${NODE_BIN}" "${APPDIR}/usr/bin/node"
if have pandoc; then
    install -Dm755 "$(readlink -f "$(command -v pandoc)")" "${APPDIR}/usr/bin/pandoc"
else
    warn "pandoc not found on PATH — PPTX export will be unavailable in this build"
fi

# ---------- Desktop + icon for linuxdeploy/appimagetool ----------
# linuxdeploy insists the desktop's Exec name a *deployed ELF*; our launcher is
# a shell script, so it would reject Exec=lantern. Point Exec at the bundled
# python while linuxdeploy runs (it deploys this staged file to the AppDir root),
# then restore Exec=lantern afterward. AppRun is the real entry point regardless.
STAGED_DESKTOP="${APPDIR}/usr/share/applications/${APP_ID}.desktop"
sed -i "s|^Exec=.*|Exec=python${PYVER} %F|" "${STAGED_DESKTOP}"
ICON_SRC="data/icons/hicolor/scalable/apps/${APP_ID}.svg"

# ---------- Gather library dependencies (linuxdeploy + gtk plugin) ----------
# linuxdeploy walks ldd for the libs we name, copies them into usr/lib, patches
# rpaths and honours the AppImage exclude list (so libc/libGL/драйв stay on the
# host). The gtk plugin lays down gdk-pixbuf loaders with a relocatable cache,
# GSettings schemas and an env hook. We hand it the GUI libs explicitly because
# they're dlopen'd through gi, not linked into python.
LD_LIBS=(
    "${LIBDIR}/libgtk-4.so.1"
    "${LIBDIR}/libadwaita-1.so.0"
    "${LIBDIR}/libgtksourceview-5.so.0"
    "${LIBDIR}/libwebkitgtk-6.0.so.4"
    "${LIBDIR}/libjavascriptcoregtk-6.0.so.1"
    "${LIBDIR}/libgirepository-1.0.so.1"
)
ld_args=()
for l in "${LD_LIBS[@]}"; do [ -f "$l" ] && ld_args+=(--library "$l"); done
# The GI extension and webkit helpers link the same libraries as the ones above,
# so the --library walk already gathers their dependencies; we don't hand them
# to linuxdeploy directly (that would copy them into usr/bin).

log "Running linuxdeploy (+gtk plugin) to gather dependencies"
DEPLOY_GTK_VERSION=4 NO_STRIP=1 "${LINUXDEPLOY}" \
    --appdir "${APPDIR}" \
    --executable "${APPDIR}/usr/bin/python${PYVER}" \
    --desktop-file "${STAGED_DESKTOP}" \
    --icon-file "${ICON_SRC}" \
    "${ld_args[@]}" \
    --plugin gtk

# Restore the launcher as the desktop's Exec (the AppDir-root copy is a symlink
# to this file, so one edit fixes both).
sed -i "s|^Exec=.*|Exec=lantern %F|" "${STAGED_DESKTOP}"

# ---------- Patch WebKitGTK's hardcoded helper path ----------
# WebKitGTK 6.0 execs WebKit{Network,Web,GPU}Process from a compile-time path
# (/usr/lib/<multiarch>/webkitgtk-6.0) with no env override. Rewrite the single
# NUL-terminated occurrence of that string to /tmp/lantern-wk (shorter, padded);
# AppRun symlinks /tmp/lantern-wk -> the bundled helpers at launch. The other
# occurrence (".../injected-bundle") is left alone — it has the
# WEBKIT_INJECTED_BUNDLE_PATH env override, which AppRun sets.
patch_webkit() {
    local lib="$1"
    python3 - "$lib" <<'PY'
import re, sys
p = sys.argv[1]
data = bytearray(open(p, "rb").read())
# Match the real multiarch libexec dir, terminated by NUL (the standalone one).
patched = 0
for m in re.finditer(rb"/usr/lib/[a-z0-9_]+-linux-gnu/webkitgtk-6\.0\x00", data):
    s, e = m.start(), m.end() - 1          # keep the trailing NUL
    repl = b"/tmp/lantern-wk"
    data[s:e] = repl + b"\x00" * ((e - s) - len(repl))
    patched += 1
open(p, "wb").write(data)
print(f"  patched {patched} occurrence(s) in {p}")
sys.exit(0 if patched else 3)
PY
}
WEBKIT_LIB="$(find "${APPDIR}/usr/lib" -maxdepth 1 -name 'libwebkitgtk-6.0.so.4*' -type f | head -1)"
[ -n "${WEBKIT_LIB}" ] || err "bundled libwebkitgtk not found after linuxdeploy"
log "Patching WebKitGTK helper path in $(basename "${WEBKIT_LIB}")"
patch_webkit "${WEBKIT_LIB}" || err "failed to patch WebKitGTK helper path"

# ---------- AppRun ----------
log "Writing AppRun"
cat > "${APPDIR}/AppRun" <<APPRUN
#!/bin/bash
# Lantern AppImage entry point. Wires the bundled runtime, then launches the app.
HERE="\$(dirname "\$(readlink -f "\${0}")")"
export APPDIR="\${HERE}"

# GTK plugin's env hook (gdk-pixbuf loaders, GSettings, etc.), if present.
for hook in "\${APPDIR}"/apprun-hooks/*.sh; do
    [ -e "\${hook}" ] && . "\${hook}"
done

export PATH="\${APPDIR}/usr/bin:\${PATH}"
export LD_LIBRARY_PATH="\${APPDIR}/usr/lib:\${APPDIR}/usr/lib/${MULTIARCH}\${LD_LIBRARY_PATH:+:\${LD_LIBRARY_PATH}}"
export PYTHONHOME="\${APPDIR}/usr"
export PYTHONPATH="\${APPDIR}/usr/share/lantern:\${APPDIR}/usr/lib/python3/dist-packages\${PYTHONPATH:+:\${PYTHONPATH}}"
export PYTHONDONTWRITEBYTECODE=1
export GI_TYPELIB_PATH="\${APPDIR}/usr/lib/girepository-1.0\${GI_TYPELIB_PATH:+:\${GI_TYPELIB_PATH}}"
export GIO_MODULE_DIR="\${APPDIR}/usr/lib/gio/modules"
export GSETTINGS_SCHEMA_DIR="\${APPDIR}/usr/share/glib-2.0/schemas\${GSETTINGS_SCHEMA_DIR:+:\${GSETTINGS_SCHEMA_DIR}}"
export XDG_DATA_DIRS="\${APPDIR}/usr/share:\${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"

# WebKitGTK: point it at the bundled helpers (see patch_webkit) and injected
# bundle, and fall back to the software path that survives odd GPUs/VMs.
ln -sfn "\${APPDIR}/usr/lib/webkitgtk-6.0" /tmp/lantern-wk 2>/dev/null || true
export WEBKIT_INJECTED_BUNDLE_PATH="\${APPDIR}/usr/lib/webkitgtk-6.0/injected-bundle"
export WEBKIT_DISABLE_DMABUF_RENDERER=1

# marp-cli + pandoc live inside the image; tell Lantern where.
export LANTERN_MARP_BIN="\${APPDIR}/usr/share/lantern/node_modules/.bin/marp"
[ -x "\${APPDIR}/usr/bin/pandoc" ] && export LANTERN_PANDOC_BIN="\${APPDIR}/usr/bin/pandoc"

# Make the bundled IBM Plex Mono resolvable without disturbing host fonts.
FONTCFG_DIR="\${XDG_CACHE_HOME:-\${HOME}/.cache}/lantern-appimage"
mkdir -p "\${FONTCFG_DIR}/fc-cache"
cat > "\${FONTCFG_DIR}/fonts.conf" <<FC
<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <include ignore_missing="yes">/etc/fonts/fonts.conf</include>
  <dir>\${APPDIR}/usr/share/fonts</dir>
  <cachedir>\${FONTCFG_DIR}/fc-cache</cachedir>
</fontconfig>
FC
export FONTCONFIG_FILE="\${FONTCFG_DIR}/fonts.conf"

exec "\${APPDIR}/usr/bin/python${PYVER}" -m lantern "\$@"
APPRUN
chmod 755 "${APPDIR}/AppRun"

# ---------- Build the AppImage ----------
mkdir -p "${HERE}/build"
OUT="${HERE}/build/${APP_NAME}-${ARCH}.AppImage"
rm -f "${OUT}"
log "Packing AppImage with appimagetool"
ARCH="${ARCH}" "${APPIMAGETOOL}" --no-appstream "${APPDIR}" "${OUT}"

log "Done."
echo
echo "AppImage written to:  ${OUT}"
echo "Run:                  ${OUT}"
echo "(Built against glibc $(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}'); runs on that or newer.)"
