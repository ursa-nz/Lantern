#!/usr/bin/env bash
# Build a shareable Lantern flatpak bundle.
#
# Output: build/nz.ursa.Lantern.flatpak — a single file you can hand to
# anyone, who installs it with `flatpak install --user ./nz.ursa.Lantern.flatpak`.
set -euo pipefail

APP_ID="nz.ursa.Lantern"
MANIFEST="flatpak/${APP_ID}.yaml"
RUNTIME_VERSION="49"
NODE_EXT="org.freedesktop.Sdk.Extension.node22"
NODE_BRANCH="25.08"     # GNOME 49 is based on freedesktop 25.08

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)"
cd "${HERE}"

# Build sandbox must be on a filesystem that supports symlinks.  If the
# source tree lives on a cloud-synced mount (e.g. kDrive, Dropbox) that
# blocks symlinks, do the build under ~/.cache and copy out only the
# final bundle.
WORK_ROOT="${HOME}/.cache/lantern-build"
BUILD_DIR="${WORK_ROOT}/builder"
REPO_DIR="${WORK_ROOT}/repo"
mkdir -p "${WORK_ROOT}"

c_blue='\033[1;34m'; c_yellow='\033[1;33m'; c_red='\033[1;31m'; c_reset='\033[0m'
log()  { printf "${c_blue}==>${c_reset} %s\n" "$*"; }
warn() { printf "${c_yellow}warn:${c_reset} %s\n" "$*" >&2; }
err()  { printf "${c_red}error:${c_reset} %s\n" "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# ---------- Tools ----------
have flatpak         || err "flatpak missing. Install: sudo apt install flatpak"
have flatpak-builder || err "flatpak-builder missing. Install: sudo apt install flatpak-builder"

# ---------- Flathub remote ----------
if ! flatpak remotes --user --columns=name | grep -qx flathub; then
    if ! flatpak remotes --columns=name | grep -qx flathub; then
        log "Adding flathub remote (user scope)..."
        flatpak remote-add --user --if-not-exists flathub \
            https://flathub.org/repo/flathub.flatpakrepo
    fi
fi

# ---------- Runtime + SDK ----------
ensure_installed() {
    local ref="$1"
    if ! flatpak info "${ref}" >/dev/null 2>&1; then
        log "Installing ${ref} from flathub..."
        flatpak install --user -y flathub "${ref}"
    fi
}

ensure_installed "org.gnome.Platform//${RUNTIME_VERSION}"
ensure_installed "org.gnome.Sdk//${RUNTIME_VERSION}"
ensure_installed "${NODE_EXT}//${NODE_BRANCH}"

# ---------- Build ----------
mkdir -p build
BUNDLE="$(pwd)/build/${APP_ID}.flatpak"
MANIFEST_ABS="$(pwd)/${MANIFEST}"   # flatpak-builder resolves sources relative to the manifest

rm -rf "${BUILD_DIR}"

log "Running flatpak-builder (work dir: ${WORK_ROOT})..."
flatpak-builder \
    --force-clean \
    --user \
    --install-deps-from=flathub \
    --state-dir="${WORK_ROOT}/.flatpak-builder" \
    --repo="${REPO_DIR}" \
    "${BUILD_DIR}" \
    "${MANIFEST_ABS}"

log "Exporting single-file bundle..."
flatpak build-bundle "${REPO_DIR}" "${BUNDLE}" "${APP_ID}"

log "Done."
echo
echo "Bundle written to:  ${BUNDLE}"
echo "Install locally:    flatpak install --user ${BUNDLE}"
echo "Run:                flatpak run ${APP_ID}"
echo
echo "Share by sending the .flatpak file. Recipient installs with the same command."
